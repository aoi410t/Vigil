import { useEffect, useState } from 'react';
import FieldStats from './FieldStats.jsx';
import ReportDetail from './ReportDetail.jsx';
import WatchedReports from './WatchedReports.jsx';
import { useMe } from './me.jsx';

export default function Reports() {
  const { me } = useMe();
  const isDev = me?.is_developer === true;
  const [reports, setReports] = useState([]);
  const [selected, setSelected] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    fetch('/api/reports')
      .then((r) => r.json())
      .then((rows) => {
        setReports(rows);
        if (rows.length === 1) setSelected(rows[0].code);
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, []);

  return (
    <section className="fade-in">
      <h1 className="mb-0">Reports</h1>
      <p className="muted text-sm">
        Ingested logs and the live watchlist. Open a report to see wipes,
        deaths, GCD drops, burst alignment, and the cactbot timeline diff.
      </p>

      <WatchedReports />
      {isDev && <FieldStats />}

      {error && (
        <div className="card" style={{ borderColor: 'var(--danger)' }}>
          <span className="pill pill-danger">error</span>{' '}
          <span className="text-sm">{error}</span>
        </div>
      )}

      {loading ? (
        <div className="empty"><span className="loading">Loading</span></div>
      ) : reports.length === 0 ? (
        <div className="empty">
          No reports ingested yet. Add one to the watchlist above and click
          <code> poll now</code>, or run{' '}
          <code>python -m jobs.poll_watched</code>.
        </div>
      ) : (
        <div className="row row-stack-mobile" style={{ alignItems: 'flex-start' }}>
          <div className="sidebar">
            <h5 style={{ marginBottom: 'var(--s-2)' }}>
              Ingested ({reports.length})
            </h5>
            <div style={{ maxHeight: 640, overflowY: 'auto', paddingRight: 4 }}>
              {reports.map((r) => (
                <button
                  key={r.code}
                  onClick={() => setSelected(r.code)}
                  className={`tile ${selected === r.code ? 'is-selected' : ''}`}
                >
                  <div className="tile-title mono small">{r.code}</div>
                  <div className="tile-meta">
                    enc {r.encounter_id} · {r.fight_count} pulls ·{' '}
                    <span style={{ color: 'var(--success)' }}>{r.kill_count}K</span>{' '}
                    <span className="muted">/</span>{' '}
                    <span style={{ color: 'var(--danger)' }}>{r.wipe_count}W</span>
                    {r.start_time && ` · ${new Date(r.start_time).toLocaleDateString()}`}
                  </div>
                </button>
              ))}
            </div>
          </div>
          <div className="grow">
            {selected ? (
              <ReportDetail
                code={selected}
                encounterId={
                  reports.find((r) => r.code === selected)?.encounter_id ?? null
                }
              />
            ) : (
              <div className="empty">Pick a report on the left.</div>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
