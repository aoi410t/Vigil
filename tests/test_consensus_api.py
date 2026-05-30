"""T-104 consensus API smoke."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.main import app
from db.session import engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)

client = TestClient(app)


def test_consensus_unknown_encounter_returns_note():
    body = client.get("/api/encounters/999999/consensus").json()
    assert body["phases"] == []
    assert "note" in body


def test_consensus_fru_returns_six_phase_shape():
    body = client.get("/api/encounters/1079/consensus").json()
    if not body["phases"]:
        pytest.skip("no FRU consensus available in dev DB")
    assert len(body["phases"]) == 6
    for p in body["phases"]:
        assert {"phase_index", "pulls_reaching",
                "canonical_abilities", "all_abilities"} <= p.keys()
        if p["canonical_abilities"]:
            ab = p["canonical_abilities"][0]
            assert {"ability_game_id", "occurrence_rate",
                    "median_relative_t_ms", "variance_ms",
                    "sample_count"} <= ab.keys()
