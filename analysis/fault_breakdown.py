"""v1.13.0: per-(player, killing-ability) fault breakdown.

The joint table that powers two drill-down views on Home:
  1. **Per-mechanic** — for a row in "What's killing us", which players are
     eating this mechanic most? (e.g. "Alice has died to Cyclonic Break 6 of
     12 occurrences").
  2. **Per-member** — for a row in "Who's contributing to wipes", what
     mechanics is this player consistently failing on? (e.g. "Alice's top
     three killers: Cyclonic Break ×6, Powder Mark Trail ×3, Sinsmoke ×2").

Both pivots are the same underlying data. We return the joint table and
the UI pivots client-side — keeps the API surface small.

Reads from `fault_scores.reasons.deaths` (populated by T-302 / v1.12.0
classifier), so the breakdown reflects the current classification rules
(including mit_failure attribution).
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analysis._encounter import canonical_encounter_id, encounter_id_group
from db.models import Ability, FaultScore, Fight


def fault_breakdown_for_encounter(
    session: Session, encounter_id: int, static_id: int,
) -> dict[str, Any]:
    """For each (player, killing_ability) across watched wipes of this
    encounter, return death counts + kind breakdown + name lookup.

    Returns:
      {
        "encounter_id": int,
        "wipes_aggregated": int,
        "rows": [
          {
            "player_id": int, "player_name": str | None, "job": str | None,
            "ability_game_id": int | None,  # null = FFLogs non-attributable
            "ability_name": str | None,
            "ability_label": str | None,
            "deaths": int,
            "fights_affected": int,  # how many distinct wipes this pair touched
            "by_kind": {"root": int, "cascade": int, "mit_failure": int,
                        "enrage": int, "unknown": int}
          }
        ]
      }

    Rows are sorted by death count desc, tiebroken by player_name. Static
    is enforced via FaultScore.static_id; cross-static rows are invisible.
    """
    # v1.17.0: union fault scores across the cloned-encounter group.
    rows = session.execute(
        select(FaultScore, Fight)
        .join(Fight, Fight.id == FaultScore.fight_id)
        .where(Fight.encounter_id.in_(encounter_id_group(encounter_id)),
               Fight.is_kill.is_(False),
               FaultScore.static_id == static_id)
    ).all()

    # v1.16.2: key by (character_name, ability_id) — NOT (player_id, ability_id).
    # FFLogs player_ids are report-scoped, so pid=2 might be "Alice" in report A
    # and "Bob" in report B. Keying by name attributes deaths to the right
    # character. The name comes from FaultScore.reasons.name, which was set
    # during compute_fault_scores_for_fight from the combatant row for THAT fight.
    joint: dict[tuple[str | None, int | None], dict[str, Any]] = defaultdict(
        lambda: {
            "player_id": None, "player_name": None, "job": None,
            "ability_game_id": None, "ability_label": None,
            "deaths": 0,
            "fights": set(),
            "by_kind": {"root": 0, "cascade": 0, "mit_failure": 0,
                        "enrage": 0, "unknown": 0},
        }
    )
    all_ability_ids: set[int] = set()

    for fs, fight in rows:
        reasons = fs.reasons or {}
        name = reasons.get("name")
        job = reasons.get("job")
        for d in reasons.get("deaths", []):
            aid = d.get("ability_game_id")
            # v1.16.1: also use inferred_ability_id for the breakdown view —
            # otherwise non-attributable deaths all clump under "ability: null"
            # even when we've figured out what killed them.
            if aid is None and d.get("inferred_ability_id") is not None:
                aid = d["inferred_ability_id"]
            kind = d.get("kind") or "unknown"
            label = d.get("ability_label") or d.get("inferred_ability_label")
            key = (name, aid)
            bucket = joint[key]
            # Keep player_id around for legacy breakdown filter calls, but
            # it's only a representative pid (the first one we saw).
            if bucket["player_id"] is None:
                bucket["player_id"] = fs.player_id
            bucket["player_name"] = name or bucket["player_name"]
            bucket["job"] = job or bucket["job"]
            bucket["ability_game_id"] = aid
            bucket["ability_label"] = label or bucket["ability_label"]
            bucket["deaths"] += 1
            bucket["fights"].add(fight.id)
            if kind in bucket["by_kind"]:
                bucket["by_kind"][kind] += 1
            if aid is not None:
                all_ability_ids.add(aid)

    # Ability name resolution in one query.
    name_lookup: dict[int, str | None] = {}
    if all_ability_ids:
        name_rows = session.execute(
            select(Ability.ability_game_id, Ability.name)
            .where(Ability.ability_game_id.in_(all_ability_ids))
        ).all()
        name_lookup = {aid: name for aid, name in name_rows}

    out_rows = []
    for (name, aid), b in joint.items():
        out_rows.append({
            "player_id": b["player_id"],  # representative pid (legacy)
            "player_name": name,
            "job": b["job"],
            "ability_game_id": aid,
            "ability_name": name_lookup.get(aid) if aid is not None else None,
            "ability_label": b["ability_label"],
            "deaths": b["deaths"],
            "fights_affected": len(b["fights"]),
            "by_kind": b["by_kind"],
        })
    out_rows.sort(
        key=lambda r: (-r["deaths"], r["player_name"] or "")
    )

    return {
        "encounter_id": canonical_encounter_id(encounter_id),
        "wipes_aggregated": len({fs.fight_id for fs, _ in rows}),
        "rows": out_rows,
    }
