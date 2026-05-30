"""T-107: resolve fight combatants to roster members via character aliases.

Reads stored `combatants` and joins to `character_aliases` by `(name, server)`.
When `combatants.server` is NULL (FFLogs sometimes omits it), falls back to
matching on `character_name` alone — but only if the alias is unique by name,
to avoid silently mis-attributing alts on different servers.

Per PLAN T-107 AC: returns per-fight member→job mapping. Job comes from the
combatant row (already derived per fight from CombatantInfo), so a member who
re-rolls jobs still resolves correctly.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import CharacterAlias, Combatant, Fight, Member


def _alias_lookup(session: Session) -> tuple[
    dict[tuple[str, str], int], dict[str, list[int]]
]:
    """Return ((name, server) → member_id, name → [member_id, …]) maps."""
    rows = session.execute(
        select(CharacterAlias.character_name, CharacterAlias.server,
               CharacterAlias.member_id)
    ).all()
    keyed: dict[tuple[str, str], int] = {}
    by_name: dict[str, list[int]] = defaultdict(list)
    for name, server, mid in rows:
        if server is not None:
            keyed[(name, server)] = mid
        by_name[name].append(mid)
    return keyed, by_name


def resolve_combatants_for_report(session: Session, code: str) -> dict[str, Any]:
    """Per-fight resolution: `{report_code, fights: [{fight_id, combatants: [{…}]}, …]}`.

    Each combatant entry: `player_id, name, server, job, member_id, member_name`.
    `member_id` is `None` when no alias matches — the combatant is either an
    unrostered player or a roster member who hasn't had this character added yet.
    """
    fights = session.execute(
        select(Fight).where(Fight.report_code == code).order_by(Fight.start_time, Fight.id)
    ).scalars().all()
    if not fights:
        return {"report_code": code, "fights": []}

    fight_ids = [f.id for f in fights]
    combatants = session.execute(
        select(Combatant).where(Combatant.fight_id.in_(fight_ids))
    ).scalars().all()

    keyed, by_name = _alias_lookup(session)
    member_names: dict[int, str] = {
        mid: name for (mid, name) in session.execute(
            select(Member.id, Member.name)
        ).all()
    }

    by_fight: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for c in combatants:
        member_id: int | None = None
        if c.name is None:
            pass
        elif c.server is not None and (c.name, c.server) in keyed:
            member_id = keyed[(c.name, c.server)]
        else:
            candidates = by_name.get(c.name, [])
            if len(candidates) == 1:
                member_id = candidates[0]
            # >1 alias under the same name with different servers → ambiguous,
            # leave unresolved rather than guess.
        by_fight[c.fight_id].append({
            "player_id": c.player_id,
            "name": c.name,
            "server": c.server,
            "job": c.job,
            "member_id": member_id,
            "member_name": member_names.get(member_id) if member_id is not None else None,
        })

    return {
        "report_code": code,
        "fights": [
            {"fight_id": f.id,
             "fight_id_in_report": f.fight_id_in_report,
             "combatants": by_fight.get(f.id, [])}
            for f in fights
        ],
    }


def coverage_summary(session: Session, code: str) -> dict[str, Any]:
    """Quick stats: how many distinct character names in this report resolve
    to a member, and which are unresolved? Useful for telling the user 'you've
    rostered 7 of 8 players in this report; missing: <name>'."""
    fights = session.execute(
        select(Fight.id).where(Fight.report_code == code)
    ).scalars().all()
    if not fights:
        return {"report_code": code, "total_characters": 0,
                "resolved": 0, "unresolved": []}

    # FFLogs uses a few pseudo-actor names that aren't real characters and
    # would otherwise show up as "unresolved" forever.
    PSEUDO_ACTORS = ("", "Multiple Players", "Limit Break", "Environment")
    distinct_names = session.execute(
        select(Combatant.name, func.max(Combatant.server))
        .where(Combatant.fight_id.in_(fights),
               Combatant.name.is_not(None),
               Combatant.name.notin_(PSEUDO_ACTORS))
        .group_by(Combatant.name)
    ).all()

    keyed, by_name = _alias_lookup(session)
    resolved = 0
    unresolved: list[dict[str, str | None]] = []
    for name, server in distinct_names:
        if server is not None and (name, server) in keyed:
            resolved += 1
            continue
        if len(by_name.get(name, [])) == 1:
            resolved += 1
            continue
        unresolved.append({"name": name, "server": server})
    return {
        "report_code": code,
        "total_characters": len(distinct_names),
        "resolved": resolved,
        "unresolved": unresolved,
    }
