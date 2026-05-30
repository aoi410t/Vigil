import { useEffect, useMemo, useState } from 'react';

const TYPE_COLOR = {
  raidwide:   'var(--type-raidwide)',
  tankbuster: 'var(--type-tankbuster)',
  aoe_party:  'var(--type-aoe_party)',
  enrage:     'var(--type-enrage)',
  cosmetic:   'var(--type-cosmetic)',
  unknown:    'var(--type-unknown)',
};

// Per-role colors for the overlay bars. 'any' = muted grey.
const ROLE_COLOR = {
  MT: 'var(--role-MT)', OT: 'var(--role-OT)',
  H1: 'var(--role-H1)', H2: 'var(--role-H2)',
  D1: 'var(--role-D1)', D2: 'var(--role-D2)',
  D3: 'var(--role-D3)', D4: 'var(--role-D4)',
  any: 'var(--role-any)', null: 'var(--role-any)',
};

const FALLBACK_MIT_WINDOW_MS = 15_000;
const DRAG_SNAP_MS = 500;

export default function StratEditor({ encounterId }) {
  const [model, setModel] = useState(null);
  const [configs, setConfigs] = useState({});
  const [roles, setRoles] = useState([]);
  const [abilityNames, setAbilityNames] = useState({});
  const [mitAbilities, setMitAbilities] = useState([]);
  const [error, setError] = useState(null);
  const [selectedMech, setSelectedMech] = useState(null);

  const refresh = async () => {
    setError(null);
    try {
      const [m, s] = await Promise.all([
        fetch(`/api/encounters/${encounterId}/fight-model`).then((r) => r.json()),
        fetch(`/api/encounters/${encounterId}/strat-config`).then((r) => r.json()),
      ]);
      setModel(m);
      setRoles(s.roles);
      const map = {};
      for (const row of s.rows) map[row.mechanic_ref] = row;
      setConfigs(map);
    } catch (e) {
      setError(String(e));
    }
  };

  useEffect(() => {
    setModel(null); setConfigs({}); setSelectedMech(null);
    refresh();
    // eslint-disable-next-line
  }, [encounterId]);

  useEffect(() => {
    fetch('/api/abilities/labels?limit=5000')
      .then((r) => r.json())
      .then((rows) => {
        const names = {};
        const mits = [];
        for (const r of rows) {
          if (r.name) names[r.ability_game_id] = r.name;
          if (['mit_party', 'mit_self', 'mit_boss_debuff'].includes(r.label)) {
            mits.push(r);
          }
        }
        setAbilityNames(names);
        setMitAbilities(mits.sort((a, b) =>
          (a.name || '').localeCompare(b.name || '')));
      })
      .catch(() => {});
  }, []);

  if (error) return <p className="text-sm" style={{ color: 'var(--danger)' }}>{error}</p>;
  if (!model) return <div className="empty"><span className="loading">Loading</span></div>;

  if (model.phases.length === 0) {
    return (
      <div className="empty">
        No persisted fight model for this encounter yet. Run{' '}
        <code>POST /api/encounters/{encounterId}/fight-model/persist</code>{' '}
        and <code>/classify</code> first.
      </div>
    );
  }

  const allMechanics = [];
  for (const p of model.phases) {
    const seen = {};
    for (const a of p.abilities) {
      const occ = (seen[a.ability_game_id] || 0);
      seen[a.ability_game_id] = occ + 1;
      const mechanic_ref = `${a.ability_game_id}_${occ}`;
      allMechanics.push({
        mechanic_ref,
        ability_game_id: a.ability_game_id,
        occurrence: occ,
        phase: p.phase,
        seq: a.seq,
        type_label: a.type_label,
        relative_t_ms: a.relative_t_ms,
        confidence: a.confidence,
        configured: !!configs[mechanic_ref],
      });
    }
  }
  const interestingTypes = new Set(['raidwide', 'tankbuster', 'aoe_party', 'enrage']);
  const focused = allMechanics.filter((m) => interestingTypes.has(m.type_label));

  return (
    <div>
      <p className="muted text-sm" style={{ marginBottom: 'var(--s-3)' }}>
        Per-mechanic mit plan + role assignments. Mit palette pulls from
        abilities you've labeled <code>mit_party</code> /{' '}
        <code>mit_self</code> / <code>mit_boss_debuff</code> in the Abilities
        review queue. Recurring mechanics (Akh Morn etc.) carry distinct
        configs per occurrence.
      </p>
      <div className="row row-stack-mobile" style={{ alignItems: 'flex-start' }}>
        <MechanicPicker
          mechanics={focused}
          allMechanics={allMechanics}
          abilityNames={abilityNames}
          selected={selectedMech?.mechanic_ref}
          onSelect={setSelectedMech}
        />
        <div className="grow">
          {selectedMech ? (
            <MechanicEditor
              encounterId={encounterId}
              mechanic={selectedMech}
              config={configs[selectedMech.mechanic_ref]}
              roles={roles}
              abilityNames={abilityNames}
              mitAbilities={mitAbilities}
              onSaved={refresh}
            />
          ) : (
            <div className="empty">Pick a mechanic on the left.</div>
          )}
        </div>
      </div>
    </div>
  );
}

