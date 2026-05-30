"""M-GATE (T-207): DPS-gated vs mechanics-gated verdict, per fight per phase.

For one of our pulls (typically a wipe but works on kills too), for each
phase:
  - look at our **raid DPS** versus the encounter's empirical distribution
    (T-204 `compare_fight_to_target`)
  - look at our **deaths** within the phase's timestamp window

Verdict per phase:
  - `dps_gated`        — DPS below p25 AND ≤1 death
  - `mechanics_gated`  — DPS fine (between/above p25) AND ≥2 deaths
  - `both_gated`       — DPS below p25 AND ≥2 deaths
  - `not_gated`        — DPS fine AND ≤1 death (cleared cleanly)
  - `no_target`        — encounter has no empirical DPS distribution yet
                          (fewer than 3 ingested kills); falls back to a
                          death-only verdict (`many_deaths` / `clean`)

The verdict tells the static where to focus: DPS uptime/optimization on a
`dps_gated` phase vs. mechanic execution on a `mechanics_gated` phase.
Mode-2 enrichment (T-302 M-FAULT) refines the death attribution but the
gate verdict itself is Mode-1.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analysis.cartography import _active_players_by_fight
from analysis.dps_check import compare_fight_to_target
from analysis.phases import detect_phase_boundaries
from db.models import Event, Fight

DEATHS_FOR_MECHANICS_GATE = 2


def gate_diagnostic_for_fight(
    session: Session, fight_id: int,
) -> dict[str, Any]:
    """Per-phase gated/not-gated verdict for one fight."""
    f = session.get(Fight, fight_id)
    if f is None:
        return {"fight_id": fight_id, "phases": [], "note": "fight not found"}

    dps_result = compare_fight_to_target(session, fight_id)
    dps_by_phase = {p["phase_index"]: p for p in dps_result["phases"]}

    # v1.17.0: surface the canonical encounter ID so the UI dedupe lines up.
    from analysis._encounter import canonical_encounter_id
    canonical_enc = canonical_encounter_id(f.encounter_id)
    phases = detect_phase_boundaries(session, fight_id)["phases"]
    if not phases:
        return {"fight_id": fight_id, "encounter_id": canonical_enc,
                "phases": [], "note": "no phases detected"}

    active = _active_players_by_fight(session, [fight_id]).get(fight_id, set())

    death_rows = session.execute(
        select(Event.target_id, Event.ts, Event.ability_game_id)
        .where(Event.fight_id == fight_id, Event.type == "death")
    ).all()

    out = []
    for p in phases:
        start, end = p["start_ts"], p["end_ts"]
        deaths_in_phase = [
            (target_id, ts, aid)
            for target_id, ts, aid in death_rows
            if target_id in active and ts is not None and start <= ts <= end
        ]
        dps_info = dps_by_phase.get(p["index"], {})
        dps_status = dps_info.get("verdict", "no_target")

        dps_low = (dps_status == "below_p25")
        many_deaths = len(deaths_in_phase) >= DEATHS_FOR_MECHANICS_GATE

        if dps_status == "no_target":
            verdict = "many_deaths" if many_deaths else "clean"
        elif dps_low and many_deaths:
            verdict = "both_gated"
        elif dps_low:
            verdict = "dps_gated"
        elif many_deaths:
            verdict = "mechanics_gated"
        else:
            verdict = "not_gated"

        out.append({
            "phase_index": p["index"],
            "dps_status": dps_status,
            "raid_dps": dps_info.get("raid_dps"),
            "target": dps_info.get("target"),
            "deaths": len(deaths_in_phase),
            "verdict": verdict,
        })

    return {
        "fight_id": fight_id,
        "encounter_id": canonical_enc,
        "kills_in_target": dps_result.get("kills_in_target", 0),
        "phases": out,
    }
