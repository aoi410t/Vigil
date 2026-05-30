import { useEffect, useMemo, useState } from 'react';
import {
  CartesianGrid, ComposedChart, Line, ResponsiveContainer, Scatter,
  Tooltip, XAxis, YAxis,
} from 'recharts';
import ProgPoints from './ProgPoints.jsx';
import { useMe } from './me.jsx';

// Keep in sync with Encounters.jsx / FieldStats.jsx (small + slow-changing).
// v1.17.0: cloned encounters dedupe to canonical; the API normalizes
// 1065 → 1076 for DSR, so only the canonical IDs appear here.
const ENCOUNTER_NAMES = {
  1079: 'FRU',
  1068: 'TOP',
  1076: 'DSR',
  1075: 'TEA',
  1074: 'UWU',
  1073: 'UCoB',
  101: 'M9S',
  102: 'M10S',
  103: 'M11S',
  104: 'M12S',
  105: 'M12S-P2',
};
const encName = (id) => ENCOUNTER_NAMES[id] || `enc ${id}`;

export default function Home() {
  const { me } = useMe();
  const [reports, setReports] = useState(null);
  const [watched, setWatched] = useState(null);
  const [myEnc, setMyEnc] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    Promise.all([
      fetch('/api/reports').then((r) => r.json()),
      fetch('/api/watched-reports').then((r) => r.json()),
      fetch('/api/me/encounters').then((r) => r.json()),
    ])
      .then(([rs, ws, me]) => { setReports(rs); setWatched(ws); setMyEnc(me); })
      .catch((e) => setError(String(e)));
  }, []);

  if (error) {
    return (
      <section className="fade-in">
        <h1 className="mb-0">Home</h1>
        <div className="card" style={{ borderColor: 'var(--danger)' }}>
          <span className="pill pill-danger">error</span>{' '}
          <span className="text-sm">{error}</span>
        </div>
      </section>
    );
  }
  if (!reports || !watched || !myEnc || !me) {
    return (
      <section className="fade-in">
        <h1 className="mb-0">Home</h1>
        <div className="empty"><span className="loading">Loading</span></div>
      </section>
    );
  }

  const isDev = me.is_developer;
  const hasOwnData = watched.length > 0;
  const hasEncounterData = (myEnc.encounters || []).length > 0;

  // Brand-new non-dev user → onboarding flow (unchanged from v1.7.1).
  if (!isDev && !hasOwnData) {
    return <Onboarding me={me} />;
  }

  // Dev view stays the all-ingested snapshot.
  if (isDev) {
    return <DevHome reports={reports} />;
  }

  // Consumer Home: per-encounter prog dashboard.
  // Falls back to a "watched but not yet ingested" empty state when the
  // poller hasn't run yet against the user's first report.
  if (!hasEncounterData) {
    return <WatchedButNoData />;
  }

  return <ConsumerHome me={me} myEnc={myEnc} />;
}

/* -------------------------------------------------------------------------- */
/*                          consumer (non-dev) Home                           */
/* -------------------------------------------------------------------------- */

function ConsumerHome({ me, myEnc }) {
  const [activeId, setActiveId] = useState(myEnc.active);
  const active = useMemo(
    () => myEnc.encounters.find((e) => e.encounter_id === activeId)
          || myEnc.encounters[0],
    [activeId, myEnc.encounters],
  );

  // v1.13.0: fetch the joint (player × ability) breakdown once and share
  // it with the drill-down expansions on both Wipe Mechanics and Fault
  // sections. Single round trip; pivots are cheap client-side.
  const [breakdown, setBreakdown] = useState(null);
  useEffect(() => {
    setBreakdown(null);
    fetch(`/api/encounters/${active.encounter_id}/fault-breakdown`)
      .then((r) => r.json())
      .then(setBreakdown)
      .catch(() => setBreakdown({ rows: [] }));
  }, [active.encounter_id]);

  return (
    <section className="fade-in stack-lg">
      <div className="row" style={{ alignItems: 'flex-end',
                                    justifyContent: 'space-between' }}>
        <div>
          <h1 className="mb-0">Prog dashboard</h1>
          <p className="muted text-sm mb-0">
            {me.statics.find((s) => s.id === me.current_static_id)?.name
              || 'Your static'}'s progression on the selected encounter.
          </p>
        </div>
        <EncounterPicker
          encounters={myEnc.encounters}
          activeId={active.encounter_id}
          onChange={setActiveId}
        />
      </div>

      <EncounterHeader active={active} />

      <ProgSection encounterId={active.encounter_id} />

      <WipeMechanicsSection encounterId={active.encounter_id}
                            breakdown={breakdown} />

      <MitAuditSection encounterId={active.encounter_id} />

      <FaultSection encounterId={active.encounter_id}
                    wipeCount={active.wipes}
                    breakdown={breakdown} />

      <DpsComparisonSection encounterId={active.encounter_id} />
    </section>
  );
}

function EncounterPicker({ encounters, activeId, onChange }) {
  if (encounters.length <= 1) {
    return (
      <span className="pill pill-accent">
        {encName(activeId)} · {encounters[0]?.pulls ?? 0} pulls
      </span>
    );
  }
  return (
    <label className="row-tight gap-2">
      <span className="muted small">Encounter</span>
      <select value={activeId}
              onChange={(e) => onChange(Number(e.target.value))}>
        {encounters.map((e) => (
          <option key={e.encounter_id} value={e.encounter_id}>
            {encName(e.encounter_id)} ({e.pulls} pulls
            {e.kills > 0 ? `, ${e.kills}K` : ''})
          </option>
        ))}
      </select>
    </label>
  );
}

function EncounterHeader({ active }) {
  const killRate = active.pulls
    ? Math.round((active.kills / active.pulls) * 100)
    : 0;
  return (
    <div className="stat-grid">
      <Stat label="Pulls" value={active.pulls} />
      <Stat label="Kills" value={active.kills}
            accent={active.kills > 0 ? 'success' : null} />
      <Stat label="Wipes" value={active.wipes} />
      <Stat label="Kill rate" value={`${killRate}%`}
            hint={active.pulls ? `${active.kills}/${active.pulls}` : '—'} />
    </div>
  );
}

/* ------------ Section 1: Prog (auto curve + manual entries) -------------- */

function ProgSection({ encounterId }) {
  const [prog, setProg] = useState(null);
  const [error, setError] = useState(null);

  const refresh = () => {
    setError(null);
    fetch(`/api/encounters/${encounterId}/prog-curve`)
      .then((r) => r.json())
      .then(setProg)
      .catch((e) => setError(String(e)));
  };

  useEffect(() => {
    setProg(null);
    refresh();
    // eslint-disable-next-line
  }, [encounterId]);

  return (
    <div>
      <h2 className="mb-0" style={{ marginBottom: 'var(--s-2)' }}>
        Where we are
      </h2>
      <p className="muted text-sm">
        The curve plots fight-percentage remaining over time — lower is
        further into the fight. Auto-derived from your watched reports;
        add a manual point when ACT is down (post-patch days) so the
        trajectory doesn't break.
      </p>
      {error && (
        <div className="card" style={{ borderColor: 'var(--danger)' }}>
          <span className="pill pill-danger">error</span>{' '}
          <span className="text-sm">{error}</span>
        </div>
      )}
      {!prog ? (
        <div className="card"><span className="loading">Loading</span></div>
      ) : (
        <ProgTrajectory prog={prog} onManualAdded={refresh} />
      )}
    </div>
  );
}

