import { useEffect, useState } from 'react';

export default function WatchedReports() {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const refresh = async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await fetch('/api/watched-reports');
      if (!r.ok) throw new Error(`GET ${r.status}`);
      setRows(await r.json());
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { refresh(); }, []);

  return (
    <div className="card">
      <div className="row" style={{ alignItems: 'baseline',
                                    justifyContent: 'space-between',
                                    marginBottom: 'var(--s-2)' }}>
        <h3 className="mb-0">Watchlist</h3>
        <span className="muted small">
          {rows.length} report{rows.length === 1 ? '' : 's'} watched
        </span>
      </div>
      <p className="muted text-sm">
        Paste an FFLogs URL or code. <code>python -m jobs.poll_watched</code>{' '}
        ingests new fights and events on its next pass; <em>poll now</em>{' '}
        forces an immediate ingest.
      </p>
      <NewWatchForm onCreated={refresh} />
      {error && (
        <p className="text-sm" style={{ color: 'var(--danger)' }}>{error}</p>
      )}
      {loading ? (
        <div className="muted small loading">Loading</div>
      ) : rows.length === 0 ? (
        <div className="muted text-sm">No reports being watched.</div>
      ) : (
        <div className="stack-sm">
          {rows.map((r) => (
            <WatchRow key={r.code} row={r} onChange={refresh} />
          ))}
        </div>
      )}
    </div>
  );
}

function NewWatchForm({ onCreated }) {
  const [input, setInput] = useState('');
  const [label, setLabel] = useState('');
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    if (!input.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      const r = await fetch('/api/watched-reports', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code_or_url: input.trim(), label: label.trim() || null }),
      });
      if (!r.ok) {
        const b = await r.json().catch(() => ({}));
        throw new Error(b.detail || `POST ${r.status}`);
      }
      setInput('');
      setLabel('');
      onCreated();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className="row-tight gap-2 wrap"
          style={{ marginBottom: 'var(--s-3)' }}>
      <input
        placeholder="report code or fflogs URL"
        value={input}
        onChange={(e) => setInput(e.target.value)}
        style={{ flex: 1, minWidth: 240 }}
        required
      />
      <input
        placeholder="label (optional)"
        value={label}
        onChange={(e) => setLabel(e.target.value)}
        style={{ width: 200 }}
      />
      <button type="submit" disabled={busy} className="btn-primary">Watch</button>
      {err && <span className="text-sm" style={{ color: 'var(--danger)' }}>{err}</span>}
    </form>
  );
}

function WatchRow({ row, onChange }) {
  const [pollBusy, setPollBusy] = useState(false);
  const [pollResult, setPollResult] = useState(null);
  const [showReport, setShowReport] = useState(false);

  const toggle = async () => {
    await fetch(`/api/watched-reports/${row.code}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ active: !row.active }),
    });
    onChange();
  };
  const remove = async () => {
    if (!confirm(`Stop watching ${row.code}?`)) return;
    await fetch(`/api/watched-reports/${row.code}`, { method: 'DELETE' });
    onChange();
  };
  const pollNow = async () => {
    setPollBusy(true);
    setPollResult(null);
    try {
      const r = await fetch(`/api/watched-reports/${row.code}/poll`, {
        method: 'POST',
      });
      const body = await r.json();
      setPollResult(body);
    } catch (e) {
      setPollResult({ status: 'error', error: String(e) });
    } finally {
      setPollBusy(false);
      onChange();
    }
  };

  const summarize = (r) => {
    if (!r) return null;
    if (r.status === 'ok') {
      const meta = r.meta || {};
      const ev = r.events || {};
      return `ingested · fights+${meta.new_fights ?? 0} · events+${ev.events_inserted ?? 0}`;
    }
    if (r.status === 'skipped_complete') return 'already complete · no work';
    if (r.status === 'error') return `error: ${r.error}`;
    return r.status;
  };

  return (
    <div className="tile" style={{
      cursor: 'default',
      opacity: row.active ? 1 : 0.6,
    }}>
      <div className="row" style={{ alignItems: 'baseline',
                                    justifyContent: 'space-between',
                                    gap: 'var(--s-3)' }}>
        <div className="grow" style={{ minWidth: 0 }}>
          <div className="row-tight gap-2">
            <span className="mono text-strong">{row.code}</span>
            {row.label && <span className="muted">· {row.label}</span>}
            {!row.active && <span className="pill">paused</span>}
          </div>
          <div className="small muted" style={{ marginTop: 2 }}>
            {row.last_polled_at
              ? `last polled ${new Date(row.last_polled_at).toLocaleString()}`
              : 'never polled'}
            {row.last_error && (
              <span style={{ color: 'var(--danger)' }}> · error: {row.last_error}</span>
            )}
            {pollResult && (
              <span style={{
                color: pollResult.status === 'error'
                  ? 'var(--danger)' : 'var(--success)',
                marginLeft: 8,
              }}>
                · {summarize(pollResult)}
              </span>
            )}
          </div>
        </div>
        <div className="row-tight gap-1">
          <button onClick={pollNow} disabled={pollBusy}
                  className="btn-sm" title="Ingest this report immediately">
            {pollBusy ? <><span className="spinner" /> polling</> : 'poll now'}
          </button>
          <button onClick={() => setShowReport(true)} className="btn-sm"
                  title="Generate Discord-pasteable session summary">
            report
          </button>
          <button onClick={toggle} className="btn-sm">
            {row.active ? 'pause' : 'resume'}
          </button>
          <button onClick={remove} className="btn-sm"
                  style={{ color: 'var(--danger)' }}>
            remove
          </button>
        </div>
      </div>
      {showReport && <SessionReportModal code={row.code}
                                          onClose={() => setShowReport(false)} />}
    </div>
  );
}

function SessionReportModal({ code, onClose }) {
  const [body, setBody] = useState(null);
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    setBody(null);
    fetch(`/api/reports/${code}/session-report`)
      .then((r) => r.json())
      .then(setBody)
      .catch((e) => setBody({ markdown: '', note: String(e) }));
  }, [code]);

  const copy = async () => {
    if (!body?.markdown) return;
    try {
      await navigator.clipboard.writeText(body.markdown);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch { /* ignore */ }
  };

  return (
    <div className="modal-overlay" onClick={(e) => {
      if (e.target.classList.contains('modal-overlay')) onClose();
    }}>
      <div className="modal" style={{ maxWidth: 720 }}>
        <div className="modal-header">
          <h3 className="mb-0">
            <span className="muted small">Session report</span>{' '}
            <code>{code}</code>
          </h3>
          <button onClick={onClose} className="btn-ghost btn-sm">✕</button>
        </div>
        <div className="modal-body">
          {!body ? (
            <p className="muted loading">Loading</p>
          ) : body.note ? (
            <p className="muted">{body.note}</p>
          ) : (
            <pre style={{
              whiteSpace: 'pre-wrap',
              background: 'var(--bg-input)',
              padding: 'var(--s-3)',
              borderRadius: 'var(--radius-sm)',
              fontSize: 'var(--fs-sm)',
              margin: 0,
              border: '1px solid var(--border)',
            }}>{body.markdown}</pre>
          )}
        </div>
        <div className="modal-footer">
          <button onClick={copy} disabled={!body?.markdown}
                  className="btn-primary">
            {copied ? 'copied ✓' : 'copy markdown'}
          </button>
          <button onClick={onClose}>close</button>
        </div>
      </div>
    </div>
  );
}
