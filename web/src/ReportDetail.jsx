import { useEffect, useMemo, useState } from 'react';
import {
  Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts';

const PHASE_TINTS = [
  'var(--role-MT)', 'var(--role-D4)', 'var(--role-D1)',
  'var(--role-H1)', 'var(--role-D2)', 'var(--role-D3)', 'var(--role-any)',
];

const VERDICT_COLOR = {
  not_gated: 'var(--success)',
  clean: 'var(--success)',
  dps_gated: 'var(--accent)',
  mechanics_gated: 'var(--danger)',
  both_gated: 'var(--type-enrage)',
  many_deaths: 'var(--danger)',
  no_target: 'var(--fg-faint)',
};
const VERDICT_LABEL = {
  not_gated: 'OK', clean: 'OK', dps_gated: 'DPS',
  mechanics_gated: 'MECH', both_gated: 'BOTH',
  many_deaths: 'DEATHS', no_target: '—',
};

export default function ReportDetail({ code, encounterId = null }) {
  const [wipes, setWipes] = useState(null);
  const [faults, setFaults] = useState(null);
  const [gcd, setGcd] = useState(null);
  const [roster, setRoster] = useState(null);
  const [burst, setBurst] = useState(null);
  const [error, setError] = useState(null);
  const [selectedFight, setSelectedFight] = useState(null);

  useEffect(() => {
    setWipes(null); setFaults(null); setGcd(null); setRoster(null);
    setBurst(null); setError(null); setSelectedFight(null);
    Promise.all([
      fetch(`/api/reports/${code}/wipes`).then((r) => r.json()),
      fetch(`/api/reports/${code}/faults`).then((r) => r.json()),
      fetch(`/api/reports/${code}/gcd`).then((r) => r.json()),
      fetch(`/api/reports/${code}/roster-resolution`).then((r) => r.json()),
      fetch(`/api/reports/${code}/burst`).then((r) => r.json()),
    ])
      .then(([w, f, g, r, b]) => {
        setWipes(w); setFaults(f); setGcd(g); setRoster(r); setBurst(b);
      })
      .catch((e) => setError(String(e)));
  }, [code]);

  if (error) return <p className="text-sm" style={{ color: 'var(--danger)' }}>{error}</p>;
  if (!wipes || !faults || !gcd || !roster || !burst) {
    return <div className="empty"><span className="loading">Loading</span></div>;
  }

  const fights = faults.fights || [];
  const gcdByFight = Object.fromEntries((gcd.fights || []).map((f) => [f.fight_id, f]));
  const burstByFight = Object.fromEntries((burst.fights || []).map((f) => [f.fight_id, f]));
  const memberByFightPlayer = {};
  for (const f of roster.fights || []) {
    memberByFightPlayer[f.fight_id] = {};
    for (const c of f.combatants) {
      memberByFightPlayer[f.fight_id][c.player_id] = c.member_name;
    }
  }

  return (
    <div className="stack">
      <div className="card">
        <div className="row" style={{ alignItems: 'baseline',
                                      justifyContent: 'space-between' }}>
          <h2 className="mb-0 mono">{code}</h2>
          <span className="row-tight gap-2 small">
            <span className="pill pill-success">
              {wipes.total_kills} kills
            </span>
            <span className="pill pill-danger">
              {wipes.total_wipes} wipes
            </span>
          </span>
        </div>
        <RosterCoverage coverage={roster.coverage} />
      </div>

      {encounterId != null && (
        <ConsensusTimeline encounterId={encounterId} />
      )}

      <div className="card">
        <h3 style={{ marginBottom: 'var(--s-3)' }}>Wipe location</h3>
        <WipeHistogram wipes={wipes} />
      </div>

      <div>
        <h3 style={{ marginBottom: 'var(--s-3)' }}>Pulls</h3>
        <div className="row row-stack-mobile" style={{ alignItems: 'flex-start' }}>
          <div className="sidebar">
            <PullList
              fights={fights}
              selected={selectedFight}
              onSelect={setSelectedFight}
            />
          </div>
          <div className="grow">
            {selectedFight ? (
              <PullDetail
                faults={fights.find((f) => f.fight_id === selectedFight)}
                gcd={gcdByFight[selectedFight]}
                burst={burstByFight[selectedFight]}
                memberOf={memberByFightPlayer[selectedFight] || {}}
              />
            ) : (
              <div className="empty">Pick a pull on the left.</div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function WipeHistogram({ wipes }) {
  const data = useMemo(
    () => (wipes.buckets || []).map((b) => ({
      label: `P${b.phase ?? '?'} · ${b.ability_game_id ?? '—'}`,
      count: b.count,
    })),
    [wipes],
  );
  if (data.length === 0) {
    return <div className="muted text-sm">No wipes to histogram.</div>;
  }
  return (
    <div>
      <div className="muted small" style={{ marginBottom: 'var(--s-2)' }}>
        {data.length} distinct (phase, mechanic) buckets
      </div>
      <div style={{ width: '100%', height: 220 }}>
        <ResponsiveContainer>
          <BarChart data={data}
                    margin={{ top: 8, right: 16, bottom: 40, left: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
            <XAxis dataKey="label" interval={0} angle={-30}
                   textAnchor="end" height={60}
                   tick={{ fontSize: 11, fill: 'var(--fg-muted)' }}
                   stroke="var(--fg-muted)" />
            <YAxis allowDecimals={false}
                   tick={{ fontSize: 11, fill: 'var(--fg-muted)' }}
                   stroke="var(--fg-muted)" />
            <Tooltip />
            <Bar dataKey="count" fill="var(--danger)" />
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

function PullList({ fights, selected, onSelect }) {
  return (
    <div style={{ maxHeight: 560, overflowY: 'auto', paddingRight: 4 }}>
      {fights.map((f) => (
        <button
          key={f.fight_id}
          onClick={() => onSelect(f.fight_id)}
          className={`tile ${selected === f.fight_id ? 'is-selected' : ''}`}
        >
          <div className="row-tight gap-2">
            <span className="tile-title">#{f.fight_id_in_report}</span>
            <span className={`pill ${f.is_kill ? 'pill-success' : 'pill-danger'}`}>
              {f.is_kill ? 'KILL' : 'WIPE'}
            </span>
            {!f.is_kill && f.fight_percentage != null && (
              <span className="muted small">{f.fight_percentage}%</span>
            )}
          </div>
          <div className="tile-meta">
            P{f.last_phase ?? '?'} · {Math.round((f.duration_ms || 0) / 1000)}s ·{' '}
            {f.deaths.length} death{f.deaths.length === 1 ? '' : 's'}
          </div>
        </button>
      ))}
    </div>
  );
}

function RosterCoverage({ coverage }) {
  if (!coverage || coverage.total_characters === 0) return null;
  const fully = coverage.resolved === coverage.total_characters;
  return (
    <div className={`pill ${fully ? 'pill-success' : 'pill-warning'}`}
         style={{ marginTop: 'var(--s-3)' }}>
      Roster: <strong style={{ margin: '0 4px' }}>
        {coverage.resolved}/{coverage.total_characters}
      </strong> resolved
      {coverage.unresolved.length > 0 && (
        <span style={{ marginLeft: 8 }}>
          · Unresolved: {coverage.unresolved
            .map((u) => `${u.name}${u.server ? ` @ ${u.server}` : ''}`)
            .join(', ')}
        </span>
      )}
    </div>
  );
}

function nameWithMember(rawName, memberName) {
  if (!memberName) return rawName;
  if (memberName === rawName) return memberName;
  return `${memberName} (${rawName})`;
}

function ConsensusTimeline({ encounterId }) {
  const [data, setData] = useState(null);
  const [expanded, setExpanded] = useState(false);
  useEffect(() => {
    setData(null);
    fetch(`/api/encounters/${encounterId}/consensus`)
      .then((r) => r.json()).then(setData)
      .catch(() => setData({ phases: [], note: 'failed' }));
  }, [encounterId]);

  if (!data) return null;
  if (data.phases.length === 0) return null;
  const totalCanonical = data.phases.reduce(
    (n, p) => n + p.canonical_abilities.length, 0,
  );

  return (
    <div className="card">
      <button onClick={() => setExpanded(!expanded)}
              className="btn-ghost btn-sm"
              style={{ width: '100%', textAlign: 'left',
                       padding: 0, fontSize: 'var(--fs-md)' }}>
        <h3 className="mb-0">
          {expanded ? '▾' : '▸'} Consensus boss timeline{' '}
          <span className="muted small" style={{ fontWeight: 400 }}>
            · {data.total_pulls} pulls · {totalCanonical} canonical ·{' '}
            {data.phases.length} phases
          </span>
        </h3>
      </button>
      {expanded && (
        <div style={{ marginTop: 'var(--s-3)' }}>
          {data.phases.map((p) => (
            <div key={p.phase_index} style={{ marginBottom: 'var(--s-3)' }}>
              <h5 style={{ marginBottom: 'var(--s-2)' }}>
                P{p.phase_index} — {p.pulls_reaching} pulls ·{' '}
                {p.canonical_abilities.length} canonical
                {p.all_abilities.length > p.canonical_abilities.length &&
                  ` (+${p.all_abilities.length - p.canonical_abilities.length} sub)`}
              </h5>
              <table className="t t-tight">
                <tbody>
                  {p.canonical_abilities.slice(0, 12).map((a) => (
                    <tr key={a.ability_game_id}>
                      <td className="muted small mono">
                        t+{(a.median_relative_t_ms / 1000).toFixed(1)}s
                      </td>
                      <td className="muted small">
                        ±{(a.variance_ms / 1000).toFixed(1)}s
                      </td>
                      <td className="mono small">{a.ability_game_id}</td>
                      <td className="muted small">
                        {Math.round(a.occurrence_rate * 100)}%
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {p.canonical_abilities.length > 12 && (
                <div className="faint small">
                  …{p.canonical_abilities.length - 12} more
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function PullDetail({ faults, gcd, burst, memberOf = {} }) {
  if (!faults) return null;
  return (
    <div className="card stack">
      <div className="row" style={{ alignItems: 'baseline',
                                    justifyContent: 'space-between' }}>
        <h2 className="mb-0">
          Pull #{faults.fight_id_in_report}{' '}
          <span className={`pill ${faults.is_kill ? 'pill-success' : 'pill-danger'}`}
                style={{ fontSize: 'var(--fs-sm)', verticalAlign: 'middle' }}>
            {faults.is_kill ? 'KILL' : 'WIPE'}
          </span>
        </h2>
        <span className="muted small">
          phase {faults.last_phase ?? '?'} ·{' '}
          {Math.round((faults.duration_ms || 0) / 1000)}s
          {!faults.is_kill && faults.fight_percentage != null &&
            ` · died @ ${faults.fight_percentage}% remaining`}
        </span>
      </div>

      <PhaseStrip fightId={faults.fight_id} />
      <GateVerdictStrip fightId={faults.fight_id} />

      <Section title={`Deaths (${faults.deaths.length})`}>
        {faults.deaths.length === 0 ? (
          <div className="muted small">No deaths.</div>
        ) : (
          <table className="t t-tight">
            <tbody>
              {faults.deaths.map((d, i) => (
                <tr key={i}>
                  <td className="muted num small">{i + 1}.</td>
                  <td>
                    <span className="text-strong">
                      {nameWithMember(d.name || `player ${d.player_id}`,
                                       memberOf[d.player_id])}
                    </span>
                    {d.job && <span className="muted"> ({d.job})</span>}
                  </td>
                  <td>
                    {d.killing_ability_game_id ? (
                      <code>ability {d.killing_ability_game_id}</code>
                    ) : (
                      <span className="muted small">non-attributable</span>
                    )}
                  </td>
                  <td className="muted small num mono">
                    @ {Math.round(d.ts / 1000)}s
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>

      <Section title="Top damage takers">
        <DamageTakersTable takers={faults.damage_takers} memberOf={memberOf} />
      </Section>

      <Section title="GCD drops">
        <GcdTable players={gcd?.players || []} memberOf={memberOf} />
      </Section>

      <Section
        title="Burst alignment"
        meta={`${burst?.burst_windows?.length || 0} windows`}
      >
        <BurstTable burst={burst} memberOf={memberOf} />
      </Section>

      <Section title="Per-phase DPS">
        <ParseTable fightId={faults.fight_id} memberOf={memberOf} />
      </Section>

      <FaultScores fightId={faults.fight_id} memberOf={memberOf} />
      <MitAudit fightId={faults.fight_id} />
      <TimelineDiff fightId={faults.fight_id} />
    </div>
  );
}

function Section({ title, meta, children }) {
  return (
    <div>
      <h5 className="row-tight gap-2" style={{ marginBottom: 'var(--s-2)' }}>
        {title}
        {meta && <span className="faint small" style={{ fontWeight: 400 }}>{meta}</span>}
      </h5>
      {children}
    </div>
  );
}

function PhaseStrip({ fightId }) {
  const [phases, setPhases] = useState(null);
  useEffect(() => {
    setPhases(null);
    fetch(`/api/fights/${fightId}/phases`)
      .then((r) => r.json()).then(setPhases)
      .catch(() => setPhases({ phases: [], transitions: [] }));
  }, [fightId]);

  if (!phases) return null;
  if (phases.phases.length === 0) {
    return (
      <div className="muted small">
        Phase detection found no distinct boss actors — likely single-phase.
      </div>
    );
  }
  const total = phases.phases[phases.phases.length - 1].end_offset_ms;
  return (
    <div>
      <div className="muted small" style={{ marginBottom: 'var(--s-1)' }}>
        {phases.phases.length} phase{phases.phases.length === 1 ? '' : 's'} detected
      </div>
      <div style={{ display: 'flex', height: 26, borderRadius: 'var(--radius-sm)',
                    overflow: 'hidden', border: '1px solid var(--border)' }}>
        {phases.phases.map((p, i) => {
          const widthPct = ((p.end_offset_ms - p.start_offset_ms) / total) * 100;
          const color = PHASE_TINTS[i % PHASE_TINTS.length];
          const dur = ((p.end_offset_ms - p.start_offset_ms) / 1000).toFixed(0);
          return (
            <div
              key={p.index}
              title={`P${p.index}: ${dur}s, bosses ${p.boss_target_ids.join(', ')}`}
              style={{
                width: `${widthPct}%`,
                background: color, opacity: 0.7,
                color: '#fff', display: 'flex',
                alignItems: 'center', justifyContent: 'center',
                fontSize: 11, fontWeight: 500,
                textShadow: '0 1px 2px rgba(0,0,0,.6)',
                borderRight: i < phases.phases.length - 1
                  ? '1px solid var(--border)' : 'none',
              }}
            >
              P{p.index}
            </div>
          );
        })}
      </div>
      <div className="faint small" style={{ marginTop: 'var(--s-1)' }}>
        transitions:{' '}
        {phases.transitions.length === 0 ? '—' :
          phases.transitions.map((t) => `${(t.gap_ms / 1000).toFixed(0)}s`).join(' / ')}
      </div>
    </div>
  );
}

function GateVerdictStrip({ fightId }) {
  const [data, setData] = useState(null);
  useEffect(() => {
    setData(null);
    fetch(`/api/fights/${fightId}/gate-diagnostic`)
      .then((r) => r.json()).then(setData)
      .catch(() => setData({ phases: [] }));
  }, [fightId]);

  if (!data || !data.phases.length) return null;
  return (
    <div>
      <div className="muted small" style={{ marginBottom: 'var(--s-1)' }}>
        M-GATE verdict per phase{' '}
        {data.kills_in_target > 0
          ? `(target from ${data.kills_in_target} kills)`
          : '(no DPS target yet)'}
      </div>
      <div className="row-tight gap-1 wrap">
        {data.phases.map((p) => {
          const color = VERDICT_COLOR[p.verdict] || 'var(--fg-faint)';
          const label = VERDICT_LABEL[p.verdict] || p.verdict;
          const dps = p.raid_dps ? `${(p.raid_dps / 1000).toFixed(0)}k dps` : null;
          const tgt = p.target?.p50
            ? `target ${(p.target.p50 / 1000).toFixed(0)}k` : null;
          const tip = [`P${p.phase_index}: ${p.verdict}`, dps, tgt,
                       `${p.deaths} deaths`].filter(Boolean).join(' · ');
          return (
            <span key={p.phase_index} title={tip}
                  style={{
                    padding: '3px 9px',
                    background: color, color: 'white',
                    fontSize: 'var(--fs-xs)', fontWeight: 600,
                    borderRadius: 999, cursor: 'help',
                    boxShadow: 'inset 0 0 0 1px rgba(255,255,255,.1)',
                  }}>
              P{p.phase_index} {label}
            </span>
          );
        })}
      </div>
    </div>
  );
}

function ParseTable({ fightId, memberOf = {} }) {
  const [parse, setParse] = useState(null);
  useEffect(() => {
    setParse(null);
    fetch(`/api/fights/${fightId}/parse`)
      .then((r) => r.json()).then(setParse)
      .catch(() => setParse({ phases: [] }));
  }, [fightId]);
  if (!parse) return null;
  if (parse.phases.length === 0) {
    return <div className="muted small">No parse data.</div>;
  }
  const allPlayers = {};
  for (const p of parse.phases) {
    for (const pp of p.players) {
      const cur = allPlayers[pp.player_id] || {
        player_id: pp.player_id, name: pp.name, job: pp.job, total: 0,
      };
      cur.total += pp.damage_total;
      allPlayers[pp.player_id] = cur;
    }
  }
  const players = Object.values(allPlayers).sort((a, b) => b.total - a.total);
  const dpsLookup = {};
  for (const p of parse.phases) {
    for (const pp of p.players) {
      dpsLookup[`${p.phase_index}_${pp.player_id}`] = pp.dps;
    }
  }
  return (
    <table className="t t-tight">
      <thead>
        <tr>
          <th>Player</th>
          {parse.phases.map((p) => (
            <th key={p.phase_index} className="num">P{p.phase_index}</th>
          ))}
        </tr>
      </thead>
      <tbody>
        {players.map((pl) => (
          <tr key={pl.player_id}>
            <td>
              <span className="text-strong">
                {nameWithMember(pl.name || `player ${pl.player_id}`,
                                 memberOf[pl.player_id])}
              </span>
              {pl.job && <span className="muted"> ({pl.job})</span>}
            </td>
            {parse.phases.map((p) => {
              const dps = dpsLookup[`${p.phase_index}_${pl.player_id}`];
              return (
                <td key={p.phase_index} className="num mono"
                    style={dps ? null : { color: 'var(--fg-faint)' }}>
                  {dps ? `${(Math.round(dps / 100) / 10).toFixed(1)}k` : '—'}
                </td>
              );
            })}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function BurstTable({ burst, memberOf = {} }) {
  if (!burst || burst.players.length === 0) {
    return (
      <div className="muted text-sm">
        No burst data — either no raid-buff windows in this pull, or no
        labeled personal CDs match these players' jobs (label more personal
        CDs in the Abilities tab).
      </div>
    );
  }
  return (
    <table className="t t-tight">
      <thead>
        <tr>
          <th>Player</th>
          <th className="num">In</th>
          <th className="num">Total</th>
          <th className="num">%</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {burst.players.map((p) => {
          const pct = Math.round(p.in_window_pct * 100);
          const color = pct >= 80 ? 'var(--success)'
            : pct >= 50 ? 'var(--fg-muted)' : 'var(--danger)';
          return (
            <tr key={p.player_id}>
              <td>
                <span className="text-strong">
                  {nameWithMember(p.name || `player ${p.player_id}`,
                                   memberOf[p.player_id])}
                </span>
                {p.job && <span className="muted"> ({p.job})</span>}
              </td>
              <td className="num">{p.in_window}</td>
              <td className="num">{p.personal_casts_total}</td>
              <td className="num text-strong" style={{ color }}>{pct}%</td>
              <td style={{ width: 100 }}>
                <div className="bar-track">
                  <div className="bar-fill"
                       style={{ width: `${pct}%`, background: color }} />
                </div>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function DamageTakersTable({ takers, memberOf = {} }) {
  const top = takers.slice(0, 8);
  if (top.length === 0) return <div className="muted small">No damage data.</div>;
  const max = Math.max(...top.map((t) => t.damage_taken_total));
  return (
    <table className="t t-tight">
      <tbody>
        {top.map((t) => (
          <tr key={t.player_id}>
            <td>
              <span className="text-strong">
                {nameWithMember(t.name || `player ${t.player_id}`,
                                 memberOf[t.player_id])}
              </span>
              {t.job && <span className="muted"> ({t.job})</span>}
            </td>
            <td className="num mono">{t.damage_taken_total.toLocaleString()}</td>
            <td style={{ width: 100 }}>
              <div className="bar-track">
                <div className="bar-fill"
                     style={{ width: `${(t.damage_taken_total / max) * 100}%`,
                              background: 'var(--danger)' }} />
              </div>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function GcdTable({ players, memberOf = {} }) {
  if (players.length === 0) return <div className="muted small">No casts.</div>;
  return (
    <table className="t t-tight">
      <thead>
        <tr>
          <th>Player</th>
          <th className="num">GCD</th>
          <th className="num">Cast</th>
          <th className="num">Dropped</th>
        </tr>
      </thead>
      <tbody>
        {players.map((p) => (
          <tr key={p.player_id}>
            <td>
              <span className="text-strong">
                {nameWithMember(p.name || `player ${p.player_id}`,
                                 memberOf[p.player_id])}
              </span>
              {p.job && <span className="muted"> ({p.job})</span>}
            </td>
            <td className="num mono">{p.gcd_ms}ms</td>
            <td className="num">{p.gcds_cast}</td>
            <td className="num"
                style={{ color: p.dropped_count > 0
                  ? 'var(--danger)' : 'var(--fg-muted)',
                  fontWeight: p.dropped_count > 0 ? 600 : 400 }}>
              {p.dropped_count}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function MitAudit({ fightId }) {
  const [audit, setAudit] = useState(null);
  useEffect(() => {
    setAudit(null);
    fetch(`/api/fights/${fightId}/mit-audit`)
      .then((r) => r.json()).then(setAudit)
      .catch(() => setAudit({ raidwide_casts: [] }));
  }, [fightId]);

  if (!audit || audit.raidwide_casts.length === 0) return null;
  const totalSlots = audit.raidwide_casts.reduce(
    (n, c) => n + c.planned_slots.length, 0);
  const totalMissed = audit.raidwide_casts.reduce(
    (n, c) => n + c.missed_count, 0);
  const noPlan = audit.raidwide_casts.filter((c) => c.no_plan).length;

  return (
    <Section
      title="M-MIT audit"
      meta={`${audit.raidwide_casts.length} raidwides · ${
        totalSlots > 0
          ? `${totalSlots - totalMissed}/${totalSlots} mits fired`
          : 'no plans yet'
      }${noPlan > 0 ? ` · ${noPlan} without plan` : ''}`}
    >
      <table className="t t-tight">
        <thead>
          <tr>
            <th>Raidwide</th>
            <th>t+</th>
            <th>Plan</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {audit.raidwide_casts.map((c) => (
            <tr key={c.cast_ts}>
              <td className="mono small">
                {c.ability_id}
                {c.occurrence > 0 && (
                  <span className="muted">#{c.occurrence + 1}</span>
                )}
              </td>
              <td className="muted mono small">{(c.cast_ts / 1000).toFixed(0)}s</td>
              <td>
                {c.no_plan ? (
                  <span className="muted small">no plan</span>
                ) : c.planned_slots.length === 0 ? (
                  <span className="muted small">(empty plan)</span>
                ) : (
                  <span className="row-tight gap-2 wrap">
                    {c.planned_slots.map((s, i) => (
                      <span key={i} className="small"
                            style={{
                              color: s.fired ? 'var(--success)' : 'var(--danger)',
                            }}>
                        {s.fired ? '✓' : '✗'} {s.ability_id}
                        {s.expected_role && s.expected_role !== 'any' &&
                          ` (${s.expected_role})`}
                      </span>
                    ))}
                  </span>
                )}
              </td>
              <td>
                {c.no_plan ? (
                  <span className="muted">—</span>
                ) : c.missed_count === 0 ? (
                  <span className="pill pill-success">OK</span>
                ) : (
                  <span className="pill pill-danger">
                    {c.missed_count} missed
                  </span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Section>
  );
}

function FaultScores({ fightId, memberOf = {} }) {
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const refresh = async () => {
    try {
      const r = await fetch(`/api/fights/${fightId}/fault-scores`);
      setData(await r.json());
    } catch (e) {
      setErr(String(e));
    }
  };
  useEffect(() => { setData(null); refresh(); }, [fightId]);

  const compute = async () => {
    setBusy(true); setErr(null);
    try {
      const r = await fetch(`/api/fights/${fightId}/fault-scores/compute`,
                            { method: 'POST' });
      if (!r.ok) throw new Error(`compute ${r.status}`);
      await fetch(`/api/fights/${fightId}/fault-scores/disambiguate`,
                  { method: 'POST' });
      await refresh();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  if (!data) return null;
  const hasScores = data.players.length > 0;
  return (
    <Section
      title="M-FAULT scores"
      meta={(
        <span className="row-tight gap-2">
          <button onClick={compute} disabled={busy} className="btn-xs">
            {busy ? <><span className="spinner" /> computing</>
                  : hasScores ? 're-compute' : 'compute'}
          </button>
          {err && <span className="pill pill-danger">{err}</span>}
        </span>
      )}
    >
      {!hasScores ? (
        <div className="muted small">
          No fault scores yet — click compute. Needs fight_model labels
          (T-202/T-203) to classify root vs cascade.
        </div>
      ) : (
        <table className="t t-tight">
          <thead>
            <tr>
              <th>Player</th>
              <th className="num">Score</th>
              <th className="num">Root</th>
              <th className="num">Mit fail</th>
              <th className="num">Cascade</th>
              <th className="num">?</th>
            </tr>
          </thead>
          <tbody>
            {data.players.map((p) => {
              const r = p.reasons || {};
              const scoreColor = p.score >= 1.0 ? 'var(--danger)'
                : p.score >= 0.5 ? 'var(--warning)' : 'var(--fg-muted)';
              return (
                <tr key={p.player_id}>
                  <td>
                    <span className="text-strong">
                      {nameWithMember(r.name || `player ${p.player_id}`,
                                       memberOf[p.player_id])}
                    </span>
                    {r.job && <span className="muted"> ({r.job})</span>}
                  </td>
                  <td className="num text-strong"
                      style={{ color: scoreColor }}>
                    {p.score.toFixed(1)}
                  </td>
                  <td className="num">{r.root || 0}</td>
                  <td className="num"
                      style={{ color: r.mit_failure
                        ? 'var(--danger)' : 'var(--fg-muted)' }}>
                    {r.mit_failure || 0}
                  </td>
                  <td className="num muted">{r.cascade || 0}</td>
                  <td className="num faint">{r.unknown || 0}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </Section>
  );
}

function TimelineDiff({ fightId }) {
  const [diff, setDiff] = useState(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    setDiff(null);
    fetch(`/api/fights/${fightId}/timeline-diff`)
      .then((r) => r.json()).then(setDiff)
      .catch(() => setDiff({ phases: [], note: 'fetch failed' }));
  }, [fightId]);

  if (!diff) return null;
  if (diff.note && (!diff.phases || diff.phases.length === 0)) return null;
  const totalFired = diff.phases.reduce((s, p) => s + p.entries_fired, 0);
  const totalMissing = diff.phases.reduce((s, p) => s + p.entries_missing, 0);
  const totalAlternate = diff.phases.reduce(
    (s, p) => s + (p.entries_alternate || 0), 0);
  if (totalFired + totalMissing + totalAlternate === 0) return null;

  const driftColor = (ms) => {
    if (ms == null) return 'var(--fg-muted)';
    const abs = Math.abs(ms);
    if (abs <= 500) return 'var(--success)';
    if (abs <= 2000) return 'var(--warning)';
    return 'var(--danger)';
  };

  return (
    <div>
      <button onClick={() => setOpen(!open)}
              className="btn-ghost btn-sm"
              style={{ width: '100%', textAlign: 'left',
                       padding: 0, fontSize: 'var(--fs-md)' }}>
        <h5 className="row-tight gap-2 mb-0"
            style={{ textTransform: 'none', letterSpacing: 0,
                     color: 'var(--fg-strong)',
                     fontSize: 'var(--fs-md)' }}>
          {open ? '▾' : '▸'} Cactbot timeline diff
          <span className="muted small" style={{ fontWeight: 400 }}>
            {totalFired} fired · {totalMissing} missing
            {totalAlternate > 0 && ` · ${totalAlternate} alt`}
          </span>
        </h5>
      </button>
      {open && (
        <div className="stack" style={{ marginTop: 'var(--s-3)' }}>
          {diff.phases.map((p) => (
            <div key={p.phase_index}>
              <div className="row-tight gap-2 small muted"
                   style={{ marginBottom: 'var(--s-1)' }}>
                <span className="text-strong"
                      style={{ color: 'var(--fg)' }}>
                  {p.phase_label || `P${p.phase_index}`}
                </span>
                <span>· {p.entries_fired}/{p.entries_total} fired</span>
                {p.median_drift_ms != null && (
                  <span style={{ color: driftColor(p.median_drift_ms) }}>
                    · median drift {p.median_drift_ms >= 0 ? '+' : ''}
                    {(p.median_drift_ms / 1000).toFixed(1)}s
                  </span>
                )}
              </div>
              <table className="t t-tight">
                <thead>
                  <tr>
                    <th>Mechanic</th>
                    <th>Type</th>
                    <th className="num">Expected</th>
                    <th className="num">Actual</th>
                    <th className="num">Drift</th>
                  </tr>
                </thead>
                <tbody>
                  {p.entries.map((e, i) => (
                    <tr key={i}
                        style={!e.fired && !e.alternate_variant
                          ? { background: 'rgba(248,81,73,.08)' }
                          : e.alternate_variant
                          ? { color: 'var(--fg-muted)' }
                          : null}>
                      <td>
                        {e.cactbot_label || (
                          <span className="muted">
                            ability {e.ability_game_id}
                          </span>
                        )}
                      </td>
                      <td className="muted small">{e.type_label}</td>
                      <td className="num mono">
                        {e.expected_t_ms != null
                          ? `${(e.expected_t_ms / 1000).toFixed(1)}s`
                          : '—'}
                      </td>
                      <td className="num mono">
                        {e.fired
                          ? `${(e.actual_t_ms / 1000).toFixed(1)}s`
                          : e.alternate_variant
                            ? <span className="muted small">alt variant</span>
                            : <span style={{ color: 'var(--danger)' }}>did not fire</span>}
                      </td>
                      <td className="num mono"
                          style={{ color: driftColor(e.drift_ms),
                                   fontWeight: e.drift_ms != null
                                     && Math.abs(e.drift_ms) > 2000 ? 600 : 400 }}>
                        {e.drift_ms != null
                          ? `${e.drift_ms >= 0 ? '+' : ''}${(e.drift_ms / 1000).toFixed(1)}s`
                          : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
