import { useEffect, useState } from 'react';

// v1.17.0: cloned encounters dedupe to canonical (DSR = 1076 only).
const ENCOUNTER_NAMES = {
  1079: 'FRU',
  1068: 'TOP',
  1076: 'DSR',
  101: 'M9S',
  102: 'M10S',
  103: 'M11S',
  104: 'M12S',
  105: 'M12S-P2',
};

export default function FieldStats() {
  const [stats, setStats] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    fetch('/api/field-stats')
      .then((r) => r.json())
      .then(setStats)
      .catch((e) => setError(String(e)));
  }, []);

  if (error) {
    return (
      <p className="text-sm" style={{ color: 'var(--danger)' }}>{error}</p>
    );
  }
  if (!stats) return null;

  return (
    <div className="card">
      <div className="row" style={{ alignItems: 'baseline',
                                    justifyContent: 'space-between',
                                    marginBottom: 'var(--s-2)' }}>
        <h3 className="mb-0">Field data</h3>
        <span className="muted small">
          {stats.reduce((s, e) => s + e.kills_with_events, 0)} kills w/ events total
        </span>
      </div>
      <p className="muted text-sm">
        Backfilled public reports per encounter. Run{' '}
        <code>python -m jobs.backfill_field</code> to top up.
      </p>
      <table className="t t-tight">
        <thead>
          <tr>
            <th>Encounter</th>
            <th className="num">Reports</th>
            <th className="num">Kills w/ events</th>
          </tr>
        </thead>
        <tbody>
          {stats.map((s) => (
            <tr key={s.encounter_id}>
              <td>
                <span className="text-strong">
                  {ENCOUNTER_NAMES[s.encounter_id] || `enc ${s.encounter_id}`}
                </span>
                <span className="muted small"> ({s.encounter_id})</span>
              </td>
              <td className="num">{s.reports_ingested}</td>
              <td className="num">
                <span style={{
                  color: s.kills_with_events >= 3
                    ? 'var(--success)'
                    : 'var(--fg-muted)',
                  fontWeight: s.kills_with_events >= 3 ? 600 : 400,
                }}>
                  {s.kills_with_events}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
