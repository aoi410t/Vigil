"""v1.15.0 — roster discovery + classification.

Tests:
- Members.kind round-trips through /api/members POST + PATCH (defaults 'core').
- /api/roster/characters returns distinct (name, server) from this static's
  watched-report combatants with correct classification.
- /api/roster/classify routes core/substitute/sub/ignore/clear correctly
  (idempotent; switching action wipes prior state).
- Cross-static isolation: characters seen in another static's reports
  don't leak into the discovery list.

Tests use disposable user/static via the v1.6.0 multi-static pattern so
they don't collide with the dev DB's Default Static data.
"""
from __future__ import annotations

import base64
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from api.main import app
from db.models import (
    CharacterAlias, Combatant, Fight, IgnoredCharacter, Member, ProgPoint,
    Report, Static, StaticMembership, StratConfig, User, WatchedReport,
)
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)


def _auth(username: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:test".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def user_with_static():
    """One disposable user in a fresh static, seeded with a small set of
    watched-report combatants. Yields (username, static_id, report_code) +
    cleans up everything created in the test.
    """
    uname = f"test_disc_{uuid.uuid4().hex[:8]}"
    client = TestClient(app)
    client.get("/api/me", headers=_auth(uname))
    r = client.post("/api/statics", json={"name": f"Disc_{uname}"},
                    headers=_auth(uname))
    assert r.status_code == 201
    sid = r.json()["id"]

    code = f"DISC_{uuid.uuid4().hex[:8]}"
    fight_ids: list[int] = []
    with SessionLocal() as s:
        s.add(Report(code=code, owner="test", region="NA", is_public=True,
                     start_time=datetime.now(timezone.utc),
                     end_time=datetime.now(timezone.utc),
                     ingested_at=datetime.now(timezone.utc)))
        s.flush()
        s.add(WatchedReport(static_id=sid, code=code, label="test", active=True,
                            added_at=datetime.now(timezone.utc)))
        for i in range(2):
            f = Fight(
                report_code=code, fight_id_in_report=i + 1,
                encounter_id=1076, is_kill=False,
                fight_percentage=85.0, last_phase=1,
                start_time=1000 + i, end_time=2000 + i, duration_ms=1000,
            )
            s.add(f)
            s.flush()
            fight_ids.append(f.id)
        # 4 distinct (name, server) characters across 2 fights:
        # - Alice Tankerton @ Aether — 2 fights (PLD then WAR)
        # - Bob Heals      @ Aether — 1 fight (WHM)
        # - Carol Caster   @ Primal — 1 fight (BLM)
        # - Dan Pug        @ Aether — 1 fight (DRG)
        seeds = [
            (fight_ids[0], 1, "Alice Tankerton", "Aether", "PLD"),
            (fight_ids[0], 2, "Bob Heals",       "Aether", "WHM"),
            (fight_ids[0], 3, "Carol Caster",    "Primal", "BLM"),
            (fight_ids[0], 4, "Dan Pug",         "Aether", "DRG"),
            (fight_ids[1], 1, "Alice Tankerton", "Aether", "WAR"),
        ]
        for fid, pid, name, server, job in seeds:
            s.add(Combatant(fight_id=fid, player_id=pid, name=name,
                            server=server, job=job))
        s.commit()

    yield uname, sid, code

    # Teardown.
    with SessionLocal() as s:
        s.execute(delete(IgnoredCharacter).where(IgnoredCharacter.static_id == sid))
        s.execute(delete(CharacterAlias).where(
            CharacterAlias.member_id.in_(
                select(Member.id).where(Member.static_id == sid))))
        s.execute(delete(Member).where(Member.static_id == sid))
        s.execute(delete(WatchedReport).where(WatchedReport.static_id == sid))
        s.execute(delete(StratConfig).where(StratConfig.static_id == sid))
        s.execute(delete(ProgPoint).where(ProgPoint.static_id == sid))
        for fid in fight_ids:
            s.execute(delete(Combatant).where(Combatant.fight_id == fid))
            s.execute(delete(Fight).where(Fight.id == fid))
        s.execute(delete(Report).where(Report.code == code))
        # static + membership + user
        u = s.execute(select(User).where(User.username == uname)).scalar_one_or_none()
        s.execute(delete(StaticMembership).where(StaticMembership.static_id == sid))
        s.execute(delete(Static).where(Static.id == sid))
        if u is not None:
            u.current_static_id = None
            s.flush()
            s.execute(delete(StaticMembership).where(StaticMembership.user_id == u.id))
            s.delete(u)
        s.commit()


# ---------- /api/members kind support ----------

def test_member_kind_defaults_to_core(user_with_static):
    uname, _, _ = user_with_static
    client = TestClient(app)
    r = client.post("/api/members", json={"name": "Alice"},
                    headers=_auth(uname))
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "core"


def test_member_kind_substitute_round_trips(user_with_static):
    uname, _, _ = user_with_static
    client = TestClient(app)
    r = client.post("/api/members",
                    json={"name": "Bob", "kind": "substitute"},
                    headers=_auth(uname))
    assert r.status_code == 201
    assert r.json()["kind"] == "substitute"
    r2 = client.get("/api/members", headers=_auth(uname))
    bob = next(m for m in r2.json() if m["name"] == "Bob")
    assert bob["kind"] == "substitute"


def test_member_kind_patch_validates(user_with_static):
    uname, _, _ = user_with_static
    client = TestClient(app)
    r = client.post("/api/members", json={"name": "Bob"}, headers=_auth(uname))
    mid = r.json()["id"]
    r2 = client.patch(f"/api/members/{mid}", json={"kind": "nonsense"},
                      headers=_auth(uname))
    assert r2.status_code == 422
    r3 = client.patch(f"/api/members/{mid}", json={"kind": "substitute"},
                      headers=_auth(uname))
    assert r3.status_code == 200
    assert r3.json()["kind"] == "substitute"


def test_member_kind_invalid_rejected_on_create(user_with_static):
    uname, _, _ = user_with_static
    client = TestClient(app)
    r = client.post("/api/members", json={"name": "X", "kind": "lead"},
                    headers=_auth(uname))
    assert r.status_code == 422


# ---------- /api/roster/characters discovery ----------

def test_discovery_lists_distinct_characters(user_with_static):
    uname, _, _ = user_with_static
    client = TestClient(app)
    r = client.get("/api/roster/characters", headers=_auth(uname))
    assert r.status_code == 200
    chars = r.json()["characters"]
    assert {c["character_name"] for c in chars} == {
        "Alice Tankerton", "Bob Heals", "Carol Caster", "Dan Pug",
    }
    alice = next(c for c in chars if c["character_name"] == "Alice Tankerton")
    assert alice["fights_seen"] == 2
    # Latest job (last fight) should be WAR for Alice.
    assert alice["latest_job"] == "WAR"
    assert alice["classification"] == "unclassified"


def test_discovery_classifies_aliases(user_with_static):
    uname, sid, _ = user_with_static
    client = TestClient(app)
    # Mark Alice as core via classify, then verify she shows as 'core'.
    r = client.post("/api/roster/classify", json={
        "character_name": "Alice Tankerton", "server": "Aether",
        "action": "core",
    }, headers=_auth(uname))
    assert r.status_code == 200
    r2 = client.get("/api/roster/characters", headers=_auth(uname))
    alice = next(c for c in r2.json()["characters"]
                 if c["character_name"] == "Alice Tankerton")
    assert alice["classification"] == "core"
    assert alice["linked_member_name"] == "Alice Tankerton"


def test_discovery_marks_subs_when_owner_has_multiple_aliases(user_with_static):
    uname, _, _ = user_with_static
    client = TestClient(app)
    # Make Alice the primary (core, member created), then attach Bob as her
    # sub-account. Bob should now classify as 'sub'.
    r = client.post("/api/roster/classify", json={
        "character_name": "Alice Tankerton", "server": "Aether",
        "action": "core", "member_name": "Alice",
    }, headers=_auth(uname))
    assert r.status_code == 200
    alice_member_id = r.json()["member_id"]
    r2 = client.post("/api/roster/classify", json={
        "character_name": "Bob Heals", "server": "Aether",
        "action": "sub", "member_id": alice_member_id,
    }, headers=_auth(uname))
    assert r2.status_code == 200
    chars = client.get("/api/roster/characters",
                       headers=_auth(uname)).json()["characters"]
    bob = next(c for c in chars if c["character_name"] == "Bob Heals")
    assert bob["classification"] == "sub"
    assert bob["linked_member_name"] == "Alice"
    # v1.15.1 regression: the PRIMARY alias (Alice Tankerton, attached first)
    # must stay classified as the member's kind ("core"), not "sub". Previously
    # both Alice + Bob both showed up as each other's sub.
    alice = next(c for c in chars if c["character_name"] == "Alice Tankerton")
    assert alice["classification"] == "core"
    assert alice["linked_member_name"] == "Alice"


def test_classify_ignore_then_clear(user_with_static):
    uname, _, _ = user_with_static
    client = TestClient(app)
    r = client.post("/api/roster/classify", json={
        "character_name": "Dan Pug", "server": "Aether", "action": "ignore",
    }, headers=_auth(uname))
    assert r.status_code == 200
    chars = client.get("/api/roster/characters",
                       headers=_auth(uname)).json()["characters"]
    dan = next(c for c in chars if c["character_name"] == "Dan Pug")
    assert dan["classification"] == "ignored"
    # Clear returns him to unclassified.
    r2 = client.post("/api/roster/classify", json={
        "character_name": "Dan Pug", "server": "Aether", "action": "clear",
    }, headers=_auth(uname))
    assert r2.status_code == 200
    chars = client.get("/api/roster/characters",
                       headers=_auth(uname)).json()["characters"]
    dan = next(c for c in chars if c["character_name"] == "Dan Pug")
    assert dan["classification"] == "unclassified"


def test_classify_switching_action_replaces_prior_state(user_with_static):
    """Marking 'ignore' then 'core' should leave Alice classified as core with
    no lingering ignore row (which would otherwise leave her ambiguous)."""
    uname, _, _ = user_with_static
    client = TestClient(app)
    client.post("/api/roster/classify", json={
        "character_name": "Alice Tankerton", "server": "Aether",
        "action": "ignore",
    }, headers=_auth(uname))
    client.post("/api/roster/classify", json={
        "character_name": "Alice Tankerton", "server": "Aether",
        "action": "core",
    }, headers=_auth(uname))
    chars = client.get("/api/roster/characters",
                       headers=_auth(uname)).json()["characters"]
    alice = next(c for c in chars if c["character_name"] == "Alice Tankerton")
    assert alice["classification"] == "core"


def test_classify_sub_without_member_id_returns_422(user_with_static):
    uname, _, _ = user_with_static
    client = TestClient(app)
    r = client.post("/api/roster/classify", json={
        "character_name": "Alice Tankerton", "server": "Aether",
        "action": "sub",
    }, headers=_auth(uname))
    assert r.status_code == 422


def test_classify_invalid_action_rejected(user_with_static):
    uname, _, _ = user_with_static
    client = TestClient(app)
    r = client.post("/api/roster/classify", json={
        "character_name": "Alice Tankerton", "server": "Aether",
        "action": "leader",
    }, headers=_auth(uname))
    assert r.status_code == 422


def test_discovery_isolated_per_static(user_with_static):
    """A second user in a different static must NOT see this static's
    characters."""
    uname, _, _ = user_with_static
    other = f"test_other_{uuid.uuid4().hex[:8]}"
    client = TestClient(app)
    client.get("/api/me", headers=_auth(other))
    r = client.post("/api/statics", json={"name": f"Other_{other}"},
                    headers=_auth(other))
    other_sid = r.json()["id"]
    try:
        r2 = client.get("/api/roster/characters", headers=_auth(other))
        assert r2.status_code == 200
        assert r2.json()["characters"] == []
    finally:
        # Teardown the second user.
        with SessionLocal() as s:
            u = s.execute(select(User).where(User.username == other)).scalar_one_or_none()
            s.execute(delete(StaticMembership).where(
                StaticMembership.static_id == other_sid))
            s.execute(delete(Static).where(Static.id == other_sid))
            if u is not None:
                u.current_static_id = None
                s.flush()
                s.execute(delete(StaticMembership).where(StaticMembership.user_id == u.id))
                s.delete(u)
            s.commit()


def test_ignore_is_idempotent(user_with_static):
    """Marking the same character ignore twice should not fail or duplicate."""
    uname, _, _ = user_with_static
    client = TestClient(app)
    for _ in range(2):
        r = client.post("/api/roster/classify", json={
            "character_name": "Dan Pug", "server": "Aether",
            "action": "ignore",
        }, headers=_auth(uname))
        assert r.status_code == 200
    # Only one row should exist.
    with SessionLocal() as s:
        n = s.execute(select(IgnoredCharacter).where(
            IgnoredCharacter.character_name == "Dan Pug",
            IgnoredCharacter.server == "Aether",
        )).scalars().all()
        assert len(n) == 1
