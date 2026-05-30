"""API tests for T-010 manual prog-point entry. Hit the live dev DB; clean up after."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from api.main import app
from db.models import ProgPoint
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)


@pytest.fixture(autouse=True)
def _clean_prog_points():
    yield
    with SessionLocal() as s:
        s.execute(delete(ProgPoint))
        s.commit()


client = TestClient(app)


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat()


def test_list_empty():
    r = client.get("/api/prog-points")
    assert r.status_code == 200
    assert r.json() == []


def test_create_and_list_manual_point():
    t = datetime(2026, 5, 24, 18, 0, tzinfo=timezone.utc)
    r = client.post("/api/prog-points", json={
        "ts": _iso(t),
        "phase": 3,
        "fight_percentage": 47.5,
        "pull_count": 84,
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["phase"] == 3
    assert body["fight_percentage"] == 47.5
    assert body["pull_count"] == 84
    assert body["source"] == "manual"

    rows = client.get("/api/prog-points").json()
    assert len(rows) == 1
    assert rows[0]["id"] == body["id"]


def test_create_requires_phase_or_percentage():
    r = client.post("/api/prog-points", json={
        "ts": _iso(datetime.now(timezone.utc)),
        "pull_count": 1,
    })
    assert r.status_code == 422
    assert "phase" in r.json()["detail"]


def test_list_sorted_oldest_first():
    base = datetime(2026, 5, 24, tzinfo=timezone.utc)
    # Insert out of order.
    for offset in [2, 0, 1]:
        client.post("/api/prog-points", json={
            "ts": _iso(base + timedelta(hours=offset)),
            "phase": offset + 1,
        })
    rows = client.get("/api/prog-points").json()
    assert [r["phase"] for r in rows] == [1, 2, 3]


def test_delete_prog_point():
    r = client.post("/api/prog-points", json={
        "ts": _iso(datetime.now(timezone.utc)),
        "phase": 1,
    })
    pid = r.json()["id"]
    r2 = client.delete(f"/api/prog-points/{pid}")
    assert r2.status_code == 204
    assert client.get("/api/prog-points").json() == []


def test_delete_unknown_404():
    assert client.delete("/api/prog-points/999999").status_code == 404


def test_percentage_only_accepted():
    """Either phase or percentage alone is enough."""
    r = client.post("/api/prog-points", json={
        "ts": _iso(datetime.now(timezone.utc)),
        "fight_percentage": 12.3,
    })
    assert r.status_code == 201
    assert r.json()["fight_percentage"] == 12.3
    assert r.json()["phase"] is None
