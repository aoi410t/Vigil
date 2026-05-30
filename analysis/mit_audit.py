"""M-MIT (T-303) mitigation audit.

For each raidwide cast in a fight, compare the **planned** mits
(strat_config.mit_plan slots) against the **actual** mit casts/applies in a
window around the raidwide. Output: per-raidwide missed-mit list, so the
healer/tank brain trust can see exactly which cooldown was supposed to fire
and didn't.

Window heuristic: any cast (or status applybuff) of a planned mit ability
in `[raidwide_cast_ts - PRE_WINDOW_MS, raidwide_cast_ts + POST_WINDOW_MS]`
counts as "fired for this raidwide". Strat-defined `window_offset_ms` is
informational (T-309 visual editor can use it); for the audit we just check
presence in the broad window. Tighter windowing is a T-304 follow-up.

Feeds T-304 fault disambiguation: a raidwide death with mits down here = mit
failure root cause; with mits up = amplified by an earlier death (cascade).
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analysis._encounter import canonical_encounter_id, encounter_id_group
from analysis.cartography import _active_players_by_fight
from analysis.strat_config import encode_mechanic_ref
from db.models import (
    Ability, Event, Fight, FightModel, StratConfig, WatchedReport,
)

# Mits typically pre-applied; raidwide hits ~0-3s after the cast. A
# generous window catches mits intended for this raidwide without bleeding
# into the next one.
PRE_WINDOW_MS = 15_000
POST_WINDOW_MS = 3_000


def _raidwide_casts(session: Session, fight: Fight,
                    version: int = 1) -> list[dict[str, Any]]:
    """Per raidwide cast in the fight: `{cast_ts, ability_id, occurrence}`.

    Occurrence is the 0-indexed cast order of this ability across the whole
    fight — matches the mechanic_ref encoding from T-301.
    """
    # v1.17.0: fight_model + strat_config live at canonical IDs.
    canonical_enc = canonical_encounter_id(fight.encounter_id)
    raidwide_ids = list(session.execute(
        select(FightModel.ability_game_id)
        .where(FightModel.encounter_id == canonical_enc,
               FightModel.version == version,
               FightModel.type_label == "raidwide")
    ).scalars().all())
    if not raidwide_ids:
        return []

    cast_rows = session.execute(
        select(Event.ts, Event.ability_game_id)
        .where(Event.fight_id == fight.id,
               Event.type == "cast",
               Event.ability_game_id.in_(raidwide_ids))
        .order_by(Event.ts)
    ).all()

    occurrence_counter: dict[int, int] = defaultdict(int)
    out = []
    for ts, aid in cast_rows:
        occ = occurrence_counter[aid]
        occurrence_counter[aid] += 1
        out.append({"cast_ts": int(ts), "ability_id": int(aid), "occurrence": occ})
    return out


def _mit_event_lookup(session: Session, fight_id: int,
                      mit_ability_ids: set[int]) -> list[tuple[int, int, int]]:
    """All mit-relevant events in the fight: `[(ts, ability_id, source_id)]`.

    Includes both action `cast` events (the mit cast moment) and status
    `applybuff` events (when the buff actually activates). Either suffices
    as evidence the mit was used.
    """
    if not mit_ability_ids:
        return []
    rows = session.execute(
        select(Event.ts, Event.ability_game_id, Event.source_id)
        .where(
            Event.fight_id == fight_id,
            Event.ability_game_id.in_(mit_ability_ids),
            Event.type.in_(("cast", "applybuff", "refreshbuff")),
            Event.ts.is_not(None),
        )
    ).all()
    return [(int(ts), int(aid), int(sid) if sid is not None else -1)
            for ts, aid, sid in rows]


def mit_audit_for_fight(
    session: Session, fight_id: int, static_id: int,
    *, version: int = 1,
) -> dict[str, Any]:
    """Per-raidwide-cast missed-mit list, joined to strat_config.

    Returns:
      {
        "fight_id": int,
        "raidwide_casts": [
          {
            "cast_ts": int, "ability_id": int, "occurrence": int,
            "mechanic_ref": "<ability>_<occurrence>",
            "planned_slots": [{ability_id, expected_role, fired,
                                fired_at_ts, fired_source_id}],
            "missed_count": int,
            "no_plan": bool
          }
        ]
      }
    """
    fight = session.get(Fight, fight_id)
    if fight is None:
        return {"fight_id": fight_id, "raidwide_casts": [],
                "note": "fight not found"}

    raidwides = _raidwide_casts(session, fight, version=version)
    if not raidwides:
        return {"fight_id": fight_id, "raidwide_casts": [],
                "note": "no raidwide casts in fight (need T-202+T-203 on this encounter)"}

    # Collect every mechanic_ref that has a strat_config (scoped to caller's static)
    strat_rows = session.execute(
        select(StratConfig.mechanic_ref, StratConfig.mit_plan)
        .where(StratConfig.encounter_id == canonical_encounter_id(fight.encounter_id),
               StratConfig.static_id == static_id)
    ).all()
    strat_by_ref: dict[str, dict[str, Any]] = {
        ref: plan for ref, plan in strat_rows
    }

    # Gather all mit ability IDs referenced by any of those plans
    planned_mit_ids: set[int] = set()
    for plan in strat_by_ref.values():
        for slot in (plan or {}).get("slots", []):
            try:
                planned_mit_ids.add(int(slot["ability_id"]))
            except (TypeError, ValueError, KeyError):
                continue

    mit_events = _mit_event_lookup(session, fight_id, planned_mit_ids)
    # Index by ability_id → sorted ts list
    by_ability: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for ts, aid, sid in mit_events:
        by_ability[aid].append((ts, sid))
    for aid in by_ability:
        by_ability[aid].sort()

    casts_out: list[dict[str, Any]] = []
    for cast in raidwides:
        ref = encode_mechanic_ref(cast["ability_id"], cast["occurrence"])
        plan = strat_by_ref.get(ref)
        slot_rows = (plan or {}).get("slots", []) if plan else []
        cast_ts = cast["cast_ts"]
        lo, hi = cast_ts - PRE_WINDOW_MS, cast_ts + POST_WINDOW_MS

        rendered_slots = []
        missed = 0
        for slot in slot_rows:
            try:
                slot_aid = int(slot["ability_id"])
            except (TypeError, ValueError, KeyError):
                continue
            fired_at = None
            fired_source = None
            for ts, sid in by_ability.get(slot_aid, []):
                if lo <= ts <= hi:
                    fired_at = ts
                    fired_source = sid
                    break
            if fired_at is None:
                missed += 1
            rendered_slots.append({
                "ability_id": slot_aid,
                "expected_role": slot.get("expected_role"),
                "fired": fired_at is not None,
                "fired_at_ts": fired_at,
                "fired_source_id": fired_source,
            })

        casts_out.append({
            "cast_ts": cast_ts,
            "ability_id": cast["ability_id"],
            "occurrence": cast["occurrence"],
            "mechanic_ref": ref,
            "planned_slots": rendered_slots,
            "missed_count": missed,
            "no_plan": plan is None,
        })

    return {"fight_id": fight_id, "raidwide_casts": casts_out}


def mit_audit_aggregate_for_encounter(
    session: Session, encounter_id: int, static_id: int,
    *, version: int = 1,
) -> dict[str, Any]:
    """Aggregate T-303 mit audits across all watched fights for an encounter (v1.9.0).

    Powers the consumer Home "How mit usage is going" section: which mits
    are dropping the most, which raidwide mechanics are taking the most
    unmitigated hits.

    Returns:
      {
        "encounter_id": int,
        "fights_aggregated": int,
        "raidwide_casts": int,           # total raidwide occurrences seen
        "planned_slots_total": int,      # sum of planned mits across all casts
        "missed_mits_total": int,
        "mit_hit_rate": float | None,    # planned-and-fired / planned, 0..1
        "worst_mits": [                  # sorted by miss_rate desc
          {"ability_id": int, "ability_name": str | None,
           "planned": int, "missed": int, "miss_rate": float}
        ],
        "worst_mechanics": [             # sorted by missed desc
          {"ability_id": int, "ability_name": str | None,
           "occurrences": int, "planned_slots": int, "missed": int,
           "miss_rate": float | None}
        ]
      }

    Only fights from reports in the static's watchlist count — same scoping
    semantics as the v1.8.0 watchlist-scoped cartography.
    """
    # v1.17.0: union watchlist fights across the cloned-encounter group.
    group = encounter_id_group(encounter_id)
    canonical = canonical_encounter_id(encounter_id)
    fight_rows = session.execute(
        select(Fight.id).where(
            Fight.encounter_id.in_(group),
            Fight.report_code.in_(
                select(WatchedReport.code).where(
                    WatchedReport.static_id == static_id
                )
            ),
        )
    ).scalars().all()

    if not fight_rows:
        return {
            "encounter_id": canonical,
            "fights_aggregated": 0,
            "raidwide_casts": 0,
            "planned_slots_total": 0,
            "missed_mits_total": 0,
            "mit_hit_rate": None,
            "worst_mits": [],
            "worst_mechanics": [],
        }

    # Per-mit-ability aggregates: planned occurrences vs missed.
    mit_planned: dict[int, int] = defaultdict(int)
    mit_missed: dict[int, int] = defaultdict(int)
    # Per-raidwide-mechanic (keyed on raidwide ability_id since occurrence
    # varies per pull length): occurrences seen, planned slots, missed.
    mech_occ: dict[int, int] = defaultdict(int)
    mech_planned: dict[int, int] = defaultdict(int)
    mech_missed: dict[int, int] = defaultdict(int)

    raidwide_casts_total = 0
    planned_slots_total = 0
    missed_mits_total = 0
    fights_with_data = 0

    for fid in fight_rows:
        audit = mit_audit_for_fight(session, fid, static_id, version=version)
        casts = audit.get("raidwide_casts") or []
        if not casts:
            continue
        fights_with_data += 1
        for cast in casts:
            raidwide_casts_total += 1
            mech_aid = cast["ability_id"]
            mech_occ[mech_aid] += 1
            for slot in cast["planned_slots"]:
                aid = slot["ability_id"]
                mit_planned[aid] += 1
                mech_planned[mech_aid] += 1
                planned_slots_total += 1
                if not slot["fired"]:
                    mit_missed[aid] += 1
                    mech_missed[mech_aid] += 1
                    missed_mits_total += 1

    # Resolve ability names in one query (mits + mechanics).
    all_aids = set(mit_planned.keys()) | set(mech_occ.keys())
    name_lookup: dict[int, str | None] = {}
    if all_aids:
        name_rows = session.execute(
            select(Ability.ability_game_id, Ability.name)
            .where(Ability.ability_game_id.in_(all_aids))
        ).all()
        name_lookup = {aid: name for aid, name in name_rows}

    worst_mits = [
        {
            "ability_id": aid,
            "ability_name": name_lookup.get(aid),
            "planned": planned,
            "missed": mit_missed.get(aid, 0),
            "miss_rate": (
                round(mit_missed.get(aid, 0) / planned, 3)
                if planned > 0 else 0.0
            ),
        }
        for aid, planned in mit_planned.items()
    ]
    # Surface the highest-miss-rate mits, then highest miss-count to tiebreak
    # — so a mit missed 3/3 ranks above one missed 5/20.
    worst_mits.sort(key=lambda m: (-m["miss_rate"], -m["missed"]))

    worst_mechanics = [
        {
            "ability_id": aid,
            "ability_name": name_lookup.get(aid),
            "occurrences": occ,
            "planned_slots": mech_planned.get(aid, 0),
            "missed": mech_missed.get(aid, 0),
            "miss_rate": (
                round(mech_missed.get(aid, 0) / mech_planned.get(aid, 0), 3)
                if mech_planned.get(aid, 0) > 0 else None
            ),
        }
        for aid, occ in mech_occ.items()
    ]
    # Rank by absolute miss count — "5 missed mits on Cyclonic Break" matters
    # more than "100% miss rate on one occurrence of a rarely-hit mechanic".
    worst_mechanics.sort(key=lambda m: (-m["missed"], -m["occurrences"]))

    return {
        "encounter_id": canonical,
        "fights_aggregated": fights_with_data,
        "raidwide_casts": raidwide_casts_total,
        "planned_slots_total": planned_slots_total,
        "missed_mits_total": missed_mits_total,
        "mit_hit_rate": (
            round((planned_slots_total - missed_mits_total) / planned_slots_total, 3)
            if planned_slots_total > 0 else None
        ),
        "worst_mits": worst_mits,
        "worst_mechanics": worst_mechanics,
    }


def mit_audit_summary(session: Session, fight_id: int,
                      static_id: int) -> dict[str, Any]:
    """High-level counts: raidwides with a plan vs. without, total missed-mits."""
    audit = mit_audit_for_fight(session, fight_id, static_id)
    casts = audit["raidwide_casts"]
    with_plan = sum(1 for c in casts if not c["no_plan"])
    missing_plan = sum(1 for c in casts if c["no_plan"])
    total_missed = sum(c["missed_count"] for c in casts)
    total_slots = sum(len(c["planned_slots"]) for c in casts)
    return {
        "fight_id": fight_id,
        "raidwide_count": len(casts),
        "with_plan": with_plan,
        "missing_plan": missing_plan,
        "planned_slots_total": total_slots,
        "missed_mits_total": total_missed,
        "mit_hit_rate": (
            round((total_slots - total_missed) / total_slots, 3)
            if total_slots > 0 else None
        ),
    }
