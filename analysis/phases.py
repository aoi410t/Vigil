"""T-103: phase segmentation (boss-side).

For each fight, derive phase intervals from per-enemy damage activity windows.
Each distinct enemy actor we hit in the fight has a [first_hit, last_hit]
window; sorting those windows chronologically and merging overlaps gives
boss-side phase boundaries — exactly what PLAN §9 M-INFER #1 calls for.

Why this signal: multi-phase FFXIV fights swap the active boss actor between
phases (Fatebreaker → Usurper → Adds → Oracle → … in FRU), with brief
untargetable transitions in between. Activity windows of distinct enemy IDs
align 1:1 with phases without needing encounter-specific knowledge.

Boss-side only per PLAN Invariant 3 — this reads damage *targets*, not
player decisions or strat.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import Combatant, Event

# Defaults tuned against FRU + M5S samples:
# - 30-hit minimum filters one-shot adds and short-lived environmental effects.
# - Merge-gap default 0 = overlap-only. FRU phase 4 has two concurrent boss
#   actors (Pandora's Knight + a second model) — their activity windows
#   genuinely overlap so they fold in. Adjacent-but-distinct phases (P1→P2
#   transition is ~5s in FRU) keep small gaps that we must NOT merge across,
#   so anything > 0 risks collapsing real phase boundaries.
DEFAULT_MIN_HITS = 30
DEFAULT_MERGE_GAP_MS = 0


def _player_ids(session: Session, fight_id: int) -> set[int]:
    return set(session.execute(
        select(Combatant.player_id).where(Combatant.fight_id == fight_id)
    ).scalars().all())


def detect_phase_boundaries(
    session: Session,
    fight_id: int,
    *,
    min_hits: int = DEFAULT_MIN_HITS,
    merge_gap_ms: int = DEFAULT_MERGE_GAP_MS,
) -> dict[str, Any]:
    """Return inferred phase intervals for one fight.

    Output:
      {
        "fight_id": int,
        "phases": [
          {"index": 0, "start_ts": int, "end_ts": int,
           "boss_target_ids": [int, …], "hit_count": int}
        ],
        "transitions": [{"after_phase": int, "gap_ms": int}, …],
      }
    Single-phase fights return one phase covering all damage activity.
    Empty fights (no damage events) return `phases=[]`.
    """
    players = _player_ids(session, fight_id)

    # Per-enemy activity: (target_id, first_ts, last_ts, hit_count).
    # FFLogs emits both `damage` (resolved) and `calculateddamage` (pre-mit)
    # for actions; either suffices for the timing signal — prefer the union.
    rows = session.execute(
        select(
            Event.target_id,
            func.count(),
            func.min(Event.ts),
            func.max(Event.ts),
        )
        .where(
            Event.fight_id == fight_id,
            Event.type.in_(("damage", "calculateddamage")),
            Event.target_id.is_not(None),
            Event.ts.is_not(None),
        )
        .group_by(Event.target_id)
    ).all()

    enemies = [
        (tid, cnt, int(t0), int(t1))
        for tid, cnt, t0, t1 in rows
        if tid not in players and cnt >= min_hits
    ]
    if not enemies:
        return {"fight_id": fight_id, "phases": [], "transitions": []}

    # Sort by first activity, then merge overlapping or close-gap intervals
    # into one logical phase.
    enemies.sort(key=lambda e: e[2])
    merged: list[dict[str, Any]] = []
    for tid, cnt, t0, t1 in enemies:
        if merged and t0 - merged[-1]["end_ts"] <= merge_gap_ms:
            cur = merged[-1]
            cur["end_ts"] = max(cur["end_ts"], t1)
            cur["boss_target_ids"].append(tid)
            cur["hit_count"] += cnt
        else:
            merged.append({
                "start_ts": t0,
                "end_ts": t1,
                "boss_target_ids": [tid],
                "hit_count": int(cnt),
            })

    phases = [
        {"index": i, **m} for i, m in enumerate(merged)
    ]
    transitions = [
        {"after_phase": i, "gap_ms": phases[i + 1]["start_ts"] - phases[i]["end_ts"]}
        for i in range(len(phases) - 1)
    ]
    return {
        "fight_id": fight_id,
        "phases": phases,
        "transitions": transitions,
    }
