"""T-304 fault disambiguation via mit audit.

Refines T-302's `cascade` classification with T-303's mit-audit data: a death
to a raidwide whose **mit plan was followed** stays as cascade (amplified by
some earlier failure — to be hunted down elsewhere); a death to a raidwide
where **mits were missed** is upgraded to `mit_failure` (the root cause is
the missing cooldown itself, not a downstream chain reaction).

Doesn't try to assign mit_failure to a specific player — role→member
resolution is ambiguous without strict roster locking (MT vs OT etc.). The
M-MIT panel surfaces which ability was missed; the human reads it and
assigns blame from their own roster knowledge. T-309 polish could add
explicit member-locked roles to T-301 to automate this.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analysis._encounter import canonical_encounter_id
from analysis.mit_audit import mit_audit_for_fight
from db.models import Event, FaultScore, Fight, FightModel


def _raidwide_ability_ids(session: Session, encounter_id: int,
                          version: int = 1) -> set[int]:
    # v1.17.0: fight_model lives at the canonical encounter ID.
    return set(session.execute(
        select(FightModel.ability_game_id)
        .where(FightModel.encounter_id == canonical_encounter_id(encounter_id),
               FightModel.version == version,
               FightModel.type_label == "raidwide")
    ).scalars().all())


def _assign_death_to_occurrence(death_ts: int, ability_id: int,
                                 raidwide_casts: list[dict[str, Any]]) -> int | None:
    """Find the most recent same-ability raidwide cast at or before this death.
    Returns its occurrence index, or None if no preceding cast was found."""
    best_occ = None
    for c in raidwide_casts:
        if c["ability_id"] != ability_id:
            continue
        if c["cast_ts"] <= death_ts:
            best_occ = c["occurrence"]
        else:
            break
    return best_occ


def disambiguate_for_fight(session: Session, fight_id: int, static_id: int,
                            *, version: int = 1) -> dict[str, Any]:
    """Read existing fault_scores + mit audit (both scoped to static),
    re-classify cascade deaths that lost their fight to a missed-mit
    raidwide as `mit_failure`."""
    fight = session.get(Fight, fight_id)
    if fight is None:
        return {"fight_id": fight_id, "reclassified": 0, "note": "fight not found"}

    fault_rows = list(session.execute(
        select(FaultScore).where(FaultScore.fight_id == fight_id,
                                 FaultScore.static_id == static_id)
    ).scalars().all())
    if not fault_rows:
        return {"fight_id": fight_id, "reclassified": 0,
                "note": "no fault_scores rows — run T-302 compute first"}

    audit = mit_audit_for_fight(session, fight_id, static_id, version=version)
    raidwide_casts = audit["raidwide_casts"]
    if not raidwide_casts:
        return {"fight_id": fight_id, "reclassified": 0,
                "note": "no raidwides in fight_model"}

    # Per (ability_id, occurrence) → did this raidwide miss any planned mit?
    missed_lookup: dict[tuple[int, int], bool] = {}
    for c in raidwide_casts:
        # no_plan or empty plan = we don't know → treat as not-missed
        if c["no_plan"] or not c["planned_slots"]:
            missed_lookup[(c["ability_id"], c["occurrence"])] = False
        else:
            missed_lookup[(c["ability_id"], c["occurrence"])] = (
                c["missed_count"] > 0
            )

    raidwide_ids = _raidwide_ability_ids(session, fight.encounter_id, version)
    reclassified = 0

    for row in fault_rows:
        reasons = dict(row.reasons or {})
        deaths = list(reasons.get("deaths") or [])
        new_deaths: list[dict[str, Any]] = []
        new_kind_counts = {"root": 0, "cascade": 0, "mit_failure": 0,
                           "enrage": 0, "unknown": 0}
        for d in deaths:
            d = dict(d)
            kind = d.get("kind")
            ability_id = d.get("ability_game_id")
            if (kind == "cascade" and ability_id in raidwide_ids):
                occ = _assign_death_to_occurrence(
                    int(d.get("ts", 0)), int(ability_id), raidwide_casts,
                )
                if occ is not None and missed_lookup.get(
                    (int(ability_id), int(occ)), False
                ):
                    d["kind"] = "mit_failure"
                    d["mit_audit_occurrence"] = occ
                    reclassified += 1
            new_deaths.append(d)
            k = d.get("kind", "unknown")
            new_kind_counts[k] = new_kind_counts.get(k, 0) + 1

        reasons["deaths"] = new_deaths
        reasons["root"] = new_kind_counts["root"]
        reasons["cascade"] = new_kind_counts["cascade"]
        reasons["mit_failure"] = new_kind_counts["mit_failure"]
        reasons["enrage"] = new_kind_counts["enrage"]
        reasons["unknown"] = new_kind_counts["unknown"]
        row.reasons = reasons
        # Score: root + mit_failure count as full faults; cascade stays at 0.1.
        row.score = (
            new_kind_counts["root"] * 1.0
            + new_kind_counts["mit_failure"] * 1.0
            + new_kind_counts["cascade"] * 0.1
        )

    session.commit()
    return {
        "fight_id": fight_id,
        "reclassified": reclassified,
        "fault_rows": len(fault_rows),
    }
