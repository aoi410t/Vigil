"""T-104: cross-pull consensus timeline (boss-side only).

For one encounter, look at the boss casts in every ingested kill (or any pull
that reaches the phase) and find the abilities that **recur at low time-
variance** across most pulls. Those are the canonical boss timeline — the
deterministic, crowd-mappable structure PLAN §3 Invariant 3 says is the only
thing we ever cross-compare.

Algorithm (PLAN §9 M-INFER #2):
  1. Per pull, run T-103 phase segmentation.
  2. Per phase, collect each boss cast as `(ability_game_id, t - phase_start)`.
     Player casts are filtered out via the `combatants` table.
  3. Cluster casts by `ability_game_id` within each phase. For each ability:
       - occurrence_rate = pulls_with_at_least_one_cast / total_pulls_reaching_phase
       - median(relative_t) and an IQR-style variance proxy
       - confidence = occurrence_rate * (variance_score)
  4. Canonical iff `occurrence_rate >= consensus_threshold` (default 0.70 per PLAN).

This module is purely read-side; persisting to the `fight_model` table is
deliberately separate so the analyzer can be re-run with different
thresholds without thrashing rows. (Persistence will become a
`write_to_fight_model()` helper when a downstream module needs it.)
"""
from __future__ import annotations

from collections import defaultdict
from statistics import median
from typing import Any

from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from analysis._encounter import canonical_encounter_id, encounter_id_group
from analysis.phases import detect_phase_boundaries
from db.models import Combatant, Event, Fight, FightModel

DEFAULT_CONSENSUS_THRESHOLD = 0.70


def _player_ids(session: Session, fight_id: int) -> set[int]:
    return set(session.execute(
        select(Combatant.player_id).where(Combatant.fight_id == fight_id)
    ).scalars().all())


def _boss_casts_per_phase(
    session: Session, fight_id: int,
) -> list[list[tuple[int, int]]]:
    """Returns one list per phase: `[(ability_game_id, relative_ts), …]`."""
    phases = detect_phase_boundaries(session, fight_id)["phases"]
    if not phases:
        return []

    players = _player_ids(session, fight_id)
    cast_rows = session.execute(
        select(Event.source_id, Event.ts, Event.ability_game_id)
        .where(
            Event.fight_id == fight_id,
            Event.type == "cast",
            Event.source_id.is_not(None),
            Event.ts.is_not(None),
            Event.ability_game_id.is_not(None),
        )
        .order_by(Event.ts)
    ).all()

    per_phase: list[list[tuple[int, int]]] = [[] for _ in phases]
    for sid, ts, aid in cast_rows:
        if sid in players:
            continue
        for i, p in enumerate(phases):
            if p["start_ts"] <= ts <= p["end_ts"]:
                per_phase[i].append((int(aid), int(ts) - int(p["start_ts"])))
                break
    return per_phase


