"""T-108 API tests: review queue + user-override endpoints."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from api.main import app
from db.models import Ability, AbilityLabel
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)

# Disposable IDs unlikely to collide with real XIVAPI IDs we'll ingest later.
HIGH = 990001
LOW = 990002
MISSING = 990003


@pytest.fixture(autouse=True)
def _clean():
    yield
    ids = (HIGH, LOW, MISSING)
    with SessionLocal() as s:
        s.execute(delete(AbilityLabel).where(AbilityLabel.ability_game_id.in_(ids)))
        s.execute(delete(Ability).where(Ability.ability_game_id.in_(ids)))
        s.commit()


def _seed(s):
    now = datetime.now(timezone.utc)
    s.add(Ability(ability_game_id=HIGH, kind="action", name="High Conf",
                  description="d", fetched_at=now))
    s.add(Ability(ability_game_id=LOW, kind="action", name="Low Conf",
                  description="d", fetched_at=now))
    s.add(Ability(ability_game_id=MISSING, kind="action", name="No Label",
                  description="d", fetched_at=now))
    s.add(AbilityLabel(ability_game_id=HIGH, label="raid_buff",
                       confidence=0.95, source="auto", updated_at=now))
    s.add(AbilityLabel(ability_game_id=LOW, label="ignore",
                       confidence=0.3, source="auto", updated_at=now))
    # MISSING gets no label row → must show in review queue.


client = TestClient(app)


def test_review_queue_includes_low_confidence_and_missing():
    with SessionLocal() as s:
        _seed(s)
        s.commit()
    # Dev DB carries hundreds of live-bootstrapped rows, so pull a high limit
    # to be sure our seeded rows are in the slice.
    ids = {r["ability_game_id"]
           for r in client.get("/api/abilities/review-queue?limit=5000").json()}
    assert LOW in ids
    assert MISSING in ids
    assert HIGH not in ids


def test_user_override_locks_label_as_user_source():
    with SessionLocal() as s:
        _seed(s)
        s.commit()
    r = client.patch(f"/api/abilities/{LOW}/label",
                     json={"label": "mit_party", "notes": "obvious from cooldown timing"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["label"] == "mit_party"
    assert body["source"] == "user"
    assert body["confidence"] == 1.0
    # And it disappears from the review queue (source='user').
    rq_ids = {r["ability_game_id"] for r in client.get("/api/abilities/review-queue").json()}
    assert LOW not in rq_ids


def test_user_override_rejects_unknown_label():
    with SessionLocal() as s:
        _seed(s)
        s.commit()
    r = client.patch(f"/api/abilities/{LOW}/label",
                     json={"label": "not_a_real_label"})
    assert r.status_code == 422


def test_user_override_404_when_ability_missing():
    r = client.patch("/api/abilities/9999998/label", json={"label": "ignore"})
    assert r.status_code == 404


def test_list_labels_filter_by_label():
    with SessionLocal() as s:
        _seed(s)
        s.commit()
    rows = client.get("/api/abilities/labels?label=raid_buff").json()
    assert any(r["ability_game_id"] == HIGH for r in rows)
    assert all(r["label"] == "raid_buff" for r in rows if r["label"] is not None)


def test_set_label_creates_row_when_missing():
    with SessionLocal() as s:
        _seed(s)
        s.commit()
    r = client.patch(f"/api/abilities/{MISSING}/label", json={"label": "ignore"})
    assert r.status_code == 200
    assert r.json()["source"] == "user"
    # Now in /labels. High limit because live bootstrap fills the table.
    rows = client.get("/api/abilities/labels?label=ignore&limit=5000").json()
    assert any(rr["ability_game_id"] == MISSING for rr in rows)


def test_review_queue_kind_filter():
    """kind=status should restrict the queue to status abilities only."""
    with SessionLocal() as s:
        _seed(s)  # all are kind=action
        # Add one of kind=status (low confidence so it appears in the queue)
        now = datetime.now(timezone.utc)
        s.add(Ability(ability_game_id=990010, kind="status",
                      name="Status thing", description="d", fetched_at=now))
        s.add(AbilityLabel(ability_game_id=990010, label="ignore",
                           confidence=0.2, source="auto", updated_at=now))
        s.commit()
    try:
        rows = client.get(
            "/api/abilities/review-queue?kind=status&limit=5000"
        ).json()
        ids = {r["ability_game_id"] for r in rows}
        assert 990010 in ids
        assert LOW not in ids  # action, should be filtered out
        assert MISSING not in ids  # also action
    finally:
        with SessionLocal() as s:
            s.execute(delete(AbilityLabel).where(AbilityLabel.ability_game_id == 990010))
            s.execute(delete(Ability).where(Ability.ability_game_id == 990010))
            s.commit()


def test_review_queue_current_label_filter():
    """current_label='ignore' should restrict the queue to abilities currently
    labeled 'ignore'. Empty string filters for rows with no label at all."""
    with SessionLocal() as s:
        _seed(s)
        s.commit()
    rows = client.get(
        "/api/abilities/review-queue?current_label=ignore&limit=5000"
    ).json()
    ids = {r["ability_game_id"] for r in rows}
    assert LOW in ids  # has label 'ignore' at low conf
    assert MISSING not in ids  # no label

    # empty string -> no label
    rows = client.get(
        "/api/abilities/review-queue?current_label=&limit=5000"
    ).json()
    ids = {r["ability_game_id"] for r in rows}
    assert MISSING in ids
    assert LOW not in ids


def test_bulk_set_labels_writes_user_source():
    with SessionLocal() as s:
        _seed(s)
        s.commit()
    r = client.patch("/api/abilities/labels/bulk",
                     json={"ability_ids": [LOW, MISSING], "label": "ignore"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["updated"] == 2
    assert body["skipped_unknown_ids"] == []
    # Verify both are now user-source
    with SessionLocal() as s:
        for aid in (LOW, MISSING):
            lbl = s.get(AbilityLabel, aid)
            assert lbl is not None
            assert lbl.label == "ignore"
            assert lbl.source == "user"
            assert lbl.confidence == 1.0


def test_bulk_set_labels_skips_unknown_ids():
    with SessionLocal() as s:
        _seed(s)
        s.commit()
    bogus = 9_999_997
    r = client.patch("/api/abilities/labels/bulk",
                     json={"ability_ids": [LOW, bogus], "label": "ignore"})
    assert r.status_code == 200
    body = r.json()
    assert body["updated"] == 1
    assert body["skipped_unknown_ids"] == [bogus]


def test_bulk_set_labels_rejects_invalid_label():
    r = client.patch("/api/abilities/labels/bulk",
                     json={"ability_ids": [1, 2], "label": "not_a_real_label"})
    assert r.status_code == 422


def test_bulk_set_labels_empty_list_is_noop():
    r = client.patch("/api/abilities/labels/bulk",
                     json={"ability_ids": [], "label": "ignore"})
    assert r.status_code == 200
    assert r.json() == {"updated": 0, "skipped_unknown_ids": []}


def test_labels_endpoint_exposes_duration_ms():
    """v1.5.7 adds abilities.duration_ms; v1.5.8 surfaces it so the strat
    editor's window overlay can size mit bars correctly."""
    with SessionLocal() as s:
        _seed(s)
        s.commit()
    with SessionLocal() as s:
        a = s.get(Ability, HIGH)
        a.duration_ms = 30_000
        s.commit()
    rows = client.get("/api/abilities/labels?label=raid_buff&limit=5000").json()
    match = [r for r in rows if r["ability_game_id"] == HIGH]
    assert len(match) == 1
    assert match[0]["duration_ms"] == 30_000
