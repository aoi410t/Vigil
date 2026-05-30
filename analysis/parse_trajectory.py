"""M-PARSE (T-106): per-phase damage trajectory.

PLAN Invariant 6: parses (FFLogs percentiles) only exist on kills. During prog
we track **per-phase aDPS** — for each player, how much damage they did during
each detected phase, normalized to time-in-phase.

This is the Mode-1 version: raw per-player DPS per phase, using `DamageDone`
events from stored data. True aDPS (FFLogs' raid-buff-adjusted metric that
shares credit with the buff providers) needs the labelled buff catalog plus a
damage-attribution model — both available now via T-108, but the credit-sharing
math itself is M-PARSE-mode-2 (deferred). The raw per-phase trend is enough
for the "are we DPS-gated?" prog-tracking question (PLAN T-204 / T-207).
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analysis.phases import detect_phase_boundaries
from db.models import Combatant, Event, Fight


def parse_per_phase_for_fight(
    session: Session, fight_id: int,
) -> dict[str, Any]:
    """Per-phase, per-player damage totals + DPS for one fight."""
    phases = detect_phase_boundaries(session, fight_id)["phases"]
    if not phases:
        return {"fight_id": fight_id, "phases": []}

    combatants = session.execute(
        select(Combatant).where(Combatant.fight_id == fight_id)
    ).scalars().all()
    combatant_lookup = {c.player_id: c for c in combatants}
    player_ids = set(combatant_lookup)

    # Pull all DamageDone events sourced by players in one shot.
    # FFLogs emits both `damage` (final) and `calculateddamage` (pre-mit) for
    # each cast — count `damage` only to avoid doubling.
    damage_rows = session.execute(
        select(Event.source_id, Event.ts, Event.amount)
        .where(
            Event.fight_id == fight_id,
            Event.type == "damage",
            Event.source_id.in_(player_ids) if player_ids else False,
            Event.amount.is_not(None),
            Event.ts.is_not(None),
        )
    ).all()

    out_phases = []
    for phase in phases:
        start, end = phase["start_ts"], phase["end_ts"]
        duration_ms = end - start
        per_player_dmg: dict[int, int] = defaultdict(int)
        for sid, ts, amount in damage_rows:
            if start <= ts <= end:
                per_player_dmg[sid] += int(amount or 0)

        players_out = []
        for pid, total in per_player_dmg.items():
            c = combatant_lookup.get(pid)
            dps = round(total / (duration_ms / 1000), 1) if duration_ms > 0 else 0.0
            players_out.append({
                "player_id": pid,
                "name": c.name if c else None,
                "job": c.job if c else None,
                "damage_total": total,
                "dps": dps,
            })
        players_out.sort(key=lambda p: -p["damage_total"])

        out_phases.append({
            "phase_index": phase["index"],
            "start_offset_ms": start - phases[0]["start_ts"],
            "end_offset_ms": end - phases[0]["start_ts"],
            "duration_ms": duration_ms,
            "players": players_out,
        })

    return {"fight_id": fight_id, "phases": out_phases}
