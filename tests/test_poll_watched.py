"""T-101 poller tests with mocked FFLogsClient."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import delete

from db.models import (
    Fight,
    IngestionLedger,
    Report,
    WatchedReport,
)
from db.session import SessionLocal, engine
from jobs.poll_watched import poll_once

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)


@pytest.fixture(autouse=True)
def _clean():
    yield
    with SessionLocal() as s:
        codes = ("T101_A", "T101_B", "T101_DONE")
        s.execute(delete(Fight).where(Fight.report_code.in_(codes)))
        s.execute(delete(IngestionLedger).where(IngestionLedger.report_code.in_(codes)))
        s.execute(delete(Report).where(Report.code.in_(codes)))
        s.execute(delete(WatchedReport).where(WatchedReport.code.in_(codes)))
        s.commit()


def _fake_meta_response(code: str, end_time: int, fights: list[dict]) -> dict:
    return {
        "reportData": {"report": {
            "owner": {"name": "tester"},
            "region": {"slug": "NA"},
            "visibility": "public",
            "startTime": 0,
            "endTime": end_time,
            "fights": fights,
            "masterData": {"actors": []},
        }}
    }


def _ours(summaries):
    """Filter poll_once results to just the test's seeded T101_* codes.
    poll_once globally polls every active watched_report across all statics;
    these tests only care about the rows they seeded."""
    return [s for s in summaries if s["code"].startswith("T101_")]


def test_empty_watchlist_returns_no_summaries():
    """When no T101_* codes are watched, poll_once returns nothing for them."""
    with SessionLocal() as s:
        result = poll_once(s, MagicMock())
    assert _ours(result) == []


def test_complete_report_is_skipped_no_network_call():
    """Reports whose ledger is `complete` must not trigger an API call (PLAN Invariant 1)."""
    with SessionLocal() as s:
        s.add(Report(code="T101_DONE", ingested_at=datetime.now(timezone.utc)))
        s.flush()
        s.add(IngestionLedger(report_code="T101_DONE", status="complete",
                              fights_ingested=[1, 2], last_event_ts=1000,
                              last_polled_at=datetime.now(timezone.utc)))
        s.add(WatchedReport(static_id=1, code="T101_DONE", active=True,
                            added_at=datetime.now(timezone.utc)))
        s.commit()

        client = MagicMock()
        summaries = _ours(poll_once(s, client))

    assert len(summaries) == 1
    assert summaries[0]["status"] == "skipped_complete"
    client.graphql.assert_not_called()


def test_inactive_report_is_ignored():
    with SessionLocal() as s:
        s.add(WatchedReport(static_id=1, code="T101_A", active=False,
                            added_at=datetime.now(timezone.utc)))
        s.commit()
        result = _ours(poll_once(s, MagicMock()))
    assert result == []


def test_error_is_captured_on_watch_row():
    """When ingest_report raises, the error message lands on `last_error` and
    poll_once moves on without crashing."""
    with SessionLocal() as s:
        s.add(WatchedReport(static_id=1, code="T101_A", active=True,
                            added_at=datetime.now(timezone.utc)))
        s.commit()

        client = MagicMock()
        client.graphql.side_effect = RuntimeError("simulated FFLogs outage")
        summaries = _ours(poll_once(s, client))

        # Reload from DB to check persistence of the error trace.
        w = s.get(WatchedReport, (1, "T101_A"))
        s.refresh(w)

    assert len(summaries) == 1
    assert summaries[0]["status"] == "error"
    assert "simulated FFLogs outage" in summaries[0]["error"]
    assert w.last_error is not None
    assert "RuntimeError" in w.last_error
    assert w.last_polled_at is not None
