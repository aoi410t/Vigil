"""M-WIPE: wipe-location histogram (T-006, PLAN §9).

For each wipe (`is_kill=False`) in a report, bucket by `(phase, last boss cast)`
where the boss cast is the most recent non-player `cast` event in a lookback
window ending at the fight's `end_time`. Counts per (phase, mechanic) give the
"where are we dying" headline for Mode 1.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Combatant, Event, Fight

DEFAULT_LOOKBACK_MS = 15_000


def wipe_histogram_for_report(
    session: Session,
    code: str,
    *,
    lookback_ms: int = DEFAULT_LOOKBACK_MS,
) -> dict[str, Any]:
    """Bucket wipes by `(last_phase, last boss cast ability_game_id)`.

    Returns a JSON-serializable dict for the API layer. Buckets are sorted
    by count desc.
    """
    fights: list[Fight] = (
        session.query(Fight).filter(Fight.report_code == code).all()
    )
    wipes = [f for f in fights if f.is_kill is False]
    kills = [f for f in fights if f.is_kill is True]

    if not wipes:
        return {
            "report_code": code,
            "total_wipes": 0,
            "total_kills": len(kills),
            "buckets": [],
        }

    player_ids_by_fight: dict[int, set[int]] = defaultdict(set)
    if fights:
        for c in (
            session.query(Combatant)
            .filter(Combatant.fight_id.in_([f.id for f in fights]))
            .all()
        ):
            player_ids_by_fight[c.fight_id].add(c.player_id)

    buckets: dict[tuple[int | None, int | None], list[int]] = defaultdict(list)

    for fight in wipes:
        end_ts = fight.end_time
        if end_ts is None:
            buckets[(fight.last_phase, None)].append(fight.id)
            continue

        filters = [
            Event.fight_id == fight.id,
            Event.type == "cast",
            Event.ts.is_not(None),
            Event.ts >= end_ts - lookback_ms,
            Event.ts <= end_ts,
            Event.source_id.is_not(None),
        ]
        pids = player_ids_by_fight.get(fight.id) or set()
        if pids:
            filters.append(Event.source_id.notin_(pids))

        candidate = session.execute(
            select(Event).where(*filters).order_by(Event.ts.desc()).limit(1)
        ).scalar_one_or_none()
        ability = candidate.ability_game_id if candidate is not None else None
        buckets[(fight.last_phase, ability)].append(fight.id)

    bucket_list = [
        {
            "phase": phase,
            "ability_game_id": ability,
            "count": len(fight_ids),
            "wipes": sorted(fight_ids),
        }
        for (phase, ability), fight_ids in buckets.items()
    ]
    bucket_list.sort(
        key=lambda b: (-b["count"], b["phase"] or 0, b["ability_game_id"] or 0)
    )

    return {
        "report_code": code,
        "total_wipes": len(wipes),
        "total_kills": len(kills),
        "buckets": bucket_list,
    }
