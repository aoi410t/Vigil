import { useEffect, useState } from 'react';

const LABELS = [
  'raid_buff',
  'personal_buff',
  'mit_party',
  'mit_boss_debuff',
  'mit_self',
  'damage_down',
  'ignore',
  'unknown',
];
const BULK_TARGETS = LABELS.filter((l) => l !== 'unknown');
const KINDS = ['action', 'status', 'unknown'];
const XIVAPI_BASE = 'https://xivapi.com';

const SUBTABS = [
  ['review', 'Review queue'],
  ['all', 'All labels'],
];

export default function Abilities() {
  const [tab, setTab] = useState('review');
  return (
    <section className="fade-in">
      <h1 className="mb-0">Abilities</h1>
      <p className="muted text-sm">
        XIVAPI metadata + rule-based labels (T-108). The review queue is
        anything the classifier wasn't confident about, plus abilities with no
        label yet. Confirming a label promotes it to{' '}
        <code>source=user</code> so re-runs of the classifier won't overwrite it.
      </p>
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
      {tab === 'review' ? <ReviewQueue /> : <AllLabels />}
    </section>
  );
}

function ReviewQueue() {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [kindFilter, setKindFilter] = useState('');
  const [currentLabelFilter, setCurrentLabelFilter] = useState('__any__');
  const [selected, setSelected] = useState(new Set());
  const [bulkLabel, setBulkLabel] = useState('ignore');
  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkResult, setBulkResult] = useState(null);

  const refresh = async () => {
    setLoading(true);
    setError(null);
    setSelected(new Set());
    try {
      const params = new URLSearchParams({ limit: '100' });
      if (kindFilter) params.set('kind', kindFilter);
      if (currentLabelFilter !== '__any__') {
        params.set('current_label', currentLabelFilter);
      }
      const r = await fetch(`/api/abilities/review-queue?${params.toString()}`);
      if (!r.ok) throw new Error(`GET ${r.status}`);
      setRows(await r.json());
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { refresh(); /* eslint-disable-next-line */ },
            [kindFilter, currentLabelFilter]);

  const toggle = (id) => {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setSelected(next);
  };

  const selectAll = () => {
    if (selected.size === rows.length) setSelected(new Set());
    else setSelected(new Set(rows.map((r) => r.ability_game_id)));
  };

  const applyBulk = async () => {
    if (selected.size === 0) return;
    setBulkBusy(true);
    setBulkResult(null);
    try {
      const r = await fetch('/api/abilities/labels/bulk', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ability_ids: Array.from(selected),
          label: bulkLabel,
        }),
      });
      const body = await r.json();
      if (!r.ok) throw new Error(body.detail || `PATCH ${r.status}`);
      setBulkResult({ ok: true,
                      msg: `Updated ${body.updated} as ${bulkLabel}.` });
      await refresh();
    } catch (e) {
      setBulkResult({ ok: false, msg: e.message || String(e) });
    } finally {
      setBulkBusy(false);
    }
  };

  const markUnknownKindAsIgnore = async () => {
    setBulkBusy(true);
    setBulkResult(null);
    try {
      const r = await fetch(
        '/api/abilities/review-queue?kind=unknown&limit=500'
      );
      const list = await r.json();
      const ids = list.map((x) => x.ability_game_id);
      if (ids.length === 0) {
        setBulkResult({ ok: true, msg: 'No unknown-kind abilities in the queue.' });
        setBulkBusy(false);
        return;
      }
      const r2 = await fetch('/api/abilities/labels/bulk', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ability_ids: ids, label: 'ignore' }),
      });
      const body = await r2.json();
      if (!r2.ok) throw new Error(body.detail || `PATCH ${r2.status}`);
      setBulkResult({ ok: true,
                      msg: `Marked ${body.updated} unknown-kind as ignore.` });
      await refresh();
    } catch (e) {
      setBulkResult({ ok: false, msg: e.message || String(e) });
    } finally {
      setBulkBusy(false);
    }
  };

  return (
    <div className="stack">
      {/* filter bar */}
      <div className="card card-tight row-tight gap-3 wrap"
           style={{ background: 'var(--bg-elevated)' }}>
        <label className="row-tight gap-1 small muted">
          kind
          <select value={kindFilter}
                  onChange={(e) => setKindFilter(e.target.value)}>
            <option value="">(all)</option>
            {KINDS.map((k) => (
              <option key={k} value={k}>{k}</option>
            ))}
          </select>
        </label>
        <label className="row-tight gap-1 small muted">
          current label
          <select value={currentLabelFilter}
                  onChange={(e) => setCurrentLabelFilter(e.target.value)}>
            <option value="__any__">(any)</option>
            <option value="">(none)</option>
            {LABELS.map((l) => (
              <option key={l} value={l}>{l}</option>
            ))}
          </select>
        </label>
        <button onClick={refresh} className="btn-sm">Refresh</button>
        <button onClick={markUnknownKindAsIgnore} disabled={bulkBusy}
                className="btn-sm" style={{ marginLeft: 'auto' }}>
          Mark all kind=unknown as ignore
        </button>
      </div>

      {/* bulk apply bar */}
      {selected.size > 0 && (
        <div className="card card-tight row-tight gap-3 wrap"
             style={{ background: 'var(--bg-selected)',
                      borderColor: 'var(--accent)' }}>
          <span className="text-sm">
            <span className="text-strong">{selected.size}</span> selected
          </span>
          <select value={bulkLabel}
                  onChange={(e) => setBulkLabel(e.target.value)}>
            {BULK_TARGETS.map((l) => (
              <option key={l} value={l}>{l}</option>
            ))}
          </select>
          <button onClick={applyBulk} disabled={bulkBusy} className="btn-primary btn-sm">
            Apply to selected
          </button>
          <button onClick={() => setSelected(new Set())} className="btn-sm">
            Clear
          </button>
        </div>
      )}

      {bulkResult && (
        <div className={`pill ${bulkResult.ok ? 'pill-success' : 'pill-danger'}`}>
          {bulkResult.msg}
        </div>
      )}

      {loading ? (
        <div className="empty"><span className="loading">Loading</span></div>
      ) : error ? (
        <p className="text-sm" style={{ color: 'var(--danger)' }}>{error}</p>
      ) : rows.length === 0 ? (
        <div className="empty">
          Review queue is empty for these filters. Try a different filter, or
          re-run the classifier via{' '}
          <code>scripts/bootstrap_abilities.py</code>.
        </div>
      ) : (
        <>
          <div className="row-tight gap-3 small muted">
            <span>Showing {rows.length} item{rows.length === 1 ? '' : 's'}</span>
            <button onClick={selectAll} className="btn-ghost btn-xs">
              {selected.size === rows.length ? 'Deselect all' : 'Select all'}
            </button>
          </div>
          <div className="stack-sm">
            {rows.map((r) => (
              <AbilityCard
                key={r.ability_game_id}
                row={r}
                onUpdated={refresh}
                selectable
                selected={selected.has(r.ability_game_id)}
                onToggle={() => toggle(r.ability_game_id)}
              />
            ))}
          </div>
        </>
      )}
    </div>
  );
}