function ProgTrajectory({ prog, onManualAdded }) {
  // Continuous prog distance = phase + (1 - bossHP/100). Higher = further.
  // E.g. P3 at 50% boss HP = 3.5, P3 at 0% boss HP = 4.0 = P4 just started.
  // FFLogs' fightPercentage is a single value (the LAGGING boss's HP for
  // multi-boss phases like DSR P4 Eyes / P6 Thordan+Nidhogg); we'd need
  // separate ingest work to track each boss independently. Tooltip is
  // honest about that.
  const sessions = (prog.our_sessions || [])
    .filter((s) => s.best_phase != null && s.ts_ms != null);
  const manual = (prog.manual_points || [])
    .filter((p) => p.phase != null && p.ts);

  const progDistance = (phase, fp) => {
    if (phase == null) return null;
    if (fp == null) return phase;
    return phase + (1 - Math.max(0, Math.min(100, fp)) / 100);
  };

  const series = useMemo(() => {
    const out = [];
    for (const s of sessions) {
      out.push({
        ts: s.ts_ms,
        auto_dist: progDistance(s.best_phase, s.best_fight_percentage),
        phase: s.best_phase,
        fp: s.best_fight_percentage,
        pulls: s.pulls,
        kills: s.kills,
      });
    }
    for (const p of manual) {
      out.push({
        ts: new Date(p.ts).getTime(),
        manual_dist: progDistance(p.phase, p.fight_percentage),
        phase: p.phase,
        fp: p.fight_percentage,
        pull_count: p.pull_count,
      });
    }
    out.sort((a, b) => a.ts - b.ts);
    return out;
  }, [sessions, manual]);

  // Best = highest continuous prog distance achieved.
  const allDists = series
    .map((e) => e.auto_dist ?? e.manual_dist)
    .filter((v) => v != null);
  const overallBestDist = allDists.length ? Math.max(...allDists) : null;
  const overallBestPhase = Math.floor(overallBestDist ?? 0);
  const overallBestFp = overallBestDist != null
    ? (1 - (overallBestDist - overallBestPhase)) * 100
    : null;
  const maxPhaseSeen = series.reduce(
    (acc, e) => Math.max(acc, e.phase || 0),
    0,
  );

  if (series.length === 0) {
    return (
      <div className="card stack-sm">
        <p className="muted text-sm mb-0">
          No prog data yet for this encounter. Add a watched report
          (Reports tab) or log a manual prog point below.
        </p>
        <ManualPointForm onAdded={onManualAdded} />
      </div>
    );
  }

  return (
    <div className="stack">
      <div className="card">
        <div className="row" style={{ alignItems: 'baseline',
                                       justifyContent: 'space-between',
                                       marginBottom: 'var(--s-2)' }}>
          <h4 className="mb-0">Progression curve</h4>
          <span className="muted small">
            {sessions.length} session{sessions.length === 1 ? '' : 's'}
            {' · '}{manual.length} manual point{manual.length === 1 ? '' : 's'}
            {overallBestDist != null && (
              ` · best: P${overallBestPhase}` +
              (overallBestFp != null
                ? ` (boss HP ${overallBestFp.toFixed(0)}%)`
                : '')
            )}
          </span>
        </div>
        <ResponsiveContainer width="100%" height={260}>
          <ComposedChart data={series}
                         margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
            <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" />
            <XAxis dataKey="ts" type="number" domain={['dataMin', 'dataMax']}
                   scale="time"
                   tickFormatter={(ms) => new Date(ms).toLocaleDateString()}
                   stroke="var(--fg-muted)" />
            <YAxis dataKey="auto_dist" type="number"
                   domain={[0, Math.max(maxPhaseSeen + 1, 2)]}
                   ticks={Array.from({ length: maxPhaseSeen + 2 },
                                     (_, i) => i)}
                   tickFormatter={(v) => `P${v}`}
                   stroke="var(--fg-muted)"
                   label={{ value: 'prog distance',
                            angle: -90, position: 'insideLeft',
                            fill: 'var(--fg-muted)' }} />
            <Tooltip
              labelFormatter={(ms) => new Date(ms).toLocaleString()}
              contentStyle={{ background: 'var(--bg-elevated)',
                              border: '1px solid var(--border)',
                              borderRadius: 'var(--radius-md)' }}
              formatter={(val, name, props) => {
                if (val == null) return ['—', name];
                const p = props?.payload || {};
                const fpDetail = p.fp != null
                  ? ` (boss HP ${p.fp.toFixed(1)}% remaining)`
                  : '';
                return [`P${p.phase}${fpDetail}`,
                        name === 'auto_dist' ? 'session best'
                                              : 'manual'];
              }} />
            <Line type="monotone" dataKey="auto_dist"
                  stroke="var(--accent)"
                  strokeWidth={2} dot={{ r: 3 }} connectNulls
                  name="session best" />
            <Scatter dataKey="manual_dist" fill="var(--warning)"
                     name="manual" shape="diamond" />
          </ComposedChart>
        </ResponsiveContainer>
        <p className="muted small" style={{ marginTop: 'var(--s-2)',
                                              marginBottom: 0 }}>
          Y axis is a continuous "prog distance" — integer marks are phase
          starts, decimals are within-phase progress (boss HP depleted).
          E.g. P3.5 = halfway through P3's boss HP. For multi-boss phases
          (DSR P4 Eyes, P6 Thordan+Nidhogg) the value tracks FFLogs' single
          fightPercentage which is the lagging boss's HP — per-boss
          tracking would need a separate ingest pass.
        </p>
      </div>

      {sessions.length > 0 && (
        <div className="card card-tight">
          <h4 className="mb-0" style={{ marginBottom: 'var(--s-2)' }}>
            Sessions
          </h4>
          <table className="t t-tight">
            <thead>
              <tr>
                <th>Date</th>
                <th className="num">Pulls</th>
                <th className="num">Kills</th>
                <th className="num">Best phase</th>
                <th className="num"
                    title="Wiping boss's HP % remaining at the best phase reached — informational only">
                  Boss HP at phase
                </th>
              </tr>
            </thead>
            <tbody>
              {[...sessions].reverse().slice(0, 10).map((s) => (
                <tr key={s.report_code}>
                  <td className="small">
                    {s.ts_ms ? new Date(s.ts_ms).toLocaleDateString() : '—'}
                  </td>
                  <td className="num">{s.pulls}</td>
                  <td className="num"
                      style={{ color: s.kills > 0 ? 'var(--success)' : 'var(--fg-muted)' }}>
                    {s.kills || '—'}
                  </td>
                  <td className="num">
                    <strong>{s.best_phase != null ? `P${s.best_phase}` : '—'}</strong>
                  </td>
                  <td className="num muted small">
                    {s.best_fight_percentage != null
                      ? `${s.best_fight_percentage.toFixed(1)}%`
                      : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {sessions.length > 10 && (
            <p className="muted small"
               style={{ marginTop: 'var(--s-2)', marginBottom: 0 }}>
              Showing 10 most recent of {sessions.length} sessions.
            </p>
          )}
        </div>
      )}

      <div className="card">
        <h4 className="mb-0" style={{ marginBottom: 'var(--s-2)' }}>
          Log a manual prog point
        </h4>
        <p className="muted small" style={{ marginBottom: 'var(--s-3)' }}>
          For when ACT is down (post-patch ~2-3 days) so the trajectory
          doesn't break.
        </p>
        <ManualPointForm onAdded={onManualAdded} />
      </div>
    </div>
  );
}

function ManualPointForm({ onAdded }) {
  const nowLocal = () => {
    const d = new Date();
    d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
    return d.toISOString().slice(0, 16);
  };
  const [ts, setTs] = useState(nowLocal());
  const [phase, setPhase] = useState('');
  const [pct, setPct] = useState('');
  const [pulls, setPulls] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const submit = async (e) => {
    e.preventDefault();
    if (!phase && !pct) {
      setErr('Need at least phase or fight %');
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      const body = {
        ts: new Date(ts).toISOString(),
        phase: phase === '' ? null : Number(phase),
        fight_percentage: pct === '' ? null : Number(pct),
        pull_count: pulls === '' ? null : Number(pulls),
      };
      const r = await fetch('/api/prog-points', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        const b = await r.json().catch(() => ({}));
        throw new Error(b.detail || `POST ${r.status}`);
      }
      setPhase(''); setPct(''); setPulls(''); setTs(nowLocal());
      onAdded();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className="row-tight gap-2 wrap">
      <label className="row-tight gap-1 small muted">
        when
        <input type="datetime-local" value={ts}
               onChange={(e) => setTs(e.target.value)} required />
      </label>
      <input placeholder="phase" type="number" min="0" value={phase}
             onChange={(e) => setPhase(e.target.value)}
             style={{ width: 90 }} />
      <input placeholder="% remaining" type="number" step="0.1" min="0" max="100"
             value={pct} onChange={(e) => setPct(e.target.value)}
             style={{ width: 130 }} />
      <input placeholder="pulls so far" type="number" min="0" value={pulls}
             onChange={(e) => setPulls(e.target.value)}
             style={{ width: 120 }} />
      <button type="submit" disabled={busy} className="btn-primary">Log</button>
      {err && <span className="text-sm" style={{ color: 'var(--danger)' }}>{err}</span>}
    </form>
  );
}

/* ------------ Section 2: Top wipe mechanics (cartography) ---------------- */

// Strip trailing " N" (cactbot occurrence number) so "Sacred Sever 1",
// "Sacred Sever 2" → "Sacred Sever" for grouping.
function baseCactbotLabel(label) {
  if (!label) return null;
  return label.replace(/\s+\d+$/, '').trim();
}

function displayMechanicName(bucket) {
  const base = baseCactbotLabel(bucket.cactbot_label);
  if (base) return base;
  if (bucket.ability_name) return bucket.ability_name;
  if (bucket.ability_game_id != null) return `ability ${bucket.ability_game_id}`;
  return 'non-attributable';
}

function WipeMechanicsSection({ encounterId, breakdown }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [expanded, setExpanded] = useState(null);  // key or null
  const [grouped, setGrouped] = useState(true);    // group by mechanic name?
  // v1.16.4: phase filter. `null` = all phases (default). Each tab also
  // shows that phase's death count as a numerical hint.
  const [selectedPhase, setSelectedPhase] = useState(null);

  useEffect(() => {
    setData(null);
    setError(null);
    setExpanded(null);
    setSelectedPhase(null);
    fetch(`/api/encounters/${encounterId}/cartography?watched_only=true`)
      .then((r) => r.json())
      .then(setData)
      .catch((e) => setError(String(e)));
  }, [encounterId]);

  // Hooks must be called unconditionally on every render (Rules of Hooks).
  // Compute the grouped rows from `data` here BEFORE any early returns;
  // guard against null data inside the memo so the hook always runs.
  const real = data
    ? (data.buckets || []).filter((b) => !b.non_attributable)
    : [];
  const nonAttrib = data
    ? (data.buckets || []).find((b) => b.non_attributable)
    : null;

  // v1.16.5: per-phase tabs labeled with WIPES (was deaths). Wipe counts
  // come from `data.wipes_by_phase` (server-side Fight.last_phase tally).
  // Only phases that have at least one wipe appear; the 'unknown' chip is
  // present iff there are wipes with no last_phase.
  const phaseTabs = useMemo(() => {
    const wbp = (data && data.wipes_by_phase) || {};
    const tabs = [];
    const knownPhases = Object.keys(wbp)
      .filter((k) => k !== 'unknown')
      .map((k) => Number(k))
      .filter((n) => !Number.isNaN(n))
      .sort((a, b) => a - b);
    for (const p of knownPhases) {
      tabs.push({ phase: p, count: wbp[String(p)] || 0 });
    }
    if (wbp['unknown']) {
      tabs.push({ phase: 'unknown', count: wbp['unknown'] });
    }
    return tabs;
  }, [data]);

  // Filter buckets by selected phase (uses the v1.16.5 resolved `phase`
  // field which falls back to T-103 inference when fight_model_phase is
  // None). `null` = all phases; `'unknown'` = phase-unresolved.
  const realFiltered = useMemo(() => {
    if (selectedPhase == null) return real;
    if (selectedPhase === 'unknown') {
      return real.filter((b) => b.phase == null);
    }
    return real.filter((b) => b.phase === selectedPhase);
  }, [real, selectedPhase]);

  // When `grouped`, merge buckets by display name (base cactbot label or
  // ability name). Sums deaths, takes max of fights_affected (approximation;
  // truly accurate union would need per-fight ability id lists).
  const grouped_rows = useMemo(() => {
    if (!grouped) {
      return realFiltered.slice(0, 12).map((b) => ({
        key: `aid-${b.ability_game_id}`,
        display: displayMechanicName(b),
        deaths: b.deaths,
        fights_affected: b.fights_affected,
        phase: b.phase,
        phase_source: b.phase_source,
        type_label: b.fight_model_label,
        inferred_deaths: b.inferred_deaths || 0,
        phase_inferred_deaths: b.phase_inferred_deaths || 0,
        members: [b],
      }));
    }
    const byName = new Map();
    for (const b of realFiltered) {
      const name = displayMechanicName(b);
      const cur = byName.get(name) || {
        key: `grp-${name}`,
        display: name,
        deaths: 0,
        inferred_deaths: 0,
        phase_inferred_deaths: 0,
        phases: new Set(),
        phase_sources: new Set(),
        type_labels: new Set(),
        members: [],
      };
      cur.deaths += b.deaths;
      cur.inferred_deaths += b.inferred_deaths || 0;
      cur.phase_inferred_deaths += b.phase_inferred_deaths || 0;
      cur.fights_affected_max = Math.max(cur.fights_affected_max || 0,
                                          b.fights_affected);
      if (b.phase != null) cur.phases.add(b.phase);
      if (b.phase_source) cur.phase_sources.add(b.phase_source);
      if (b.fight_model_label) cur.type_labels.add(b.fight_model_label);
      cur.members.push(b);
      byName.set(name, cur);
    }
    const rows = [...byName.values()]
      .map((g) => ({
        key: g.key,
        display: g.display,
        deaths: g.deaths,
        fights_affected: g.fights_affected_max || 0,
        phase: g.phases.size === 1 ? [...g.phases][0] : null,
        phase_source: g.phase_sources.size === 1 ? [...g.phase_sources][0]
                      : (g.phase_sources.has('inferred') ? 'inferred' : null),
        type_label: g.type_labels.size === 1 ? [...g.type_labels][0] : null,
        inferred_deaths: g.inferred_deaths,
        phase_inferred_deaths: g.phase_inferred_deaths,
        members: g.members,
      }))
      .sort((a, b) => b.deaths - a.deaths);
    return rows.slice(0, 12);
  }, [grouped, realFiltered]);

  const top = grouped_rows;
  // v1.16.5: chip counts are WIPES, not deaths. "All" = total_wipes
  // (cartography response). Per-phase = wipes_by_phase[phase].
  const totalWipesAll = data ? (data.total_wipes || 0) : 0;

  if (error) {
    return (
      <div className="card" style={{ borderColor: 'var(--danger)' }}>
        <span className="pill pill-danger">error</span>{' '}
        <span className="text-sm">{error}</span>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="card"><span className="loading">Loading</span></div>
    );
  }

  return (
    <div>
      <h2 className="mb-0" style={{ marginBottom: 'var(--s-2)' }}>
        What's killing us
      </h2>
      <p className="muted text-sm">
        Deaths bucketed by boss ability across {data.total_wipes} watched
        wipe{data.total_wipes === 1 ? '' : 's'}. The mechanic at the top is
        the biggest prog wall right now.
      </p>
      {real.length === 0 ? (
        <div className="card">
          <p className="muted">
            No attributable deaths yet. Poll your watched reports first.
          </p>
        </div>
      ) : (
        <div className="card card-tight">
          {/* v1.16.4: per-phase phase tabs above the table. "All" is the
              default; clicking a phase chip filters to just that phase's
              mechanics. */}
          {phaseTabs.length > 1 && (
            <div className="row-tight gap-1 wrap"
                 style={{ marginBottom: 'var(--s-2)' }}>
              <button
                onClick={() => setSelectedPhase(null)}
                className={selectedPhase == null
                  ? 'btn-primary btn-sm'
                  : 'btn-ghost btn-sm'}
                title="All watched wipes for this encounter">
                All ({totalWipesAll} wipe{totalWipesAll === 1 ? '' : 's'})
              </button>
              {phaseTabs.map(({ phase, count }) => (
                <button
                  key={String(phase)}
                  onClick={() => setSelectedPhase(phase)}
                  className={selectedPhase === phase
                    ? 'btn-primary btn-sm'
                    : 'btn-ghost btn-sm'}
                  title={phase === 'unknown'
                    ? `${count} wipe${count === 1 ? '' : 's'} have no last-phase recorded`
                    : `${count} wipe${count === 1 ? '' : 's'} ended in P${phase}`}>
                  {phase === 'unknown' ? 'Unknown' : `P${phase}`}
                  {' '}({count})
                </button>
              ))}
            </div>
          )}
          <div className="row" style={{ justifyContent: 'flex-end',
                                          alignItems: 'baseline',
                                          marginBottom: 'var(--s-2)' }}>
            <label className="row-tight gap-1 small muted">
              <input type="checkbox" checked={grouped}
                     onChange={(e) => setGrouped(e.target.checked)} />
              group by mechanic name
            </label>
          </div>
          {top.length === 0 ? (
            <p className="muted small" style={{ margin: 'var(--s-3) 0' }}>
              No attributable deaths in this phase yet.
            </p>
          ) : (
          <table className="t t-tight">
            <thead>
              <tr>
                <th style={{ width: 24 }}></th>
                <th>Mechanic</th>
                <th>Phase</th>
                <th>Type</th>
                <th className="num">Deaths</th>
                <th className="num">Wipes affected</th>
              </tr>
            </thead>
            <tbody>
              {top.map((row) => {
                const isOpen = expanded === row.key;
                // For grouped rows aggregate offenders across all members;
                // for ungrouped rows just use the single ability.
                const memberAids = row.members
                  .map((m) => m.ability_game_id)
                  .filter((v) => v != null);
                const offenders = topOffendersForMechanicSet(
                  breakdown, memberAids, 5,
                );
                return [
                  <tr key={row.key}
                      onClick={() => setExpanded(isOpen ? null : row.key)}
                      style={{ cursor: 'pointer' }}>
                    <td className="muted">{isOpen ? '▾' : '▸'}</td>
                    <td>
                      {row.display}
                      {row.members.length > 1 && (
                        <span className="muted small"
                              title={row.members
                                .map((m) => `${m.ability_name || m.ability_game_id}: ${m.deaths}`)
                                .join('\n')}>
                          {' '}({row.members.length} variants)
                        </span>
                      )}
                      {(row.inferred_deaths > 0
                        || row.phase_inferred_deaths > 0) && (
                        <span className="pill pill-warning small"
                              style={{ marginLeft: 6 }}
                              title={
                                ['Inferred fields for this row:']
                                  .concat(row.inferred_deaths > 0 ? [
                                    `• MECHANIC: ${row.inferred_deaths} of `
                                    + `${row.deaths} death${row.deaths === 1 ? '' : 's'} `
                                    + 'were FFLogs sourceID=-1 / null killing-ability — '
                                    + 'we inferred this mechanic via cast-proximity (most '
                                    + 'recent boss cast within 8s before death) or '
                                    + 'cactbot drift (predicted cactbot expected time '
                                    + '+ this pull\'s per-phase drift, ±2.5s).'
                                  ] : [])
                                  .concat(row.phase_inferred_deaths > 0 ? [
                                    `• PHASE: ${row.phase_inferred_deaths} death`
                                    + `${row.phase_inferred_deaths === 1 ? '' : 's'} `
                                    + 'had no fight_model phase recorded for this '
                                    + 'ability — we inferred the phase from T-103 '
                                    + 'phase boundaries (which T-103 phase contained '
                                    + 'the death\'s timestamp in that pull).'
                                  ] : [])
                                  .join('\n')
                              }>
                          {(() => {
                            const parts = [];
                            if (row.inferred_deaths > 0) {
                              parts.push(row.inferred_deaths === row.deaths
                                ? 'mechanic guessed'
                                : `${row.inferred_deaths} mech-guess`);
                            }
                            if (row.phase_inferred_deaths > 0) {
                              parts.push(row.phase_inferred_deaths === row.deaths
                                ? 'phase guessed'
                                : `${row.phase_inferred_deaths} phase-guess`);
                            }
                            return parts.join(' · ');
                          })()}
                        </span>
                      )}
                    </td>
                    <td>
                      {row.phase != null ? (
                        <>
                          P{row.phase}
                          {row.phase_source === 'inferred' && (
                            <span className="muted small"
                                  title="Phase guessed from T-103 phase boundaries (no fight_model phase tag on this ability)"
                                  style={{ marginLeft: 4 }}>
                              ?
                            </span>
                          )}
                        </>
                      ) : '—'}
                    </td>
                    <td>
                      {row.type_label
                        ? <MechanicPill label={row.type_label} />
                        : <span className="muted small">—</span>}
                    </td>
                    <td className="num"><strong>{row.deaths}</strong></td>
                    <td className="num">
                      {row.fights_affected} / {data.total_wipes}
                    </td>
                  </tr>,
                  isOpen && (
                    <tr key={`${row.key}-expand`}>
                      <td></td>
                      <td colSpan={5}
                          style={{ background: 'var(--bg-elevated)',
                                   padding: 'var(--s-3)' }}>
                        {row.members.length > 1 && (
                          <div className="muted small"
                               style={{ marginBottom: 'var(--s-2)' }}>
                            Variants in this group:{' '}
                            {row.members.map((m, i) => (
                              <span key={m.ability_game_id}>
                                {i > 0 && ', '}
                                <code>{m.ability_name || `id ${m.ability_game_id}`}</code>
                                {' ('}{m.deaths}{')'}
                              </span>
                            ))}
                          </div>
                        )}
                        {offenders.length === 0 ? (
                          <span className="muted small">
                            No fault data yet — analyse wipes below to populate.
                          </span>
                        ) : (
                          <>
                            <div className="muted small"
                                 style={{ marginBottom: 'var(--s-2)' }}>
                              Players eating this mechanic most:
                            </div>
                            <table className="t t-tight">
                              <tbody>
                                {offenders.map((o) => (
                                  <tr key={o.player_id}>
                                    <td>{o.player_name || `player ${o.player_id}`}</td>
                                    <td className="muted small">{o.job || '—'}</td>
                                    <td className="num">
                                      <strong>{o.deaths}</strong> death{o.deaths === 1 ? '' : 's'}
                                    </td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </>
                        )}
                      </td>
                    </tr>
                  ),
                ];
              })}
            </tbody>
          </table>
          )}
        </div>
      )}
      {nonAttrib && nonAttrib.deaths > 0 && (
        <p className="muted small" style={{ marginTop: 'var(--s-2)' }}>
          {nonAttrib.deaths} additional death{nonAttrib.deaths === 1 ? '' : 's'}{' '}
          had no killing-ability attributed (cascade-of-cascade follow-ups —
          M-FAULT untangles these below).
        </p>
      )}
    </div>
  );
}

function MechanicPill({ label }) {
  const cls = {
    raidwide: 'pill-danger',
    tankbuster: 'pill-warning',
    aoe_party: 'pill-warning',
    enrage: 'pill-danger',
    cosmetic: 'pill',
  }[label] || 'pill';
  return <span className={`pill ${cls}`}>{label}</span>;
}

/* ------------ Section 2b: Mit audit aggregate (v1.9.0) ------------------- */

function MitAuditSection({ encounterId }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    setData(null);
    setError(null);
    fetch(`/api/encounters/${encounterId}/mit-audit-aggregate`)
      .then((r) => r.json())
      .then(setData)
      .catch((e) => setError(String(e)));
  }, [encounterId]);

  if (error) {
    return (
      <div className="card" style={{ borderColor: 'var(--danger)' }}>
        <span className="pill pill-danger">error</span>{' '}
        <span className="text-sm">{error}</span>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="card"><span className="loading">Loading</span></div>
    );
  }

  // No plans configured anywhere — point the user at the strat editor.
  if (data.planned_slots_total === 0) {
    return (
      <div>
        <h2 className="mb-0" style={{ marginBottom: 'var(--s-2)' }}>
          How mit usage is going
        </h2>
        <div className="card stack-sm">
          <p className="muted text-sm mb-0">
            No mit plans configured for this encounter yet. Open the{' '}
            <a href="#encounters">Encounters tab → Strat</a> sub-tab to set up
            planned mits per raidwide. Then we can flag misses here.
          </p>
        </div>
      </div>
    );
  }

  const hitRate = data.mit_hit_rate;
  const hitPct = hitRate != null ? Math.round(hitRate * 100) : null;
  const hitColor = hitPct == null
    ? 'var(--fg-muted)'
    : hitPct >= 90 ? 'var(--success)'
    : hitPct >= 75 ? 'var(--warning)'
    : 'var(--danger)';

  return (
    <div>
      <h2 className="mb-0" style={{ marginBottom: 'var(--s-2)' }}>
        How mit usage is going
      </h2>
      <p className="muted text-sm">
        Across {data.raidwide_casts} raidwide cast
        {data.raidwide_casts === 1 ? '' : 's'} in {data.fights_aggregated}{' '}
        watched fight{data.fights_aggregated === 1 ? '' : 's'},{' '}
        {data.planned_slots_total - data.missed_mits_total}/
        {data.planned_slots_total} planned mits fired in their window.
      </p>

      <div className="stat-grid">
        <div className="stat">
          <div className="stat-label">Mit hit rate</div>
          <div className="stat-value" style={{ color: hitColor }}>
            {hitPct != null ? `${hitPct}%` : '—'}
          </div>
        </div>
        <div className="stat">
          <div className="stat-label">Missed mits</div>
          <div className="stat-value"
               style={{ color: data.missed_mits_total > 0
                                ? 'var(--danger)' : 'var(--fg)' }}>
            {data.missed_mits_total}
          </div>
        </div>
        <div className="stat">
          <div className="stat-label">Raidwides</div>
          <div className="stat-value">{data.raidwide_casts}</div>
        </div>
      </div>

      {data.worst_mits.length > 0 && (
        <div className="card card-tight" style={{ marginTop: 'var(--s-3)' }}>
          <div className="row" style={{ alignItems: 'baseline',
                                        justifyContent: 'space-between',
                                        marginBottom: 'var(--s-2)' }}>
            <h4 className="mb-0">Mits dropping the most</h4>
            <span className="muted small">
              top {Math.min(data.worst_mits.length, 5)} by miss rate
            </span>
          </div>
          <table className="t t-tight">
            <thead>
              <tr>
                <th>Mit</th>
                <th className="num">Planned</th>
                <th className="num">Missed</th>
                <th className="num">Miss rate</th>
              </tr>
            </thead>
            <tbody>
              {data.worst_mits.slice(0, 5).map((m) => {
                const pct = Math.round(m.miss_rate * 100);
                const color = pct >= 50 ? 'var(--danger)'
                            : pct >= 20 ? 'var(--warning)'
                            : 'var(--fg)';
                return (
                  <tr key={m.ability_id}>
                    <td>{m.ability_name || `ability ${m.ability_id}`}</td>
                    <td className="num">{m.planned}</td>
                    <td className="num">{m.missed}</td>
                    <td className="num">
                      <strong style={{ color }}>{pct}%</strong>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {data.worst_mechanics.some((m) => m.missed > 0) && (
        <div className="card card-tight" style={{ marginTop: 'var(--s-3)' }}>
          <div className="row" style={{ alignItems: 'baseline',
                                        justifyContent: 'space-between',
                                        marginBottom: 'var(--s-2)' }}>
            <h4 className="mb-0">Raidwides taking the most damage unmitigated</h4>
            <span className="muted small">
              top {Math.min(data.worst_mechanics.filter((m) => m.missed > 0).length, 5)} by missed slots
            </span>
          </div>
          <table className="t t-tight">
            <thead>
              <tr>
                <th>Raidwide</th>
                <th className="num">Occurrences</th>
                <th className="num">Planned slots</th>
                <th className="num">Missed</th>
              </tr>
            </thead>
            <tbody>
              {data.worst_mechanics
                  .filter((m) => m.missed > 0)
                  .slice(0, 5)
                  .map((m) => (
                <tr key={m.ability_id}>
                  <td>{m.ability_name || `ability ${m.ability_id}`}</td>
                  <td className="num">{m.occurrences}</td>
                  <td className="num">{m.planned_slots}</td>
                  <td className="num">
                    <strong style={{ color: 'var(--danger)' }}>
                      {m.missed}
                    </strong>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/* ------------ Section 3: Fault contributors (per-player) ----------------- */

function FaultSection({ encounterId, wipeCount, breakdown }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [computing, setComputing] = useState(false);
  const [expanded, setExpanded] = useState(null);  // name key or null
  // v1.14.4: per-wipe normalization mode. On (default) = rates per wipe
  // attended, so a part-timer's per-attempt fault density is comparable
  // to a regular's. Off = raw totals (older volume-of-harm view).
  const [perWipe, setPerWipe] = useState(true);

  const refresh = () => {
    setError(null);
    fetch(`/api/encounters/${encounterId}/fault-aggregate`)
      .then((r) => r.json())
      .then(setData)
      .catch((e) => setError(String(e)));
  };

  useEffect(() => {
    setData(null);
    setExpanded(null);
    refresh();
    // eslint-disable-next-line
  }, [encounterId]);

  const compute = async () => {
    setComputing(true);
    try {
      const r = await fetch(
        `/api/encounters/${encounterId}/fault-scores/compute-all`,
        { method: 'POST' },
      );
      if (!r.ok) throw new Error(`POST ${r.status}`);
      refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setComputing(false);
    }
  };

  // Group rows by ROSTER MEMBER first (v1.15.1) — when a member has a sub
  // account registered, both characters' fault rows roll up into one. Falls
  // back to character name when no member is attached (the v1.14.3 behavior).
  // Per-character + per-job breakdowns surface in the expansion.
  const players = data?.players || [];
  const groupedPlayers = useMemo(() => {
    const byKey = new Map();
    for (const p of players) {
      const charName = p.name || `player ${p.player_id}`;
      // Prefer the member identity when present so main + sub accounts merge.
      const key = p.member_id != null ? `m:${p.member_id}` : `n:${charName}`;
      const displayName = p.member_name || charName;
      const cur = byKey.get(key) || {
        key,
        name: displayName,
        member_id: p.member_id ?? null,
        member_name: p.member_name ?? null,
        player_ids: [],
        characters: new Map(),  // charName -> {server, score, fights}
        root: 0, cascade: 0, mit_failure: 0,
        heal_failure: 0, heal_failure_caused: 0,
        enrage: 0, unknown: 0,
        avoidable_damage: 0, damage_downs: 0,
        past_wall_offenses: 0,
        fights: 0, score: 0, raw_score: 0,
        jobs: new Map(),
        worst_wipes: [],  // v1.16.0 score decomposition (top-N across the group)
        // v1.16.1 scoped top contributors restricted to fights this group
        // attended. Backend emits the same array on every player_id that
        // shares an identity, so the first one wins.
        scoped_top_contributors: p.scoped_top_contributors || [],
        scoped_wipes_count: p.scoped_wipes_count || 0,
      };
      cur.player_ids.push(p.player_id);
      cur.root += p.root || 0;
      cur.cascade += p.cascade || 0;
      cur.mit_failure += p.mit_failure || 0;
      cur.heal_failure += p.heal_failure || 0;
      cur.heal_failure_caused += p.heal_failure_caused || 0;
      cur.enrage += p.enrage || 0;
      cur.unknown += p.unknown || 0;
      cur.avoidable_damage += p.avoidable_damage || 0;
      cur.damage_downs += p.damage_downs || 0;
      cur.past_wall_offenses += p.past_wall_offenses || 0;
      cur.fights += p.fights || 0;
      cur.score += p.score || 0;
      cur.raw_score += p.raw_score || 0;
      // Merge worst_wipes from each constituent player row into the
      // group; trim to top-5 below after summation.
      for (const w of p.worst_wipes || []) cur.worst_wipes.push(w);

      // Track per-character roll-up so the UI can list "Alice Tankerton
      // (PLD/WAR) · Alice Backup (PLD)" when a member has multiple
      // accounts attached. v1.16.2: jobs come from the backend's per-
      // character `jobs_breakdown` so a character that played multiple
      // jobs shows them all.
      const charKey = `${charName}|${p.server || ''}`;
      const ch = cur.characters.get(charKey) || {
        character_name: charName, server: p.server || null,
        fights: 0, score: 0, jobs: new Set(),
      };
      ch.fights += p.fights || 0;
      ch.score += p.score || 0;
      // Use jobs_breakdown for the full job list on this character.
      const charJobs = p.jobs_breakdown || (p.job ? {[p.job]: {}} : {});
      for (const jname of Object.keys(charJobs)) {
        if (jname && jname !== '—') ch.jobs.add(jname);
      }
      cur.characters.set(charKey, ch);

      // v1.16.2: per-job breakdown read from backend `jobs_breakdown`,
      // not aggregated from `p.job`. Previously we'd attribute ALL of a
      // pid's fights to a single first-seen job — broken because pids
      // are report-scoped and can represent different characters with
      // different jobs across reports. Now the backend gives us the
      // real per-(name, server, job) tally.
      for (const [jobKey, jdata] of Object.entries(charJobs)) {
        const j = cur.jobs.get(jobKey) || {
          job: jobKey, root: 0, cascade: 0, mit_failure: 0,
          heal_failure: 0, heal_failure_caused: 0,
          enrage: 0, unknown: 0,
          avoidable_damage: 0, damage_downs: 0,
          fights: 0, score: 0,
        };
        j.root += jdata.root || 0;
        j.cascade += jdata.cascade || 0;
        j.mit_failure += jdata.mit_failure || 0;
        j.heal_failure += jdata.heal_failure || 0;
        j.heal_failure_caused += jdata.heal_failure_caused || 0;
        j.enrage += jdata.enrage || 0;
        j.unknown += jdata.unknown || 0;
        j.avoidable_damage += jdata.avoidable_damage || 0;
        j.damage_downs += jdata.damage_downs || 0;
        j.fights += jdata.fights || 0;
        j.score += jdata.score || 0;
        cur.jobs.set(jobKey, j);
      }
      byKey.set(key, cur);
    }
    // Compute confidence per group from the summed counts (rebuild
    // classified / total from the raw kind counters since the backend's
    // per-player classified_fraction wouldn't aggregate correctly across
    // different deaths_total denominators).
    for (const g of byKey.values()) {
      const classified = g.root + g.cascade + g.mit_failure
                         + g.heal_failure + g.enrage;
      const total = classified + g.unknown;
      g.classified_fraction = total > 0
        ? Math.round((classified / total) * 1000) / 1000
        : null;
      // Per-wipe rate. When fights=0 (shouldn't happen but safe), null.
      g.score_per_wipe = g.fights > 0 ? g.score / g.fights : null;
      // Sort the per-job breakdown by score desc inside each group.
      g.jobs_list = [...g.jobs.values()].sort((a, b) => b.score - a.score);
      // List of characters under this row (sorted by score desc), and the
      // count for the multi-account badge.
      g.characters_list = [...g.characters.values()]
        .map((c) => ({ ...c, jobs: [...c.jobs] }))
        .sort((a, b) => b.score - a.score);
      g.character_count = g.characters_list.length;
      // Top-5 worst-weighted wipes across the merged characters.
      g.worst_wipes.sort((a, b) => b.weighted - a.weighted);
      g.worst_wipes = g.worst_wipes.slice(0, 5);
    }
    return [...byKey.values()];
  }, [players]);

  // Sort by either absolute score or per-wipe score depending on toggle.
  const sortedPlayers = useMemo(() => {
    const out = [...groupedPlayers];
    if (perWipe) {
      out.sort((a, b) => (b.score_per_wipe ?? 0) - (a.score_per_wipe ?? 0));
    } else {
      out.sort((a, b) => b.score - a.score);
    }
    return out;
  }, [groupedPlayers, perWipe]);

  if (error) {
    return (
      <div className="card" style={{ borderColor: 'var(--danger)' }}>
        <span className="pill pill-danger">error</span>{' '}
        <span className="text-sm">{error}</span>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="card"><span className="loading">Loading</span></div>
    );
  }

  const stale = wipeCount > 0 && data.wipes_aggregated < wipeCount;

  return (
    <div>
      <div className="row" style={{ alignItems: 'baseline',
                                     justifyContent: 'space-between',
                                     marginBottom: 'var(--s-2)' }}>
        <h2 className="mb-0">Who's contributing to wipes</h2>
        <label className="row-tight gap-1 small muted"
               title="When on, divides each stat by wipes attended. Surfaces per-attempt fault density rather than absolute volume (useful when someone subbed out).">
          <input type="checkbox" checked={perWipe}
                 onChange={(e) => setPerWipe(e.target.checked)} />
          per wipe attended
        </label>
      </div>
      <p className="muted text-sm">
        <strong>Roots</strong> = a player's own mistake.{' '}
        <strong>Mit fail</strong> = died to a raidwide whose planned mits
        were missed.{' '}
        <strong>Heal fail caused</strong> = died-but-shouldn't-have shared
        across the active healers (raidwide killed someone but the mits
        fired — implies the raid wasn't topped). Cascades + the dying-side
        of heal fails surface in the row expansion.{' '}
        <strong>Avoidable</strong> = damage taken from tankbusters the
        player shouldn't have eaten. <strong>DD</strong> = Damage Down
        applications. Confidence flags players whose deaths the classifier
        couldn't label — head to the Abilities review queue.
      </p>
      <p className="muted text-sm">
        <strong>Score</strong> per wipe = raw × (phase × within × prog,{' '}
        <span title="The phase/within/prog product is capped at 8×, so one freak wipe can't dominate a whole-encounter total.">capped at 8×</span>
        ) × repeat-offender amplifier. Continuous{' '}
        <span title="prog_distance = phase + (1 - fp/100). Smooth across phase boundaries — no cliff between P4 99% and P5 100%.">prog distance</span>{' '}
        instead of phase-only delta means a near-clear backslide doesn't
        suddenly fall off. The amplifier{' '}
        <span title="Rate-based (past-wall offenses / wipes attended): exp(4 × rate), capped at 5×. Denominator floored at 20 so early-prog noise doesn't spike. v1.16.0 also counts mit_failures as past-wall offenses.">scales with attendance</span>{' '}
        so the same five offenses hit a 100-wipe roster much harder than a
        1000-wipe one. Click any row to see the worst-weighted wipes and
        the multiplier breakdown.{' '}
        {perWipe
          ? <em>Currently showing rates per wipe attended.</em>
          : <em>Toggle "per wipe" to normalize for sub-outs.</em>}
        {' '}For the mit side of the story — <em>which mits are dropping
        across the encounter</em> — see the mit-audit section above.
      </p>
      {groupedPlayers.length === 0 || stale ? (
        <div className="card stack-sm">
          <p className="muted text-sm mb-0">
            {groupedPlayers.length === 0
              ? 'No fault analysis yet.'
              : `${data.wipes_aggregated}/${wipeCount} wipes analysed — `
                + 'newer wipes need re-computing.'}
          </p>
          <div className="row-tight gap-2">
            <button onClick={compute} disabled={computing}
                    className="btn-primary">
              {computing ? <><span className="spinner" /> computing</>
                         : `Analyse all ${wipeCount} wipe${wipeCount === 1 ? '' : 's'}`}
            </button>
          </div>
        </div>
      ) : (
        <div className="card card-tight">
          <table className="t t-tight">
            <thead>
              <tr>
                <th style={{ width: 24 }}></th>
                <th>Player</th>
                <th>Job(s)</th>
                <th className="num">Roots</th>
                <th className="num" title="Died to a raidwide whose planned mits were missed (same weight as root)">Mit fail</th>
                <th className="num" title="Heal failures caused (raidwide killed someone w/ mits up). Splits 1.0 weight across the alive healers at that moment.">Heal fail</th>
                <th className="num" title="Avoidable damage taken (tankbusters on non-tanks, ≥50k per hit so splash doesn't count)">Avoidable</th>
                <th className="num" title="Damage Down applications (survive-your-mistake)">DD</th>
                <th className="num" title="Fraction of deaths the classifier labeled (vs unknown)">Conf</th>
                <th className="num">Wipes</th>
                <th className="num">Score</th>
              </tr>
            </thead>
            <tbody>
              {sortedPlayers.map((g) => {
                const conf = g.classified_fraction;
                const confPct = conf != null ? Math.round(conf * 100) : null;
                const confColor = confPct == null
                  ? 'var(--fg-muted)'
                  : confPct >= 80 ? 'var(--success)'
                  : confPct >= 50 ? 'var(--warning)'
                  : 'var(--danger)';
                // Helpers: when `perWipe` is on, divide by attended wipes
                // (g.fights). Render with 2 decimals for counts and 0
                // for amounts so a "0.05 roots/wipe" reads cleanly.
                const fmtCount = (v) => perWipe
                  ? (g.fights > 0 ? (v / g.fights).toFixed(2) : '—')
                  : v;
                const fmtCountOrDash = (v) => {
                  if (!v) return '—';
                  return perWipe
                    ? (g.fights > 0 ? (v / g.fights).toFixed(2) : '—')
                    : v;
                };
                const avoidableDisplay = g.avoidable_damage > 0
                  ? perWipe
                    ? (g.fights > 0
                        ? `${(g.avoidable_damage / g.fights / 1000).toFixed(1)}k`
                        : '—')
                    : `${(g.avoidable_damage / 1000).toFixed(0)}k`
                  : '—';
                const displayScore = perWipe
                  ? (g.score_per_wipe ?? 0)
                  : g.score;
                const scoreColorThresholds = perWipe
                  ? { warn: 0.05, danger: 0.15 }
                  : { warn: 2, danger: 5 };
                const isOpen = expanded === g.key;
                const topKillers = topMechanicsForPlayerSet(
                  breakdown,
                  g.characters_list.map((c) => c.character_name),
                  5,
                );
                const jobSummary = g.jobs_list.length === 1
                  ? g.jobs_list[0].job
                  : g.jobs_list.length <= 3
                    ? g.jobs_list.map((j) => j.job).join(', ')
                    : `${g.jobs_list.length} jobs`;
                return [
                  <tr key={g.key}
                      onClick={() => setExpanded(isOpen ? null : g.key)}
                      style={{ cursor: 'pointer' }}>
                    <td className="muted">{isOpen ? '▾' : '▸'}</td>
                    <td>
                      {g.name}
                      {g.character_count > 1 && (
                        <span className="pill pill-accent small"
                              style={{ marginLeft: 6 }}
                              title={`${g.character_count} characters merged: `
                                + g.characters_list.map(
                                    (c) => `${c.character_name}`
                                      + (c.server ? ` @ ${c.server}` : '')
                                      + (c.jobs.length ? ` (${c.jobs.join('/')})` : '')
                                  ).join(' · ')}>
                          +{g.character_count - 1} alt{g.character_count > 2 ? 's' : ''}
                        </span>
                      )}
                    </td>
                    <td className="muted small"
                        title={g.jobs_list.map((j) => `${j.job}: ${j.fights} fights, score ${j.score.toFixed(1)}`).join('\n')}>
                      {jobSummary}
                    </td>
                    <td className="num">{fmtCount(g.root)}</td>
                    <td className="num"
                        style={{ color: g.mit_failure > 0 ? 'var(--danger)' : 'var(--fg-muted)' }}>
                      {fmtCountOrDash(g.mit_failure)}
                    </td>
                    <td className="num"
                        style={{ color: g.heal_failure_caused > 0 ? 'var(--danger)' : 'var(--fg-muted)' }}>
                      {fmtCountOrDash(g.heal_failure_caused)}
                    </td>
                    <td className="num"
                        style={{ color: g.avoidable_damage > 0 ? 'var(--warning)' : 'var(--fg-muted)' }}>
                      {avoidableDisplay}
                    </td>
                    <td className="num"
                        style={{ color: g.damage_downs > 0 ? 'var(--warning)' : 'var(--fg-muted)' }}>
                      {fmtCountOrDash(g.damage_downs)}
                    </td>
                    <td className="num small">
                      {confPct != null ? (
                        <span style={{ color: confColor }}>{confPct}%</span>
                      ) : <span className="muted">—</span>}
                    </td>
                    <td className="num">{g.fights}</td>
                    <td className="num"
                        title={`raw: ${g.raw_score.toFixed(1)}`
                          + (g.past_wall_offenses > 0
                              ? ` · ${g.past_wall_offenses} past-wall offense${g.past_wall_offenses === 1 ? '' : 's'}`
                              : '')
                          + ' · click row for per-wipe decomposition'}>
                      <strong style={{
                        color: displayScore >= scoreColorThresholds.danger ? 'var(--danger)'
                             : displayScore >= scoreColorThresholds.warn ? 'var(--warning)'
                             : 'var(--fg)',
                      }}>
                        {perWipe ? displayScore.toFixed(2) : displayScore.toFixed(1)}
                      </strong>
                    </td>
                  </tr>,
                  isOpen && (
                    <tr key={`${g.key}-expand`}>
                      <td></td>
                      <td colSpan={10}
                          style={{ background: 'var(--bg-elevated)',
                                   padding: 'var(--s-3)' }}>
                        {/* v1.16.0 score decomposition — top-N worst-weighted wipes */}
                        {g.worst_wipes.length > 0 && (
                          <div style={{ marginBottom: 'var(--s-3)' }}>
                            <div className="muted small"
                                 style={{ marginBottom: 'var(--s-2)' }}>
                              Top {g.worst_wipes.length} worst-weighted wipes
                              (raw fault × multipliers → weighted contribution):
                            </div>
                            <table className="t t-tight">
                              <thead>
                                <tr>
                                  <th>Wipe</th>
                                  <th className="num" title="raw fault score before multipliers">Raw</th>
                                  <th className="num" title="phase severity (mild quadratic)">Phase</th>
                                  <th className="num" title="within-phase severity (boss HP)">Within</th>
                                  <th className="num" title="prog relevance (continuous prog distance)">Prog</th>
                                  <th className="num" title="repeat-offender amplifier on past-wall offenses">Repeat</th>
                                  <th className="num" title="combined contribution to this row's total score">Weighted</th>
                                </tr>
                              </thead>
                              <tbody>
                                {g.worst_wipes.map((w, i) => {
                                  // Coerce via Number() — backend may return
                                  // SQLAlchemy Numeric as a JSON string in
                                  // some paths; we want safe .toFixed().
                                  const fp = w.fight_percentage != null
                                    ? Number(w.fight_percentage) : null;
                                  const raw = Number(w.raw ?? 0);
                                  const phaseSev = Number(w.phase_severity ?? 1);
                                  const within = Number(w.within_phase ?? 1);
                                  const prog = Number(w.prog_relevance ?? 1);
                                  const repeat = Number(w.repeat_multiplier ?? 1);
                                  const weighted = Number(w.weighted ?? raw);
                                  const phaseLabel = w.last_phase != null
                                    ? `P${w.last_phase}`
                                    : '—';
                                  const fpLabel = fp != null ? `${fp.toFixed(1)}%` : '—';
                                  return (
                                    <tr key={`${w.fight_id}-${i}`}>
                                      <td className="small">
                                        {phaseLabel} @ {fpLabel}
                                        {w.best_phase_at_time != null
                                          && w.last_phase != null
                                          && w.last_phase < w.best_phase_at_time && (
                                            <span className="muted">
                                              {' '}(best was P{w.best_phase_at_time})
                                            </span>
                                          )}
                                      </td>
                                      <td className="num small">{raw.toFixed(1)}</td>
                                      <td className="num small">{phaseSev.toFixed(2)}×</td>
                                      <td className="num small">{within.toFixed(2)}×</td>
                                      <td className="num small">{prog.toFixed(2)}×</td>
                                      <td className="num small"
                                          style={{ color: repeat > 1.5
                                                   ? 'var(--danger)'
                                                   : repeat > 1.0
                                                     ? 'var(--warning)' : 'var(--fg-muted)' }}>
                                        {repeat.toFixed(2)}×
                                      </td>
                                      <td className="num small"><strong>{weighted.toFixed(2)}</strong></td>
                                    </tr>
                                  );
                                })}
                              </tbody>
                            </table>
                          </div>
                        )}
                        {/* v1.16.1 scoped top contributors — top 5 OTHER
                            players ranked by score, restricted to the wipes
                            THIS player attended. Useful for "in Alice's
                            attended wipes, who's actually driving the score?" */}
                        {g.scoped_top_contributors.length > 0 && (
                          <div style={{ marginBottom: 'var(--s-3)' }}>
                            <div className="muted small"
                                 style={{ marginBottom: 'var(--s-2)' }}>
                              Top contributors across the {g.scoped_wipes_count}
                              {' '}wipe{g.scoped_wipes_count === 1 ? '' : 's'} {g.name} attended
                              {' '}(scoped — others may have higher overall scores in wipes
                              {g.name === 'they' ? '' : ` ${g.name}`} wasn't in):
                            </div>
                            <table className="t t-tight">
                              <tbody>
                                {g.scoped_top_contributors.map((c, i) => (
                                  <tr key={i}>
                                    <td>{i + 1}.</td>
                                    <td>{c.name}</td>
                                    <td className="num"><strong>{Number(c.score).toFixed(1)}</strong></td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        )}
                        {/* Cascades + heal-failure-victim live in the expansion
                            since they don't drive the headline score. */}
                        {(g.cascade > 0 || g.heal_failure > 0) && (
                          <div className="muted small"
                               style={{ marginBottom: 'var(--s-3)' }}>
                            Also tracked but not score-relevant:{' '}
                            {g.cascade > 0 && <span>{g.cascade} cascade death{g.cascade === 1 ? '' : 's'}</span>}
                            {g.cascade > 0 && g.heal_failure > 0 && ' · '}
                            {g.heal_failure > 0 && <span>{g.heal_failure} heal-failure death{g.heal_failure === 1 ? '' : 's'} (the dying side; blame is on the healers — see Heal fail column)</span>}
                          </div>
                        )}
                        {g.jobs_list.length > 1 && (
                          <div style={{ marginBottom: 'var(--s-3)' }}>
                            <div className="muted small"
                                 style={{ marginBottom: 'var(--s-2)' }}>
                              Per-job breakdown ({g.jobs_list.length} jobs played
                              {perWipe ? ', per wipe attended' : ''}):
                            </div>
                            <table className="t t-tight">
                              <thead>
                                <tr>
                                  <th>Job</th>
                                  <th className="num">Roots</th>
                                  <th className="num">Mit fail</th>
                                  <th className="num">Cascades</th>
                                  <th className="num">Avoidable</th>
                                  <th className="num">DD</th>
                                  <th className="num">Wipes</th>
                                  <th className="num">Score</th>
                                </tr>
                              </thead>
                              <tbody>
                                {g.jobs_list.map((j) => {
                                  const f = j.fights;
                                  const jc = (v) => perWipe
                                    ? (f > 0 ? (v / f).toFixed(2) : '—')
                                    : v;
                                  const jcOrDash = (v) => v
                                    ? jc(v) : '—';
                                  const jAvoid = j.avoidable_damage > 0
                                    ? perWipe
                                      ? (f > 0
                                          ? `${(j.avoidable_damage / f / 1000).toFixed(1)}k`
                                          : '—')
                                      : `${(j.avoidable_damage / 1000).toFixed(0)}k`
                                    : '—';
                                  const jScore = perWipe
                                    ? (f > 0 ? j.score / f : 0)
                                    : j.score;
                                  return (
                                    <tr key={j.job}>
                                      <td>{j.job}</td>
                                      <td className="num">{jc(j.root)}</td>
                                      <td className="num"
                                          style={{ color: j.mit_failure > 0 ? 'var(--danger)' : 'var(--fg-muted)' }}>
                                        {jcOrDash(j.mit_failure)}
                                      </td>
                                      <td className="num">{jc(j.cascade)}</td>
                                      <td className="num muted small">{jAvoid}</td>
                                      <td className="num">{jcOrDash(j.damage_downs)}</td>
                                      <td className="num">{j.fights}</td>
                                      <td className="num">
                                        <strong>
                                          {perWipe ? jScore.toFixed(2) : jScore.toFixed(1)}
                                        </strong>
                                      </td>
                                    </tr>
                                  );
                                })}
                              </tbody>
                            </table>
                          </div>
                        )}
                        {g.character_count > 1 && (
                          <div style={{ marginBottom: 'var(--s-3)' }}>
                            <div className="muted small"
                                 style={{ marginBottom: 'var(--s-2)' }}>
                              Characters merged into {g.name} ({g.character_count} accounts):
                            </div>
                            <table className="t t-tight">
                              <thead>
                                <tr>
                                  <th>Character</th>
                                  <th>Server</th>
                                  <th>Job(s)</th>
                                  <th className="num">Wipes</th>
                                  <th className="num">Score</th>
                                </tr>
                              </thead>
                              <tbody>
                                {g.characters_list.map((c) => (
                                  <tr key={`${c.character_name}|${c.server || ''}`}>
                                    <td>{c.character_name}</td>
                                    <td className="muted small">{c.server || '—'}</td>
                                    <td className="muted small">
                                      {c.jobs.length ? c.jobs.join(', ') : '—'}
                                    </td>
                                    <td className="num">{c.fights}</td>
                                    <td className="num"><strong>{c.score.toFixed(1)}</strong></td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </div>
                        )}
                        {topKillers.length === 0 ? (
                          <span className="muted small">
                            No per-mechanic data — likely all-unknown deaths
                            (improve via the Abilities review queue).
                          </span>
                        ) : (
                          <>
                            <div className="muted small"
                                 style={{ marginBottom: 'var(--s-2)' }}>
                              What {g.name} most often die to:
                            </div>
                            <table className="t t-tight">
                              <tbody>
                                {topKillers.map((k) => (
                                  <tr key={k.ability_game_id ?? 'unattributable'}>
                                    <td>
                                      {k.ability_name
                                        || (k.ability_game_id == null
                                            ? 'non-attributable (FFLogs sourceID=-1)'
                                            : `ability ${k.ability_game_id}`)}
                                    </td>
                                    <td>
                                      {k.ability_label
                                        ? <MechanicPill label={k.ability_label} />
                                        : <span className="muted small">—</span>}
                                    </td>
                                    <td className="num">
                                      <strong>{k.deaths}</strong> death{k.deaths === 1 ? '' : 's'}
                                    </td>
                                    <td className="muted small">
                                      {k.by_kind.root > 0 && `${k.by_kind.root}× root `}
                                      {k.by_kind.mit_failure > 0 && `${k.by_kind.mit_failure}× mit-fail `}
                                      {k.by_kind.cascade > 0 && `${k.by_kind.cascade}× cascade `}
                                      {k.by_kind.unknown > 0 && `${k.by_kind.unknown}× unknown `}
                                    </td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </>
                        )}
                      </td>
                    </tr>
                  ),
                ];
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/* ------------ Section 4: DPS vs field (v1.10.0) -------------------------- */

function DpsComparisonSection({ encounterId }) {
  const [job, setJob] = useState('');
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    setData(null);
    setError(null);
    const qs = job ? `?job=${encodeURIComponent(job)}` : '';
    fetch(`/api/encounters/${encounterId}/dps-comparison${qs}`)
      .then((r) => r.json())
      .then(setData)
      .catch((e) => setError(String(e)));
  }, [encounterId, job]);

  if (error) {
    return (
      <div className="card" style={{ borderColor: 'var(--danger)' }}>
        <span className="pill pill-danger">error</span>{' '}
        <span className="text-sm">{error}</span>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="card"><span className="loading">Loading</span></div>
    );
  }

  // No kill data anywhere in this encounter.
  if (data.field.kills_aggregated === 0 && data.ours.kills_aggregated === 0) {
    return (
      <div>
        <h2 className="mb-0" style={{ marginBottom: 'var(--s-2)' }}>
          Your DPS vs the field
        </h2>
        <div className="card">
          <p className="muted text-sm mb-0">
            No kills ingested for this encounter yet. Field comparison
            activates once kill data exists in the database.
          </p>
        </div>
      </div>
    );
  }

  // Merge phase indices from both sides for a unified row set.
  const phaseSet = new Set([
    ...data.ours.phases.map((p) => p.phase_index),
    ...data.field.phases.map((p) => p.phase_index),
  ]);
  const phases = [...phaseSet].sort((a, b) => a - b);
  const oursByPhase = Object.fromEntries(
    data.ours.phases.map((p) => [p.phase_index, p.dps]),
  );
  const fieldByPhase = Object.fromEntries(
    data.field.phases.map((p) => [p.phase_index, p.dps]),
  );

  return (
    <div>
      <div className="row" style={{ alignItems: 'baseline',
                                    justifyContent: 'space-between',
                                    marginBottom: 'var(--s-2)' }}>
        <h2 className="mb-0">Your DPS vs the field</h2>
        <label className="row-tight gap-2">
          <span className="muted small">Filter to job</span>
          <select value={job} onChange={(e) => setJob(e.target.value)}>
            <option value="">All jobs (raid DPS)</option>
            {data.jobs_available.map((j) => (
              <option key={j} value={j}>{j}</option>
            ))}
          </select>
        </label>
      </div>
      <p className="muted text-sm">
        {job
          ? `Per-player DPS for ${job} across watched kills (ours) vs all `
            + `other ingested kills (field). Lets you see where your `
            + `${job} sits in the per-job distribution for this fight.`
          : 'Per-phase raid DPS across our watched kills vs all other '
            + 'ingested kills. Median is the headline; p25–p75 spread '
            + 'shows the typical range.'}{' '}
        {data.ours.kills_aggregated} of our kill
        {data.ours.kills_aggregated === 1 ? '' : 's'} aggregated vs{' '}
        {data.field.kills_aggregated} field kill
        {data.field.kills_aggregated === 1 ? '' : 's'}.
      </p>

      {phases.length === 0 ? (
        <div className="card">
          <p className="muted text-sm mb-0">
            {job
              ? `No ${job} found in the aggregated kills for this encounter.`
              : 'No phase data available.'}
          </p>
        </div>
      ) : (
        <div className="card card-tight">
          <table className="t t-tight">
            <thead>
              <tr>
                <th>Phase</th>
                <th className="num">Our median</th>
                <th className="num">Field p25</th>
                <th className="num">Field median</th>
                <th className="num">Field p75</th>
                <th className="num">Δ vs median</th>
              </tr>
            </thead>
            <tbody>
              {phases.map((pi) => {
                const ours = oursByPhase[pi];
                const field = fieldByPhase[pi];
                const ourP50 = ours?.p50;
                const fieldP50 = field?.p50;
                const delta = ourP50 != null && fieldP50 != null && fieldP50 > 0
                  ? Math.round(((ourP50 - fieldP50) / fieldP50) * 100)
                  : null;
                const deltaColor = delta == null
                  ? 'var(--fg-muted)'
                  : delta >= 0 ? 'var(--success)'
                  : delta >= -10 ? 'var(--warning)'
                  : 'var(--danger)';
                return (
                  <tr key={pi}>
                    <td>P{pi}</td>
                    <td className="num">
                      {ourP50 != null ? formatDps(ourP50) : '—'}
                      {ours?.n > 1 && (
                        <span className="muted small"> (n={ours.n})</span>
                      )}
                    </td>
                    <td className="num muted">
                      {field?.p25 != null ? formatDps(field.p25) : '—'}
                    </td>
                    <td className="num">
                      {field?.p50 != null ? <strong>{formatDps(field.p50)}</strong> : '—'}
                    </td>
                    <td className="num muted">
                      {field?.p75 != null ? formatDps(field.p75) : '—'}
                    </td>
                    <td className="num">
                      {delta != null ? (
                        <strong style={{ color: deltaColor }}>
                          {delta >= 0 ? '+' : ''}{delta}%
                        </strong>
                      ) : <span className="muted">—</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function formatDps(v) {
  if (v == null) return '—';
  if (v >= 1000) return `${(v / 1000).toFixed(1)}k`;
  return v.toFixed(0);
}

/* ------------ v1.13.0: breakdown pivots ---------------------------------- */

function topOffendersForMechanic(breakdown, abilityId, limit) {
  if (!breakdown || abilityId == null) return [];
  return (breakdown.rows || [])
    .filter((r) => r.ability_game_id === abilityId)
    .sort((a, b) => b.deaths - a.deaths)
    .slice(0, limit);
}

// v1.14.2: when the wipe-mechanics table groups multiple ability IDs under
// one mechanic name, aggregate offender deaths across all of them.
// v1.16.2: groups by player_NAME (was player_id) — pids are report-scoped.
function topOffendersForMechanicSet(breakdown, abilityIds, limit) {
  if (!breakdown || !abilityIds || abilityIds.length === 0) return [];
  const idSet = new Set(abilityIds);
  const byPlayer = new Map();
  for (const r of breakdown.rows || []) {
    if (!idSet.has(r.ability_game_id)) continue;
    const key = r.player_name || `pid:${r.player_id}`;
    const cur = byPlayer.get(key) || {
      player_id: r.player_id,
      player_name: r.player_name,
      job: r.job,
      deaths: 0,
    };
    cur.deaths += r.deaths;
    cur.player_name = cur.player_name || r.player_name;
    cur.job = cur.job || r.job;
    byPlayer.set(key, cur);
  }
  return [...byPlayer.values()]
    .sort((a, b) => b.deaths - a.deaths)
    .slice(0, limit);
}

function topMechanicsForPlayer(breakdown, playerId, limit) {
  if (!breakdown || playerId == null) return [];
  return (breakdown.rows || [])
    .filter((r) => r.player_id === playerId)
    .sort((a, b) => b.deaths - a.deaths)
    .slice(0, limit);
}

// v1.14.3 / v1.16.2: filter by character NAMES (was player_ids — pids are
// report-scoped so the same numeric id maps to different characters across
// reports). Caller passes the list of character names that map to this
// merged group.
function topMechanicsForPlayerSet(breakdown, characterNames, limit) {
  if (!breakdown || !characterNames || characterNames.length === 0) return [];
  const nameSet = new Set(characterNames);
  const byAid = new Map();
  for (const r of breakdown.rows || []) {
    if (!nameSet.has(r.player_name)) continue;
    const key = r.ability_game_id ?? 'unattributable';
    const cur = byAid.get(key) || {
      ability_game_id: r.ability_game_id,
      ability_name: r.ability_name,
      ability_label: r.ability_label,
      deaths: 0,
      by_kind: { root: 0, cascade: 0, mit_failure: 0, enrage: 0, unknown: 0 },
    };
    cur.deaths += r.deaths;
    cur.ability_name = cur.ability_name || r.ability_name;
    cur.ability_label = cur.ability_label || r.ability_label;
    for (const k of Object.keys(cur.by_kind)) {
      cur.by_kind[k] += (r.by_kind?.[k]) || 0;
    }
    byAid.set(key, cur);
  }
  return [...byAid.values()]
    .sort((a, b) => b.deaths - a.deaths)
    .slice(0, limit);
}

/* -------------------------------------------------------------------------- */
/*                       onboarding + alt empty states                        */
/* -------------------------------------------------------------------------- */

function Onboarding({ me }) {
  return (
    <section className="fade-in">
      <h1 className="mb-0">Welcome, {me.username}</h1>
      <p className="muted text-sm">
        Vigil tracks your static's progression on FFXIV ultimates.
        You're in <strong>{me.statics.find((s) => s.id === me.current_static_id)?.name
          || 'your static'}</strong>. Get started below.
      </p>

      <div className="split-2" style={{ marginTop: 'var(--s-5)' }}>
        <Step
          num={1}
          title="Add your roster"
          cta="Go to Roster"
          href="#roster"
          body={
            <>
              Add each member of your static and the FFXIV character names
              they play. Vigil joins this to combat-log data so analyses
              use names you recognise.
            </>
          }
        />
        <Step
          num={2}
          title="Watch a report"
          cta="Go to Reports"
          href="#reports"
          body={
            <>
              Paste an FFLogs report URL or code. Vigil pulls in every fight
              and event, then surfaces wipe locations, deaths, mit usage,
              burst alignment, and per-pull cactbot timeline drift.
            </>
          }
        />
      </div>

      <div className="row" style={{ marginTop: 'var(--s-5)',
                                    justifyContent: 'center' }}>
        <ProgPoints />
      </div>
    </section>
  );
}

function WatchedButNoData() {
  return (
    <section className="fade-in">
      <h1 className="mb-0">Prog dashboard</h1>
      <p className="muted text-sm">
        You've added reports but the poller hasn't ingested any fights yet.
      </p>
      <div className="card">
        <p className="text-sm">
          Open the <a href="#reports">Reports tab</a> and click{' '}
          <strong>poll now</strong> on each watched report to ingest its
          fights. Or wait for the scheduled poller.
        </p>
      </div>
      <ProgPoints />
    </section>
  );
}

/* -------------------------------------------------------------------------- */
/*                              dev Home (unchanged)                          */
/* -------------------------------------------------------------------------- */

function DevHome({ reports }) {
  const totals = reports.reduce(
    (acc, r) => {
      acc.fights += r.fight_count;
      acc.kills += r.kill_count;
      acc.wipes += r.wipe_count;
      return acc;
    },
    { fights: 0, kills: 0, wipes: 0 },
  );
  const killRate = totals.fights
    ? Math.round((totals.kills / totals.fights) * 100)
    : 0;
  return (
    <section className="fade-in">
      <div className="row" style={{ alignItems: 'flex-end',
                                    justifyContent: 'space-between',
                                    marginBottom: 'var(--s-4)' }}>
        <div>
          <h1 className="mb-0">Home</h1>
          <p className="muted text-sm mb-0">
            Snapshot of all ingested data (dev view — includes field backfill).
          </p>
        </div>
      </div>
      <div className="stat-grid">
        <Stat label="Reports" value={reports.length} />
        <Stat label="Total pulls" value={totals.fights} />
        <Stat label="Kills" value={totals.kills}
              accent={totals.kills > 0 ? 'success' : null} />
        <Stat label="Wipes" value={totals.wipes} />
        <Stat label="Kill rate" value={`${killRate}%`}
              hint={totals.fights ? `${totals.kills}/${totals.fights}` : '—'} />
      </div>
      <ProgPoints />
    </section>
  );
}

/* -------------------------------------------------------------------------- */
/*                                  bits                                      */
/* -------------------------------------------------------------------------- */

function Stat({ label, value, accent, hint }) {
  const accentColor =
    accent === 'success' ? 'var(--success)'
    : accent === 'danger' ? 'var(--danger)'
    : null;
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value" style={accentColor ? { color: accentColor } : null}>
        {value}
      </div>
      {hint && <div className="muted small" style={{ marginTop: 2 }}>{hint}</div>}
    </div>
  );
}

function Step({ num, title, body, cta, href }) {
  return (
    <div className="card">
      <div className="row-tight gap-3" style={{ alignItems: 'baseline' }}>
        <span style={{
          display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
          width: 28, height: 28, borderRadius: '50%',
          background: 'var(--accent-soft)', color: 'var(--accent)',
          fontWeight: 700,
        }}>{num}</span>
        <h3 className="mb-0">{title}</h3>
      </div>
      <p className="text-sm" style={{ margin: '12px 0 16px' }}>{body}</p>
      <button type="button" onClick={() => { window.location.hash = href; }}
              className="btn-primary">
        {cta} →
      </button>
    </div>
  );
}
