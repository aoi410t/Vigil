"""v1.7.1 dev/user mode split — auth middleware checks both passwords,
sets User.is_developer accordingly, /api/me exposes the flag, new non-dev
users get their own static (not Default)."""
from __future__ import annotations

import base64
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from api.config import settings
from api.main import app
from db.models import (
    Static, StaticMembership, User, WatchedReport, Member, StratConfig,
    ProgPoint,
)
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)


def _auth_header(user: str, pw: str) -> dict[str, str]:
    return {"Authorization":
            "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


@pytest.fixture
def passwords(monkeypatch):
    """Configure both AUTH_PASSWORD and DEV_PASSWORD as different values."""
    monkeypatch.setattr(settings, "auth_password", "user_pw")
    monkeypatch.setattr(settings, "dev_password", "dev_pw")
    monkeypatch.setattr(settings, "auth_username", "")  # disable fallback
    yield


@pytest.fixture
def cleanup_users():
    """Tear down any test users + their statics + scoped data."""
    yield
    with SessionLocal() as s:
        users = s.execute(
            select(User).where(User.username.like("test_v171_%"))
        ).scalars().all()
        for u in users:
            # Find each user's statics; drop scoped data then memberships
            memberships = s.execute(
                select(StaticMembership)
                .where(StaticMembership.user_id == u.id)
            ).scalars().all()
            sids = [m.static_id for m in memberships if m.static_id != 1]
            for sid in sids:
                s.execute(delete(WatchedReport).where(WatchedReport.static_id == sid))
                s.execute(delete(Member).where(Member.static_id == sid))
                s.execute(delete(StratConfig).where(StratConfig.static_id == sid))
                s.execute(delete(ProgPoint).where(ProgPoint.static_id == sid))
            s.execute(delete(StaticMembership).where(StaticMembership.user_id == u.id))
            u.current_static_id = None
            s.flush()
            s.delete(u)
            # Drop the user's per-user statics (id != 1)
            for sid in sids:
                s.execute(delete(StaticMembership).where(StaticMembership.static_id == sid))
                s.execute(delete(Static).where(Static.id == sid))
        s.commit()


def test_dev_password_promotes_to_developer(passwords, cleanup_users):
    name = f"test_v171_{uuid.uuid4().hex[:8]}"
    with TestClient(app) as c:
        r = c.get("/api/me", headers=_auth_header(name, "dev_pw"))
        assert r.status_code == 200
        body = r.json()
        assert body["username"] == name
        assert body["is_developer"] is True
        # Dev users auto-join Default Static (id=1)
        assert 1 in {s["id"] for s in body["statics"]}


def test_user_password_keeps_non_developer(passwords, cleanup_users):
    name = f"test_v171_{uuid.uuid4().hex[:8]}"
    with TestClient(app) as c:
        r = c.get("/api/me", headers=_auth_header(name, "user_pw"))
        assert r.status_code == 200
        body = r.json()
        assert body["is_developer"] is False
        # Non-dev users get their OWN static auto-created — not Default.
        ids = {s["id"] for s in body["statics"]}
        assert 1 not in ids
        # And the static's name matches the user
        names = {s["name"] for s in body["statics"]}
        assert any(name in n for n in names)


def test_wrong_password_rejected(passwords, cleanup_users):
    with TestClient(app) as c:
        r = c.get("/api/me", headers=_auth_header("anyone", "wrong_pw"))
        assert r.status_code == 401


def test_dev_password_swap_promotes_existing_user(passwords, cleanup_users):
    """User logs in once with user_pw (non-dev), then again with dev_pw —
    is_developer flips to True on the second login."""
    name = f"test_v171_{uuid.uuid4().hex[:8]}"
    with TestClient(app) as c:
        r1 = c.get("/api/me", headers=_auth_header(name, "user_pw"))
        assert r1.json()["is_developer"] is False
        r2 = c.get("/api/me", headers=_auth_header(name, "dev_pw"))
        assert r2.json()["is_developer"] is True


def test_no_auth_configured_fallback(monkeypatch, cleanup_users):
    """Dev/test mode (no AUTH_*/DEV_* env): is_developer falls back to the
    AUTH_USERNAME match (preserves the legacy single-user-dev behavior)."""
    monkeypatch.setattr(settings, "auth_password", "")
    monkeypatch.setattr(settings, "dev_password", "")
    monkeypatch.setattr(settings, "auth_username", "legacy_dev_xyz")
    name = "legacy_dev_xyz"
    with TestClient(app) as c:
        r = c.get("/api/me", headers=_auth_header(name, ""))
        # No auth required at the middleware (empty passwords); the
        # dependency falls back to username-match for dev flag.
        assert r.status_code == 200
        assert r.json()["is_developer"] is True
    # Cleanup the legacy_dev_xyz user
    with SessionLocal() as s:
        u = s.execute(select(User).where(User.username == name)).scalar_one_or_none()
        if u is not None:
            ms = s.execute(select(StaticMembership).where(StaticMembership.user_id == u.id)).scalars().all()
            for m in ms:
                s.delete(m)
            u.current_static_id = None
            s.flush()
            s.delete(u)
            s.commit()
