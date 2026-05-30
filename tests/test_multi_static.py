"""Multi-static (v1.6.0) — auth context + cross-static isolation.

Tests that:
- A new HTTP Basic username auto-provisions a User + Static membership.
- A user can list/create statics and switch their current one.
- Two users in different statics never see each other's scoped data
  (watched_reports, members, strat_config, prog_points).
- Membership management (add/remove) honors 404 vs. 409 cases.
- Last-member removal is blocked.

Tests bypass the HTTP Basic password check (AUTH_PASSWORD unset) and use the
username from the Authorization header to drive `get_context`. Each test uses
disposable usernames + cleans up its own User + Static + membership rows.
"""
from __future__ import annotations

import base64
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from api.main import app
from db.models import (
    Member, ProgPoint, Static, StaticMembership, StratConfig, User,
    WatchedReport,
)
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)


def _auth(username: str) -> dict[str, str]:
    """Authorization header for a test user. Password is irrelevant (AUTH
    middleware is disabled in tests)."""
    token = base64.b64encode(f"{username}:test".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def two_users():
    """Two disposable users in two separate statics. Yields
    (user_a_name, user_b_name, static_a_id, static_b_id) + cleans up."""
    a = f"test_user_a_{uuid.uuid4().hex[:8]}"
    b = f"test_user_b_{uuid.uuid4().hex[:8]}"
    client = TestClient(app)
    # Prime user A: hit /api/me to trigger auto-create + default-static join.
    client.get("/api/me", headers=_auth(a))
    # User A creates a new static; the API auto-switches them onto it.
    r = client.post("/api/statics", json={"name": f"Static_A_{a}"},
                    headers=_auth(a))
    assert r.status_code == 201
    sa = r.json()["id"]
    # Prime user B.
    client.get("/api/me", headers=_auth(b))
    r = client.post("/api/statics", json={"name": f"Static_B_{b}"},
                    headers=_auth(b))
    assert r.status_code == 201
    sb = r.json()["id"]

    yield a, b, sa, sb

    # Teardown: drop scoped rows + memberships + statics + users.
    with SessionLocal() as s:
        user_a = s.execute(select(User).where(User.username == a)).scalar_one_or_none()
        user_b = s.execute(select(User).where(User.username == b)).scalar_one_or_none()
        for sid in (sa, sb):
            s.execute(delete(WatchedReport).where(WatchedReport.static_id == sid))
            s.execute(delete(Member).where(Member.static_id == sid))
            s.execute(delete(StratConfig).where(StratConfig.static_id == sid))
            s.execute(delete(ProgPoint).where(ProgPoint.static_id == sid))
            s.execute(delete(StaticMembership).where(StaticMembership.static_id == sid))
            s.execute(delete(Static).where(Static.id == sid))
        for u in (user_a, user_b):
            if u is None:
                continue
            u.current_static_id = None
            s.flush()
            s.execute(delete(StaticMembership).where(StaticMembership.user_id == u.id))
            s.delete(u)
        s.commit()


def test_me_returns_user_and_statics(two_users):
    a, _, sa, _ = two_users
    client = TestClient(app)
    r = client.get("/api/me", headers=_auth(a))
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == a
    assert body["current_static_id"] == sa  # auto-switched on create
    # v1.7.1: non-dev users get their own static auto-created on first
    # /api/me touch, then sa is created explicitly — so they have ≥2 statics.
    ids = {st["id"] for st in body["statics"]}
    assert sa in ids
    assert len(ids) >= 2
    assert body["is_developer"] is False


def test_cross_static_watched_reports_isolation(two_users):
    a, b, sa, sb = two_users
    client = TestClient(app)
    # User A watches a report on their static
    r = client.post("/api/watched-reports",
                    json={"code_or_url": "ABCD1234"},
                    headers=_auth(a))
    assert r.status_code == 201
    # User B's watch list should be empty (their static, sb, has none)
    r = client.get("/api/watched-reports", headers=_auth(b))
    assert r.status_code == 200
    assert [w["code"] for w in r.json()] == []
    # And vice versa: A sees their watch
    r = client.get("/api/watched-reports", headers=_auth(a))
    assert "ABCD1234" in [w["code"] for w in r.json()]


def test_cross_static_members_isolation(two_users):
    a, b, _, _ = two_users
    client = TestClient(app)
    # User A adds a member
    r = client.post("/api/members",
                    json={"name": "Alice", "aliases": []},
                    headers=_auth(a))
    assert r.status_code == 201
    # User B's roster is empty
    r = client.get("/api/members", headers=_auth(b))
    assert [m["name"] for m in r.json()] == []
    # User A sees Alice
    r = client.get("/api/members", headers=_auth(a))
    assert "Alice" in [m["name"] for m in r.json()]


def test_cross_static_member_404_when_not_in_current_static(two_users):
    a, b, _, _ = two_users
    client = TestClient(app)
    r = client.post("/api/members",
                    json={"name": "Owned_By_A", "aliases": []},
                    headers=_auth(a))
    mid = r.json()["id"]
    # User B tries to PATCH A's member → 404 (existence-leak avoided)
    r = client.patch(f"/api/members/{mid}", json={"notes": "hijack"},
                     headers=_auth(b))
    assert r.status_code == 404
    # B can't DELETE either
    r = client.delete(f"/api/members/{mid}", headers=_auth(b))
    assert r.status_code == 404
    # A's view is unaffected
    r = client.get(f"/api/members", headers=_auth(a))
    assert any(m["id"] == mid for m in r.json())


def test_cross_static_strat_config_isolation(two_users):
    a, b, _, _ = two_users
    client = TestClient(app)
    enc = 999_001
    # User A writes a strat
    r = client.put(
        f"/api/encounters/{enc}/strat-config/12345_0",
        json={"mit_plan": {"slots": [{"ability_id": 7535,
                                      "expected_role": "MT",
                                      "window_offset_ms": 0}]},
              "assignments": {"role_map": {"tower_n": "MT"}}},
        headers=_auth(a),
    )
    assert r.status_code == 200, r.text
    # User B doesn't see it
    r = client.get(f"/api/encounters/{enc}/strat-config", headers=_auth(b))
    assert r.json()["rows"] == []
    # User A does
    r = client.get(f"/api/encounters/{enc}/strat-config", headers=_auth(a))
    assert len(r.json()["rows"]) == 1


def test_switch_to_unauthorized_static_404(two_users):
    a, _, _, sb = two_users
    client = TestClient(app)
    # A tries to switch to B's static
    r = client.patch("/api/me/current-static",
                     json={"static_id": sb}, headers=_auth(a))
    assert r.status_code == 404


def test_add_member_to_static_lets_them_see_data(two_users):
    a, b, sa, _ = two_users
    client = TestClient(app)
    # A creates a watched report on their static
    client.post("/api/watched-reports", json={"code_or_url": "INVITED"},
                headers=_auth(a))
    # B is not in A's static — empty view
    r = client.get("/api/watched-reports", headers=_auth(b))
    assert [w["code"] for w in r.json()] == []
    # A adds B to their static
    r = client.post(f"/api/statics/{sa}/members",
                    json={"username": b}, headers=_auth(a))
    assert r.status_code == 201
    # B switches to A's static
    r = client.patch("/api/me/current-static",
                     json={"static_id": sa}, headers=_auth(b))
    assert r.status_code == 200
    # Now B sees the watched report
    r = client.get("/api/watched-reports", headers=_auth(b))
    assert "INVITED" in [w["code"] for w in r.json()]


def test_add_unknown_username_returns_404(two_users):
    a, _, sa, _ = two_users
    client = TestClient(app)
    r = client.post(f"/api/statics/{sa}/members",
                    json={"username": "doesnt_exist_xyz"},
                    headers=_auth(a))
    assert r.status_code == 404


def test_remove_last_member_blocked(two_users):
    a, _, sa, _ = two_users
    client = TestClient(app)
    # A is the only member of static_a; removing themselves should 409.
    # Find A's user_id via /me.
    me = client.get("/api/me", headers=_auth(a)).json()
    r = client.delete(f"/api/statics/{sa}/members/{me['user_id']}",
                      headers=_auth(a))
    assert r.status_code == 409


def test_list_static_members(two_users):
    a, b, sa, _ = two_users
    client = TestClient(app)
    # Add B to A's static
    client.post(f"/api/statics/{sa}/members",
                json={"username": b}, headers=_auth(a))
    r = client.get(f"/api/statics/{sa}/members", headers=_auth(a))
    assert r.status_code == 200
    names = {m["username"] for m in r.json()}
    assert names == {a, b}
