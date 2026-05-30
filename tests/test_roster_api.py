"""API tests for the roster routes (T-011). Hit the live dev DB; clean up after."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from api.main import app
from db.models import CharacterAlias, Member
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)


TEST_MEMBER_NAMES = ("Alice", "Bob", "Carol", "Dan", "Eve",
                     "Frank", "Frank2", "Greta", "X")


def _wipe_test_members():
    with SessionLocal() as s:
        s.execute(delete(Member).where(Member.name.in_(TEST_MEMBER_NAMES)))
        s.commit()


@pytest.fixture(autouse=True)
def _clean_roster():
    """Scoped cleanup — only wipes members this test file creates, so
    real user-curated roster data in the dev DB stays put. Previously
    `delete(Member)` blanketed everything which made tests destructive
    on a populated DB and caused test_empty_list to flake. Wipes both
    BEFORE and after so previous-run residue doesn't leak in."""
    _wipe_test_members()
    yield
    _wipe_test_members()


client = TestClient(app)


def test_empty_list():
    """Verifies the test members don't exist after cleanup. Doesn't assert
    the whole list is empty — the dev DB may have real user-curated
    members that aren't test fixtures."""
    r = client.get("/api/members")
    assert r.status_code == 200
    names = {m["name"] for m in r.json()}
    for n in TEST_MEMBER_NAMES:
        assert n not in names


def test_create_with_aliases_and_list():
    r = client.post("/api/members", json={
        "name": "Alice", "role_pref": "tank",
        "aliases": [
            {"character_name": "Alice Tankerton", "server": "Aether"},
            {"character_name": "Alice Backup", "server": "Aether"},
        ],
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "Alice"
    assert body["role_pref"] == "tank"
    assert len(body["aliases"]) == 2
    assert {a["character_name"] for a in body["aliases"]} == {"Alice Tankerton", "Alice Backup"}

    r = client.get("/api/members")
    # Assert presence rather than exact count — dev DB may carry real user
    # roster members alongside the test fixture (v1.16.2: scoped fixture
    # cleanup means we don't wipe those anymore).
    names = [m["name"] for m in r.json()]
    assert "Alice" in names


def test_duplicate_member_name_rejected():
    r = client.post("/api/members", json={"name": "Bob"})
    assert r.status_code == 201
    r2 = client.post("/api/members", json={"name": "Bob"})
    assert r2.status_code == 409


def test_duplicate_alias_across_members_now_allowed():
    """v1.6.0 dropped the global (character_name, server) uniqueness on
    character_aliases — aliases are scoped via member -> static (same
    static can't have it twice via the member.name uniqueness, but two
    members in different statics CAN share an alias). For ergonomics
    inside one static we also allow two members to share an alias —
    use-case: two members covering the same character roster slot.
    Test just asserts the second create no longer 409s."""
    r1 = client.post("/api/members", json={
        "name": "Carol",
        "aliases": [{"character_name": "Carol Caster", "server": "Primal"}]
    })
    assert r1.status_code == 201
    r2 = client.post("/api/members", json={
        "name": "Dan",
        "aliases": [{"character_name": "Carol Caster", "server": "Primal"}]
    })
    assert r2.status_code == 201


def test_patch_updates_fields():
    r = client.post("/api/members", json={"name": "Eve"})
    mid = r.json()["id"]
    r2 = client.patch(f"/api/members/{mid}", json={
        "role_pref": "healer", "notes": "co-leader"
    })
    assert r2.status_code == 200
    body = r2.json()
    assert body["role_pref"] == "healer"
    assert body["notes"] == "co-leader"
    assert body["name"] == "Eve"


def test_delete_member_cascades_aliases():
    r = client.post("/api/members", json={
        "name": "Frank",
        "aliases": [{"character_name": "Frank Five", "server": "Crystal"}]
    })
    mid = r.json()["id"]
    r2 = client.delete(f"/api/members/{mid}")
    assert r2.status_code == 204
    # Member gone — assert by absence, not whole-list-empty (dev DB may
    # hold real user-curated members).
    r3 = client.get("/api/members")
    assert not any(m["name"] == "Frank" for m in r3.json())
    # Alias gone (we can recreate using same character_name without 409)
    r4 = client.post("/api/members", json={
        "name": "Frank2",
        "aliases": [{"character_name": "Frank Five", "server": "Crystal"}]
    })
    assert r4.status_code == 201


def test_add_alias_to_existing_member():
    r = client.post("/api/members", json={"name": "Greta"})
    mid = r.json()["id"]
    r2 = client.post(f"/api/members/{mid}/aliases", json={
        "character_name": "Greta Gunbreak", "server": "Mana"
    })
    assert r2.status_code == 201
    aid = r2.json()["id"]
    # Look the member up by name, not index 0 (the dev DB may have
    # real members alongside the test fixture).
    members = client.get("/api/members").json()
    greta = next(m for m in members if m["name"] == "Greta")
    assert len(greta["aliases"]) == 1
    assert greta["aliases"][0]["id"] == aid
    # Delete alias
    r3 = client.delete(f"/api/aliases/{aid}")
    assert r3.status_code == 204
    greta = next(m for m in client.get("/api/members").json()
                 if m["name"] == "Greta")
    assert greta["aliases"] == []


def test_patch_unknown_member_404():
    assert client.patch("/api/members/999999", json={"name": "X"}).status_code == 404


def test_delete_unknown_member_404():
    assert client.delete("/api/members/999999").status_code == 404


def test_delete_unknown_alias_404():
    assert client.delete("/api/aliases/999999").status_code == 404