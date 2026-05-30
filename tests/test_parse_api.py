"""T-106 parse trajectory API smoke."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.main import app
from db.session import engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)

client = TestClient(app)


def test_parse_unknown_fight_returns_empty():
    body = client.get("/api/fights/-1/parse").json()
    assert body == {"fight_id": -1, "phases": []}


def test_parse_fru_kill_returns_six_phases_with_players():
    body = client.get("/api/fights/1500/parse").json()
    if not body["phases"]:
        pytest.skip("FRU fight 1500 not ingested")
    assert len(body["phases"]) == 6
    for p in body["phases"]:
        assert p["players"], f"phase {p['phase_index']} has empty players"
        top = p["players"][0]
        assert top["damage_total"] > 0
        assert top["dps"] > 0
