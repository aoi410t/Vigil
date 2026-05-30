"""M-CONS (T-306) consistency per mechanic.

For each canonical mechanic in `fight_model` for an encounter, compute the
clean-execution rate across our pulls: an occurrence is "clean" iff no
player death occurs within `MECHANIC_DEATH_WINDOW_MS` of the cast (default
5s — long enough to catch the kill from the mechanic's hit, short enough
not to bleed into the next mechanic).

The metric surfaces *which mechanics we keep dying to*, separate from M-CART
(which only sees ability IDs, not occurrence-level pass/fail). Useful for
weekly prog reports — "we cleared P4 Pandora's Knight x1 cleanly 8/15 pulls,
that's where to drill next."

Watchlist-scoped: aggregates only over `Fight` rows in `WatchedReport`s
(same "ours" semantics as T-205). Public field reports don't pollute.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analysis._encounter import canonical_encounter_id, encounter_id_group
from analysis.cartography import _active_players_by_fight
from db.models import Event, Fight, FightModel, WatchedReport

MECHANIC_DEATH_WINDOW_MS = 5_000


def _our_fight_ids(session: Session, encounter_id: int,
                   static_id: int) -> list[int]:
    # v1.17.0: union across the cloned-encounter group.
    return list(session.execute(
        select(Fight.id)
        .join(WatchedReport, WatchedReport.code == Fight.report_code)
        .where(Fight.encounter_id.in_(encounter_id_group(encounter_id)),
               WatchedReport.static_id == static_id)
    ).scalars().all())


def consistency_for_encounter(
    session: Session, encounter_id: int, static_id: int,
    *, version: int = 1,
    window_ms: int = MECHANIC_DEATH_WINDOW_MS,
) -> dict[str, Any]:
    """Per-canonical-mechanic clean-execution rate across our pulls."""
    canonical = canonical_encounter_id(encounter_id)
    fm_rows = session.execute(
        select(FightModel)
        .where(FightModel.encounter_id == canonical,
               FightModel.version == version)
        .order_by(FightModel.phase, FightModel.seq)
    ).scalars().all()
    if not fm_rows:
        return {"encounter_id": canonical, "our_pulls": 0,
                "mechanics": [], "note": "no fight_model rows"}

    our_fights = _our_fight_ids(session, encounter_id, static_id)
    if not our_fights:
        return {"encounter_id": canonical, "our_pulls": 0,
                "mechanics": [],
                "note": "no fights in our watched reports for this encounter"}

    active_lookup = _active_players_by_fight(session, our_fights)

    # All boss casts (any non-player source) on canonical ability_ids across our fights
    ability_ids = list({r.ability_game_id for r in fm_rows})
    cast_rows = session.execute(
        select(Event.fight_id, Event.ts, Event.ability_game_id)
        .where(Event.fight_id.in_(our_fights),
               Event.type == "cast",
               Event.ability_game_id.in_(ability_ids),
               Event.ts.is_not(None))
        .order_by(Event.fight_id, Event.ts)
    ).all()
    # Group: (fight_id, ability_id) → sorted [ts, ts, ...]
    casts_by_fp: dict[tuple[int, int], list[int]] = defaultdict(list)
    for fid, ts, aid in cast_rows:
        casts_by_fp[(fid, aid)].append(int(ts))

    # All player deaths
    death_rows = session.execute(
        select(Event.fight_id, Event.target_id, Event.ts)
        .where(Event.fight_id.in_(our_fights), Event.type == "death")
    ).all()
    deaths_by_fight: dict[int, list[int]] = defaultdict(list)
    for fid, tid, ts in death_rows:
        if tid in active_lookup.get(fid, set()) and ts is not None:
            deaths_by_fight[fid].append(int(ts))
    for fid in deaths_by_fight:
        deaths_by_fight[fid].sort()

    fm_by_aid: dict[int, FightModel] = {}
    for r in fm_rows:
        fm_by_aid.setdefault(r.ability_game_id, r)

    # Per mechanic (ability_id), iterate all (fight, cast_ts) and check
    # whether any player death falls within [ts - window/2, ts + window].
    out: list[dict[str, Any]] = []
    for aid in ability_ids:
        fm = fm_by_aid[aid]
        occurrences_total = 0
        occurrences_clean = 0
        deaths_in_window = 0
        for (fid, fp_aid), ts_list in casts_by_fp.items():
            if fp_aid != aid:
                continue
            fight_deaths = deaths_by_fight.get(fid, [])
            for cast_ts in ts_list:
                occurrences_total += 1
                lo, hi = cast_ts, cast_ts + window_ms
                killed = sum(1 for d in fight_deaths if lo <= d <= hi)
                if killed == 0:
                    occurrences_clean += 1
                else:
                    deaths_in_window += killed
        if occurrences_total == 0:
            continue
        out.append({
            "ability_game_id": aid,
            "phase": fm.phase,
            "seq": fm.seq,
            "type_label": fm.type_label,
            "occurrences_total": occurrences_total,
            "occurrences_clean": occurrences_clean,
            "clean_rate": round(occurrences_clean / occurrences_total, 3),
            "deaths_in_window": deaths_in_window,
        })

    # Sort: worst clean_rate first (most prog-relevant), tie-break by phase
    out.sort(key=lambda m: (m["clean_rate"], m["phase"]))
    return {
        "encounter_id": canonical,
        "our_pulls": len(our_fights),
        "mechanics": out,
    }
