"""Mode-1 fault basics (T-007, PLAN §9 M-FAULT primary signals).

Per-pull deaths with their killing-ability + per-pull damage takers, ranked.
This is the strat-free Mode-1 version: no fault attribution, no root/cascade
distinction, no mit audit (all of those land in Mode 2, T-302/T-303/T-304).
Just the raw signals the UI needs to show "who died to what, and who's eating
the most damage."

Note on "avoidable" (PLAN §11 wording): without the fight model + strat config
we can't separate avoidable from unavoidable, so this returns total damage
taken from non-player sources per fight. Ranking is preserved; absolute values
are upper bounds. M-FAULT (T-302) refines.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import Combatant, Event, Fight


def _name_job(combatants_map: dict[tuple[int, int], Combatant],
              fight_id: int, player_id: int | None) -> tuple[str | None, str | None]:
    if player_id is None:
        return (None, None)
    c = combatants_map.get((fight_id, player_id))
    return (c.name, c.job) if c is not None else (None, None)


def mode1_faults_for_report(session: Session, code: str) -> dict[str, Any]:
    """Per-fight deaths (with killing ability) and per-player damage-taken totals."""
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
    players_by_fight: dict[int, set[int]] = defaultdict(set)
    for c in combatants_list:
        players_by_fight[c.fight_id].add(c.player_id)

    death_rows = (
        session.query(Event)
        .filter(Event.fight_id.in_(fight_ids), Event.type == "death")
        .order_by(Event.fight_id, Event.ts)
        .all()
    )
    deaths_by_fight: dict[int, list[Event]] = defaultdict(list)
    for d in death_rows:
        deaths_by_fight[d.fight_id].append(d)

    damage_rows = session.execute(
        select(
            Event.fight_id,
            Event.target_id,
            func.coalesce(func.sum(Event.amount), 0),
        )
        .where(
            Event.fight_id.in_(fight_ids),
            Event.type == "damage",
            Event.target_id.is_not(None),
            Event.amount.is_not(None),
        )
        .group_by(Event.fight_id, Event.target_id)
    ).all()

    damage_by_fight: dict[int, dict[int, int]] = defaultdict(dict)
    for fid, tid, amount in damage_rows:
        if tid in players_by_fight.get(fid, set()):
            damage_by_fight[fid][tid] = int(amount)

    out_fights = []
    for f in fights:
        fid = f.id
        f_deaths = []
        for d in deaths_by_fight.get(fid, []):
            name, job = _name_job(combatants, fid, d.target_id)
            f_deaths.append({
                "player_id": d.target_id,
                "name": name,
                "job": job,
                "ts": d.ts,
                "killing_ability_game_id": d.ability_game_id,
            })

        f_takers = []
        for pid, amt in sorted(
            damage_by_fight.get(fid, {}).items(),
            key=lambda kv: (-kv[1], kv[0]),
        ):
            name, job = _name_job(combatants, fid, pid)
            f_takers.append({
                "player_id": pid,
                "name": name,
                "job": job,
                "damage_taken_total": amt,
            })

        out_fights.append({
            "fight_id": fid,
            "fight_id_in_report": f.fight_id_in_report,
            "is_kill": f.is_kill,
            "last_phase": f.last_phase,
            "fight_percentage": float(f.fight_percentage)
                if f.fight_percentage is not None else None,
            "duration_ms": f.duration_ms,
            "deaths": f_deaths,
            "damage_takers": f_takers,
        })

    return {"report_code": code, "fights": out_fights}
