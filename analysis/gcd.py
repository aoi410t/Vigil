"""M-GCD: gcd-drop detection (T-008, PLAN §9).

For each player in each fight, separate GCD casts from oGCD weaves by the
inter-cast spacing, estimate the player's actual GCD interval from their own
cast stream, and count GCDs that "should have happened" inside long gaps.

Limitation (Mode 1, documented): boss-untargetable / forced-downtime windows
count as dropped GCDs here because we don't yet have the fight model. Mode 2
(T-306 M-CONS / M-INFER) will subtract those windows.

Estimated GCD comes from the median of inter-cast intervals in the [1800, 2800] ms
range — robust to skill speed without needing CombatantInfo parsing. Falls back
to 2500 ms when the player has too few casts to estimate.
"""
from __future__ import annotations

from collections import defaultdict
from statistics import median
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Combatant, Event, Fight

DEFAULT_GCD_MS = 2500
MIN_PLAUSIBLE_GCD_MS = 1800
MAX_PLAUSIBLE_GCD_MS = 2800
SPINE_THRESHOLD = 0.9  # cast counts as a GCD if ≥ 0.9 × gcd_ms after the previous GCD
MAX_DROPS_RETURNED = 200  # cap timeline positions per (fight, player)


def estimate_gcd_ms(timestamps: list[int], default_ms: int = DEFAULT_GCD_MS) -> int:
    """Median of inter-cast gaps in the plausible GCD window."""
    if len(timestamps) < 4:
        return default_ms
    ordered = sorted(timestamps)
    gaps = [ordered[i + 1] - ordered[i] for i in range(len(ordered) - 1)]
    candidates = [g for g in gaps if MIN_PLAUSIBLE_GCD_MS <= g <= MAX_PLAUSIBLE_GCD_MS]
    if len(candidates) < 3:
        return default_ms
    return int(median(candidates))


def detect_gcd_drops(timestamps: list[int], gcd_ms: int) -> dict[str, Any]:
    """Return GCD-spine stats and drop timeline positions.

    The "spine" is the player's GCD train: the first cast, then each subsequent
    cast that's ≥ SPINE_THRESHOLD × gcd_ms after the previous spine cast. Casts
    between are oGCD weaves and don't affect the spine.

    A drop is a slot on the spine grid where the player didn't press a GCD,
    inferred from gaps in the spine > 1 full GCD interval.
    """
    if len(timestamps) < 2:
        return {"gcds_cast": len(timestamps), "dropped_count": 0, "drop_positions": []}

    casts = sorted(timestamps)
    spine = [casts[0]]
    threshold = SPINE_THRESHOLD * gcd_ms
    for ts in casts[1:]:
        if ts >= spine[-1] + threshold:
            spine.append(ts)

    drops: list[int] = []
    for i in range(1, len(spine)):
        gap = spine[i] - spine[i - 1]
        slots = round(gap / gcd_ms)
        if slots >= 2:
            for k in range(1, slots):
                drops.append(spine[i - 1] + k * gcd_ms)

    return {
        "gcds_cast": len(spine),
        "dropped_count": len(drops),
        "drop_positions": drops,
    }


def mode1_gcd_for_report(session: Session, code: str) -> dict[str, Any]:
    fights: list[Fight] = (
        session.query(Fight)
        .filter(Fight.report_code == code)
        .order_by(Fight.start_time, Fight.id)
        .all()
    )
    if not fights:
        return {"report_code": code, "fights": []}

    fight_ids = [f.id for f in fights]
    combatants_list = (
        session.query(Combatant).filter(Combatant.fight_id.in_(fight_ids)).all()
    )
    combatants: dict[tuple[int, int], Combatant] = {
        (c.fight_id, c.player_id): c for c in combatants_list
    }

    cast_rows = session.execute(
        select(Event.fight_id, Event.source_id, Event.ts)
        .where(
            Event.fight_id.in_(fight_ids),
            Event.type == "cast",
            Event.source_id.is_not(None),
            Event.ts.is_not(None),
        )
        .order_by(Event.fight_id, Event.source_id, Event.ts)
    ).all()
    by_fp: dict[tuple[int, int], list[int]] = defaultdict(list)
    for fid, sid, ts in cast_rows:
        by_fp[(fid, sid)].append(int(ts))

    out_fights = []
    for f in fights:
        per_player = []
        for (fid, pid), timestamps in by_fp.items():
            if fid != f.id:
                continue
            c = combatants.get((f.id, pid))
            if c is None:
                continue  # not a known player in this fight (NPC casts, etc.)
            gcd_ms = estimate_gcd_ms(timestamps)
            stats = detect_gcd_drops(timestamps, gcd_ms=gcd_ms)
            per_player.append({
                "player_id": pid,
                "name": c.name,
                "job": c.job,
                "casts_total": len(timestamps),
                "gcd_ms": gcd_ms,
                "gcds_cast": stats["gcds_cast"],
                "dropped_count": stats["dropped_count"],
                "drop_positions": stats["drop_positions"][:MAX_DROPS_RETURNED],
            })
        per_player.sort(key=lambda p: (-p["dropped_count"], p["player_id"]))

        out_fights.append({
            "fight_id": f.id,
            "fight_id_in_report": f.fight_id_in_report,
            "is_kill": f.is_kill,
            "last_phase": f.last_phase,
            "duration_ms": f.duration_ms,
            "players": per_player,
        })

    return {"report_code": code, "fights": out_fights}
