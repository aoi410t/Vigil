"""Sanity that the FastAPI app boots and exposes its routes."""
from fastapi.testclient import TestClient

from api import __version__
from api.main import app


def test_healthz():
    client = TestClient(app)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "version": __version__}


def test_wipes_route_unknown_report_returns_empty_shape():
    """T-006 API plumbing: route resolves, queries the DB, returns the empty shape."""
    client = TestClient(app)
    r = client.get("/api/reports/__NONE__/wipes")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "report_code": "__NONE__",
        "total_wipes": 0,
        "total_kills": 0,
        "buckets": [],
    }
