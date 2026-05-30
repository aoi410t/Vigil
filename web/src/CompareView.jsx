import { useEffect, useState } from 'react';

const TYPE_COLORS = {
  raidwide:   'var(--type-raidwide)',
  tankbuster: 'var(--type-tankbuster)',
  aoe_party:  'var(--type-aoe_party)',
  enrage:     'var(--type-enrage)',
  cosmetic:   'var(--type-cosmetic)',
  unknown:    'var(--type-unknown)',
};

export default function CompareView({ encounterId }) {
  const [dps, setDps] = useState(null);
  const [cart, setCart] = useState(null);
  const [prog, setProg] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    setDps(null); setCart(null); setProg(null); setError(null);
    Promise.all([
      fetch(`/api/encounters/${encounterId}/dps-check`).then((r) => r.json()),
      fetch(`/api/encounters/${encounterId}/cartography`).then((r) => r.json()),
      fetch(`/api/encounters/${encounterId}/prog-curve`).then((r) => r.json()),
    ])
      .then(([d, c, p]) => { setDps(d); setCart(c); setProg(p); })
      .catch((e) => setError(String(e)));
  }, [encounterId]);

  if (error) return <p className="text-sm" style={{ color: 'var(--danger)' }}>{error}</p>;
  if (!dps || !cart || !prog) return <div className="empty"><span className="loading">Loading</span></div>;

  return (
    <div className="stack">
      <Card title="Prog vs field">
        <ProgCurveView prog={prog} />
      </Card>
      <Card title="Phase DPS vs clearing groups">
        <DpsTable dps={dps} />
      </Card>
      <Card title="Failure cartography">
        <CartographyTable cart={cart} />
      </Card>
    </div>
  );
}

function Card({ title, children }) {
  return (
    <div className="card">
      <h3 style={{ marginBottom: 'var(--s-3)' }}>{title}</h3>
      {children}
    </div>
  );
}

function ProgCurveView({ prog }) {
  const ourBest = prog.our_sessions.reduce((acc, s) => {
    if (s.best_fight_percentage == null) return acc;
    return acc == null ? s.best_fight_percentage : Math.min(acc, s.best_fight_percentage);
  }, null);
  const manualBest = prog.manual_points.reduce((acc, p) => {
    if (p.fight_percentage == null) return acc;
    return acc == null ? p.fight_percentage : Math.min(acc, p.fight_percentage);
  }, null);

  if (prog.field_wipes_total === 0
      && prog.our_sessions.length === 0
      && prog.manual_points.length === 0) {
    return (
      <div className="muted text-sm">
        No prog data yet — backfill field wipes via{' '}
        <code>python -m jobs.backfill_field</code> and add prog points or
        watch reports of your own pulls.
      </div>
    );
  }

  return (
    <div>
      <div className="muted small" style={{ marginBottom: 'var(--s-2)' }}>
        {prog.field_wipes_total} field wipes ·{' '}
        {prog.our_sessions.length} of our sessions ·{' '}
        {prog.manual_points.length} manual points
      </div>
      <FieldHistogram
        buckets={prog.field_buckets}
        ourBest={ourBest}
        manualBest={manualBest}
      />
      {prog.our_sessions.length > 0 && (
        <OurSessionList sessions={prog.our_sessions} />
      )}
    </div>
  );
}