def consensus_timeline_for_encounter(
    session: Session,
    encounter_id: int,
    *,
    consensus_threshold: float = DEFAULT_CONSENSUS_THRESHOLD,
    min_pulls: int = 3,
) -> dict[str, Any]:
    """Build the consensus boss-side timeline for one encounter.

    Returns:
      {
        "encounter_id": int,
        "consensus_threshold": float,
        "total_pulls": int,
        "phases": [
          {
            "phase_index": int,
            "pulls_reaching": int,
            "canonical_abilities": [
              {"ability_game_id": int, "occurrence_rate": float,
               "median_relative_t_ms": int, "variance_ms": int,
               "sample_count": int}
            ],
            "all_abilities": [...same shape, including sub-threshold...]
          }
        ]
      }
    """
    # v1.17.0: union across the canonical encounter group so cloned IDs
    # (e.g. DSR 1065 + 1076) feed the same consensus.
    group = encounter_id_group(encounter_id)
    canonical = canonical_encounter_id(encounter_id)
    fight_ids = session.execute(
        select(Fight.id)
        .where(Fight.encounter_id.in_(group), Fight.is_kill.is_not(None))
    ).scalars().all()

    if len(fight_ids) < min_pulls:
        return {
            "encounter_id": canonical,
            "consensus_threshold": consensus_threshold,
            "total_pulls": len(fight_ids),
            "phases": [],
            "note": f"need ≥{min_pulls} ingested pulls; have {len(fight_ids)}",
        }

    # phase_index → ability_game_id → list of relative_t per pull-that-reached-phase
    per_phase_per_ability: dict[int, dict[int, list[int]]] = defaultdict(
        lambda: defaultdict(list)
    )
    pulls_reaching: dict[int, int] = defaultdict(int)
    total_with_events = 0

    for fid in fight_ids:
        phase_casts = _boss_casts_per_phase(session, fid)
        if not phase_casts:
            continue  # fight has no events ingested
        total_with_events += 1
        for phase_idx, casts in enumerate(phase_casts):
            pulls_reaching[phase_idx] += 1
            seen_in_phase: dict[int, int] = {}
            for aid, rel_t in casts:
                # First-occurrence in this phase only — recurring casts within
                # a single phase (Akh Morn x4) should count once per pull.
                if aid not in seen_in_phase:
                    seen_in_phase[aid] = rel_t
                    per_phase_per_ability[phase_idx][aid].append(rel_t)

    if total_with_events < min_pulls:
        return {
            "encounter_id": canonical,
            "consensus_threshold": consensus_threshold,
            "total_pulls": total_with_events,
            "phases": [],
            "note": f"need ≥{min_pulls} pulls with events; have {total_with_events}",
        }

    out_phases = []
    for phase_idx in sorted(pulls_reaching):
        reached = pulls_reaching[phase_idx]
        all_abilities = []
        canonical_abilities = []
        for aid, times in per_phase_per_ability[phase_idx].items():
            rate = len(times) / reached
            med = int(median(times))
            # Variance proxy: half-IQR (robust against single outliers).
            sorted_t = sorted(times)
            n = len(sorted_t)
            if n >= 4:
                q1 = sorted_t[n // 4]
                q3 = sorted_t[(3 * n) // 4]
                variance_ms = (q3 - q1) // 2
            elif n >= 2:
                variance_ms = (max(sorted_t) - min(sorted_t)) // 2
            else:
                variance_ms = 0

            row = {
                "ability_game_id": aid,
                "occurrence_rate": round(rate, 3),
                "median_relative_t_ms": med,
                "variance_ms": variance_ms,
                "sample_count": len(times),
            }
            all_abilities.append(row)
            if rate >= consensus_threshold:
                canonical_abilities.append(row)

        canonical_abilities.sort(key=lambda r: r["median_relative_t_ms"])
        all_abilities.sort(key=lambda r: r["median_relative_t_ms"])
        out_phases.append({
            "phase_index": phase_idx,
            "pulls_reaching": reached,
            "canonical_abilities": canonical_abilities,
            "all_abilities": all_abilities,
        })

    return {
        "encounter_id": canonical,
        "consensus_threshold": consensus_threshold,
        "total_pulls": total_with_events,
        "phases": out_phases,
    }


def write_consensus_to_fight_model(
    session: Session,
    encounter_id: int,
    *,
    consensus_threshold: float = DEFAULT_CONSENSUS_THRESHOLD,
    version: int = 1,
) -> dict[str, Any]:
    """T-202: persist canonical consensus into `fight_model`.

    Replaces rows for `(encounter_id, version)` atomically. `seq` is assigned
    by sorting canonical abilities by median relative-t within each phase.
    `confidence` = occurrence_rate. `type_label` is left null — T-203 will
    populate (raidwide / tankbuster / tower / spread / stack / tether / enrage).

    Returns a small summary: `{encounter_id, version, phases_written,
    abilities_written, total_pulls}`.
    """
    # v1.17.0: fight_model is keyed on the canonical ID — one consensus model
    # per cloned group, regardless of which alias the caller passed in.
    canonical = canonical_encounter_id(encounter_id)
    result = consensus_timeline_for_encounter(
        session, encounter_id, consensus_threshold=consensus_threshold,
    )
    if not result["phases"]:
        return {
            "encounter_id": canonical,
            "version": version,
            "phases_written": 0,
            "abilities_written": 0,
            "total_pulls": result.get("total_pulls", 0),
            "note": result.get("note"),
        }

    session.execute(
        delete(FightModel).where(
            FightModel.encounter_id == canonical,
            FightModel.version == version,
        )
    )
    session.flush()

    now = datetime.now(timezone.utc)
    abilities_written = 0
    for phase in result["phases"]:
        for seq, ab in enumerate(phase["canonical_abilities"]):
            session.add(FightModel(
                encounter_id=canonical,
                version=version,
                phase=phase["phase_index"],
                seq=seq,
                ability_game_id=ab["ability_game_id"],
                relative_t_ms=ab["median_relative_t_ms"],
                time_variance_ms=ab["variance_ms"],
                type_label=None,
                confidence=ab["occurrence_rate"],
                meta={"sample_count": ab["sample_count"],
                      "pulls_reaching": phase["pulls_reaching"]},
                updated_at=now,
            ))
            abilities_written += 1

    session.commit()
    return {
        "encounter_id": canonical,
        "version": version,
        "phases_written": len(result["phases"]),
        "abilities_written": abilities_written,
        "total_pulls": result["total_pulls"],
    }


def read_fight_model(
    session: Session, encounter_id: int, *, version: int = 1,
) -> dict[str, Any]:
    """Read persisted boss-side timeline rows for one encounter+version.

    v1.17.0: reads at the canonical ID so cloned encounters share one model.
    """
    canonical = canonical_encounter_id(encounter_id)
    rows = session.execute(
        select(FightModel)
        .where(FightModel.encounter_id == canonical,
               FightModel.version == version)
        .order_by(FightModel.phase, FightModel.seq)
    ).scalars().all()

    by_phase: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        by_phase.setdefault(r.phase, []).append({
            "seq": r.seq,
            "ability_game_id": r.ability_game_id,
            "relative_t_ms": r.relative_t_ms,
            "time_variance_ms": r.time_variance_ms,
            "type_label": r.type_label,
            "confidence": float(r.confidence) if r.confidence is not None else None,
            "meta": r.meta,
            "cactbot_label": r.cactbot_label,
            "cactbot_phase_label": r.cactbot_phase_label,
            "cactbot_expected_t_ms": r.cactbot_expected_t_ms,
        })
    return {
        "encounter_id": canonical,
        "version": version,
        "phases": [{"phase": p, "abilities": abs_} for p, abs_ in sorted(by_phase.items())],
    }
