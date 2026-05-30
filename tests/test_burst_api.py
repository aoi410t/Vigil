"""T-105 API smoke for the burst endpoint shape."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.main import app
from db.session import engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)

client = TestClient(app)


def test_burst_unknown_report_returns_shape():
    body = client.get("/api/reports/__NONE__/burst").json()
    assert body["fights"] == []
    assert "raid_buff_ids" in body
    assert "personal_buff_ids" in body
    assert "window_ms" in body


def test_burst_real_report_returns_per_player_rows():
    reports = client.get("/api/reports").json()
    if not reports:
        pytest.skip("no ingested reports in dev DB")
    code = reports[0]["code"]
    body = client.get(f"/api/reports/{code}/burst").json()
    assert isinstance(body["fights"], list)
    if body["fights"]:
        sample = body["fights"][0]
        assert isinstance(sample["burst_windows"], list)
        assert "players" in sample
        for p in sample["players"]:
            assert {"player_id", "name", "job", "personal_casts_total",
                    "in_window", "drift", "in_window_pct"} <= p.keys()