function AllLabels() {
  const [filter, setFilter] = useState('');
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    const url = filter
      ? `/api/abilities/labels?label=${filter}&limit=200`
      : '/api/abilities/labels?limit=200';
    fetch(url)
      .then((r) => r.json())
      .then(setRows)
      .finally(() => setLoading(false));
  }, [filter]);

  return (
    <div className="stack">
      <div className="card card-tight row-tight gap-2"
           style={{ background: 'var(--bg-elevated)' }}>
        <label className="row-tight gap-1 small muted">
          filter
          <select value={filter} onChange={(e) => setFilter(e.target.value)}>
            <option value="">(all)</option>
            {LABELS.map((l) => (
              <option key={l} value={l}>{l}</option>
            ))}
          </select>
        </label>
      </div>
      {loading ? (
        <div className="empty"><span className="loading">Loading</span></div>
      ) : rows.length === 0 ? (
        <div className="empty">No abilities match.</div>
      ) : (
        <div className="stack-sm">
          {rows.map((r) => (
            <AbilityCard key={r.ability_game_id} row={r} compact />
          ))}
        </div>
      )}
    </div>
  );
}

function AbilityCard({ row, onUpdated, compact, selectable, selected, onToggle }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const setLabel = async (label) => {
    setBusy(true);
    setErr(null);
    try {
      const r = await fetch(`/api/abilities/${row.ability_game_id}/label`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label }),
      });
      if (!r.ok) {
        const b = await r.json().catch(() => ({}));
        throw new Error(b.detail || `PATCH ${r.status}`);
      }
      if (onUpdated) onUpdated();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card card-tight"
         style={selected ? { borderColor: 'var(--accent)',
                              background: 'var(--bg-selected)' } : null}>
      <div className="row-tight gap-3">
        {selectable && (
          <input type="checkbox" checked={selected} onChange={onToggle}
                 style={{ flexShrink: 0 }} />
        )}
        {row.icon && (
          <img src={`${XIVAPI_BASE}${row.icon}`} alt=""
               width="32" height="32"
               style={{ flexShrink: 0, borderRadius: 4 }} />
        )}
        <div className="grow" style={{ minWidth: 0 }}>
          <div className="row-tight gap-2">
            <span className="text-strong">
              {row.name || `id ${row.ability_game_id}`}
            </span>
            <span className="muted small">
              · {row.kind || 'unknown'} · id {row.ability_game_id}
            </span>
            {row.duration_ms != null && (
              <span className="muted small">
                · {(row.duration_ms / 1000).toFixed(0)}s
              </span>
            )}
            {row.mit_pct != null && (
              <span className="small" style={{ color: 'var(--success)' }}>
                · -{row.mit_pct}%
              </span>
            )}
          </div>
          {row.label && (
            <div className="small muted">
              current: <span className="pill pill-accent"
                              style={{ marginLeft: 2 }}>{row.label}</span>
              <span> · {row.source}</span>
              {row.confidence != null && ` · conf ${row.confidence.toFixed(2)}`}
            </div>
          )}
        </div>
      </div>
      {!compact && row.description && (
        <p className="small muted" style={{ margin: '8px 0' }}>
          {row.description.slice(0, 280)}
          {row.description.length > 280 && '…'}
        </p>
      )}
      {!compact && (
        <div className="row-tight gap-1 wrap" style={{ marginTop: 'var(--s-2)' }}>
          {LABELS.filter((l) => l !== 'unknown').map((l) => (
            <button
              key={l}
              onClick={() => setLabel(l)}
              disabled={busy}
              className={`btn-xs ${row.label === l ? 'btn-primary' : ''}`}
            >
              {l}
            </button>
          ))}
        </div>
      )}
      {err && (
        <p className="small" style={{ color: 'var(--danger)', marginTop: 6 }}>
          {err}
        </p>
      )}
    </div>
  );
}
