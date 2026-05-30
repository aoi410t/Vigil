"""M-RECOV (T-305) recovery / resilience.

For each player death in a fight: was the player resurrected? How long did
it take? Then per-fight roll-up: resilience % (deaths recovered / total
deaths), and the wipe-pattern signal (consecutive un-recovered deaths at
the end of a wipe).

"Resurrected" detection: the same `player_id` appears as `source_id` in any
cast/damage event after the death timestamp within the same fight. The
earliest such event marks the recovery moment.

Fast-rez detection: `time_to_recovery_ms < FAST_REZ_THRESHOLD_MS` (5s by
default) — likely Swiftcast/Dualcast was used since base Raise has ~8s cast.

Boss-side only per PLAN Invariant 3 — this reads player event sources, not
strat assignments.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analysis.cartography import _active_players_by_fight
from db.models import Combatant, Event, Fight

FAST_REZ_THRESHOLD_MS = 5_000
PLAYER_ACTIVITY_TYPES = ("cast", "damage", "calculateddamage")


def recovery_for_fight(session: Session, fight_id: int) -> dict[str, Any]:
    """Per-fight recovery rollup.

    Returns:
      {
        "fight_id": int,
        "total_deaths": int,
        "recovered_deaths": int,
        "fatal_deaths": int,
        "resilience_pct": float | None,  # recovered / total, 0..100
        "avg_recovery_ms": int | None,
        "fast_rez_count": int,           # recoveries within FAST_REZ_THRESHOLD_MS
        "players": [
          {player_id, name, job, deaths, recovered, fatal,
           avg_recovery_ms, fast_rez_count}
        ],
        "events": [{player_id, death_ts, recovered, recovery_ts,
                    time_to_recovery_ms, fast}]
      }
    """
    fight = session.get(Fight, fight_id)
    if fight is None:
        return {"fight_id": fight_id, "total_deaths": 0,
                "note": "fight not found"}

    active = _active_players_by_fight(session, [fight_id]).get(fight_id, set())

    death_rows = session.execute(
        select(Event.target_id, Event.ts)
        .where(Event.fight_id == fight_id, Event.type == "death")
        .order_by(Event.ts)
    ).all()
    deaths = [(tid, int(ts)) for tid, ts in death_rows
              if tid in active and ts is not None]

    # Per-player chronological "alive again" event timestamps
    activity_rows = session.execute(
        select(Event.source_id, Event.ts)
        .where(Event.fight_id == fight_id,
               Event.type.in_(PLAYER_ACTIVITY_TYPES),
               Event.source_id.in_(active),
               Event.ts.is_not(None))
        .order_by(Event.source_id, Event.ts)
    ).all()
    activity_by_pid: dict[int, list[int]] = defaultdict(list)
    for sid, ts in activity_rows:
        activity_by_pid[sid].append(int(ts))

    combatants = session.execute(
        select(Combatant).where(Combatant.fight_id == fight_id)
    ).scalars().all()
    name_job = {c.player_id: (c.name, c.job) for c in combatants}

    per_player: dict[int, dict[str, Any]] = defaultdict(
        lambda: {"deaths": 0, "recovered": 0, "fatal": 0,
                 "recoveries_ms": [], "fast_rez_count": 0}
    )
    events: list[dict[str, Any]] = []
    fast_count = 0

    for pid, death_ts in deaths:
        per_player[pid]["deaths"] += 1
        # Find the first activity strictly AFTER this death
        future = [t for t in activity_by_pid.get(pid, []) if t > death_ts]
        if future:
            recovery_ts = future[0]
            dt = recovery_ts - death_ts
            per_player[pid]["recovered"] += 1
            per_player[pid]["recoveries_ms"].append(dt)
            is_fast = dt <= FAST_REZ_THRESHOLD_MS
            if is_fast:
                per_player[pid]["fast_rez_count"] += 1
                fast_count += 1
            events.append({
                "player_id": pid, "death_ts": death_ts,
                "recovered": True, "recovery_ts": recovery_ts,
                "time_to_recovery_ms": dt, "fast": is_fast,
            })
        else:
            per_player[pid]["fatal"] += 1
            events.append({
                "player_id": pid, "death_ts": death_ts,
                "recovered": False, "recovery_ts": None,
                "time_to_recovery_ms": None, "fast": False,
            })

    total_deaths = len(deaths)
    recovered_deaths = sum(p["recovered"] for p in per_player.values())
    fatal_deaths = total_deaths - recovered_deaths
    all_recovery_times = [
        ms for p in per_player.values() for ms in p["recoveries_ms"]
    ]
    avg_recovery_ms = (sum(all_recovery_times) // len(all_recovery_times)
                       if all_recovery_times else None)

    players_out = []
    for pid, agg in per_player.items():
        name, job = name_job.get(pid, (None, None))
        avg = (sum(agg["recoveries_ms"]) // len(agg["recoveries_ms"])
               if agg["recoveries_ms"] else None)
        players_out.append({
            "player_id": pid, "name": name, "job": job,
            "deaths": agg["deaths"],
            "recovered": agg["recovered"],
            "fatal": agg["fatal"],
            "avg_recovery_ms": avg,
            "fast_rez_count": agg["fast_rez_count"],
        })
    players_out.sort(key=lambda p: (-p["deaths"], p["player_id"]))

    return {
        "fight_id": fight_id,
        "total_deaths": total_deaths,
        "recovered_deaths": recovered_deaths,
        "fatal_deaths": fatal_deaths,
        "resilience_pct": (round(recovered_deaths / total_deaths * 100, 1)
                           if total_deaths > 0 else None),
        "avg_recovery_ms": avg_recovery_ms,
        "fast_rez_count": fast_count,
        "players": players_out,
        "events": events,
    }
