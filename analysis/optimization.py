"""T-308 post-clear optimization mode.

Once an encounter has kills (PLAN Invariant 6: parses are kill-only), shift
from "are we wiping?" to "how do we shave the kill time?" — per-kill,
per-player rollup of the levers that matter:

  - **burst_in_window_pct** (from T-105 M-BURST) — fraction of personal CDs
    fired inside a raid-buff window.
  - **gcd_drops_per_min** (from T-008 M-GCD) — dropped GCDs normalized to
    fight duration; lower = better.
  - **dps_vs_target_median** (from T-204 M-PARSE check) — per-player raid DPS
    contribution relative to the median raid DPS for that phase. (Approximated
    as `fight_raid_dps / target_p50` — the player's slice scales with this.)

Each metric becomes a 0..1 score (higher = better); the composite
`polish_score` is the geometric mean. Surfaces a leaderboard of "biggest gap
to top performers" per kill — the optimization targets.

Watchlist-scoped per the same rule as T-205/T-306.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analysis._encounter import canonical_encounter_id, encounter_id_group
from analysis.burst import burst_alignment_for_report
from analysis.dps_check import compare_fight_to_target
from analysis.gcd import mode1_gcd_for_report
from db.models import Fight, Report, WatchedReport


def _our_kill_fights(session: Session,
                      encounter_id: int,
                      static_id: int) -> list[Fight]:
    # v1.17.0: union across the cloned-encounter group.
    return list(session.execute(
        select(Fight)
        .join(WatchedReport, WatchedReport.code == Fight.report_code)
        .where(Fight.encounter_id.in_(encounter_id_group(encounter_id)),
               Fight.is_kill.is_(True),
               WatchedReport.static_id == static_id)
        .order_by(Fight.start_time)
    ).scalars().all())


def _per_kill_burst(session: Session, fight: Fight) -> dict[int, float]:
    """player_id → in_window_pct, for this kill only."""
    out: dict[int, float] = {}
    report_data = burst_alignment_for_report(session, fight.report_code)
    for f in report_data.get("fights", []):
        if f["fight_id"] != fight.id:
            continue
        for p in f["players"]:
            out[p["player_id"]] = float(p["in_window_pct"])
    return out


def _per_kill_gcd(session: Session, fight: Fight) -> dict[int, dict[str, Any]]:
    """player_id → {drops_per_min}."""
    out: dict[int, dict[str, Any]] = {}
    report_data = mode1_gcd_for_report(session, fight.report_code)
    dur_min = (fight.duration_ms or 0) / 60_000
    for f in report_data.get("fights", []):
        if f["fight_id"] != fight.id:
            continue
        for p in f["players"]:
            dropped = p.get("dropped_count", 0)
            out[p["player_id"]] = {
                "dropped_count": dropped,
                "drops_per_min": (dropped / dur_min) if dur_min > 0 else 0.0,
            }
    return out


def _fight_raid_dps_vs_target(session: Session, fight: Fight) -> float | None:
    """Average ratio across phases of fight_raid_dps / target_p50."""
    cmp = compare_fight_to_target(session, fight.id)
    ratios = []
    for p in cmp.get("phases", []):
        tgt = (p.get("target") or {}).get("p50")
        rdps = p.get("raid_dps")
        if tgt and tgt > 0 and rdps:
            ratios.append(rdps / tgt)
    return sum(ratios) / len(ratios) if ratios else None


def _score_burst(pct: float) -> float:
    """0..1 score from burst alignment %. ≥0.85 → 1.0."""
    return min(1.0, pct / 0.85)


def _score_gcd(drops_per_min: float) -> float:
    """0..1 score from drops/min. 0 → 1.0; 6+/min → 0.0 (Pict-tier movement)."""
    if drops_per_min <= 0:
        return 1.0
    return max(0.0, 1.0 - drops_per_min / 6.0)


def _score_dps(ratio: float | None) -> float | None:
    """0..1 from fight raid-DPS / target_p50 ratio. ≥1.0 → 1.0; ≤0.8 → 0.0."""
    if ratio is None:
        return None
    if ratio >= 1.0:
        return 1.0
    return max(0.0, (ratio - 0.8) / 0.2)


def _composite(*scores: float | None) -> float | None:
    """Geometric mean of available (non-None) scores. Penalizes one-bad-metric."""
    vals = [s for s in scores if s is not None]
    if not vals:
        return None
    product = 1.0
    for v in vals:
        # Floor at 0.01 so a single zero doesn't collapse the score to 0
        product *= max(0.01, v)
    return round(product ** (1 / len(vals)), 3)


def post_clear_targets_for_encounter(
    session: Session, encounter_id: int, static_id: int,
) -> dict[str, Any]:
    """For each kill of this encounter (in the static's watchlist), score
    each player on burst/GCD/DPS levers. Surfaces the biggest polish gaps."""
    canonical = canonical_encounter_id(encounter_id)
    kills = _our_kill_fights(session, encounter_id, static_id)
    if not kills:
        return {"encounter_id": canonical, "kills": 0,
                "note": "no watched-report kills for this encounter"}

    out_kills = []
    for fight in kills:
        burst = _per_kill_burst(session, fight)
        gcd = _per_kill_gcd(session, fight)
        dps_ratio = _fight_raid_dps_vs_target(session, fight)
        dps_score = _score_dps(dps_ratio)

        pid_set = set(burst) | set(gcd)
        players = []
        for pid in pid_set:
            burst_pct = burst.get(pid)
            gcd_info = gcd.get(pid) or {}
            drops_per_min = gcd_info.get("drops_per_min", 0)
            s_burst = _score_burst(burst_pct) if burst_pct is not None else None
            s_gcd = _score_gcd(drops_per_min)
            polish = _composite(s_burst, s_gcd, dps_score)
            players.append({
                "player_id": pid,
                "burst_in_window_pct": burst_pct,
                "gcd_drops_per_min": round(drops_per_min, 2),
                "dps_vs_target_ratio": (round(dps_ratio, 3)
                                        if dps_ratio is not None else None),
                "score_burst": (round(s_burst, 3)
                                if s_burst is not None else None),
                "score_gcd": round(s_gcd, 3),
                "score_dps": (round(dps_score, 3)
                              if dps_score is not None else None),
                "polish_score": polish,
            })
        players.sort(key=lambda p: (p["polish_score"] or 0))  # worst first
        out_kills.append({
            "fight_id": fight.id,
            "fight_id_in_report": fight.fight_id_in_report,
            "report_code": fight.report_code,
            "duration_ms": fight.duration_ms,
            "raid_dps_vs_target_ratio": (round(dps_ratio, 3)
                                          if dps_ratio is not None else None),
            "players": players,
        })

    return {
        "encounter_id": canonical,
        "kills": len(kills),
        "fights": out_kills,
    }
