"""Schema roundtrip tests against the live Postgres dev DB (T-003)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from db.models import (
    AnalysisCache,
    Combatant,
    Event,
    FaultScore,
    Fight,
    FightModel,
    IngestionLedger,
    ProgPoint,
    Report,
    StratConfig,
)
from db.session import engine


def test_all_plan_tables_present():
    if engine is None:
        pytest.skip("DATABASE_URL not configured")
    insp = inspect(engine)
    actual = set(insp.get_table_names())
    expected = {
        "reports",
        "ingestion_ledger",
        "fights",
        "combatants",
        "events",
        "fight_model",
        "strat_config",
        "fault_scores",
        "prog_points",
        "analysis_cache",
    }
    missing = expected - actual
    assert not missing, f"missing tables: {missing}"


def test_report_fight_event_roundtrip(db_session):
    db_session.add(Report(code="TEST_RT", region="NA", is_public=True,
                          ingested_at=datetime.now(timezone.utc)))
    db_session.flush()

    fight = Fight(
        report_code="TEST_RT",
        fight_id_in_report=1,
        encounter_id=99999,
        is_kill=False,
        fight_percentage=42.5,
        last_phase=2,
        start_time=0,
        end_time=1000,
        duration_ms=1000,
    )
    db_session.add(fight)
    db_session.flush()
    assert fight.id is not None  # BIGSERIAL assigned

    db_session.add_all([
        Event(fight_id=fight.id, ts=100, type="damage", source_id=1,
              target_id=2, ability_game_id=7535, amount=12345, raw={"k": "v"}),
        Event(fight_id=fight.id, ts=200, type="death", source_id=2,
              target_id=2, ability_game_id=88888, amount=0, raw={}),
    ])
    db_session.flush()

    keyed = db_session.execute(
        select(Event).where(Event.ability_game_id == 7535, Event.fight_id == fight.id)
    ).scalar_one()
    assert keyed.amount == 12345
    assert keyed.raw == {"k": "v"}


def test_fights_unique_constraint(db_session):
    db_session.add(Report(code="TEST_UQ"))
    db_session.add_all([
        Fight(report_code="TEST_UQ", fight_id_in_report=7),
        Fight(report_code="TEST_UQ", fight_id_in_report=7),
    ])
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_ledger_array_and_status(db_session):
    db_session.add(Report(code="TEST_LEDGER"))
    db_session.flush()
    ledger = IngestionLedger(
        report_code="TEST_LEDGER",
        fights_ingested=[1, 2, 3, 5],
        last_event_ts=987654321,
        status="open",
        last_polled_at=datetime.now(timezone.utc),
    )
    db_session.add(ledger)
    db_session.flush()
    fetched = db_session.get(IngestionLedger, "TEST_LEDGER")
    assert fetched.fights_ingested == [1, 2, 3, 5]
    assert fetched.status == "open"


def test_fight_model_composite_pk(db_session):
    rows = [
        FightModel(encounter_id=1, version=1, phase=1, seq=0,
                   ability_game_id=10, type_label="raidwide", confidence=0.95),
        FightModel(encounter_id=1, version=1, phase=1, seq=1,
                   ability_game_id=11, type_label="tankbuster", confidence=0.9),
    ]
    db_session.add_all(rows)
    db_session.flush()
    rows[0].confidence = 0.99
    db_session.flush()
    refetched = db_session.get(FightModel, (1, 1, 1, 0))
    assert float(refetched.confidence) == 0.99


def test_strat_config_jsonb(db_session):
    sc = StratConfig(
        static_id=1,
        encounter_id=42,
        mechanic_ref="phase1:tower1",
        assignments={"north": "tank1", "south": "tank2"},
        mit_plan={"window_ms": 5000, "abilities": [7535, 7536]},
    )
    db_session.add(sc)
    db_session.flush()
    got = db_session.get(StratConfig, (1, 42, "phase1:tower1"))
    assert got.assignments["north"] == "tank1"
    assert got.mit_plan["abilities"] == [7535, 7536]


def test_misc_tables_smoke(db_session):
    """Ensure the rest of the §6 tables accept the obvious shape."""
    db_session.add(Report(code="TEST_MISC"))
    db_session.flush()
    fight = Fight(report_code="TEST_MISC", fight_id_in_report=1)
    db_session.add(fight)
    db_session.flush()

    db_session.add_all([
        Combatant(fight_id=fight.id, player_id=1, name="Alice",
                  server="Aether", job="PLD", stats={"sks": 2470}),
        FaultScore(static_id=1, fight_id=fight.id, player_id=1, score=0.7,
                   reasons={"avoidable_dmg": 4500}),
        ProgPoint(static_id=1, ts=datetime.now(timezone.utc), phase=3,
                  fight_percentage=12.5, pull_count=87, source="manual"),
        AnalysisCache(fight_id=fight.id, module="M-WIPE",
                      result={"phase": 2}, computed_at=datetime.now(timezone.utc)),
    ])
    db_session.flush()
