"""T-103 phase boundaries API smoke."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.main import app
from db.session import engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)

client = TestClient(app)


def test_phases_unknown_fight_returns_empty():
    body = client.get("/api/fights/-1/phases").json()
    assert body == {"fight_id": -1, "phases": [], "transitions": []}


def test_phases_fru_kill_returns_six_phases_with_offsets():
    """Live AC: FRU fight 1500 should produce 6 phases each with
    start_offset_ms / end_offset_ms relative to phase 0 start."""
    body = client.get("/api/fights/1500/phases").json()
    if not body["phases"]:
        pytest.skip("FRU fight 1500 not ingested")
    assert len(body["phases"]) == 6
    assert body["phases"][0]["start_offset_ms"] == 0
    for p in body["phases"]:
        assert "start_offset_ms" in p
        assert "end_offset_ms" in p
        assert p["end_offset_ms"] >= p["start_offset_ms"]
    assert len(body["transitions"]) == 5
