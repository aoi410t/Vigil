"""T-205 prog-vs-field curve (PLAN §10 Compare).

Two pieces:
  1. **Our prog trajectory** — manual `ProgPoint` rows (from T-010) PLUS
     auto-derived points from any of our ingested Fight rows. A "fight row of
     ours" is one whose `report_code` appears in `watched_reports` (the user
     explicitly added it). Per session, the *best* (lowest `fight_percentage`
     = furthest into the fight) is the headline.
  2. **Field distribution** — the histogram of `fight_percentage` across
     all ingested *wipes* for the encounter that aren't in our watchlist.
     Powers "where most groups die" — backbone for `prog_status` later.

T-102 was nominally the "M-PARSE-less prog-point tracker (auto + manual)".
This module covers both halves: ProgPoint feeds the manual side, Fight rows
on watched reports feed the auto side, so T-102's AC ("furthest phase /
session over time; pulls + hours") falls out of this for free. No separate
T-102 ship — folded into T-205.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analysis._encounter import canonical_encounter_id, encounter_id_group
from db.models import Fight, ProgPoint, Report, WatchedReport


def _our_report_codes(session: Session, static_id: int) -> set[str]:
    """Codes in the current static's watchlist count as "ours". Anything
    else is field (other statics' watches OR field-backfill rows)."""
    return set(session.execute(
        select(WatchedReport.code).where(WatchedReport.static_id == static_id)
    ).scalars().all())


def prog_trajectory_for_encounter(
    session: Session, encounter_id: int, static_id: int,
) -> dict[str, Any]:
    """Per-encounter: our trajectory points (manual + auto) and field distribution.

    v1.17.0: unions Fight rows across the cloned-encounter group.
    """
    group = encounter_id_group(encounter_id)
    canonical = canonical_encounter_id(encounter_id)
    our_codes = _our_report_codes(session, static_id)

    # Manual points: prog_points scoped to this static.
    manual_rows = session.execute(
        select(ProgPoint).where(ProgPoint.source == "manual",
                                ProgPoint.static_id == static_id)
        .order_by(ProgPoint.ts)
    ).scalars().all()
    manual_points = [
        {
            "ts": p.ts.isoformat() if p.ts else None,
            "phase": p.phase,
            "fight_percentage": (float(p.fight_percentage)
                                 if p.fight_percentage is not None else None),
            "pull_count": p.pull_count,
            "source": "manual",
        }
        for p in manual_rows
    ]

    # Auto: any fight in a watched report for this encounter.
    # Fight.start_time is a millisecond OFFSET from Report.start_time
    # (FFLogs convention), not an absolute timestamp. Join Report to get the
    # absolute report-start datetime and resolve to a real wall-clock ms.
    fight_rows = []
    if our_codes:
        fight_rows = session.execute(
            select(Fight.report_code, Fight.start_time, Fight.last_phase,
                   Fight.fight_percentage, Fight.is_kill,
                   Report.start_time.label("report_start"))
            .join(Report, Report.code == Fight.report_code)
            .where(Fight.encounter_id.in_(group),
                   Fight.report_code.in_(our_codes))
            .order_by(Fight.start_time)
        ).all()

    # Per-session aggregation: pick best phase + best fight_percentage.
    # `first_ts_ms` is absolute wall-clock ms (report_start + fight_offset).
    session_buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"best_phase": None, "best_fp": None, "pulls": 0, "kills": 0,
                 "first_ts_ms": None}
    )
    for code, fight_offset_ms, last_phase, fp, is_kill, report_start in fight_rows:
        bucket = session_buckets[code]
        bucket["pulls"] += 1
        if is_kill:
            bucket["kills"] += 1
        if last_phase is not None:
            bucket["best_phase"] = max(bucket["best_phase"] or 0, last_phase)
        if fp is not None:
            cur = bucket["best_fp"]
            bucket["best_fp"] = float(fp) if cur is None else min(cur, float(fp))
        if bucket["first_ts_ms"] is None:
            # Real data: report_start (TIMESTAMPTZ) + fight offset (ms).
            # Test fixtures may omit report_start; fall back to the raw
            # offset so unit tests still see a value (chart will look
            # wrong on synthetic data, but real data is what matters).
            offset = int(fight_offset_ms) if fight_offset_ms else 0
            if report_start is not None:
                bucket["first_ts_ms"] = int(report_start.timestamp() * 1000) + offset
            else:
                bucket["first_ts_ms"] = offset

    auto_sessions = [
        {
            "report_code": code,
            "ts_ms": b["first_ts_ms"],
            "pulls": b["pulls"],
            "kills": b["kills"],
            "best_phase": b["best_phase"],
            "best_fight_percentage": b["best_fp"],
        }
        for code, b in session_buckets.items()
        if b["first_ts_ms"] is not None
    ]
    auto_sessions.sort(key=lambda r: r["ts_ms"])

    # Field distribution: every non-our-wipe for this encounter, binned by FP %
    field_rows = session.execute(
        select(Fight.fight_percentage, Fight.last_phase)
        .where(Fight.encounter_id.in_(group),
               Fight.is_kill.is_(False),
               Fight.fight_percentage.is_not(None),
               Fight.report_code.notin_(our_codes) if our_codes
               else Fight.report_code.is_not(None))
    ).all()

    # 10-percent buckets — for an Ultimate this is granular enough to show
    # where the wall is, without being noisy.
    field_hist: dict[int, int] = defaultdict(int)
    for fp, _phase in field_rows:
        fp_val = float(fp)
        bucket = min(int(fp_val // 10) * 10, 90)  # 0..90 buckets
        field_hist[bucket] += 1
    field_buckets = [
        {"fight_percentage_lo": b, "fight_percentage_hi": b + 10,
         "wipe_count": field_hist[b]}
        for b in sorted(field_hist)
    ]

    return {
        "encounter_id": canonical,
        "our_sessions": auto_sessions,
        "manual_points": manual_points,
        "field_wipes_total": len(field_rows),
        "field_buckets": field_buckets,
    }