function MechanicPicker({ mechanics, allMechanics, abilityNames,
                          selected, onSelect }) {
  const [showAll, setShowAll] = useState(false);
  const list = showAll ? allMechanics : mechanics;
  return (
    <div className="sidebar">
      <label className="row-tight gap-2 small muted"
             style={{ marginBottom: 'var(--s-2)' }}>
        <input type="checkbox" checked={showAll}
               onChange={(e) => setShowAll(e.target.checked)} />
        show all mechanics
      </label>
      <div style={{ maxHeight: 600, overflowY: 'auto', paddingRight: 4 }}>
        {list.map((m) => {
          const name = abilityNames[m.ability_game_id] || `ability ${m.ability_game_id}`;
          const color = TYPE_COLOR[m.type_label] || TYPE_COLOR.unknown;
          const isSel = selected === m.mechanic_ref;
          return (
            <button
              key={m.mechanic_ref}
              onClick={() => onSelect(m)}
              className={`tile ${isSel ? 'is-selected' : ''}`}
            >
              <div className="row-tight gap-2">
                <span className="dot" style={{ background: color }} />
                <span className="tile-title">{name}</span>
                {m.occurrence > 0 && (
                  <span className="muted small">#{m.occurrence + 1}</span>
                )}
                {m.configured && (
                  <span className="pill pill-success"
                        style={{ marginLeft: 'auto' }}>★</span>
                )}
              </div>
              <div className="tile-meta">
                P{m.phase} · t+{(m.relative_t_ms / 1000).toFixed(0)}s ·{' '}
                {m.type_label}
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}

function MechanicEditor({ encounterId, mechanic, config, roles,
                          abilityNames, mitAbilities, onSaved }) {
  const [slots, setSlots] = useState(config?.mit_plan?.slots || []);
  const [assignments, setAssignments] = useState(
    Object.entries(config?.assignments?.role_map || {}),
  );
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [saveMsg, setSaveMsg] = useState(null);

  useEffect(() => {
    setSlots(config?.mit_plan?.slots || []);
    setAssignments(Object.entries(config?.assignments?.role_map || {}));
    setSaveMsg(null);
  }, [mechanic.mechanic_ref, config]);

  const save = async () => {
    setBusy(true);
    setErr(null);
    setSaveMsg(null);
    const payload = {
      mit_plan: { slots: slots.map((s) => ({
        ability_id: Number(s.ability_id),
        expected_role: s.expected_role || null,
        window_offset_ms: Number(s.window_offset_ms || 0),
      })) },
      assignments: { role_map: Object.fromEntries(
        assignments.filter(([k]) => k.trim() !== '')
      ) },
    };
    try {
      const r = await fetch(
        `/api/encounters/${encounterId}/strat-config/${mechanic.mechanic_ref}`,
        { method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload) },
      );
      if (!r.ok) {
        const b = await r.json().catch(() => ({}));
        throw new Error(b.detail || `PUT ${r.status}`);
      }
      setSaveMsg('saved');
      onSaved();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  const removeAll = async () => {
    if (!config) return;
    if (!confirm('Delete this strat config?')) return;
    await fetch(
      `/api/encounters/${encounterId}/strat-config/${mechanic.mechanic_ref}`,
      { method: 'DELETE' },
    );
    onSaved();
  };

  const mechanicName = abilityNames[mechanic.ability_game_id]
    || `ability ${mechanic.ability_game_id}`;
  const typeColor = TYPE_COLOR[mechanic.type_label] || TYPE_COLOR.unknown;

  return (
    <div className="card">
      <div className="row" style={{ alignItems: 'baseline',
                                    justifyContent: 'space-between',
                                    marginBottom: 'var(--s-3)' }}>
        <div>
          <h3 className="mb-0 row-tight gap-2">
            <span className="dot" style={{ background: typeColor,
                                            width: 10, height: 10 }} />
            {mechanicName}
            {mechanic.occurrence > 0 && (
              <span className="muted">#{mechanic.occurrence + 1}</span>
            )}
          </h3>
          <div className="muted small" style={{ marginTop: 4 }}>
            P{mechanic.phase} · t+{(mechanic.relative_t_ms / 1000).toFixed(1)}s
            {' · '}<code>{mechanic.mechanic_ref}</code>
            {' · '}{mechanic.type_label}
          </div>
        </div>
        <div className="row-tight gap-2">
          {saveMsg && (
            <span className="pill pill-success">{saveMsg}</span>
          )}
          {err && (
            <span className="pill pill-danger">{err}</span>
          )}
          {config && (
            <button onClick={removeAll} className="btn-danger btn-sm">Delete</button>
          )}
          <button onClick={save} disabled={busy} className="btn-primary">
            {busy ? <><span className="spinner" /> saving</> : 'Save'}
          </button>
        </div>
      </div>

      <h5>Mit plan ({slots.length} slot{slots.length === 1 ? '' : 's'})</h5>
      <MitSlotsEditor slots={slots} setSlots={setSlots}
                      roles={roles} mitAbilities={mitAbilities}
                      abilityNames={abilityNames} />
      <MitWindowOverlay slots={slots} setSlots={setSlots}
                        mitAbilities={mitAbilities}
                        abilityNames={abilityNames} />
      <MitPalette mitAbilities={mitAbilities}
                  onPick={(ability) => setSlots([...slots, {
                    ability_id: ability.ability_game_id,
                    expected_role: 'any',
                    window_offset_ms: 0,
                  }])} />

      <h5 style={{ marginTop: 'var(--s-4)' }}>
        Assignments ({assignments.length})
      </h5>
      <AssignmentsEditor assignments={assignments}
                         setAssignments={setAssignments}
                         roles={roles} />
    </div>
  );
}

function MitSlotsEditor({ slots, setSlots, roles, mitAbilities }) {
  const addSlot = () => {
    setSlots([...slots, { ability_id: '', expected_role: 'any',
                          window_offset_ms: 0 }]);
  };
  const update = (i, field, value) => {
    const next = slots.slice();
    next[i] = { ...next[i], [field]: value };
    setSlots(next);
  };
  const remove = (i) => setSlots(slots.filter((_, j) => j !== i));

  if (slots.length === 0) {
    return (
      <div className="muted text-sm" style={{ margin: '8px 0' }}>
        No mits planned. Pick from the palette below or click <em>+ slot</em>.
      </div>
    );
  }

  return (
    <div>
      <table className="t t-tight">
        <tbody>
          {slots.map((s, i) => (
            <tr key={i}>
              <td>
                <select value={s.ability_id}
                        onChange={(e) => update(i, 'ability_id', e.target.value)}
                        style={{ minWidth: 200 }}>
                  <option value="">(pick ability)</option>
                  {mitAbilities.map((a) => (
                    <option key={a.ability_game_id} value={a.ability_game_id}>
                      {a.name} · {a.label}
                    </option>
                  ))}
                </select>
              </td>
              <td>
                <select value={s.expected_role || 'any'}
                        onChange={(e) => update(i, 'expected_role', e.target.value)}>
                  <option value="any">any</option>
                  {roles.map((r) => <option key={r} value={r}>{r}</option>)}
                </select>
              </td>
              <td>
                <input type="number" value={s.window_offset_ms}
                       onChange={(e) => update(i, 'window_offset_ms', e.target.value)}
                       style={{ width: 90 }} step={500} />
                <span className="small muted" style={{ marginLeft: 4 }}>ms</span>
              </td>
              <td className="num">
                <button onClick={() => remove(i)}
                        className="btn-ghost btn-xs"
                        style={{ color: 'var(--danger)' }}
                        title="Remove slot">×</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <button onClick={addSlot} className="btn-sm"
              style={{ marginTop: 'var(--s-2)' }}>+ slot</button>
    </div>
  );
}

function MitPalette({ mitAbilities, onPick }) {
  const [open, setOpen] = useState(true);
  if (mitAbilities.length === 0) {
    return (
      <p className="muted text-sm" style={{ marginTop: 'var(--s-3)' }}>
        Palette empty — label some abilities as mit_party / mit_self /
        mit_boss_debuff in the Abilities tab first.
      </p>
    );
  }
  const byLabel = { mit_party: [], mit_self: [], mit_boss_debuff: [] };
  for (const a of mitAbilities) {
    if (byLabel[a.label]) byLabel[a.label].push(a);
  }
  return (
    <div style={{ marginTop: 'var(--s-3)' }}>
      <button onClick={() => setOpen(!open)} className="btn-ghost btn-sm">
        {open ? '▾' : '▸'} mit palette · click to add a slot
      </button>
      {open && (
        <div className="row gap-4 wrap" style={{ marginTop: 'var(--s-2)' }}>
          {['mit_party', 'mit_self', 'mit_boss_debuff'].map((lbl) =>
            byLabel[lbl].length > 0 && (
              <div key={lbl} style={{ minWidth: 220 }}>
                <h5 style={{ marginBottom: 4 }}>{lbl.replace('_', ' ')}</h5>
                <div className="row-tight wrap gap-1">
                  {byLabel[lbl].map((a) => (
                    <button
                      key={a.ability_game_id}
                      onClick={() => onPick(a)}
                      title={[
                        a.duration_ms ? `duration ${a.duration_ms / 1000}s` : null,
                        a.mit_pct != null ? `mit ${a.mit_pct}%` : null,
                      ].filter(Boolean).join(' · ') || 'no wiki metadata'}
                      className="chip"
                    >
                      {a.name}
                      {a.duration_ms != null && (
                        <span className="chip-meta">{(a.duration_ms / 1000).toFixed(0)}s</span>
                      )}
                      {a.mit_pct != null && (
                        <span className="chip-good">-{a.mit_pct}%</span>
                      )}
                    </button>
                  ))}
                </div>
              </div>
            )
          )}
        </div>
      )}
    </div>
  );
}

function MitWindowOverlay({ slots, setSlots, mitAbilities, abilityNames }) {
  const durationLookup = useMemo(() => {
    const m = {};
    for (const a of mitAbilities) {
      if (a.duration_ms != null) m[a.ability_game_id] = a.duration_ms;
    }
    return m;
  }, [mitAbilities]);

  const [drag, setDrag] = useState(null);

  if (slots.length === 0) return null;

  let minT = 0, maxT = 5_000;
  for (const s of slots) {
    const aid = Number(s.ability_id);
    const offset = Number(s.window_offset_ms || 0);
    const dur = durationLookup[aid] || FALLBACK_MIT_WINDOW_MS;
    minT = Math.min(minT, offset);
    maxT = Math.max(maxT, offset + dur);
  }
  const span = Math.max(maxT - minT, 1);
  const width = 560;
  const rowHeight = 16;
  const zeroLineX = ((0 - minT) / span) * width;
  const pixelsPerMs = width / span;

  const onPointerDown = (e, slotIndex) => {
    if (!setSlots) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    setDrag({
      slotIndex,
      initialClientX: e.clientX,
      initialOffsetMs: Number(slots[slotIndex].window_offset_ms || 0),
    });
  };
  const onPointerMove = (e) => {
    if (!drag || !setSlots) return;
    const deltaPx = e.clientX - drag.initialClientX;
    const deltaMs = deltaPx / pixelsPerMs;
    const raw = drag.initialOffsetMs + deltaMs;
    const snapped = Math.round(raw / DRAG_SNAP_MS) * DRAG_SNAP_MS;
    const slotAid = Number(slots[drag.slotIndex].ability_id);
    const slotDur = durationLookup[slotAid] || FALLBACK_MIT_WINDOW_MS;
    const clamped = Math.max(minT, Math.min(maxT - slotDur, snapped));
    if (clamped === Number(slots[drag.slotIndex].window_offset_ms || 0)) return;
    const next = slots.slice();
    next[drag.slotIndex] = { ...next[drag.slotIndex], window_offset_ms: clamped };
    setSlots(next);
  };
  const onPointerUp = (e) => {
    if (!drag) return;
    try { e.currentTarget.releasePointerCapture(e.pointerId); } catch {}
    setDrag(null);
  };

  return (
    <div style={{
      marginTop: 'var(--s-3)',
      border: '1px dashed var(--border-strong)',
      borderRadius: 'var(--radius-sm)',
      padding: 'var(--s-3)',
      background: 'var(--bg-elevated)',
    }}>
      <div className="small muted" style={{ marginBottom: 'var(--s-2)' }}>
        Mit windows · anchored at cast (t+0), fallback{' '}
        {FALLBACK_MIT_WINDOW_MS / 1000}s when no wiki duration
        {setSlots && (
          <span style={{ marginLeft: 8, color: 'var(--accent)' }}>
            · drag to reposition (snaps {DRAG_SNAP_MS}ms)
          </span>
        )}
      </div>
      <svg width={width} height={(slots.length + 1) * rowHeight + 10}
           style={{ display: 'block', userSelect: 'none' }}>
        <line x1={zeroLineX} x2={zeroLineX} y1={0}
              y2={(slots.length + 1) * rowHeight + 8}
              stroke="var(--danger)" strokeWidth={1.5} strokeDasharray="3,3" />
        <text x={zeroLineX + 5} y={11} fontSize="10"
              fill="var(--danger)">cast</text>
        {[5_000, 10_000, 15_000, 20_000, 25_000, 30_000].map((t) =>
          t > minT && t < maxT && (
            <g key={t}>
              <line x1={((t - minT) / span) * width}
                    x2={((t - minT) / span) * width}
                    y1={14} y2={(slots.length + 1) * rowHeight + 8}
                    stroke="var(--border)" strokeWidth={1} />
              <text x={((t - minT) / span) * width + 3} y={22}
                    fontSize="9" fill="var(--fg-faint)">
                +{t / 1000}s
              </text>
            </g>
          )
        )}
        {slots.map((s, i) => {
          const aid = Number(s.ability_id);
          if (!aid) return null;
          const offset = Number(s.window_offset_ms || 0);
          const dur = durationLookup[aid] || FALLBACK_MIT_WINDOW_MS;
          const x = ((offset - minT) / span) * width;
          const w = (dur / span) * width;
          const color = ROLE_COLOR[s.expected_role] || ROLE_COLOR.any;
          const y = (i + 1) * rowHeight + 6;
          const label = (abilityNames[aid] || `id ${aid}`)
            + (s.expected_role && s.expected_role !== 'any'
                ? ` (${s.expected_role})` : '');
          const isDragging = drag?.slotIndex === i;
          return (
            <g key={i}>
              <rect
                x={x} y={y} width={Math.max(w, 2)} height={rowHeight - 3}
                fill={color} opacity={isDragging ? 0.95 : 0.7} rx={3}
                stroke={isDragging ? 'var(--accent)' : 'rgba(0,0,0,.3)'}
                strokeWidth={isDragging ? 1.5 : 1}
                style={{ cursor: setSlots
                  ? (isDragging ? 'grabbing' : 'grab') : 'default' }}
                onPointerDown={(e) => onPointerDown(e, i)}
                onPointerMove={onPointerMove}
                onPointerUp={onPointerUp}
                onPointerCancel={onPointerUp}
              />
              <text x={x + 6} y={y + rowHeight - 6} fontSize="10"
                    fill="#fff" style={{ pointerEvents: 'none',
                                          textShadow: '0 1px 2px rgba(0,0,0,.6)' }}>
                {label}
                {isDragging && ` (t+${(offset / 1000).toFixed(1)}s)`}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

function AssignmentsEditor({ assignments, setAssignments, roles }) {
  const addRow = () => setAssignments([...assignments, ['', 'any']]);
  const updateSlot = (i, value) => {
    const next = assignments.slice();
    next[i] = [value, next[i][1]];
    setAssignments(next);
  };
  const updateRole = (i, value) => {
    const next = assignments.slice();
    next[i] = [next[i][0], value];
    setAssignments(next);
  };
  const remove = (i) => setAssignments(assignments.filter((_, j) => j !== i));

  if (assignments.length === 0) {
    return (
      <div>
        <div className="muted text-sm" style={{ margin: '8px 0' }}>
          No assignments. Add slots like "tower_north → MT", "tether_1 → H1".
        </div>
        <button onClick={addRow} className="btn-sm">+ assignment</button>
      </div>
    );
  }
  return (
    <div>
      <table className="t t-tight">
        <tbody>
          {assignments.map(([slot, role], i) => (
            <tr key={i}>
              <td>
                <input value={slot} placeholder="slot_name"
                       onChange={(e) => updateSlot(i, e.target.value)}
                       style={{ width: 200 }} />
              </td>
              <td className="muted">→</td>
              <td>
                <select value={role || 'any'}
                        onChange={(e) => updateRole(i, e.target.value)}>
                  <option value="any">any</option>
                  {roles.map((r) => <option key={r} value={r}>{r}</option>)}
                </select>
              </td>
              <td className="num">
                <button onClick={() => remove(i)}
                        className="btn-ghost btn-xs"
                        style={{ color: 'var(--danger)' }}>×</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <button onClick={addRow} className="btn-sm"
              style={{ marginTop: 'var(--s-2)' }}>+ assignment</button>
    </div>
  );
}
