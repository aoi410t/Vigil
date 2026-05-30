"""T-204 empirical DPS check (PLAN §9 M-INFER #4).

For each phase in `fight_model`, compute the **per-phase raid-DPS distribution**
across every ingested kill of the encounter. The median is the empirical
"DPS target" for that phase — if a prog group's phase-X DPS is below it, the
gate is DPS, not mechanics. M-GATE (T-207) consumes this directly.

Boss HP is not available in stored events (FFLogs only emits damage deltas,
not HP percentages), so "HP at enrage" isn't a direct read. Instead this
computes the *raid DPS that historically completes each phase* using per-phase
total damage output over per-phase duration, then aggregates. That's an
equivalent gating signal in practice: groups clearing P5 must sustain at
least the median P5 raid DPS we've ever observed clearing it.
"""
from __future__ import annotations

from statistics import median, quantiles
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analysis._encounter import canonical_encounter_id, encounter_id_group
from analysis.parse_trajectory import parse_per_phase_for_fight
from db.models import Fight, WatchedReport


def _quartiles(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"p25": None, "p50": None, "p75": None,
                "min": None, "max": None, "n": 0}
    if len(values) == 1:
        only = values[0]
        return {"p25": only, "p50": only, "p75": only,
                "min": only, "max": only, "n": 1}
    qs = quantiles(values, n=4, method="inclusive")
    return {
        "p25": round(qs[0], 1),
        "p50": round(median(values), 1),
        "p75": round(qs[2], 1),
        "min": round(min(values), 1),
        "max": round(max(values), 1),
        "n": len(values),
    }


def dps_check_for_encounter(
    session: Session, encounter_id: int,
) -> dict[str, Any]:
    """Per-phase raid-DPS distribution across all ingested kills.

    v1.17.0: unions kills across the canonical encounter group.
    """
    group = encounter_id_group(encounter_id)
    canonical = canonical_encounter_id(encounter_id)
    kill_ids = list(session.execute(
        select(Fight.id)
        .where(Fight.encounter_id.in_(group), Fight.is_kill.is_(True))
    ).scalars().all())
    if not kill_ids:
        return {"encounter_id": canonical, "kills_aggregated": 0,
                "phases": [], "note": "no kills ingested"}

    # phase_index → list of raid-DPS values, one per kill
    per_phase: dict[int, list[float]] = {}
    kills_with_phases = 0
    for fid in kill_ids:
        result = parse_per_phase_for_fight(session, fid)
        if not result["phases"]:
            continue
        kills_with_phases += 1
        for p in result["phases"]:
            total_damage = sum(pp["damage_total"] for pp in p["players"])
            duration_s = p["duration_ms"] / 1000 if p["duration_ms"] else 0
            if duration_s <= 0:
                continue
            raid_dps = total_damage / duration_s
            per_phase.setdefault(p["phase_index"], []).append(raid_dps)

    out_phases = []
    for phase_idx in sorted(per_phase):
        dist = _quartiles(per_phase[phase_idx])
        out_phases.append({
            "phase_index": phase_idx,
            "raid_dps": dist,
        })

    return {
        "encounter_id": canonical,
        "kills_aggregated": kills_with_phases,
        "phases": out_phases,
    }


