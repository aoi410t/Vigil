"""M-CART (T-206): boss-ability-keyed failure cartography (PLAN §9).

For one encounter, walk every ingested fight (kill + wipe) and aggregate
deaths by **killing boss ability**. Cross-references against `fight_model`
to attach phase + mechanic label per ability.

Output answers: which boss abilities kill the most players? And of those, are
they universally fatal (high field-wide kill count → "universal wall") or
specific to us (high count in *our* fights only → "you-problem")?
This is the data backbone for T-208's failure heatmap.

Boss-side only (PLAN Invariant 4) — we key on boss ability IDs, never on
player position or strat decisions.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from sqlalchemy import select

# Parse cactbot_phase_label values like 'P1', 'P12'. Phase-name strings
# like 'Adds' don't match — caller falls through to other phase sources.
_CACTBOT_PHASE_RE = re.compile(r"^P(\d+)$")
from sqlalchemy.orm import Session

from analysis._encounter import canonical_encounter_id, encounter_id_group
from db.models import Ability, Combatant, Event, Fight, FightModel, WatchedReport


def _active_players_by_fight(session: Session, fight_ids: list[int]) -> dict[int, set[int]]:
    """Stricter player set per fight (same heuristic as T-203) — avoids NPCs
    leaking through the masterData filter on Ultimate reports."""
    if not fight_ids:
        return {}
    cast_rows = session.execute(
        select(Event.fight_id, Event.source_id)
        .where(
            Event.fight_id.in_(fight_ids),
            Event.source_id.is_not(None),
            Event.type.in_(("cast", "damage", "calculateddamage")),
        )
        .distinct()
    ).all()
    by_fight_acted: dict[int, set[int]] = defaultdict(set)
    for fid, sid in cast_rows:
        by_fight_acted[fid].add(sid)

    combatant_rows = session.execute(
        select(Combatant.fight_id, Combatant.player_id)
        .where(Combatant.fight_id.in_(fight_ids))
    ).all()
    by_fight_combatants: dict[int, set[int]] = defaultdict(set)
    for fid, pid in combatant_rows:
        by_fight_combatants[fid].add(pid)

    return {
        fid: by_fight_acted.get(fid, set()) & by_fight_combatants.get(fid, set())
        for fid in fight_ids
    }


def cartography_for_encounter(
    session: Session, encounter_id: int, *,
    version: int = 1, static_id: int | None = None,
) -> dict[str, Any]:
    """Aggregate deaths by killing boss ability for one encounter.

    Returns:
      {
        "encounter_id": int,
        "total_fights": int,
        "total_wipes": int,
        "total_kills": int,
        "total_deaths": int,
        "buckets": [
          {"ability_game_id": int, "ability_name": str | None,
           "deaths": int, "fights_affected": int,
           "fight_model_phase": int | None,
           "fight_model_label": str | None,
           "non_attributable": bool}
        ]
      }
    Sorted by death count desc.

    When `static_id` is set, only fights from reports in that static's
    watchlist are aggregated — the consumer-side "what are *we* wiping to"
    view (v1.8.0). When unset (default), all ingested fights count —
    the legacy field-wide view (T-206 / T-208 Compare tab).
    """
    # v1.17.0: union across the canonical encounter group (e.g. DSR 1065+1076).
    group = encounter_id_group(encounter_id)
    canonical = canonical_encounter_id(encounter_id)
    fights_q = select(Fight.id, Fight.is_kill, Fight.last_phase).where(
        Fight.encounter_id.in_(group)
    )
    if static_id is not None:
        fights_q = fights_q.where(
            Fight.report_code.in_(
                select(WatchedReport.code).where(
                    WatchedReport.static_id == static_id
                )
            )
        )
    fights = session.execute(fights_q).all()
    if not fights:
        return {
            "encounter_id": canonical,
            "total_fights": 0, "total_wipes": 0, "total_kills": 0,
            "total_deaths": 0, "buckets": [],
            "wipes_by_phase": {},
        }

    fight_ids = [fid for fid, _, _ in fights]
    kill_count = sum(1 for _, k, _ in fights if k is True)
    wipe_count = sum(1 for _, k, _ in fights if k is False)
    # v1.16.5: per-phase wipe count for the UI tabs ("how many wipes
    # ended in P3"). Keys are int phase indices; unlabeled wipes
    # collapse into 'unknown'.
    wipes_by_phase: dict[Any, int] = defaultdict(int)
    last_phase_by_fight: dict[int, int | None] = {}
    for fid, is_kill, last_phase in fights:
        last_phase_by_fight[fid] = last_phase
        if is_kill:
            continue
        wipes_by_phase[last_phase if last_phase is not None
                       else "unknown"] += 1

    active_by_fight = _active_players_by_fight(session, fight_ids)

    death_rows = session.execute(
        select(Event.fight_id, Event.target_id, Event.ability_game_id,
               Event.ts)
        .where(Event.fight_id.in_(fight_ids), Event.type == "death")
    ).all()

    # v1.16.3: for non-attributable deaths (ability_game_id IS NULL),
    # infer the killing mechanic via cast proximity + cactbot drift. The
    # inference context is per-fight and lazy-loaded only for fights that
    # actually have non-attributable deaths.
    # v1.16.5: ALSO use the per-fight phase_boundaries to determine each
    # death's actual phase via T-103 — so we can recover phase info for
    # abilities whose fight_model row has no `phase` set ("Unknown" phase
    # bucket in the UI).
    from analysis import death_inference as di
    inference_ctx_by_fight: dict[int, dict[str, Any] | None] = {}
    inferred_counts_per_ability: dict[int, int] = defaultdict(int)

    def _ensure_ctx(fid: int) -> dict[str, Any] | None:
        if fid not in inference_ctx_by_fight:
            try:
                inference_ctx_by_fight[fid] = di.build_inference_context(
                    session, fid, canonical, version=version,
                )
            except Exception:
                inference_ctx_by_fight[fid] = None
        return inference_ctx_by_fight[fid]

    def _phase_for_death(fid: int, ts: int) -> int | None:
        """T-103 phase containing this death's timestamp in THIS pull.
        Returns a 1-indexed phase (P1 = 1) to match FFLogs Fight.last_phase
        and cactbot's labeling — T-103 itself is 0-indexed so we +1.
        Returns None when no boundaries are available or out-of-range."""
        ctx = _ensure_ctx(fid)
        if ctx is None:
            return None
        for ph in ctx.get("phase_boundaries", ()) or ():
            if ph["start_ts"] <= ts <= ph["end_ts"]:
                return int(ph["index"]) + 1  # 0-indexed → 1-indexed
        return None

    # ability_game_id (or None for non-attributable) → death-and-fight tallies.
    deaths_by_ability: dict[int | None, int] = defaultdict(int)
    fights_by_ability: dict[int | None, set[int]] = defaultdict(set)
    # v1.16.5: per-ability per-phase death distribution from T-103 boundaries.
    deaths_by_ability_phase: dict[int | None, dict[Any, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    total_deaths = 0
    for fid, target_id, ability_id, ts in death_rows:
        active = active_by_fight.get(fid) or set()
        if target_id is None or target_id not in active:
            continue
        total_deaths += 1
        if ability_id is not None:
            resolved_aid = ability_id
        else:
            # Non-attributable — try to infer the killing ability
            ctx = _ensure_ctx(fid)
            inferred_aid = None
            if ctx is not None and ts is not None:
                inferred_aid, _label, _src = di.infer_killer(ctx, int(ts))
            if inferred_aid is not None:
                resolved_aid = inferred_aid
                inferred_counts_per_ability[resolved_aid] += 1
            else:
                resolved_aid = None  # truly non-attributable
        deaths_by_ability[resolved_aid] += 1
        fights_by_ability[resolved_aid].add(fid)
        # Per-phase tally: ALWAYS look up the T-103 phase for this death
        # so the per-phase tab counts are accurate (independent of
        # fight_model_phase which is per-ability not per-event).
        ph = _phase_for_death(fid, int(ts)) if ts is not None else None
        deaths_by_ability_phase[resolved_aid][ph if ph is not None
                                              else "unknown"] += 1

    # Join with fight_model + abilities for labels/names
    ability_ids = [aid for aid in deaths_by_ability if aid is not None]
    fm_lookup: dict[int, FightModel] = {}
    if ability_ids:
        fm_rows = session.execute(
            select(FightModel)
            .where(FightModel.encounter_id == canonical,
                   FightModel.version == version,
                   FightModel.ability_game_id.in_(ability_ids))
        ).scalars().all()
        for r in fm_rows:
            fm_lookup.setdefault(r.ability_game_id, r)

    name_lookup: dict[int, str | None] = {}
    if ability_ids:
        ab_rows = session.execute(
            select(Ability.ability_game_id, Ability.name)
            .where(Ability.ability_game_id.in_(ability_ids))
        ).all()
        name_lookup = {aid: name for aid, name in ab_rows}

    buckets = []
    for aid, count in sorted(deaths_by_ability.items(),
                             key=lambda kv: -kv[1]):
        deaths_phase_map = dict(deaths_by_ability_phase.get(aid, {}))
        if aid is None:
            buckets.append({
                "ability_game_id": None,
                "ability_name": None,
                "cactbot_label": None,
                "deaths": count,
                "fights_affected": len(fights_by_ability[aid]),
                "fight_model_phase": None,
                "fight_model_label": None,
                "non_attributable": True,
                "inferred_deaths": 0,
                "phase_inferred_deaths": 0,
                "deaths_by_phase": deaths_phase_map,
                "phase": None,
                "phase_source": "unknown",
            })
            continue
        fm = fm_lookup.get(aid)
        # All output phase numbers are 1-INDEXED (P1, P2, ...) to match
        # FFLogs Fight.last_phase and cactbot's labeling — what users
        # see in-game. fight_model.phase is 0-indexed in the DB so we
        # +1 when promoting it to a user-facing phase. Raw value is
        # preserved in `fight_model_phase` for back-compat.
        fight_model_phase_raw = fm.phase if fm else None  # 0-indexed
        fight_model_phase_1 = (
            (fight_model_phase_raw + 1) if fight_model_phase_raw is not None
            else None
        )
        cactbot_phase: int | None = None
        if fm and fm.cactbot_phase_label:
            m = _CACTBOT_PHASE_RE.match(fm.cactbot_phase_label)
            if m:
                cactbot_phase = int(m.group(1))  # already 1-indexed
        inferred = inferred_counts_per_ability.get(aid, 0)
        # Phase-resolution priority (v1.16.5):
        #   1. T-103 inference (per-pull, accurate when events ingested)
        #   2. cactbot_phase_label (per-ability annotation)
        #   3. fight_model_phase ≥ 2 (raw value ≥ 1; trust real-looking
        #      consensus, distrust the bulk-tagged 0/P1 default)
        #   4. None → "Unknown" tab
        primary_phase: int | None = None
        phase_source = "unknown"
        phase_inferred_deaths = 0
        known_phase_entries = [
            (ph, cnt) for ph, cnt in deaths_phase_map.items()
            if isinstance(ph, int)
        ]
        if known_phase_entries:
            primary_phase = max(known_phase_entries,
                                 key=lambda kv: kv[1])[0]
            phase_inferred_deaths = sum(cnt for _, cnt in known_phase_entries)
            # Don't mark as a guess when T-103 agrees with cactbot or
            # fight_model (consensus already there).
            agreed = (
                (cactbot_phase is not None and primary_phase == cactbot_phase)
                or (fight_model_phase_1 is not None
                    and primary_phase == fight_model_phase_1)
            )
            if agreed:
                phase_source = "fight_model"
                phase_inferred_deaths = 0
            else:
                phase_source = "inferred"
        elif cactbot_phase is not None:
            primary_phase = cactbot_phase
            if fight_model_phase_1 == cactbot_phase:
                phase_source = "fight_model"
            else:
                phase_source = "inferred"
                phase_inferred_deaths = count
        elif fight_model_phase_1 is not None and fight_model_phase_1 >= 2:
            # raw ≥ 1 → real-looking consensus, not the bulk-tagged default
            primary_phase = fight_model_phase_1
            phase_source = "fight_model"
        elif fight_model_phase_1 == 1:
            # raw = 0; could be "actually phase 1" or "bulk-tagged default".
            # Without inference to override, accept it but flag as inferred
            # since the data quality is uncertain.
            primary_phase = 1
            phase_source = "fight_model"
        fight_model_phase = fight_model_phase_raw
        buckets.append({
            "ability_game_id": aid,
            "ability_name": name_lookup.get(aid),
            "cactbot_label": fm.cactbot_label if fm else None,
            "deaths": count,
            "fights_affected": len(fights_by_ability[aid]),
            "fight_model_phase": fight_model_phase,
            "fight_model_label": fm.type_label if fm else None,
            "non_attributable": False,
            "inferred_deaths": inferred,
            # v1.16.5
            "phase": primary_phase,  # what the UI buckets under
            "phase_source": phase_source,  # 'fight_model' | 'inferred' | 'unknown'
            "phase_inferred_deaths": phase_inferred_deaths,
            "deaths_by_phase": deaths_phase_map,
        })

    return {
        "encounter_id": canonical,
        "total_fights": len(fights),
        "total_wipes": wipe_count,
        "total_kills": kill_count,
        "total_deaths": total_deaths,
        "wipes_by_phase": dict(wipes_by_phase),
        "buckets": buckets,
    }
