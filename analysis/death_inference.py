"""v1.16.3: shared non-attributable death inference.

When FFLogs emits a death with `ability_game_id = None` and
`source_id = -1`, we try to identify the killing mechanic via:

1. **Cast proximity** — most recent enemy cast within INFER_LOOKBACK_MS
   (8s) whose `fight_model.type_label` is actionable. Fast + accurate
   when the cast is in our events table.

2. **Cactbot drift fallback** — predicted cactbot expected time + this
   pull's per-phase drift, matched to the death timestamp within
   INFER_CACTBOT_TOLERANCE_MS (±2.5s). Catches deaths to sub-cast VFX
   that don't surface as boss cast events.

Used by `fault_attribution.compute_fault_scores_for_fight` (per-death
classification) AND by `cartography.cartography_for_encounter`
(grouping non-attributable deaths under the inferred mechanic so the
"What's killing us" view shows real strat names instead of one giant
"non-attributable" bucket).

Inferences are ALWAYS flagged as guesses — the original `ability_id`
stays None in the persisted death record; `inferred_*` fields say
what we think killed them and whether it came from `cast_proximity` or
`cactbot_drift`.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analysis._encounter import canonical_encounter_id
from db.models import Combatant, Event, Fight, FightModel
from ingest.cactbot import ParsedTimeline, load_timeline_for_encounter

INFER_LOOKBACK_MS = 8_000
INFER_CACTBOT_TOLERANCE_MS = 2_500
INFER_ACTIONABLE_LABELS = ("raidwide", "aoe_party", "tankbuster", "enrage")


def infer_killer_from_cast_proximity(
    death_ts: int,
    enemy_casts: list[tuple[int, int]],
    label_of: dict[int, str],
) -> tuple[int | None, str | None]:
    """Most-recent enemy cast within INFER_LOOKBACK_MS before `death_ts`
    whose type_label is actionable. Returns (None, None) if no match.

    `enemy_casts`: sorted [(ability_id, cast_ts)] in ascending ts order.
    """
    best: tuple[int, str] | None = None
    for aid, ts in enemy_casts:
        if ts > death_ts:
            break
        if death_ts - ts > INFER_LOOKBACK_MS:
            continue
        label = label_of.get(aid)
        if label in INFER_ACTIONABLE_LABELS:
            best = (aid, label)
    return best if best else (None, None)


def infer_killer_from_cactbot_drift(
    death_ts: int,
    death_phase_idx: int,
    phase_start_ts: int,
    cactbot_entries: list[Any],
    phase_drift_ms: int,
    label_of: dict[int, str],
) -> tuple[int | None, str | None]:
    """Drift-adjusted cactbot lookup. For each cactbot entry in the death's
    phase, compute predicted actual_ts = `phase_start_ts +
    entry.phase_relative_t_s*1000 + phase_drift_ms`. Pick the entry whose
    predicted_ts is closest to death_ts within INFER_CACTBOT_TOLERANCE_MS
    and whose mapped ability has an actionable type_label."""
    best: tuple[int, str, int] | None = None
    for entry in cactbot_entries:
        expected_ms = int(entry.phase_relative_t_s * 1000)
        predicted_ts = phase_start_ts + expected_ms + phase_drift_ms
        delta = abs(death_ts - predicted_ts)
        if delta > INFER_CACTBOT_TOLERANCE_MS:
            continue
        for aid in entry.ability_ids:
            label = label_of.get(aid)
            if label in INFER_ACTIONABLE_LABELS:
                if best is None or delta < best[2]:
                    best = (aid, label, delta)
                break
    if best is None:
        return (None, None)
    return (best[0], best[1])


def build_phase_drift_map(timeline_diff_result: dict[str, Any]) -> dict[int, int]:
    """Per-phase median drift from `timeline_diff_for_fight` output."""
    out: dict[int, int] = {}
    for ph in timeline_diff_result.get("phases", []):
        idx = ph.get("phase_index")
        drift = ph.get("median_drift_ms")
        if idx is not None and drift is not None:
            out[int(idx)] = int(drift)
    return out


def boss_cast_events(session: Session, fight_id: int) -> list[tuple[int, int]]:
    """[(ability_game_id, ts)] for enemy-sourced cast events in this fight,
    sorted by ts ascending. Same heuristic as analysis/timeline_diff.py
    (filter out player source_ids via the Combatant table)."""
    players = set(session.execute(
        select(Combatant.player_id).where(Combatant.fight_id == fight_id)
    ).scalars().all())
    rows = session.execute(
        select(Event.ability_game_id, Event.ts, Event.source_id)
        .where(
            Event.fight_id == fight_id,
            Event.type == "cast",
            Event.ability_game_id.is_not(None),
            Event.ts.is_not(None),
        )
        .order_by(Event.ts)
    ).all()
    out: list[tuple[int, int]] = []
    for aid, ts, src in rows:
        if aid is None or ts is None:
            continue
        if src is not None and src in players:
            continue
        out.append((int(aid), int(ts)))
    return out


def build_inference_context(
    session: Session, fight_id: int, encounter_id: int,
    *, version: int = 1,
) -> dict[str, Any]:
    """Pre-load everything needed to run the two-layer inference for one
    fight: boss casts, label_of, cactbot timeline + drift map + phase
    boundaries. Returns a context dict callers pass to `infer_killer`.

    All sub-loads are best-effort — missing data just means a layer
    returns no match.
    """
    # v1.17.0: fight_model lives at the canonical encounter ID.
    canonical = canonical_encounter_id(encounter_id)
    label_rows = session.execute(
        select(FightModel.ability_game_id, FightModel.type_label)
        .where(FightModel.encounter_id == canonical,
               FightModel.version == version)
    ).all()
    label_of: dict[int, str] = {aid: lbl for aid, lbl in label_rows if lbl}

    enemy_casts = boss_cast_events(session, fight_id)

    cactbot_timeline: ParsedTimeline | None = None
    phase_drift_map: dict[int, int] = {}
    phase_boundaries: list[dict[str, Any]] = []
    cactbot_entries_by_phase: dict[int, list[Any]] = defaultdict(list)
    try:
        from analysis.phases import detect_phase_boundaries
        from analysis.timeline_diff import timeline_diff_for_fight
        cactbot_timeline = load_timeline_for_encounter(canonical)
        if cactbot_timeline is not None:
            tdiff = timeline_diff_for_fight(session, fight_id, version=version)
            phase_drift_map = build_phase_drift_map(tdiff)
            phase_boundaries = detect_phase_boundaries(
                session, fight_id,
            )["phases"]
            for e in cactbot_timeline.entries:
                cactbot_entries_by_phase[e.phase_index].append(e)
    except Exception:
        pass

    return {
        "label_of": label_of,
        "enemy_casts": enemy_casts,
        "cactbot_timeline": cactbot_timeline,
        "phase_drift_map": phase_drift_map,
        "phase_boundaries": phase_boundaries,
        "cactbot_entries_by_phase": dict(cactbot_entries_by_phase),
    }


def infer_killer(ctx: dict[str, Any],
                  death_ts: int) -> tuple[int | None, str | None, str | None]:
    """Two-layer inference for one death. Returns
    (inferred_ability_id, inferred_label, inferred_from)
    where inferred_from is "cast_proximity" | "cactbot_drift" | None.
    """
    label_of = ctx["label_of"]
    enemy_casts = ctx["enemy_casts"]
    aid, label = infer_killer_from_cast_proximity(
        death_ts, enemy_casts, label_of,
    )
    if aid is not None:
        return (aid, label, "cast_proximity")

    cactbot_timeline = ctx.get("cactbot_timeline")
    phase_boundaries = ctx.get("phase_boundaries") or []
    if cactbot_timeline is None or not phase_boundaries:
        return (None, None, None)

    ph_idx: int | None = None
    ph_start: int | None = None
    for ph in phase_boundaries:
        if ph["start_ts"] <= death_ts <= ph["end_ts"]:
            ph_idx = int(ph["index"])
            ph_start = int(ph["start_ts"])
            break
    if ph_idx is None or ph_start is None:
        return (None, None, None)

    cactbot_entries_by_phase = ctx.get("cactbot_entries_by_phase") or {}
    drift = ctx.get("phase_drift_map", {}).get(ph_idx, 0)
    from analysis.timeline_diff import _align_phases
    cactbot_phase_count = (max(cactbot_entries_by_phase) + 1
                            if cactbot_entries_by_phase else 0)
    phase_alignment = _align_phases(len(phase_boundaries),
                                     cactbot_phase_count)
    cb_phase = phase_alignment.get(ph_idx, ph_idx)
    cb_entries = cactbot_entries_by_phase.get(cb_phase, [])
    aid, label = infer_killer_from_cactbot_drift(
        death_ts, ph_idx, ph_start, cb_entries, drift, label_of,
    )
    if aid is not None:
        return (aid, label, "cactbot_drift")
    return (None, None, None)
