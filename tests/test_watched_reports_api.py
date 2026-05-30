"""T-101 watchlist CRUD API tests."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from api.main import app
from db.models import WatchedReport
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)

client = TestClient(app)
TEST_CODES = ("WATCH_AAA", "WATCH_BBB", "abc123def4567890")


@pytest.fixture(autouse=True)
def _clean():
    yield
    with SessionLocal() as s:
        s.execute(delete(WatchedReport).where(WatchedReport.code.in_(TEST_CODES)))
        s.commit()


def test_list_initially_excludes_test_codes():
    rows = client.get("/api/watched-reports").json()
    codes = {r["code"] for r in rows}
    assert not (codes & set(TEST_CODES))


def test_create_with_bare_code():
    r = client.post("/api/watched-reports", json={
        "code_or_url": "WATCH_AAA", "label": "FRU prog week 3"
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["code"] == "WATCH_AAA"
    assert body["label"] == "FRU prog week 3"
    assert body["active"] is True


def test_create_with_full_fflogs_url_extracts_code():
    r = client.post("/api/watched-reports", json={
        "code_or_url": "https://www.fflogs.com/reports/abc123def4567890#fight=15"
    })
    assert r.status_code == 201
    assert r.json()["code"] == "abc123def4567890"


def test_create_duplicate_rejected_with_409():
    client.post("/api/watched-reports", json={"code_or_url": "WATCH_AAA"})
    r = client.post("/api/watched-reports", json={"code_or_url": "WATCH_AAA"})
    assert r.status_code == 409


def test_patch_can_deactivate():
    client.post("/api/watched-reports", json={"code_or_url": "WATCH_AAA"})
    r = client.patch("/api/watched-reports/WATCH_AAA", json={"active": False})
    assert r.status_code == 200
    assert r.json()["active"] is False


def test_patch_can_update_label():
    client.post("/api/watched-reports", json={"code_or_url": "WATCH_AAA"})
    r = client.patch("/api/watched-reports/WATCH_AAA", json={"label": "rebrand"})
    assert r.json()["label"] == "rebrand"


def test_patch_unknown_404():
    assert client.patch("/api/watched-reports/NOPE",
                        json={"active": False}).status_code == 404


def test_delete_removes_row():
    client.post("/api/watched-reports", json={"code_or_url": "WATCH_AAA"})
    assert client.delete("/api/watched-reports/WATCH_AAA").status_code == 204
    assert client.delete("/api/watched-reports/WATCH_AAA").status_code == 404


def test_create_rejects_empty_after_url_strip():
    r = client.post("/api/watched-reports", json={"code_or_url": "   "})
    assert r.status_code == 422


def test_poll_now_404_when_not_watching():
    r = client.post("/api/watched-reports/NOT_WATCHED/poll")
    assert r.status_code == 404


def test_poll_now_skips_when_ledger_complete(monkeypatch):
    """If the report is already `complete`, the endpoint returns
    `skipped_complete` and doesn't call FFLogs at all."""
    from datetime import datetime as _dt, timezone as _tz

    from db.models import IngestionLedger, Report

    with SessionLocal() as s:
        client.post("/api/watched-reports", json={"code_or_url": "WATCH_AAA"})
        s.add(Report(code="WATCH_AAA", ingested_at=_dt.now(_tz.utc)))
        s.flush()
        s.add(IngestionLedger(report_code="WATCH_AAA", status="complete",
                              fights_ingested=[1], last_event_ts=0,
                              last_polled_at=_dt.now(_tz.utc)))
        s.commit()

    try:
        r = client.post("/api/watched-reports/WATCH_AAA/poll")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "skipped_complete"
    finally:
        from sqlalchemy import delete as _delete
        with SessionLocal() as s:
            s.execute(_delete(IngestionLedger).where(
                IngestionLedger.report_code == "WATCH_AAA"))
            s.execute(_delete(Report).where(Report.code == "WATCH_AAA"))
            s.commit()
