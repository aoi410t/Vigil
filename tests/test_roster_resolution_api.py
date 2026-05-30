"""T-107 API smoke: roster-resolution endpoint shape."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.main import app
from db.session import engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)


client = TestClient(app)


def test_unknown_report_returns_empty():
    r = client.get("/api/reports/__UNKNOWN__/roster-resolution")
    assert r.status_code == 200
    body = r.json()
    assert body["fights"] == []
    assert body["coverage"]["total_characters"] == 0
    assert body["coverage"]["resolved"] == 0
    assert body["coverage"]["unresolved"] == []


def test_real_report_returns_full_shape():
    """Smoke-test against any ingested report in the dev DB."""
    reports = client.get("/api/reports").json()
    if not reports:
        pytest.skip("no ingested reports in dev DB")
    code = reports[0]["code"]
    body = client.get(f"/api/reports/{code}/roster-resolution").json()
    assert "fights" in body
    assert "coverage" in body
    cov = body["coverage"]
    assert isinstance(cov["total_characters"], int)
    assert isinstance(cov["resolved"], int)
    assert isinstance(cov["unresolved"], list)
    if body["fights"]:
        sample = body["fights"][0]["combatants"]
        assert all("member_id" in c and "member_name" in c and "job" in c
                   for c in sample)
