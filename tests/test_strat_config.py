"""T-301 strat_config tests — encoding, validation, CRUD, API."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from analysis.strat_config import (
    ROLES,
    decode_mechanic_ref,
    encode_mechanic_ref,
    validate_assignments,
    validate_mit_plan,
)
from api.main import app
from db.models import StratConfig
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)

ENC = 50301
CLEANUP_ENCOUNTERS = (ENC,)


@pytest.fixture(autouse=True)
def _clean():
    yield
    with SessionLocal() as s:
        s.execute(delete(StratConfig).where(
            StratConfig.encounter_id.in_(CLEANUP_ENCOUNTERS)))
        s.commit()


# ---- pure-function tests ----

def test_encode_decode_roundtrip():
    assert decode_mechanic_ref(encode_mechanic_ref(16552, 0)) == (16552, 0)
    assert decode_mechanic_ref(encode_mechanic_ref(7535, 3)) == (7535, 3)


def test_encode_rejects_negative():
    with pytest.raises(ValueError):
        encode_mechanic_ref(-1, 0)
    with pytest.raises(ValueError):
        encode_mechanic_ref(16552, -1)


def test_decode_rejects_malformed():
    for bad in ("", "abc", "16552", "16552_", "_0", "16552_0_1"):
        with pytest.raises(ValueError):
            decode_mechanic_ref(bad)


def test_validate_mit_plan_canonical_shape():
    out = validate_mit_plan({"slots": [
        {"ability_id": 7535, "expected_role": "MT", "window_offset_ms": -2000},
        {"ability_id": 7382, "expected_role": "any"},
    ]})
    assert len(out["slots"]) == 2
    assert out["slots"][0]["window_offset_ms"] == -2000
    assert out["slots"][1]["window_offset_ms"] == 0


def test_validate_mit_plan_rejects_bad_role():
    with pytest.raises(ValueError):
        validate_mit_plan({"slots": [{"ability_id": 1, "expected_role": "NOPE"}]})


def test_validate_mit_plan_empty_defaults():
    assert validate_mit_plan(None) == {"slots": []}
    assert validate_mit_plan({}) == {"slots": []}


def test_validate_assignments_all_roles_accepted():
    payload = {"role_map": {f"slot_{r}": r for r in ROLES}}
    out = validate_assignments(payload)
    assert set(out["role_map"].values()) == set(ROLES)


def test_validate_assignments_rejects_bad_role():
    with pytest.raises(ValueError):
        validate_assignments({"role_map": {"x": "WAR"}})


def test_validate_assignments_allows_any_and_null():
    out = validate_assignments({"role_map": {"a": "any", "b": None}})
    assert out["role_map"]["a"] == "any"
    assert out["role_map"]["b"] is None


# ---- API CRUD ----

client = TestClient(app)


def test_list_empty():
    r = client.get(f"/api/encounters/{ENC}/strat-config")
    assert r.status_code == 200
    body = r.json()
    assert body["encounter_id"] == ENC
    assert body["rows"] == []
    assert list(body["roles"]) == list(ROLES)


def test_put_creates_then_get_returns_it():
    body = {
        "assignments": {"role_map": {"tower_n": "MT", "tower_s": "OT"}},
        "mit_plan": {"slots": [
            {"ability_id": 7535, "expected_role": "MT", "window_offset_ms": -2000}
        ]},
    }
    r = client.put(f"/api/encounters/{ENC}/strat-config/16552_0", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["mechanic_ref"] == "16552_0"
    assert out["ability_game_id"] == 16552
    assert out["occurrence"] == 0
    assert out["assignments"]["role_map"]["tower_n"] == "MT"
    assert out["mit_plan"]["slots"][0]["ability_id"] == 7535

    r2 = client.get(f"/api/encounters/{ENC}/strat-config/16552_0")
    assert r2.status_code == 200
    assert r2.json()["mit_plan"]["slots"][0]["ability_id"] == 7535


def test_compound_key_separates_occurrences():
    """Akh Morn x4 — first cast wants no mit, second wants Reprisal, etc.
    Per-occurrence keying must allow distinct configs."""
    payloads = [
        ({"slots": []}),
        ({"slots": [{"ability_id": 7535, "expected_role": "any"}]}),
        ({"slots": [{"ability_id": 7382, "expected_role": "MT"}]}),
        ({"slots": [{"ability_id": 7405, "expected_role": "any"}]}),
    ]
    for i, plan in enumerate(payloads):
        r = client.put(f"/api/encounters/{ENC}/strat-config/99999_{i}",
                       json={"mit_plan": plan, "assignments": None})
        assert r.status_code == 200, r.text

    rows = client.get(f"/api/encounters/{ENC}/strat-config").json()["rows"]
    by_occ = {r["occurrence"]: r["mit_plan"]["slots"] for r in rows
              if r["ability_game_id"] == 99999}
    assert by_occ[0] == []
    assert by_occ[1][0]["ability_id"] == 7535
    assert by_occ[2][0]["ability_id"] == 7382
    assert by_occ[3][0]["ability_id"] == 7405


def test_put_replaces_existing():
    client.put(f"/api/encounters/{ENC}/strat-config/16552_0",
               json={"mit_plan": {"slots": [{"ability_id": 1}]},
                     "assignments": None})
    client.put(f"/api/encounters/{ENC}/strat-config/16552_0",
               json={"mit_plan": {"slots": [{"ability_id": 2}]},
                     "assignments": None})
    out = client.get(f"/api/encounters/{ENC}/strat-config/16552_0").json()
    assert len(out["mit_plan"]["slots"]) == 1
    assert out["mit_plan"]["slots"][0]["ability_id"] == 2


def test_get_404_when_missing():
    r = client.get(f"/api/encounters/{ENC}/strat-config/16552_0")
    assert r.status_code == 404


def test_delete_works_and_returns_404_after():
    client.put(f"/api/encounters/{ENC}/strat-config/16552_0",
               json={"mit_plan": None, "assignments": None})
    assert client.delete(f"/api/encounters/{ENC}/strat-config/16552_0").status_code == 204
    assert client.delete(f"/api/encounters/{ENC}/strat-config/16552_0").status_code == 404


def test_put_rejects_malformed_mechanic_ref():
    r = client.put(f"/api/encounters/{ENC}/strat-config/garbage",
                   json={"mit_plan": None, "assignments": None})
    assert r.status_code == 422


def test_put_rejects_bad_role_in_assignments():
    r = client.put(f"/api/encounters/{ENC}/strat-config/16552_0",
                   json={"assignments": {"role_map": {"x": "WARRIOR"}}})
    assert r.status_code == 422


def test_put_rejects_bad_mit_plan_shape():
    r = client.put(f"/api/encounters/{ENC}/strat-config/16552_0",
                   json={"mit_plan": {"slots": [{"expected_role": "MT"}]}})  # no ability_id
    assert r.status_code == 422
