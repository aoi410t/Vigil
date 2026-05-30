import { useEffect, useState } from 'react';

export default function FFLogsAuthStatus() {
  const [status, setStatus] = useState(null);
  const [busy, setBusy] = useState(false);

  const refresh = () => {
    fetch('/api/fflogs-auth/status')
      .then((r) => r.json())
      .then(setStatus)
      .catch(() => setStatus({ connected: false, error: true }));
  };

  useEffect(() => {
    refresh();
    if (window.location.hash === '#fflogs-connected') {
      window.history.replaceState(null, '', '#home');
    }
  }, []);

  const disconnect = async () => {
    if (!confirm('Disconnect FFLogs Gold account?')) return;
    setBusy(true);
    try {
      await fetch('/api/fflogs-auth/connection', { method: 'DELETE' });
      refresh();
    } finally {
      setBusy(false);
    }
  };

  if (status === null) {
    return <span className="muted small loading">FFLogs</span>;
  }

  if (!status.connected) {
    return (
      <a
        href="/auth/fflogs/login"
        className="pill pill-accent"
        style={{ textDecoration: 'none' }}
        title="Connect your Gold-tier FFLogs account to access archived + private reports"
      >
        Connect FFLogs Gold
      </a>
    );
  }

  return (
    <span className="row-tight gap-2"
          title={`Scope: ${status.scope || '(default)'}\nConnected: ${status.connected_at || 'unknown'}`}>
      <span className="pill pill-success">FFLogs Gold</span>
      <button onClick={disconnect} disabled={busy}
              className="btn-ghost btn-xs">
        disconnect
      </button>
    </span>
  );
}
