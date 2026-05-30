import { useEffect, useState } from 'react';
import Abilities from './Abilities.jsx';
import Encounters from './Encounters.jsx';
import FFLogsAuthStatus from './FFLogsAuthStatus.jsx';
import Home from './Home.jsx';
import Members from './Members.jsx';
import Reports from './Reports.jsx';
import StaticSwitcher from './StaticSwitcher.jsx';
import { useMe } from './me.jsx';

// `dev: true` = tab only renders for developers. Non-dev users never see it
// in the nav nor can hash-route to it (the router falls back to Home).
const TABS = [
  { id: 'home', label: 'Home' },
  { id: 'reports', label: 'Reports' },
  { id: 'encounters', label: 'Encounters' },
  { id: 'roster', label: 'Roster' },
  { id: 'abilities', label: 'Abilities', dev: true },
];

export default function App() {
  const { me, actual_is_developer, view_as_user, setViewAsUser } = useMe();
  const isDev = me?.is_developer === true;
  const visibleTabs = TABS.filter((t) => !t.dev || isDev);

  const [tab, setTab] = useState(() => {
    const hash = window.location.hash.replace('#', '');
    return TABS.some((t) => t.id === hash) ? hash : 'home';
  });
  const [health, setHealth] = useState(null);
  const [error, setError] = useState(null);
  const [staticKey, setStaticKey] = useState(0);

  useEffect(() => {
    fetch('/healthz')
      .then((r) => r.json())
      .then(setHealth)
      .catch((e) => setError(String(e)));
  }, []);

  useEffect(() => { window.location.hash = tab; }, [tab]);

  // If a non-dev user lands on a dev-only tab (via stale hash, etc.),
  // bounce them home.
  useEffect(() => {
    if (!isDev && TABS.find((t) => t.id === tab)?.dev) {
      setTab('home');
    }
  }, [isDev, tab]);

  return (
    <>
      <header className="header">
        <div className="header-brand">
          <span className="brand-mark">V</span>
          <span>Vigil</span>
          <span className="muted small" style={{ marginLeft: 8 }}>
            FFLogs progression tracker
          </span>
          {isDev && (
            <span className="pill pill-warning" style={{ marginLeft: 8 }}>
              dev mode
            </span>
          )}
          {actual_is_developer && view_as_user && (
            <span className="pill pill-accent" style={{ marginLeft: 8 }}
                  title="You are a dev seeing the consumer Home for testing">
              viewing as user
            </span>
          )}
        </div>
        <div className="header-actions">
          {actual_is_developer && (
            <button onClick={() => setViewAsUser(!view_as_user)}
                    className="btn-ghost btn-xs"
                    title={view_as_user
                      ? "Switch back to dev view"
                      : "Render consumer Home as a non-dev user would see it"}>
              {view_as_user ? '← back to dev view' : 'view as user'}
            </button>
          )}
          <StaticSwitcher onStaticChange={() => setStaticKey((k) => k + 1)} />
          <FFLogsAuthStatus />
          <span className="row-tight small muted">
            <span className={`status-dot ${error ? 'is-danger' : ''}`} />
            {health
              ? `v${health.version}`
              : error
              ? 'API down'
              : <span className="loading">connecting</span>}
          </span>
        </div>
      </header>

      <main className="app-main fade-in">
        <nav className="tabs">
          {visibleTabs.map((t) => (
            <button
              key={t.id}
              className={`tab ${tab === t.id ? 'is-active' : ''}`}
              onClick={() => setTab(t.id)}
            >
              {t.label}
            </button>
          ))}
        </nav>

        {tab === 'home' && <Home key={staticKey} />}
        {tab === 'reports' && <Reports key={staticKey} />}
        {tab === 'encounters' && <Encounters key={staticKey} />}
        {tab === 'abilities' && isDev && <Abilities key={staticKey} />}
        {tab === 'roster' && <Members key={staticKey} />}
      </main>
    </>
  );
}
