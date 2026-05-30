"""T-203 mechanic classification — label canonical abilities by effect signature.

For each row in `fight_model`, scan the actual damage events the ability
produced across ingested kills of the encounter and emit a label:

  - `raidwide`      — hits ≥ N_RAIDWIDE players simultaneously (default 6/8)
  - `tankbuster`    — hits exactly 1 player with damage in tank-buster range
  - `aoe_party`     — hits 2..5 players (spread/stack candidates)
  - `dot`           — damage-over-time signature (multiple ticks on same target)
  - `enrage`        — last canonical ability in its phase + ≥5 player deaths
                      within 3s of cast
  - `cosmetic`      — no damage events found (purely visual / boss-only casts)
  - `unknown`       — heuristic couldn't decide

Writes back to `fight_model.type_label`. Per PLAN §3 Invariant 3 this stays
boss-side: we measure *outcomes* on players, not strat assignments.

The "active players" filter (which `combatants` rows count as real players)
is intentionally stricter than the raw `combatants` table because Ultimate
reports leak NPCs through masterData — a row counts only if its actor cast
something during the fight.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analysis._encounter import canonical_encounter_id, encounter_id_group
from db.models import Combatant, Event, Fight, FightModel

# Tunables.
RAIDWIDE_MIN_TARGETS = 6
SAME_CAST_WINDOW_MS = 1500
ENRAGE_DEATH_WINDOW_MS = 3000
ENRAGE_MIN_DEATHS = 5


def _active_players(session: Session, fight_id: int) -> set[int]:
    """Players that actually acted in this fight — source of ≥1 cast or damage.

    The `combatants` table can over-include NPCs on Ultimate reports because
    FFLogs masterData lists every actor; this filter scopes to the slice
    that's verifiably playing.
    """
    rows = session.execute(
        select(Event.source_id)
        .where(
            Event.fight_id == fight_id,
            Event.source_id.is_not(None),
            Event.type.in_(("cast", "damage", "calculateddamage")),
        )
        .distinct()
    ).scalars().all()
    combatant_ids = set(session.execute(
        select(Combatant.player_id).where(Combatant.fight_id == fight_id)
    ).scalars().all())
    return set(rows) & combatant_ids


def _kill_fight_ids(session: Session, encounter_id: int) -> list[int]:
    # v1.17.0: union across the cloned-encounter group.
    return list(session.execute(
        select(Fight.id)
        .where(Fight.encounter_id.in_(encounter_id_group(encounter_id)),
               Fight.is_kill.is_(True))
    ).scalars().all())


def _ability_damage_signature(
    session: Session, fight_ids: list[int], ability_id: int,
) -> dict[str, Any]:
    """Aggregate damage targets per cast of `ability_id` across kill fights.

    Returns `{cast_count, max_targets, mean_targets, mean_amount_per_target,
    total_damage_events, tick_count}` aggregated across all input fights.
    """
    if not fight_ids:
        return {"cast_count": 0, "max_targets": 0, "mean_targets": 0.0,
                "mean_amount_per_target": 0.0, "total_damage_events": 0}

    rows = session.execute(
        select(Event.fight_id, Event.ts, Event.target_id, Event.amount)
        .where(
            Event.fight_id.in_(fight_ids),
            Event.ability_game_id == ability_id,
            Event.type.in_(("damage", "calculateddamage")),
            Event.target_id.is_not(None),
        )
        .order_by(Event.fight_id, Event.ts)
    ).all()

    if not rows:
        return {"cast_count": 0, "max_targets": 0, "mean_targets": 0.0,
                "mean_amount_per_target": 0.0, "total_damage_events": 0}

    # Bucket consecutive same-fight rows within SAME_CAST_WINDOW_MS into one cast.
    casts: list[dict[str, Any]] = []
    last_fid = None
    for fid, ts, tid, amt in rows:
        if (not casts or fid != last_fid
                or ts - casts[-1]["last_ts"] > SAME_CAST_WINDOW_MS):
            casts.append({"fight_id": fid, "first_ts": ts, "last_ts": ts,
                          "targets": set(), "amount_sum": 0,
                          "events": 0})
        cur = casts[-1]
        cur["targets"].add(tid)
        cur["amount_sum"] += int(amt or 0)
        cur["events"] += 1
        cur["last_ts"] = ts
        last_fid = fid

    cast_count = len(casts)
    max_targets = max(len(c["targets"]) for c in casts)
    mean_targets = sum(len(c["targets"]) for c in casts) / cast_count
    total_amount = sum(c["amount_sum"] for c in casts)
    total_events = sum(c["events"] for c in casts)
    mean_amount_per_target = (
        total_amount / sum(len(c["targets"]) for c in casts)
        if total_amount > 0 else 0.0
    )
    return {
        "cast_count": cast_count,
        "max_targets": int(max_targets),
        "mean_targets": round(mean_targets, 2),
        "mean_amount_per_target": int(mean_amount_per_target),
        "total_damage_events": total_events,
    }


def _has_enrage_signature(
    session: Session, fight_ids: list[int], ability_id: int,
) -> bool:
    """An ability is enrage-like if, within ENRAGE_DEATH_WINDOW_MS of its last
    cast in each kill fight, ≥ENRAGE_MIN_DEATHS player deaths land."""
    rows = session.execute(
        select(Event.fight_id, Event.ts)
        .where(
            Event.fight_id.in_(fight_ids),
            Event.ability_game_id == ability_id,
            Event.type == "cast",
        )
        .order_by(Event.fight_id, Event.ts)
    ).all()
    if not rows:
        return False

    last_cast_by_fight: dict[int, int] = {}
    for fid, ts in rows:
        last_cast_by_fight[fid] = max(last_cast_by_fight.get(fid, 0), int(ts))

    fights_with_enrage = 0
    for fid, last_ts in last_cast_by_fight.items():
        active = _active_players(session, fid)
        if not active:
            continue
        deaths = session.execute(
            select(Event.target_id)
            .where(
                Event.fight_id == fid,
                Event.type == "death",
                Event.ts.between(last_ts, last_ts + ENRAGE_DEATH_WINDOW_MS),
                Event.target_id.in_(active),
            )
        ).all()
        if len(deaths) >= ENRAGE_MIN_DEATHS:
            fights_with_enrage += 1

    return fights_with_enrage / len(last_cast_by_fight) >= 0.5


def _label(signature: dict[str, Any], enrage_like: bool) -> str:
    if signature["cast_count"] == 0:
        return "cosmetic"
    if enrage_like:
        return "enrage"
    max_t = signature["max_targets"]
    mean_t = signature["mean_targets"]
    if max_t >= RAIDWIDE_MIN_TARGETS:
        return "raidwide"
    if max_t == 1 and signature["mean_amount_per_target"] >= 50_000:
        # Tankbuster heuristic — single target, high amount. Threshold is
        # rough but matches Ultimate-tier hits where regular auto-attacks
        # land for ~5-15k.
        return "tankbuster"
    if 2 <= mean_t <= 5:
        return "aoe_party"
    return "unknown"


def classify_canonical_abilities(
    session: Session, encounter_id: int, *, version: int = 1,
) -> dict[str, Any]:
    """Walk `fight_model` rows for one encounter; write `type_label` based on
    each canonical ability's damage signature across ingested kill events.

    v1.17.0: reads fight_model at the canonical ID, scans kill events across
    the full cloned group.
    """
    canonical = canonical_encounter_id(encounter_id)
    rows = session.execute(
        select(FightModel)
        .where(FightModel.encounter_id == canonical,
               FightModel.version == version)
        .order_by(FightModel.phase, FightModel.seq)
    ).scalars().all()
    if not rows:
        return {"encounter_id": canonical, "version": version,
                "labeled": 0, "note": "no fight_model rows"}

    fight_ids = _kill_fight_ids(session, encounter_id)
    label_counts: dict[str, int] = defaultdict(int)
    now = datetime.now(timezone.utc)

    # For enrage, only check the last ability in each phase (cheap optimization).
    last_seq_per_phase: dict[int, int] = {}
    for r in rows:
        last_seq_per_phase[r.phase] = max(last_seq_per_phase.get(r.phase, -1), r.seq)

    for r in rows:
        sig = _ability_damage_signature(session, fight_ids, r.ability_game_id)
        is_last_in_phase = (r.seq == last_seq_per_phase.get(r.phase))
        enrage_like = (is_last_in_phase
                       and _has_enrage_signature(session, fight_ids, r.ability_game_id))
        label = _label(sig, enrage_like)
        r.type_label = label
        r.updated_at = now
        meta = dict(r.meta or {})
        meta["signature"] = sig
        r.meta = meta
        label_counts[label] += 1

    session.commit()
    return {
        "encounter_id": canonical,
        "version": version,
        "labeled": len(rows),
        "label_counts": dict(label_counts),
    }
