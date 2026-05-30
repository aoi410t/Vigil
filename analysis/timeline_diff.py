"""Per-pull expected-vs-actual timeline diff (cactbot Stage 2 + 2.2 slot-driven).

For one fight, compare every actionable cactbot slot in this encounter against
when an ability matching that slot's ID set actually fired in the pull. Output
includes:
- `expected_t_ms` from cactbot (phase-relative).
- `actual_t_ms` from the pull's events (phase-relative, using T-103 phase
  segmentation for THIS fight).
- `drift_ms` = actual - expected.
- `fired: bool` — false when no cast in the slot's ID set fired in the
  pull's reachable phase window.
- `alternate_variant: bool` — true when this slot is one of several
  mutually-exclusive variants of the same mechanic and a sibling fired.
  Excluded from "missing" count and median drift.

**Slot-driven matching (v1.5.0):**
Cactbot writes `Ability { id: ["X", "Y"] }` for random-variant mechanics like
Sinsmoke/Sinsmite — at the same timestamp, exactly one of the listed IDs fires.
Prior versions matched per fight_model row (one row per ability_id), which
mis-attributed drift when the variant assignment differed across pulls. This
version iterates **cactbot slots** instead, where each slot may map to any of
its declared IDs.

**Variant collapsing (v1.5.3):**
Cactbot sometimes writes mutually-exclusive variants as *separate* slots —
e.g. r10s "Alley-oop Inferno" (B5C0) and "Alley-oop Inferno" (B5C1) at the
same expected time. Only one fires per pull. After the per-slot matching pass,
slots within ±1500ms in the same phase that share either a base label
(stripping parenthesized suffixes) or any ability ID are grouped as variants;
if any variant fired, the non-firing siblings are marked
`alternate_variant: true` and excluded from the missing-count + median drift.

Type-label filtering still comes from `fight_model` — a cactbot slot is
"actionable" iff at least one of its IDs has a non-cosmetic `type_label` in
our consensus, ensuring we only diff real mechanics.

Per-phase `median_drift_ms` summary surfaces cascade effects (e.g. "phase 1
ran 4s long → all P2 entries shifted +4s").
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analysis._assignment import min_cost_assignment
from analysis.phases import detect_phase_boundaries
from db.models import Combatant, Event, Fight, FightModel
from ingest.cactbot import ParsedTimeline, load_timeline_for_encounter

# Type labels we surface in the diff. Cosmetic excluded.
_DIFFABLE_TYPE_LABELS: tuple[str, ...] = (
    "raidwide", "tankbuster", "aoe_party", "enrage",
)

# Priority order for picking a "primary" type label per multi-ID slot.
_TYPE_PRIORITY: tuple[str, ...] = ("enrage", "tankbuster", "aoe_party", "raidwide")

# Variant clustering window. Two slots within this expected-time gap (in ms)
# can be mutually-exclusive variants if they also share a label prefix or any
# ability ID.
_VARIANT_TIME_WINDOW_MS = 1500

_PAREN_SUFFIX = re.compile(r"\s*\([^)]*\)\s*$")


def _strip_paren_suffix(label: str) -> str:
    """'Burnt Strike (fire)' -> 'Burnt Strike'. Used for variant detection."""
    return _PAREN_SUFFIX.sub("", label).strip()


def _align_phases(fight_phase_count: int, cactbot_phase_count: int) -> dict[int, int]:
    """Map each fight phase index to a cactbot phase index.

    T-103's per-pull phase segmentation doesn't always agree with cactbot's
    authored phase list (e.g. M9S has 2 fight phases per T-103, 1 per cactbot;
    other encounters can have the opposite). Strict equality on `phase_index`
    causes unmatched fight phases to draw zero cactbot entries.

    Strategy — proportional interpolation: fight phase `f` (0..N-1) maps to
    cactbot phase `round(f * (M-1) / (N-1))`. When either side has only one
    phase, all of the other side maps to index 0. Handles N==M as identity.
    """
    if fight_phase_count <= 0 or cactbot_phase_count <= 0:
        return {}
    if fight_phase_count == 1 or cactbot_phase_count == 1:
        return {f: 0 if cactbot_phase_count == 1 else min(f, cactbot_phase_count - 1)
                for f in range(fight_phase_count)}
    return {
        f: round(f * (cactbot_phase_count - 1) / (fight_phase_count - 1))
        for f in range(fight_phase_count)
    }


def _player_ids(session: Session, fight_id: int) -> set[int]:
    return set(session.execute(
        select(Combatant.player_id).where(Combatant.fight_id == fight_id)
    ).scalars().all())


def _boss_cast_events(session: Session, fight_id: int) -> list[tuple[int, int]]:
    """Return [(ability_game_id, ts)] for enemy-sourced cast events in this fight."""
    players = _player_ids(session, fight_id)
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


def _empty(fight_id: int, encounter_id: int | None, note: str) -> dict[str, Any]:
    return {"fight_id": fight_id, "encounter_id": encounter_id,
            "phases": [], "note": note}


def timeline_diff_for_fight(
    session: Session,
    fight_id: int,
    *,
    version: int = 1,
    _timeline: ParsedTimeline | None = None,
) -> dict[str, Any]:
    """Compute the cactbot expected-vs-actual diff for one pull.

    Args:
        session: SQLAlchemy session.
        fight_id: fight PK.
        version: fight_model version (default 1).
        _timeline: injection point for tests; if None, load via
            `ingest.cactbot.load_timeline_for_encounter`.
    """
    fight = session.get(Fight, fight_id)
    if fight is None:
        return _empty(fight_id, None, "fight not found")

    # v1.17.0: cactbot timeline + fight_model live at the canonical ID.
    from analysis._encounter import canonical_encounter_id
    encounter_id = canonical_encounter_id(fight.encounter_id)
    phase_info = detect_phase_boundaries(session, fight_id)
    fight_phases = phase_info["phases"]
    if not fight_phases:
        return _empty(fight_id, encounter_id, "no phases detected for this fight")

    timeline = _timeline if _timeline is not None else load_timeline_for_encounter(encounter_id)
    if timeline is None or not timeline.entries:
        return _empty(fight_id, encounter_id, "no cactbot timeline for encounter")

    # fight_model type-label lookup. A cactbot slot is "actionable" iff at
    # least one of its declared IDs has a non-cosmetic type_label here.
    fm_rows = session.execute(
        select(FightModel).where(
            FightModel.encounter_id == encounter_id,
            FightModel.version == version,
        )
    ).scalars().all()
    if not fm_rows:
        return _empty(fight_id, encounter_id, "no fight_model persisted for encounter")
    type_by_ability: dict[int, str] = {}
    for r in fm_rows:
        if r.type_label is not None:
            type_by_ability[r.ability_game_id] = r.type_label

    # Boss cast events, indexed for fast lookup
    casts = _boss_cast_events(session, fight_id)

    # Cactbot entries grouped by phase
    cactbot_by_phase: dict[int, list[Any]] = {}
    for e in timeline.entries:
        cactbot_by_phase.setdefault(e.phase_index, []).append(e)

    # Phase alignment: T-103 phase count vs cactbot phase count may differ
    # (e.g. M9S = 2 fight phases / 1 cactbot phase). Map proportionally so
    # every fight phase pulls entries from *some* cactbot phase.
    cactbot_phase_count = (max(cactbot_by_phase) + 1) if cactbot_by_phase else 0
    phase_alignment = _align_phases(len(fight_phases), cactbot_phase_count)

    # Track which cactbot phases never get pulled by an aligned fight phase;
    # fold their entries into the nearest fight phase so we don't drop them.
    aligned_cactbot_phases = set(phase_alignment.values())
    unaligned_extras: dict[int, list[Any]] = {}
    for cb_phase, entries_ in cactbot_by_phase.items():
        if cb_phase in aligned_cactbot_phases:
            continue
        if not phase_alignment:
            continue
        # Pick the fight phase whose aligned cactbot phase is closest to cb_phase
        nearest_fight_phase = min(
            phase_alignment.keys(),
            key=lambda f: abs(phase_alignment[f] - cb_phase),
        )
        unaligned_extras.setdefault(nearest_fight_phase, []).extend(entries_)

    out_phases: list[dict[str, Any]] = []
    for fight_phase in fight_phases:
        phase_idx = fight_phase["index"]
        ph_start = fight_phase["start_ts"]
        ph_end = fight_phase["end_ts"]
        ph_dur_ms = ph_end - ph_start

        cactbot_phase_idx = phase_alignment.get(phase_idx, phase_idx)
        cactbot_entries = list(cactbot_by_phase.get(cactbot_phase_idx, []))
        cactbot_entries.extend(unaligned_extras.get(phase_idx, []))

        # All cactbot timeline-body slots are diffable. Cactbot's act of listing
        # an entry in the timeline body IS the curation signal — we don't second-
        # guess it with our `cosmetic` classifier (that's a damage-signature
        # heuristic; cactbot e.g. labels "Sinbound Blizzard III" by its cast ID
        # even though the damage comes from a follow-up sub-cast we'd mark as
        # cosmetic).
        actionable: list[tuple[Any, str | None, int]] = []  # (entry, primary_type, primary_aid)
        for entry in cactbot_entries:
            slot_types = {type_by_ability.get(aid) for aid in entry.ability_ids}
            slot_types.discard(None)
            # Primary type for display: prefer real-mechanic types over cosmetic
            real_types = slot_types & set(_DIFFABLE_TYPE_LABELS)
            if real_types:
                primary_type = next(
                    (t for t in _TYPE_PRIORITY if t in real_types),
                    next(iter(real_types)),
                )
            elif slot_types:
                # All matched fight_model rows are cosmetic-typed; still include
                # the slot, just label it cosmetic for the UI.
                primary_type = "cosmetic"
            else:
                # No fight_model row for any of the slot's IDs — unknown to us.
                primary_type = None
            primary_aid = next(
                (aid for aid in entry.ability_ids if aid in type_by_ability),
                entry.ability_ids[0],
            )
            actionable.append((entry, primary_type, primary_aid))

        # Build the cast pool for this phase.
        cast_pool: list[tuple[int, int]] = []  # (ability_id, ts)
        for aid, ts in casts:
            if ph_start <= ts <= ph_end:
                cast_pool.append((aid, ts))

        # Sort actionable slots in expected-time order for output stability.
        actionable.sort(key=lambda x: x[0].phase_relative_t_s)

        # Build cost matrix for Hungarian assignment:
        # rows = slots, cols = casts. cost[i][j] = |actual_t - expected_t| in ms
        # iff cast j's ability is in slot i's id set; else SKIP_PENALTY.
        # SKIP_PENALTY = 10x phase duration ensures forbidden matches are never
        # preferred over leaving a slot unmatched.
        skip_penalty = max(10 * ph_dur_ms, 60 * 60 * 1000)
        cost_matrix: list[list[float]] = []
        for entry, _ptype, _paid in actionable:
            expected_ms = int(entry.phase_relative_t_s * 1000)
            slot_ids = set(entry.ability_ids)
            row: list[float] = []
            for aid, ts in cast_pool:
                actual_ms = ts - ph_start
                if aid in slot_ids:
                    row.append(float(abs(actual_ms - expected_ms)))
                else:
                    row.append(float(skip_penalty))
            cost_matrix.append(row)

        if cost_matrix and cast_pool:
            slot_to_cast = min_cost_assignment(cost_matrix, skip_penalty=float(skip_penalty))
        else:
            slot_to_cast = [-1] * len(actionable)

        entries: list[dict[str, Any]] = []
        entry_slot_ids: list[set[int]] = []
        for i, (entry, primary_type, primary_aid) in enumerate(actionable):
            expected_ms = int(entry.phase_relative_t_s * 1000)
            slot_ids = set(entry.ability_ids)
            cast_idx = slot_to_cast[i] if i < len(slot_to_cast) else -1
            if cast_idx < 0:
                entries.append({
                    "cactbot_label": entry.label,
                    "ability_game_id": primary_aid,
                    "type_label": primary_type,
                    "expected_t_ms": expected_ms,
                    "consensus_t_ms": None,
                    "actual_t_ms": None,
                    "drift_ms": None,
                    "fired": False,
                    "alternate_variant": False,
                })
                entry_slot_ids.append(slot_ids)
                continue
            cast_aid, cast_ts = cast_pool[cast_idx]
            actual_ms = cast_ts - ph_start
            entries.append({
                "cactbot_label": entry.label,
                "ability_game_id": cast_aid,
                "type_label": primary_type,
                "expected_t_ms": expected_ms,
                "consensus_t_ms": None,
                "actual_t_ms": actual_ms,
                "drift_ms": actual_ms - expected_ms,
                "fired": True,
                "alternate_variant": False,
            })
            entry_slot_ids.append(slot_ids)

        # Variant collapsing — for each non-firing entry, check whether a sibling
        # within ±VARIANT_TIME_WINDOW_MS in the same phase fired AND shares either
        # a base label (stripping parenthesized suffix) or any ability ID. If so,
        # this entry is one of N mutually-exclusive variants and we mark it as
        # such instead of treating it as a true miss.
        for i, e in enumerate(entries):
            if e["fired"]:
                continue
            base_i = _strip_paren_suffix(e["cactbot_label"])
            for j, other in enumerate(entries):
                if i == j or not other["fired"]:
                    continue
                if abs(e["expected_t_ms"] - other["expected_t_ms"]) > _VARIANT_TIME_WINDOW_MS:
                    continue
                base_j = _strip_paren_suffix(other["cactbot_label"])
                shares_label = base_i and base_i == base_j
                shares_id = bool(entry_slot_ids[i] & entry_slot_ids[j])
                if shares_label or shares_id:
                    e["alternate_variant"] = True
                    break

        drifts = [e["drift_ms"] for e in entries
                  if e["fired"] and e["drift_ms"] is not None]
        if drifts:
            drifts_sorted = sorted(drifts)
            median_drift = drifts_sorted[len(drifts_sorted) // 2]
        else:
            median_drift = None

        # Phase label: pick from the first cactbot entry in this phase if any
        phase_label = cactbot_entries[0].phase_label if cactbot_entries else None

        out_phases.append({
            "phase_index": phase_idx,
            "phase_label": phase_label,
            "duration_ms": ph_dur_ms,
            "median_drift_ms": median_drift,
            "entries_total": len(entries),
            "entries_fired": sum(1 for e in entries if e["fired"]),
            "entries_missing": sum(
                1 for e in entries if not e["fired"] and not e["alternate_variant"]
            ),
            "entries_alternate": sum(1 for e in entries if e["alternate_variant"]),
            "entries": entries,
        })

    return {
        "fight_id": fight_id,
        "encounter_id": encounter_id,
        "phases": out_phases,
    }
