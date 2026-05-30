"""T-202 fight-model persistence tests."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete

from analysis.consensus import (
    read_fight_model,
    write_consensus_to_fight_model,
)
from db.models import Combatant, Event, Fight, FightModel, IngestionLedger, Report
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)

# Use an encounter id that won't collide with real ingested encounters.
TEST_ENCOUNTER = 654321


@pytest.fixture
def three_pulls():
    codes = ("T202_A", "T202_B", "T202_C")
    fight_ids: list[int] = []
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        for code in codes:
            s.add(Report(code=code, ingested_at=now))
        s.flush()
        BOSS, PLAYER = 9999, 1
        for i, code in enumerate(codes):
            f = Fight(report_code=code, fight_id_in_report=1,
                      encounter_id=TEST_ENCOUNTER, is_kill=True,
                      start_time=0, end_time=30_000, duration_ms=30_000)
            s.add(f)
            s.flush()
            fight_ids.append(f.id)
            s.add(Combatant(fight_id=f.id, player_id=PLAYER, name="P", job="WAR"))
            for j in range(30):
                s.add(Event(fight_id=f.id, ts=j * 800, type="damage",
                            source_id=PLAYER, target_id=BOSS,
                            ability_game_id=999, amount=100))
            s.add(Event(fight_id=f.id, ts=10_000 + i * 100,
                        type="cast", source_id=BOSS,
                        ability_game_id=555))
        s.commit()
        try:
            yield s
        finally:
            s.execute(delete(FightModel).where(
                FightModel.encounter_id == TEST_ENCOUNTER))
            s.execute(delete(Event).where(Event.fight_id.in_(fight_ids)))
            s.execute(delete(Combatant).where(Combatant.fight_id.in_(fight_ids)))
            s.execute(delete(Fight).where(Fight.id.in_(fight_ids)))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code.in_(codes)))
            s.execute(delete(Report).where(Report.code.in_(codes)))
            s.commit()


def test_write_creates_fight_model_rows(three_pulls):
    session = three_pulls
    summary = write_consensus_to_fight_model(session, TEST_ENCOUNTER)
    assert summary["phases_written"] >= 1
    assert summary["abilities_written"] >= 1
    rows = session.execute(
        FightModel.__table__.select()
        .where(FightModel.encounter_id == TEST_ENCOUNTER)
    ).all()
    assert len(rows) == summary["abilities_written"]


def test_write_is_idempotent_replaces_old_rows(three_pulls):
    session = three_pulls
    s1 = write_consensus_to_fight_model(session, TEST_ENCOUNTER)
    s2 = write_consensus_to_fight_model(session, TEST_ENCOUNTER)
    # Same input → same row count after second write (deleted + reinserted)
    assert s1["abilities_written"] == s2["abilities_written"]
    rows = session.execute(
        FightModel.__table__.select()
        .where(FightModel.encounter_id == TEST_ENCOUNTER)
    ).all()
    assert len(rows) == s2["abilities_written"]


def test_seq_is_ordered_by_relative_t(three_pulls):
    session = three_pulls
    # Add a second canonical ability that fires earlier than 555 in all pulls.
    earlier_ability = 444
    for fid in session.execute(
        FightModel.__table__.select()
        .where(FightModel.encounter_id == TEST_ENCOUNTER)
    ).all():
        pass  # noop
    # Fetch each fight's BOSS to inject 444 cast at ts=5000
    for f in session.query(Fight).filter(Fight.encounter_id == TEST_ENCOUNTER).all():
        session.add(Event(fight_id=f.id, ts=5000, type="cast",
                          source_id=9999, ability_game_id=earlier_ability))
    session.commit()
    write_consensus_to_fight_model(session, TEST_ENCOUNTER)
    model = read_fight_model(session, TEST_ENCOUNTER)
    phase0 = model["phases"][0]
    # seq=0 should be the earlier ability (444); seq=1 should be 555
    assert phase0["abilities"][0]["ability_game_id"] == earlier_ability
    assert phase0["abilities"][0]["seq"] == 0


def test_write_with_insufficient_pulls_returns_note():
    """No fights for this encounter → no rows written, note explains why."""
    with SessionLocal() as s:
        summary = write_consensus_to_fight_model(s, 999_999_999)
    assert summary["abilities_written"] == 0
    assert summary["phases_written"] == 0
    assert "note" in summary


def test_read_fight_model_empty_when_no_rows():
    with SessionLocal() as s:
        body = read_fight_model(s, 999_999_999)
    assert body["phases"] == []
