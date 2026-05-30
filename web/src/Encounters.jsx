import { useEffect, useState } from 'react';
import CompareView from './CompareView.jsx';
import FightMap from './FightMap.jsx';
import StratEditor from './StratEditor.jsx';
import { useMe } from './me.jsx';

// v1.17.0: cloned encounters share one canonical label (DSR 1065 is
// merged into 1076 server-side by canonical_encounter_id() — only the
// canonical ID surfaces in the API, so 1065 should never appear here).
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

const SUBTABS = [
  ['fightmap', 'Fight map'],
  ['compare', 'Compare'],
  ['strat', 'Strat'],
];

export default function Encounters() {
  const { me } = useMe();
  const isDev = me?.is_developer === true;
  const [encounters, setEncounters] = useState([]);
  const [selected, setSelected] = useState(null);
  const [tab, setTab] = useState('fightmap');
  const [error, setError] = useState(null);
  const [showAll, setShowAll] = useState(false);

  useEffect(() => {
    fetch('/api/encounters')
      .then((r) => r.json())
      .then((rows) => {
        setEncounters(rows);
        const meaningful = rows.find(
          (r) => r.fight_model_abilities > 0 || r.kills_with_events >= 3,
        );
        if (meaningful) setSelected(meaningful.encounter_id);
      })
      .catch((e) => setError(String(e)));
  }, []);

  const visible = showAll
    ? encounters
    : encounters.filter(
        (r) => r.fight_model_abilities > 0 || r.kills_with_events >= 3,
      );

  return (
    <section className="fade-in">
      <h1 className="mb-0">Encounters</h1>
      <p className="muted text-sm">
        Boss-side fight model + field comparison per encounter. Persist a fight
        model via{' '}
        <code>POST /api/encounters/{'{id}'}/fight-model/persist</code>{' '}
        and classify with{' '}
        <code>/classify</code>.
      </p>
      {error && (
        <p className="text-sm" style={{ color: 'var(--danger)' }}>{error}</p>
      )}

      <div className="row row-stack-mobile" style={{ alignItems: 'flex-start' }}>
        <div className="sidebar">
          {isDev && (
            <label className="row-tight gap-2 small muted"
                   style={{ marginBottom: 'var(--s-2)' }}>
              <input type="checkbox" checked={showAll}
                     onChange={(e) => setShowAll(e.target.checked)} />
              show all encounters
            </label>
          )}
          <div style={{ maxHeight: 640, overflowY: 'auto', paddingRight: 4 }}>
            {visible.length === 0 ? (
              <div className="empty small">
                Nothing with a fight model or ≥3 kill events yet. Backfill via{' '}
                <code>python -m jobs.backfill_field</code>.
              </div>
            ) : (
              visible.map((r) => {
                const name = ENCOUNTER_NAMES[r.encounter_id]
                  || `enc ${r.encounter_id}`;
                const hasModel = r.fight_model_abilities > 0;
                return (
                  <button
                    key={r.encounter_id}
                    onClick={() => setSelected(r.encounter_id)}
                    className={`tile ${selected === r.encounter_id ? 'is-selected' : ''}`}
                  >
                    <div className="row-tight gap-2">
                      <span className="tile-title">{name}</span>
                      <span className="muted small">· {r.encounter_id}</span>
                      {hasModel && (
                        <span className="pill pill-accent"
                              style={{ marginLeft: 'auto' }}>
                          model
                        </span>
                      )}
                    </div>
                    <div className="tile-meta">
                      <span style={{ color: 'var(--success)' }}>{r.kills}K</span>{' '}
                      / <span style={{ color: 'var(--danger)' }}>{r.wipes}W</span>{' '}
                      · {r.kills_with_events} kills w/ events
                    </div>
                  </button>
                );
              })
            )}
          </div>
        </div>

        <div className="grow">
          {selected ? (
            <>
              <div className="subtabs">
                {SUBTABS.map(([id, label]) => (
                  <button
                    key={id}
                    onClick={() => setTab(id)}
                    className={`subtab ${tab === id ? 'is-active' : ''}`}
                  >
                    {label}
                  </button>
                ))}
              </div>
              {tab === 'fightmap' && <FightMap encounterId={selected} />}
              {tab === 'compare' && <CompareView encounterId={selected} />}
              {tab === 'strat' && <StratEditor encounterId={selected} />}
            </>
          ) : (
            <div className="empty">Pick an encounter on the left.</div>
          )}
        </div>
      </div>
    </section>
  );
}
