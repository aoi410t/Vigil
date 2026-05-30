import { useEffect, useState } from 'react';

const LABEL_COLOR = {
  raidwide:    'var(--type-raidwide)',
  tankbuster:  'var(--type-tankbuster)',
  aoe_party:   'var(--type-aoe_party)',
  enrage:      'var(--type-enrage)',
  damage_down: 'var(--fg-muted)',
  cosmetic:    'var(--type-cosmetic)',
  unknown:     'var(--type-unknown)',
};

// Subtle alternating row tints so phases visually separate without competing
// with the colored dots. All low-saturation surfaces tuned for dark mode.
const PHASE_TINTS = [
  'rgba(88, 166, 255, 0.06)',
  'rgba(192, 132, 252, 0.06)',
  'rgba(248, 81, 73, 0.06)',
  'rgba(63, 185, 80, 0.06)',
  'rgba(210, 153, 34, 0.06)',
  'rgba(255, 166, 87, 0.06)',
];

export default function FightMap({ encounterId }) {
  const [model, setModel] = useState(null);
  const [consensus, setConsensus] = useState(null);
  const [abilityNames, setAbilityNames] = useState({});
  const [error, setError] = useState(null);

  useEffect(() => {
    setModel(null);
    setConsensus(null);
    setError(null);
    Promise.all([
      fetch(`/api/encounters/${encounterId}/fight-model`).then((r) => r.json()),
      fetch(`/api/encounters/${encounterId}/consensus`).then((r) => r.json()),
    ])
      .then(([m, c]) => {
        setModel(m);
        setConsensus(c);
      })
      .catch((e) => setError(String(e)));
  }, [encounterId]);

  useEffect(() => {
    if (!model || !model.phases.length) return;
    fetch('/api/abilities/labels?limit=5000')
      .then((r) => r.json())
      .then((rows) => {
        const map = {};
        for (const r of rows) {
          if (r.name) map[r.ability_game_id] = r.name;
        }
        setAbilityNames(map);
      })
      .catch(() => {});
  }, [model]);

  if (error) {
    return <p className="text-sm" style={{ color: 'var(--danger)' }}>{error}</p>;
  }
  if (!model) {
    return <div className="empty"><span className="loading">Loading</span></div>;
  }

  if (model.phases.length === 0) {
    return (
      <div className="card">
        <p className="muted text-sm">
          No persisted fight model for encounter {encounterId} yet. Run{' '}
          <code>POST /api/encounters/{encounterId}/fight-model/persist</code>{' '}
          after you have ≥3 ingested kills with events.
        </p>
        {consensus && consensus.note && (
          <p className="muted small mb-0">Consensus says: {consensus.note}</p>
        )}
      </div>
    );
  }

  const totalAbilities = model.phases.reduce(
    (n, p) => n + p.abilities.length, 0);

  return (
    <div className="card">
      <div className="row" style={{ alignItems: 'baseline',
                                    justifyContent: 'space-between',
                                    marginBottom: 'var(--s-3)' }}>
        <div>
          <span className="text-strong">{model.phases.length} phases</span>
          <span className="muted"> · {totalAbilities} canonical abilities</span>
          {consensus && consensus.total_pulls && (
            <span className="muted">
              {' '}· aggregated from{' '}
              <span className="text-strong">{consensus.total_pulls}</span> pulls
            </span>
          )}
        </div>
      </div>

      <div className="stack-sm">
        {model.phases.map((p, pi) => (
          <PhaseRow
            key={p.phase}
            phase={p}
            tint={PHASE_TINTS[pi % PHASE_TINTS.length]}
            abilityNames={abilityNames}
          />
        ))}
      </div>

      <Legend />
    </div>
  );
}

function PhaseRow({ phase, tint, abilityNames }) {
  const last = phase.abilities[phase.abilities.length - 1];
  const phaseDurMs = last ? last.relative_t_ms : 0;
  const pxPerSec = 6;
  const width = Math.max(200, Math.min(900, (phaseDurMs / 1000) * pxPerSec));

  return (
    <div className="row-tight gap-3" style={{ alignItems: 'flex-start' }}>
      <div className="mono small muted" style={{
        minWidth: 64, textAlign: 'right', paddingTop: 18,
      }}>
        {phase.abilities[0]?.cactbot_phase_label || `P${phase.phase}`}
      </div>
      <div style={{
        position: 'relative',
        background: tint,
        height: 56,
        width,
        border: '1px solid var(--border)',
        borderRadius: 'var(--radius-sm)',
        flexShrink: 0,
      }}>
        {phase.abilities.map((a) => {
          const leftPct = phaseDurMs > 0
            ? (a.relative_t_ms / phaseDurMs) * 100
            : 0;
          const color = LABEL_COLOR[a.type_label] || LABEL_COLOR.unknown;
          const dotSize = a.type_label === 'enrage' ? 14
            : a.type_label === 'raidwide' ? 11
            : a.type_label === 'tankbuster' ? 10
            : 7;
          const tooltipName = a.cactbot_label
            || abilityNames[a.ability_game_id]
            || `ability ${a.ability_game_id}`;
          const t = (a.relative_t_ms / 1000).toFixed(1);
          const conf = a.confidence ? Math.round(a.confidence * 100) : 0;
          const driftParts = [];
          if (a.cactbot_expected_t_ms != null) {
            const driftMs = (a.relative_t_ms || 0) - a.cactbot_expected_t_ms;
            const dStr = `${driftMs >= 0 ? '+' : ''}${(driftMs / 1000).toFixed(1)}s`;
            driftParts.push(`expected ${(a.cactbot_expected_t_ms / 1000).toFixed(1)}s (drift ${dStr})`);
          }
          return (
            <span
              key={a.seq}
              title={`${tooltipName} (${a.ability_game_id}) · ${a.type_label || 'unknown'} · t+${t}s · ${conf}% recurrence${driftParts.length ? ' · ' + driftParts.join(' · ') : ''}`}
              style={{
                position: 'absolute',
                left: `calc(${leftPct}% - ${dotSize / 2}px)`,
                top: 22 - dotSize / 2 + (a.type_label === 'tankbuster' ? 12 : 0),
                width: dotSize, height: dotSize,
                borderRadius: '50%',
                background: color,
                border: '1px solid rgba(0,0,0,.5)',
                boxShadow: '0 0 0 1px rgba(255,255,255,.05)',
                cursor: 'help',
              }}
            />
          );
        })}
        <div className="mono small muted" style={{
          position: 'absolute', bottom: 2, right: 6,
        }}>
          {(phaseDurMs / 1000).toFixed(0)}s
        </div>
      </div>
    </div>
  );
}

function Legend() {
  return (
    <div className="row-tight gap-3 wrap small muted"
         style={{ marginTop: 'var(--s-3)' }}>
      {Object.entries(LABEL_COLOR).map(([label, color]) => (
        <span key={label} className="row-tight gap-1">
          <span className="dot" style={{ background: color,
                                          border: '1px solid rgba(0,0,0,.5)' }} />
          {label}
        </span>
      ))}
    </div>
  );
}
