"""T-205 prog trajectory tests + live AC."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete

from analysis.prog_trajectory import prog_trajectory_for_encounter
from db.models import (
    Fight, IngestionLedger, ProgPoint, Report, WatchedReport,
)
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)

ENC = 87654321
OUR_CODES = ("T205_OUR_1", "T205_OUR_2")
FIELD_CODES = ("T205_FIELD_1", "T205_FIELD_2")


@pytest.fixture
def seeded():
    """Seed: 2 watched (ours) + 2 unwatched (field) reports, each with some
    wipes at known fight_percentages."""
    inserted_points: list[int] = []
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        # Watched (ours)
        for code in OUR_CODES:
            s.add(Report(code=code, ingested_at=now))
        s.flush()
        for code in OUR_CODES:
            s.add(WatchedReport(static_id=1, code=code, active=True, added_at=now))
        # Field
        for code in FIELD_CODES:
            s.add(Report(code=code, ingested_at=now))
        s.flush()
        # Our session 1: 5 wipes, best at fp=20
        s.add(Fight(report_code="T205_OUR_1", fight_id_in_report=1,
                    encounter_id=ENC, is_kill=False,
                    fight_percentage=20.0, last_phase=4,
                    start_time=1_000_000, end_time=1_500_000))
        # Our session 2: 3 wipes, best at fp=5
        s.add(Fight(report_code="T205_OUR_2", fight_id_in_report=1,
                    encounter_id=ENC, is_kill=False,
                    fight_percentage=5.0, last_phase=5,
                    start_time=2_000_000, end_time=2_500_000))
        # Field reports
        s.add(Fight(report_code="T205_FIELD_1", fight_id_in_report=1,
                    encounter_id=ENC, is_kill=False,
                    fight_percentage=55.0, last_phase=2,
                    start_time=3_000_000, end_time=3_500_000))
        s.add(Fight(report_code="T205_FIELD_2", fight_id_in_report=1,
                    encounter_id=ENC, is_kill=False,
                    fight_percentage=15.0, last_phase=4,
                    start_time=4_000_000, end_time=4_500_000))
        # A manual prog point
        p = ProgPoint(static_id=1, ts=now, phase=3, fight_percentage=30.0,
                      pull_count=10, source="manual")
        s.add(p)
        s.flush()
        inserted_points.append(p.id)
        s.commit()
        try:
            yield s
        finally:
            s.execute(delete(Fight).where(
                Fight.report_code.in_(OUR_CODES + FIELD_CODES)))
            s.execute(delete(WatchedReport).where(
                WatchedReport.code.in_(OUR_CODES)))
            s.execute(delete(IngestionLedger).where(
                IngestionLedger.report_code.in_(OUR_CODES + FIELD_CODES)))
            s.execute(delete(Report).where(
                Report.code.in_(OUR_CODES + FIELD_CODES)))
            s.execute(delete(ProgPoint).where(ProgPoint.id.in_(inserted_points)))
            s.commit()


def test_our_sessions_aggregate_best_fp(seeded):
    session = seeded
    r = prog_trajectory_for_encounter(session, ENC, 1)
    by_code = {s["report_code"]: s for s in r["our_sessions"]}
    assert by_code["T205_OUR_1"]["best_fight_percentage"] == 20.0
    assert by_code["T205_OUR_2"]["best_fight_percentage"] == 5.0
    assert by_code["T205_OUR_2"]["best_phase"] == 5


def test_manual_points_surface(seeded):
    session = seeded
    r = prog_trajectory_for_encounter(session, ENC, 1)
    assert len(r["manual_points"]) == 1
    assert r["manual_points"][0]["fight_percentage"] == 30.0


def test_field_excludes_our_codes(seeded):
    """Field distribution must not include wipes from watched reports."""
    session = seeded
    r = prog_trajectory_for_encounter(session, ENC, 1)
    # We have 2 field wipes (T205_FIELD_1 + T205_FIELD_2). Ours wipes excluded.
    assert r["field_wipes_total"] == 2


def test_field_buckets_at_10pct_resolution(seeded):
    """Field wipes at fp=55 and fp=15 should land in 50 and 10 buckets."""
    session = seeded
    r = prog_trajectory_for_encounter(session, ENC, 1)
    bucket_los = {b["fight_percentage_lo"] for b in r["field_buckets"]}
    assert 50 in bucket_los
    assert 10 in bucket_los


def test_our_sessions_sorted_chronologically(seeded):
    session = seeded
    r = prog_trajectory_for_encounter(session, ENC, 1)
    ts_seq = [s["ts_ms"] for s in r["our_sessions"]]
    assert ts_seq == sorted(ts_seq)


def test_empty_encounter():
    with SessionLocal() as s:
        r = prog_trajectory_for_encounter(s, 999_999_998, 1)
    assert r["our_sessions"] == []
    assert r["field_wipes_total"] == 0
    # manual_points isn't scoped by encounter (T-010 has no encounter_id),
    # so it may carry rows from other tests in the DB. Just assert it's a list.
    assert isinstance(r["manual_points"], list)
