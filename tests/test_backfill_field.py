"""T-201 backfill tests with mocked FFLogsClient."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import delete

from db.models import Fight, IngestionLedger, Report
from db.session import SessionLocal, engine
from jobs.backfill_field import (
    DEFAULT_EVENTS_TOP_N,
    backfill_once,
    field_stats,
)

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)

TEST_CODES = ("BF_A", "BF_B", "BF_DONE")


@pytest.fixture(autouse=True)
def _clean():
    yield
    with SessionLocal() as s:
        s.execute(delete(Fight).where(Fight.report_code.in_(TEST_CODES)))
        s.execute(delete(IngestionLedger).where(IngestionLedger.report_code.in_(TEST_CODES)))
        s.execute(delete(Report).where(Report.code.in_(TEST_CODES)))
        s.commit()


def _rankings_for(codes_with_fights):
    return {
        "worldData": {"encounter": {"fightRankings": {
            "rankings": [
                {"report": {"code": code, "fightID": fid}, "duration": 1000}
                for code, fid in codes_with_fights
            ],
        }}}
    }


def test_dry_run_writes_nothing_but_counts():
    client = MagicMock()
    client.graphql.return_value = _rankings_for([("BF_A", 1), ("BF_B", 2)])
    with SessionLocal() as s:
        summary = backfill_once(s, client, encounter_ids=(1079,),
                                reports_per_encounter=10, events_top_n=0,
                                dry_run=True)
    assert summary[1079]["rankings_seen"] == 2
    assert summary[1079]["reports_ingested"] == 2
    # No Report rows persisted
    with SessionLocal() as s:
        rows = [c for c in s.query(Report.code).filter(Report.code.in_(TEST_CODES)).all()]
    assert rows == []


def test_complete_report_skipped_no_ingest_call():
    """Reports that already have a `complete` ledger row are short-circuited."""
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        s.add(Report(code="BF_DONE", ingested_at=now))
        s.flush()
        s.add(IngestionLedger(report_code="BF_DONE", status="complete",
                              fights_ingested=[1], last_event_ts=0,
                              last_polled_at=now))
        s.commit()

        client = MagicMock()
        client.graphql.return_value = _rankings_for([("BF_DONE", 1)])
        summary = backfill_once(s, client, encounter_ids=(1079,),
                                reports_per_encounter=10, events_top_n=0)

    assert summary[1079]["reports_skipped_complete"] == 1
    assert summary[1079]["reports_ingested"] == 0
    # Only the rankings query, no ingest_report graphql call
    assert client.graphql.call_count == 1


def test_field_stats_counts_per_encounter():
    """field_stats returns per-encounter aggregates of what's already in DB."""
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        s.add(Report(code="BF_A", ingested_at=now))
        s.add(Report(code="BF_B", ingested_at=now))
        s.flush()
        s.add(Fight(report_code="BF_A", fight_id_in_report=1,
                    encounter_id=9988, is_kill=True))
        s.add(Fight(report_code="BF_A", fight_id_in_report=2,
                    encounter_id=9988, is_kill=False))
        s.add(Fight(report_code="BF_B", fight_id_in_report=1,
                    encounter_id=9988, is_kill=True))
        s.commit()
        stats = field_stats(s, encounter_ids=(9988,))
    assert stats[0]["reports_ingested"] == 2


def test_error_in_one_report_does_not_abort_pass():
    """An exception during ingest_report for one code is captured per-code,
    the loop continues to the next ranking entry rather than raising up."""
    client = MagicMock()
    client.graphql.return_value = _rankings_for([("BF_A", 1), ("BF_B", 2)])

    def always_fail(session, c, code, **kwargs):
        raise RuntimeError(f"simulated outage on {code}")

    with SessionLocal() as s:
        from unittest.mock import patch
        with patch("jobs.backfill_field.ingest_report", side_effect=always_fail):
            summary = backfill_once(s, client, encounter_ids=(1079,),
                                    reports_per_encounter=10, events_top_n=0)

    assert summary[1079]["rankings_seen"] == 2
    # Both reports failed, both errors captured, no crash
    assert len(summary[1079]["errors"]) == 2
    error_codes = {e["code"] for e in summary[1079]["errors"]}
    assert error_codes == {"BF_A", "BF_B"}
    assert all("simulated outage" in e["error"]
               for e in summary[1079]["errors"])


def test_no_rankings_returned_yields_empty_summary():
    client = MagicMock()
    client.graphql.return_value = _rankings_for([])
    with SessionLocal() as s:
        summary = backfill_once(s, client, encounter_ids=(1079,),
                                reports_per_encounter=10, events_top_n=0)
    assert summary[1079]["rankings_seen"] == 0
    assert summary[1079]["reports_ingested"] == 0
