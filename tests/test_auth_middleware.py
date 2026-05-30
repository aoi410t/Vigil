"""HTTP Basic auth middleware tests (single-origin prod mode).

The middleware is gated on `settings.auth_username` + `auth_password` being
non-empty. These tests flip those at module-level via monkeypatch.
"""
from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from api.main import app
from api.config import settings


def _auth_header(user: str, pw: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


@pytest.fixture
def auth_enabled(monkeypatch):
    monkeypatch.setattr(settings, "auth_username", "vigil")
    monkeypatch.setattr(settings, "auth_password", "hunter2")
    yield


def test_healthz_passes_without_auth_even_when_enabled(auth_enabled):
    """Monitoring probes hit /healthz; it must never require auth."""
    with TestClient(app) as client:
        r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_api_route_rejects_missing_auth(auth_enabled):
    with TestClient(app) as client:
        r = client.get("/api/reports")
    assert r.status_code == 401
    assert r.headers.get("www-authenticate", "").lower().startswith("basic")


def test_api_route_rejects_wrong_password(auth_enabled):
    with TestClient(app) as client:
        r = client.get("/api/reports", headers=_auth_header("vigil", "wrong"))
    assert r.status_code == 401


def test_api_route_accepts_any_username_with_right_password(auth_enabled):
    """v1.6.0 multi-static: AUTH_PASSWORD is a single shared password but the
    USERNAME is free-form — anyone with the password can claim a username
    and that becomes their user record on first request. Auto-provisioning
    is in api/auth.ensure_user_and_membership."""
    with TestClient(app) as client:
        r = client.get("/api/reports", headers=_auth_header("someone_new", "hunter2"))
    # Should not 401; whether it's 200 or 503 depends on DB availability.
    assert r.status_code != 401


def test_api_route_accepts_correct_creds(auth_enabled):
    with TestClient(app) as client:
        r = client.get("/api/reports", headers=_auth_header("vigil", "hunter2"))
    # Either 200 (db configured) or 503 (db skipped). What matters: not 401.
    assert r.status_code != 401


def test_malformed_auth_header_rejected(auth_enabled):
    with TestClient(app) as client:
        r = client.get("/api/reports", headers={"Authorization": "Bearer foo"})
    assert r.status_code == 401


def test_invalid_base64_rejected(auth_enabled):
    with TestClient(app) as client:
        r = client.get("/api/reports", headers={"Authorization": "Basic !!!not-base64!!!"})
    assert r.status_code == 401


def test_auth_disabled_when_env_unset():
    """With creds env-unset (default), middleware is a no-op."""
    assert not (settings.auth_username and settings.auth_password)
    with TestClient(app) as client:
        r = client.get("/api/reports")
    # No auth required => should not 401.
    assert r.status_code != 401
