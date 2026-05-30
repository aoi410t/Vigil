// Roster page (v1.15.0) — discovery + classification UX.
//
// Top: Core members. Per-member dropdown to attach a discovered character as
// a sub-account.
// Mid: Substitute members (collapsed by default unless any exist).
// Bottom: Characters seen in reports — checklist where each row's
// classification can be set to core / substitute / sub of X / ignore / clear.
//
// All writes route through /api/roster/classify (new in v1.15.0) which is
// idempotent and handles alias creation, ignore upserts, and clearing.

import { useEffect, useMemo, useState } from 'react';

const KIND_CORE = 'core';
const KIND_SUBSTITUTE = 'substitute';

export default function Members() {
  const [members, setMembers] = useState([]);
  const [characters, setCharacters] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [statusMsg, setStatusMsg] = useState(null);

  const refresh = async () => {
    setError(null);
    try {
      const [mr, cr] = await Promise.all([
        fetch('/api/members'),
        fetch('/api/roster/characters'),
      ]);
      if (!mr.ok) throw new Error(`GET /api/members ${mr.status}`);
      if (!cr.ok) throw new Error(`GET /api/roster/characters ${cr.status}`);
      const mj = await mr.json();
      const cj = await cr.json();
      setMembers(mj);
      setCharacters(cj.characters || []);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { refresh(); }, []);

  const classify = async (payload) => {
    setStatusMsg(null);
    try {
      const r = await fetch('/api/roster/classify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        throw new Error(body.detail || `POST ${r.status}`);
      }
      await refresh();
    } catch (e) {
      setStatusMsg(String(e));
    }
  };

  const removeMember = async (memberId, name) => {
    if (!confirm(`Delete member ${name}? Their attached characters will become unclassified.`)) return;
    await fetch(`/api/members/${memberId}`, { method: 'DELETE' });
    await refresh();
  };

  const updateMemberKind = async (memberId, kind) => {
    await fetch(`/api/members/${memberId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind }),
    });
    await refresh();
  };

  const core = members.filter((m) => (m.kind || KIND_CORE) === KIND_CORE);
  const subs = members.filter((m) => m.kind === KIND_SUBSTITUTE);

  // Characters not currently attached to any member (and not ignored) — what
  // a member's sub-account dropdown should offer.
  const unattached = useMemo(
    () => characters.filter((c) => c.classification === 'unclassified'),
    [characters]
  );

  return (
    <section className="fade-in">
      <h1 className="mb-0">Roster</h1>
      <p className="muted text-sm">
        <strong>Core</strong> = a real person's main account.{' '}
        <strong>Sub</strong> = a secondary character of the SAME person
        (alt / sub-account). Attach subs to the core member they belong to.{' '}
        <strong>Substitute</strong> = a backup member who fills in occasionally
        (their own person, their own characters).{' '}
        <strong>Ignore</strong> = pugs, loot trades, anyone you don't want
        appearing in analytics.
      </p>
      <p className="muted text-sm">
        Job is derived per fight from combat logs — never stored on the member.
      </p>

      {error && (
        <div className="card" style={{ borderColor: 'var(--danger)' }}>
          <span className="pill pill-danger">error</span>{' '}
          <span className="text-sm">{error}</span>
        </div>
      )}
      {statusMsg && (
        <div className="card" style={{ borderColor: 'var(--warning)' }}>
          <span className="pill pill-warning">heads-up</span>{' '}
          <span className="text-sm">{statusMsg}</span>
        </div>
      )}

      {loading ? (
        <div className="empty"><span className="loading">Loading</span></div>
      ) : (
        <>
          <MembersSection
            title="Core members"
            subtitle="The static. Analytics surface these names everywhere."
            members={core}
            unattached={unattached}
            classify={classify}
            removeMember={removeMember}
            updateMemberKind={updateMemberKind}
            emptyHint="No core members yet. Mark someone as core in the table below or add manually."
          />

          {subs.length > 0 && (
            <MembersSection
              title="Substitutes"
              subtitle="Backups who fill in. Currently treated identically to core in analytics — the tag is for your mental model."
              members={subs}
              unattached={unattached}
              classify={classify}
              removeMember={removeMember}
              updateMemberKind={updateMemberKind}
              emptyHint=""
            />
          )}

          <NewMemberForm onCreated={refresh} />

          <CharactersChecklist
            characters={characters}
            members={members}
            classify={classify}
          />
        </>
      )}
    </section>
  );
}

function MembersSection({ title, subtitle, members, unattached, classify,
                          removeMember, updateMemberKind, emptyHint }) {
  return (
    <section style={{ marginTop: 'var(--s-5)' }}>
      <h2 className="mb-0">{title}</h2>
      {subtitle && <p className="muted text-sm">{subtitle}</p>}
      {members.length === 0 ? (
        <div className="empty">{emptyHint}</div>
      ) : (
        <div className="stack">
          {members.map((m) => (
            <MemberRow
              key={m.id}
              member={m}
              unattached={unattached}
              classify={classify}
              removeMember={removeMember}
              updateMemberKind={updateMemberKind}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function MemberRow({ member, unattached, classify, removeMember,
                     updateMemberKind }) {
  const [pickValue, setPickValue] = useState('');

  const attach = async () => {
    if (!pickValue) return;
    const [name, server] = pickValue.split('|||');
    await classify({
      character_name: name,
      server: server || null,
      action: 'sub',
      member_id: member.id,
    });
    setPickValue('');
  };

  const detach = async (alias) => {
    await classify({
      character_name: alias.character_name,
      server: alias.server || null,
      action: 'clear',
    });
  };

  return (
    <div className="card">
      <div className="row" style={{ alignItems: 'baseline',
                                    justifyContent: 'space-between' }}>
        <div className="row gap-2" style={{ alignItems: 'baseline' }}>
          <h3 className="mb-0">{member.name}</h3>
          <span className="pill">{member.kind || 'core'}</span>
          {member.role_pref && (
            <span className="pill">{member.role_pref}</span>
          )}
        </div>
        <div className="row gap-2">
          {member.kind !== KIND_SUBSTITUTE && (
            <button onClick={() => updateMemberKind(member.id, KIND_SUBSTITUTE)}
                    className="btn-ghost btn-sm">
              mark substitute
            </button>
          )}
          {member.kind === KIND_SUBSTITUTE && (
            <button onClick={() => updateMemberKind(member.id, KIND_CORE)}
                    className="btn-ghost btn-sm">
              promote to core
            </button>
          )}
          <button onClick={() => removeMember(member.id, member.name)}
                  className="btn-ghost btn-sm"
                  style={{ color: 'var(--danger)' }}>
            delete
          </button>
        </div>
      </div>

      <div style={{ marginTop: 'var(--s-3)' }}>
        <h5 style={{ marginBottom: 'var(--s-2)' }}>
          Characters {member.aliases.length > 0 && `(${member.aliases.length})`}
        </h5>
        {member.aliases.length === 0 ? (
          <div className="muted small">No characters yet — attach one below.</div>
        ) : (
          <div className="row-tight wrap gap-2">
            {member.aliases.map((a) => (
              <span key={a.id} className="pill">
                {a.character_name}
                {a.server && <span className="muted"> @ {a.server}</span>}
                <button onClick={() => detach(a)}
                        className="btn-ghost btn-xs"
                        style={{ padding: '0 4px', marginLeft: 4 }}
                        title="Detach character">×</button>
              </span>
            ))}
          </div>
        )}
      </div>

      <div className="row-tight gap-2 wrap" style={{ marginTop: 'var(--s-3)' }}>
        <select value={pickValue}
                onChange={(e) => setPickValue(e.target.value)}
                style={{ minWidth: 260 }}>
          <option value="">
            {unattached.length === 0
              ? '— no unclassified characters available —'
              : '+ attach sub-account from logs…'}
          </option>
          {unattached.map((c) => (
            <option key={`${c.character_name}|${c.server || ''}`}
                    value={`${c.character_name}|||${c.server || ''}`}>
              {c.character_name}
              {c.server ? ` @ ${c.server}` : ''}
              {c.latest_job ? ` (${c.latest_job})` : ''}
              {` · ${c.fights_seen} pulls`}
            </option>
          ))}
        </select>
        <button onClick={attach} disabled={!pickValue} className="btn-sm">
          Attach
        </button>
      </div>
    </div>
  );
}

function NewMemberForm({ onCreated }) {
  const [name, setName] = useState('');
  const [kind, setKind] = useState(KIND_CORE);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const submit = async (e) => {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      const r = await fetch('/api/members', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name.trim(), kind }),
      });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        throw new Error(body.detail || `POST ${r.status}`);
      }
      setName('');
      setKind(KIND_CORE);
      onCreated();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className="card row-tight gap-2 wrap"
          style={{ marginTop: 'var(--s-4)' }}>
      <span className="text-sm muted">Add a member manually:</span>
      <input
        placeholder="Member name"
        value={name}
        onChange={(e) => setName(e.target.value)}
        required
        style={{ minWidth: 200 }}
      />
      <select value={kind} onChange={(e) => setKind(e.target.value)}>
        <option value={KIND_CORE}>core</option>
        <option value={KIND_SUBSTITUTE}>substitute</option>
      </select>
      <button type="submit" disabled={busy} className="btn-primary">
        Add member
      </button>
      {err && <span className="text-sm" style={{ color: 'var(--danger)' }}>{err}</span>}
    </form>
  );
}

function CharactersChecklist({ characters, members, classify }) {
  const [filter, setFilter] = useState('all');
  const [search, setSearch] = useState('');

  const filtered = useMemo(() => {
    let rows = characters;
    if (filter === 'unclassified') {
      rows = rows.filter((c) => c.classification === 'unclassified');
    } else if (filter === 'hide_ignored') {
      rows = rows.filter((c) => c.classification !== 'ignored');
    } else if (filter === 'ignored') {
      rows = rows.filter((c) => c.classification === 'ignored');
    }
    if (search.trim()) {
      const q = search.trim().toLowerCase();
      rows = rows.filter((c) =>
        c.character_name.toLowerCase().includes(q)
        || (c.server || '').toLowerCase().includes(q)
        || (c.linked_member_name || '').toLowerCase().includes(q)
      );
    }
    return rows;
  }, [characters, filter, search]);

  const counts = useMemo(() => {
    const c = { total: characters.length, unclassified: 0, ignored: 0,
                core: 0, substitute: 0, sub: 0 };
    for (const ch of characters) {
      c[ch.classification] = (c[ch.classification] || 0) + 1;
    }
    return c;
  }, [characters]);

  return (
    <section style={{ marginTop: 'var(--s-5)' }}>
      <h2 className="mb-0">Characters seen in reports</h2>
      <p className="muted text-sm">
        Every distinct character that appeared in your watched reports.
        Classify each:{' '}
        <strong>core</strong> — main account, creates a new member.{' '}
        <strong>substitute</strong> — backup member, creates a new member.{' '}
        <strong>sub of …</strong> — alt of an existing member (attach as a
        sub-account).{' '}
        <strong>ignore</strong> — hide from analytics.
      </p>

      <div className="card row-tight gap-3 wrap">
        <div className="row-tight gap-1 wrap">
          {[
            ['all', `all (${counts.total})`],
            ['unclassified', `unclassified (${counts.unclassified || 0})`],
            ['hide_ignored', 'hide ignored'],
            ['ignored', `ignored (${counts.ignored || 0})`],
          ].map(([k, label]) => (
            <button key={k}
                    onClick={() => setFilter(k)}
                    className={filter === k ? 'btn-primary btn-sm' : 'btn-ghost btn-sm'}>
              {label}
            </button>
          ))}
        </div>
        <input
          placeholder="search name / server / member…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          style={{ minWidth: 220 }}
        />
        <span className="muted text-sm">
          {counts.core || 0} core · {counts.substitute || 0} substitute ·{' '}
          {counts.sub || 0} sub · {counts.unclassified || 0} unclassified ·{' '}
          {counts.ignored || 0} ignored
        </span>
      </div>

      {filtered.length === 0 ? (
        <div className="empty">
          {characters.length === 0
            ? 'No characters in logs yet. Once a watched report is ingested they appear here.'
            : 'No characters match the current filter.'}
        </div>
      ) : (
        <div className="card card-flush" style={{ marginTop: 'var(--s-3)' }}>
          <table className="t">
            <thead>
              <tr>
                <th>Character</th>
                <th>Server</th>
                <th>Latest job</th>
                <th className="num">Pulls</th>
                <th>Status</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((c) => (
                <CharacterRow
                  key={`${c.character_name}|${c.server || ''}`}
                  character={c}
                  members={members}
                  classify={classify}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function CharacterRow({ character, members, classify }) {
  const [subTarget, setSubTarget] = useState('');

  const doAction = (action, extra = {}) =>
    classify({
      character_name: character.character_name,
      server: character.server || null,
      action,
      ...extra,
    });

  const attachAsSub = () => {
    if (!subTarget) return;
    doAction('sub', { member_id: Number(subTarget) });
    setSubTarget('');
  };

  const statusPill = (() => {
    switch (character.classification) {
      case 'core':
        return <span className="pill pill-accent">core: {character.linked_member_name}</span>;
      case 'substitute':
        return <span className="pill pill-warning">substitute: {character.linked_member_name}</span>;
      case 'sub':
        return <span className="pill">sub of {character.linked_member_name}</span>;
      case 'ignored':
        return <span className="pill pill-danger">ignored</span>;
      default:
        return <span className="pill" style={{ opacity: 0.6 }}>unclassified</span>;
    }
  })();

  return (
    <tr>
      <td><strong>{character.character_name}</strong></td>
      <td className="muted text-sm">{character.server || '—'}</td>
      <td className="text-sm">{character.latest_job || '—'}</td>
      <td className="num">{character.fights_seen}</td>
      <td>{statusPill}</td>
      <td>
        <div className="row-tight gap-1 wrap">
          <button onClick={() => doAction('core')} className="btn-sm">core</button>
          <button onClick={() => doAction('substitute')} className="btn-sm">substitute</button>
          {members.length > 0 && (
            <span className="row-tight gap-1">
              <select value={subTarget}
                      onChange={(e) => setSubTarget(e.target.value)}>
                <option value="">sub of…</option>
                {members.map((m) => (
                  <option key={m.id} value={m.id}>{m.name}</option>
                ))}
              </select>
              <button onClick={attachAsSub} disabled={!subTarget}
                      className="btn-sm">
                attach
              </button>
            </span>
          )}
          <button onClick={() => doAction('ignore')} className="btn-ghost btn-sm">
            ignore
          </button>
          {character.classification !== 'unclassified' && (
            <button onClick={() => doAction('clear')} className="btn-ghost btn-sm">
              clear
            </button>
          )}
        </div>
      </td>
    </tr>
  );
}