function FieldHistogram({ buckets, ourBest, manualBest }) {
  if (!buckets.length) {
    return <div className="muted small">No field wipes ingested.</div>;
  }
  const maxN = Math.max(...buckets.map((b) => b.wipe_count));
  return (
    <table className="t t-tight">
      <thead>
        <tr>
          <th>% remaining</th>
          <th className="num">Wipes</th>
          <th>Distribution</th>
        </tr>
      </thead>
      <tbody>
        {buckets.map((b) => {
          const widthPct = (b.wipe_count / maxN) * 100;
          const inBucket =
            (ourBest != null
              && ourBest >= b.fight_percentage_lo
              && ourBest < b.fight_percentage_hi)
            || (manualBest != null
              && manualBest >= b.fight_percentage_lo
              && manualBest < b.fight_percentage_hi);
          return (
            <tr key={b.fight_percentage_lo}
                style={inBucket ? { background: 'var(--bg-selected)' } : null}>
              <td>
                {b.fight_percentage_lo}–{b.fight_percentage_hi}%
                {inBucket && (
                  <span className="text-strong"
                        style={{ color: 'var(--accent)' }}> ← you</span>
                )}
              </td>
              <td className="num">{b.wipe_count}</td>
              <td style={{ width: 240 }}>
                <div className="bar-track">
                  <div className="bar-fill"
                       style={{ width: `${widthPct}%`,
                                background: 'var(--danger)' }} />
                </div>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function OurSessionList({ sessions }) {
  return (
    <div style={{ marginTop: 'var(--s-3)' }}>
      <h5 style={{ marginBottom: 'var(--s-2)' }}>Our sessions</h5>
      <div className="stack-sm">
        {sessions.map((s) => (
          <div key={s.report_code} className="row-tight gap-2 small">
            <span className="mono text-strong">{s.report_code}</span>
            <span className="muted">·</span>
            <span>{s.pulls} pulls</span>
            {s.kills > 0 && (
              <span style={{ color: 'var(--success)' }}>· {s.kills} kill</span>
            )}
            {s.best_phase != null && (
              <span className="muted">· best P{s.best_phase}</span>
            )}
            {s.best_fight_percentage != null && (
              <span className="muted">
                @ {s.best_fight_percentage.toFixed(1)}%
              </span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function DpsTable({ dps }) {
  if (!dps.phases.length) {
    return (
      <div className="muted text-sm">{dps.note || 'No DPS distribution yet.'}</div>
    );
  }
  return (
    <div>
      <div className="muted small" style={{ marginBottom: 'var(--s-2)' }}>
        Aggregated from{' '}
        <span className="text-strong">{dps.kills_aggregated}</span>{' '}
        ingested kills
      </div>
      <table className="t t-tight">
        <thead>
          <tr>
            <th>Phase</th>
            <th className="num">p25</th>
            <th className="num">Median</th>
            <th className="num">p75</th>
            <th className="num">Spread</th>
            <th className="num">n</th>
          </tr>
        </thead>
        <tbody>
          {dps.phases.map((p) => {
            const r = p.raid_dps;
            const spread = r.p50 ? ((r.p75 - r.p25) / r.p50 * 100).toFixed(1) : '—';
            return (
              <tr key={p.phase_index}>
                <td className="mono">P{p.phase_index}</td>
                <td className="num">{(r.p25 / 1000).toFixed(0)}k</td>
                <td className="num text-strong"
                    style={{ color: 'var(--fg-strong)' }}>
                  {(r.p50 / 1000).toFixed(0)}k
                </td>
                <td className="num">{(r.p75 / 1000).toFixed(0)}k</td>
                <td className="num muted">±{spread}%</td>
                <td className="num muted">{r.n}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function CartographyTable({ cart }) {
  if (cart.total_deaths === 0) {
    return (
      <div className="muted text-sm">
        No deaths recorded across {cart.total_fights} ingested fights
        ({cart.total_kills}K/{cart.total_wipes}W).
      </div>
    );
  }
  const top = cart.buckets.slice(0, 12);
  const maxDeaths = Math.max(...top.map((b) => b.deaths));
  return (
    <div>
      <div className="muted small" style={{ marginBottom: 'var(--s-2)' }}>
        {cart.total_deaths} deaths across {cart.total_fights} fights
        ({cart.total_kills}K / {cart.total_wipes}W)
      </div>
      <table className="t t-tight">
        <thead>
          <tr>
            <th>Ability</th>
            <th>Phase</th>
            <th>Label</th>
            <th className="num">Deaths</th>
            <th>Distribution</th>
          </tr>
        </thead>
        <tbody>
          {top.map((b, i) => {
            const widthPct = (b.deaths / maxDeaths) * 100;
            const labelColor = TYPE_COLORS[b.fight_model_label] || 'var(--fg-muted)';
            return (
              <tr key={i}>
                <td>
                  {b.non_attributable ? (
                    <span className="muted">(non-attributable)</span>
                  ) : (
                    <>
                      <span className="mono">{b.ability_game_id}</span>
                      {b.ability_name && (
                        <span className="muted"> · {b.ability_name}</span>
                      )}
                    </>
                  )}
                </td>
                <td className="muted">
                  {b.fight_model_phase != null ? `P${b.fight_model_phase}` : '—'}
                </td>
                <td>
                  {b.fight_model_label ? (
                    <span className="row-tight gap-1">
                      <span className="dot" style={{ background: labelColor }} />
                      <span>{b.fight_model_label}</span>
                    </span>
                  ) : <span className="faint">—</span>}
                </td>
                <td className="num">{b.deaths}</td>
                <td style={{ width: 200 }}>
                  <div className="bar-track">
                    <div className="bar-fill"
                         style={{ width: `${widthPct}%`,
                                  background: 'var(--danger)' }} />
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {cart.buckets.length > 12 && (
        <div className="faint small" style={{ marginTop: 'var(--s-2)' }}>
          …{cart.buckets.length - 12} more
        </div>
      )}
    </div>
  );
}
