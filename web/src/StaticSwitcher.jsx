import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { useMe } from './me.jsx';

/**
 * v1.6.0 multi-static UI. v1.7.0 design refresh. v1.7.1 consumes shared
 * `useMe()` instead of fetching /api/me itself — the App renders the dev
 * badge / hides tabs based on the same data, so a single source of truth
 * keeps everything consistent on static switch.
 */
export default function StaticSwitcher({ onStaticChange }) {
  const { me, error, refresh } = useMe();
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState('');
  const [managing, setManaging] = useState(null);

  if (error) {
    return <span className="small" style={{ color: 'var(--danger)' }}>{error}</span>;
  }
  if (!me) return <span className="small muted loading">loading</span>;

  const switchTo = async (sid) => {
    if (Number(sid) === me.current_static_id) return;
    const r = await fetch('/api/me/current-static', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ static_id: Number(sid) }),
    });
    if (r.ok) {
      await refresh();
      if (onStaticChange) onStaticChange();
    }
  };

  const createStatic = async () => {
    const name = newName.trim();
    if (!name) return;
    const r = await fetch('/api/statics', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    if (r.ok) {
      setNewName('');
      setCreating(false);
      await refresh();
      if (onStaticChange) onStaticChange();
    }
  };

  return (
    <div className="row-tight gap-2">
      <span className="muted small">{me.username}</span>
      <span className="faint">·</span>
      <select
        value={me.current_static_id}
        onChange={(e) => switchTo(e.target.value)}
        style={{ minWidth: 140 }}
      >
        {me.statics.map((s) => (
          <option key={s.id} value={s.id}>{s.name}</option>
        ))}
      </select>
      <button onClick={() => setManaging(me.current_static_id)}
              className="btn-ghost btn-xs"
              title="Manage members of the current static">
        members
      </button>
      <button onClick={() => setCreating(!creating)}
              className="btn-ghost btn-xs"
              title="Create a new static">
        + static
      </button>
      {creating && (
        <span className="row-tight gap-1">
          <input
            value={newName}
            placeholder="new static name"
            onChange={(e) => setNewName(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') createStatic(); }}
            autoFocus
            style={{ minWidth: 140 }}
          />
          <button onClick={createStatic} className="btn-primary btn-xs">create</button>
          <button onClick={() => setCreating(false)} className="btn-ghost btn-xs">cancel</button>
        </span>
      )}
      {managing != null && (
        <MembersModal
          staticId={managing}
          staticName={me.statics.find((s) => s.id === managing)?.name}
          currentUserId={me.user_id}
          onClose={() => { setManaging(null); refresh(); }}
        />
      )}
    </div>
  );
}

function MembersModal({ staticId, staticName, currentUserId, onClose }) {
  const [members, setMembers] = useState(null);
  const [adding, setAdding] = useState('');
  const [error, setError] = useState(null);

  const refresh = async () => {
    setError(null);
    const r = await fetch(`/api/statics/${staticId}/members`);
    if (!r.ok) {
      setError(`GET /api/statics/${staticId}/members -> ${r.status}`);
      return;
    }
    setMembers(await r.json());
  };
  useEffect(() => { refresh(); /* eslint-disable-next-line */ }, [staticId]);

  const add = async () => {
    const u = adding.trim();
    if (!u) return;
    const r = await fetch(`/api/statics/${staticId}/members`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: u }),
    });
    if (r.ok) { setAdding(''); await refresh(); }
    else {
      const b = await r.json().catch(() => ({}));
      setError(b.detail || `POST -> ${r.status}`);
    }
  };

  const remove = async (userId) => {
    const r = await fetch(`/api/statics/${staticId}/members/${userId}`, {
      method: 'DELETE',
    });
    if (r.ok) await refresh();
    else {
      const b = await r.json().catch(() => ({}));
      setError(b.detail || `DELETE -> ${r.status}`);
    }
  };

  return createPortal(
    <div className="modal-overlay" onClick={(e) => {
      if (e.target.classList.contains('modal-overlay')) onClose();
    }}>
      <div className="modal">
        <div className="modal-header">
          <h3 className="mb-0">
            <span className="muted small">Members of</span> {staticName}
          </h3>
          <button onClick={onClose} className="btn-ghost btn-sm">✕</button>
        </div>
        <div className="modal-body stack">
          {!members ? (
            <p className="muted loading">Loading</p>
          ) : members.length === 0 ? (
            <p className="muted">No members.</p>
          ) : (
            <table className="t t-tight">
              <tbody>
                {members.map((m) => (
                  <tr key={m.user_id}>
                    <td>
                      {m.username}
                      {m.user_id === currentUserId && (
                        <span className="muted small"> (you)</span>
                      )}
                    </td>
                    <td className="num">
                      <button onClick={() => remove(m.user_id)}
                              className="btn-ghost btn-xs"
                              style={{ color: 'var(--danger)' }}>
                        remove
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <div className="row-tight gap-2">
            <input
              value={adding}
              placeholder="username to add"
              onChange={(e) => setAdding(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') add(); }}
              style={{ flex: 1 }}
            />
            <button onClick={add} className="btn-primary">add</button>
          </div>
          {error && (
            <p className="text-sm" style={{ color: 'var(--danger)' }}>{error}</p>
          )}
        </div>
        <div className="modal-footer">
          <button onClick={onClose} className="btn-primary">Done</button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
