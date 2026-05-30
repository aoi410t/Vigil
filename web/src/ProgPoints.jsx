import { useEffect, useMemo, useState } from 'react';
import {
  CartesianGrid, Line, LineChart, ResponsiveContainer,
  Tooltip, XAxis, YAxis,
} from 'recharts';

export default function ProgPoints() {
  const [points, setPoints] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const refresh = async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await fetch('/api/prog-points');
      if (!r.ok) throw new Error(`GET /api/prog-points ${r.status}`);
      setPoints(await r.json());
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { refresh(); }, []);

  return (
    <section className="stack-lg">
      <div className="card">
        <div className="row" style={{ alignItems: 'baseline',
                                      justifyContent: 'space-between',
                                      marginBottom: 'var(--s-2)' }}>
          <h3 className="mb-0">Prog points</h3>
          <span className="muted small">
            {points.length} point{points.length === 1 ? '' : 's'}
          </span>
        </div>
        <p className="muted text-sm">
          Manual entries record where the group is in the fight at a given
          moment. The curve plots fight-percentage remaining over time —{' '}
          lower is further into the fight.
        </p>
        <NewPointForm onCreated={refresh} />
        {error && (
          <p className="text-sm" style={{ color: 'var(--danger)' }}>{error}</p>
        )}
        <ProgCurve points={points} />
      </div>

      <div className="card">
        <h5 style={{ marginBottom: 'var(--s-2)' }}>Log</h5>
        {loading ? (
          <div className="muted small loading">Loading</div>
        ) : (
          <PointsList points={points} onChange={refresh} />
        )}
      </div>
    </section>
  );
}

function NewPointForm({ onCreated }) {
  const nowLocal = () => {
    const d = new Date();
    d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
    return d.toISOString().slice(0, 16);
  };
  const [ts, setTs] = useState(nowLocal());
  const [phase, setPhase] = useState('');
  const [pct, setPct] = useState('');
  const [pulls, setPulls] = useState('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const submit = async (e) => {
    e.preventDefault();
    if (!phase && !pct) {
      setErr('Need at least phase or fight %');
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      const body = {
        ts: new Date(ts).toISOString(),
        phase: phase === '' ? null : Number(phase),
        fight_percentage: pct === '' ? null : Number(pct),
        pull_count: pulls === '' ? null : Number(pulls),
      };
      const r = await fetch('/api/prog-points', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        const b = await r.json().catch(() => ({}));
        throw new Error(b.detail || `POST ${r.status}`);
      }
      setPhase(''); setPct(''); setPulls(''); setTs(nowLocal());
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
      <label className="row-tight gap-1 small muted">
        when
        <input type="datetime-local" value={ts}
               onChange={(e) => setTs(e.target.value)} required />
      </label>
      <input placeholder="phase" type="number" min="0" value={phase}
             onChange={(e) => setPhase(e.target.value)}
             style={{ width: 90 }} />
      <input placeholder="% remaining" type="number" step="0.1" min="0" max="100"
             value={pct} onChange={(e) => setPct(e.target.value)}
             style={{ width: 130 }} />
      <input placeholder="pulls so far" type="number" min="0" value={pulls}
             onChange={(e) => setPulls(e.target.value)}
             style={{ width: 120 }} />
      <button type="submit" disabled={busy} className="btn-primary">
        Log
      </button>
      {err && <span className="text-sm" style={{ color: 'var(--danger)' }}>{err}</span>}
    </form>
  );
}

function ProgCurve({ points }) {
  const data = useMemo(
    () =>
      points
        .filter((p) => p.fight_percentage != null)
        .map((p) => ({
          ts: new Date(p.ts).getTime(),
          pct: p.fight_percentage,
        })),
    [points],
  );
  if (data.length === 0) {
    return (
      <div className="empty" style={{ padding: 'var(--s-4)' }}>
        No fight-% points yet — log one above to see the curve.
      </div>
    );
  }
  const tsFmt = (t) => new Date(t).toLocaleDateString();
  return (
    <div style={{ width: '100%', height: 240 }}>
      <ResponsiveContainer>
        <LineChart data={data} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
          <XAxis
            dataKey="ts" type="number" domain={['dataMin', 'dataMax']}
            tickFormatter={tsFmt} scale="time"
            stroke="var(--fg-muted)" tick={{ fontSize: 11 }}
          />
          <YAxis
            domain={[0, 100]} reversed
            stroke="var(--fg-muted)" tick={{ fontSize: 11 }}
            label={{ value: '% remaining', angle: -90, position: 'insideLeft',
                     style: { fill: 'var(--fg-muted)', fontSize: 11 } }}
          />
          <Tooltip labelFormatter={tsFmt} formatter={(v) => `${v}%`} />
          <Line type="monotone" dataKey="pct" stroke="var(--accent)"
                strokeWidth={2} dot={{ r: 3 }} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function PointsList({ points, onChange }) {
  if (points.length === 0) {
    return <div className="muted text-sm">No prog points yet.</div>;
  }
  const remove = async (id) => {
    if (!confirm('Delete this prog point?')) return;
    await fetch(`/api/prog-points/${id}`, { method: 'DELETE' });
    onChange();
  };
  return (
    <table className="t t-tight">
      <thead>
        <tr>
          <th>When</th>
          <th className="num">Phase</th>
          <th className="num">% rem</th>
          <th className="num">Pulls</th>
          <th>Source</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {points.slice().reverse().map((p) => (
          <tr key={p.id}>
            <td>{new Date(p.ts).toLocaleString()}</td>
            <td className="num">{p.phase ?? '—'}</td>
            <td className="num">{p.fight_percentage ?? '—'}</td>
            <td className="num">{p.pull_count ?? '—'}</td>
            <td>
              <span className={`pill ${p.source === 'auto' ? 'pill-accent' : ''}`}>
                {p.source}
              </span>
            </td>
            <td className="num">
              <button onClick={() => remove(p.id)}
                      className="btn-ghost btn-xs"
                      style={{ color: 'var(--danger)' }}>
                delete
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