def dps_comparison_for_encounter(
    session: Session, encounter_id: int, static_id: int,
    *, job: str | None = None,
) -> dict[str, Any]:
    """Per-phase DPS distribution split into 'ours' vs 'field' (v1.10.0).

    Without `job`: per-phase **raid DPS** (total damage / phase duration).
    Each kill contributes one value to its side's distribution.

    With `job`: per-phase **per-player DPS for that job**. Each player of
    the matching job in each kill contributes a value (so a kill with two
    SAMs adds two values). Lets a user see "our SAM is at the 30th
    percentile of SAMs clearing this fight" rather than only top-line raid
    DPS.

    Membership rule: a kill is "ours" iff its `report_code` is in the
    static's `watched_reports`. Everything else is field — same scoping as
    v1.8.0 cartography. Static with no kills yet → ours.kills_aggregated=0
    and the Home renders only the field distribution.

    `jobs_available` lists every job seen across all kills (ours + field)
    in this encounter — drives the Home dropdown without a second query.
    """
    # v1.17.0: union across the canonical encounter group.
    group = encounter_id_group(encounter_id)
    canonical = canonical_encounter_id(encounter_id)
    kill_rows = session.execute(
        select(Fight.id, Fight.report_code)
        .where(Fight.encounter_id.in_(group), Fight.is_kill.is_(True))
    ).all()
    if not kill_rows:
        return {
            "encounter_id": canonical,
            "job": job,
            "jobs_available": [],
            "ours": {"kills_aggregated": 0, "phases": []},
            "field": {"kills_aggregated": 0, "phases": []},
        }

    our_codes = set(session.execute(
        select(WatchedReport.code).where(WatchedReport.static_id == static_id)
    ).scalars().all())

    # Per side: phase_index → list[float]
    our_per_phase: dict[int, list[float]] = {}
    field_per_phase: dict[int, list[float]] = {}
    our_kills = 0
    field_kills = 0
    jobs_seen: set[str] = set()

    for fid, code in kill_rows:
        parsed = parse_per_phase_for_fight(session, fid)
        if not parsed["phases"]:
            continue
        is_ours = code in our_codes
        if is_ours:
            our_kills += 1
        else:
            field_kills += 1
        bucket = our_per_phase if is_ours else field_per_phase

        for p in parsed["phases"]:
            duration_s = p["duration_ms"] / 1000 if p["duration_ms"] else 0
            if duration_s <= 0:
                continue
            for pp in p["players"]:
                if pp.get("job"):
                    jobs_seen.add(pp["job"])
            if job is None:
                total = sum(pp["damage_total"] for pp in p["players"])
                bucket.setdefault(p["phase_index"], []).append(total / duration_s)
            else:
                for pp in p["players"]:
                    if pp.get("job") == job:
                        bucket.setdefault(p["phase_index"], []).append(
                            pp["damage_total"] / duration_s
                        )

    def _phases(per_phase: dict[int, list[float]]) -> list[dict[str, Any]]:
        return [
            {"phase_index": pi, "dps": _quartiles(per_phase[pi])}
            for pi in sorted(per_phase)
        ]

    return {
        "encounter_id": canonical,
        "job": job,
        "jobs_available": sorted(jobs_seen),
        "ours": {"kills_aggregated": our_kills, "phases": _phases(our_per_phase)},
        "field": {"kills_aggregated": field_kills,
                  "phases": _phases(field_per_phase)},
    }


def compare_fight_to_target(
    session: Session, fight_id: int,
) -> dict[str, Any]:
    """For one specific fight (prog or kill), compare each of its phases'
    raid DPS to the encounter's empirical distribution. Surfaces the per-phase
    verdict: `below_p25` / `between_p25_p75` / `above_p75` / `no_target`.
    M-GATE will key off this in T-207.
    """
    f = session.get(Fight, fight_id)
    if f is None:
        return {"fight_id": fight_id, "phases": [], "note": "fight not found"}

    target = dps_check_for_encounter(session, f.encounter_id)
    target_by_phase = {p["phase_index"]: p["raid_dps"] for p in target["phases"]}

    parse = parse_per_phase_for_fight(session, fight_id)
    out = []
    for p in parse["phases"]:
        total_damage = sum(pp["damage_total"] for pp in p["players"])
        duration_s = p["duration_ms"] / 1000 if p["duration_ms"] else 0
        raid_dps = round(total_damage / duration_s, 1) if duration_s > 0 else 0.0
        tgt = target_by_phase.get(p["phase_index"])
        if tgt is None or tgt["p50"] is None:
            verdict = "no_target"
        elif raid_dps < tgt["p25"]:
            verdict = "below_p25"
        elif raid_dps > tgt["p75"]:
            verdict = "above_p75"
        else:
            verdict = "between_p25_p75"
        out.append({
            "phase_index": p["phase_index"],
            "raid_dps": raid_dps,
            "target": tgt,
            "verdict": verdict,
        })

    return {
        "fight_id": fight_id,
        "encounter_id": canonical_encounter_id(f.encounter_id),
        "kills_in_target": target["kills_aggregated"],
        "phases": out,
    }
