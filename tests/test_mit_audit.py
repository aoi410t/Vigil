"""T-303 M-MIT mitigation audit tests."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete

from analysis.mit_audit import mit_audit_for_fight, mit_audit_summary
from analysis.strat_config import encode_mechanic_ref, upsert as strat_upsert
from db.models import (
    Combatant, Event, Fight, FightModel,
    IngestionLedger, Report, StratConfig,
)
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)

ENC = 50303
CODE = "T303_AUDIT"


@pytest.fixture
def seeded():
    """One wipe fight with:
      - 2 raidwide casts of ability 200 (Reprisal-able)
      - one Reprisal (ability 7535) cast 2s before the first raidwide
      - second raidwide has NO mit fired
      - strat_config expects Reprisal on both raidwides
    """
    fid_holder = []
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        s.add(Report(code=CODE, ingested_at=now))
        s.flush()
        f = Fight(report_code=CODE, fight_id_in_report=1,
                  encounter_id=ENC, is_kill=False,
                  start_time=0, end_time=60_000, duration_ms=60_000)
        s.add(f)
        s.flush()
        fid_holder.append(f.id)
        # Players
        for pid in (1, 2):
            s.add(Combatant(fight_id=f.id, player_id=pid, name=f"P{pid}", job="WAR"))
            s.add(Event(fight_id=f.id, ts=0, type="cast",
                        source_id=pid, ability_game_id=8888))
        # Raidwide casts at t=10s and t=30s
        BOSS = 9999
        s.add(Event(fight_id=f.id, ts=10_000, type="cast",
                    source_id=BOSS, ability_game_id=200))
        s.add(Event(fight_id=f.id, ts=30_000, type="cast",
                    source_id=BOSS, ability_game_id=200))
        # Reprisal applied at t=8000 (within window of first raidwide; outside second)
        s.add(Event(fight_id=f.id, ts=8000, type="applybuff",
                    source_id=1, target_id=BOSS, ability_game_id=7535))
        # FightModel: ability 200 is the raidwide
        s.add(FightModel(encounter_id=ENC, version=1, phase=0, seq=0,
                         ability_game_id=200, type_label="raidwide",
                         relative_t_ms=10_000, time_variance_ms=0,
                         confidence=1.0, meta={}, updated_at=now))
        s.commit()
        # Strat: expect Reprisal on both occurrences
        strat_upsert(s, ENC, encode_mechanic_ref(200, 0),
                     assignments=None,
                     mit_plan={"slots": [{"ability_id": 7535,
                                          "expected_role": "MT",
                                          "window_offset_ms": -2000}]},
                     static_id=1)
        strat_upsert(s, ENC, encode_mechanic_ref(200, 1),
                     assignments=None,
                     mit_plan={"slots": [{"ability_id": 7535,
                                          "expected_role": "MT",
                                          "window_offset_ms": -2000}]},
                     static_id=1)
        try:
            yield s, f.id
        finally:
            s.execute(delete(StratConfig).where(StratConfig.encounter_id == ENC))
            s.execute(delete(FightModel).where(FightModel.encounter_id == ENC))
            s.execute(delete(Event).where(Event.fight_id.in_(fid_holder)))
            s.execute(delete(Combatant).where(Combatant.fight_id.in_(fid_holder)))
            s.execute(delete(Fight).where(Fight.id.in_(fid_holder)))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code == CODE))
            s.execute(delete(Report).where(Report.code == CODE))
            s.commit()


def test_audit_finds_both_raidwide_casts(seeded):
    session, fid = seeded
    audit = mit_audit_for_fight(session, fid, 1)
    assert len(audit["raidwide_casts"]) == 2
    assert audit["raidwide_casts"][0]["occurrence"] == 0
    assert audit["raidwide_casts"][1]["occurrence"] == 1


def test_first_raidwide_has_mit_fired(seeded):
    session, fid = seeded
    audit = mit_audit_for_fight(session, fid, 1)
    first = audit["raidwide_casts"][0]
    assert len(first["planned_slots"]) == 1
    assert first["planned_slots"][0]["fired"] is True
    assert first["missed_count"] == 0


def test_second_raidwide_missed_mit(seeded):
    session, fid = seeded
    audit = mit_audit_for_fight(session, fid, 1)
    second = audit["raidwide_casts"][1]
    assert second["planned_slots"][0]["fired"] is False
    assert second["missed_count"] == 1


def test_no_plan_flag_set_when_strat_missing():
    """A raidwide cast with no strat_config row should have no_plan=True."""
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        code = "T303_NOPLAN"
        s.add(Report(code=code, ingested_at=now))
        s.flush()
        f = Fight(report_code=code, fight_id_in_report=1,
                  encounter_id=88888, is_kill=False,
                  start_time=0, end_time=10_000, duration_ms=10_000)
        s.add(f)
        s.flush()
        s.add(Event(fight_id=f.id, ts=5000, type="cast",
                    source_id=9999, ability_game_id=200))
        s.add(FightModel(encounter_id=88888, version=1, phase=0, seq=0,
                         ability_game_id=200, type_label="raidwide",
                         relative_t_ms=5000, time_variance_ms=0,
                         confidence=1.0, meta={}, updated_at=now))
        s.commit()
        try:
            audit = mit_audit_for_fight(s, f.id, 1)
            assert audit["raidwide_casts"][0]["no_plan"] is True
            assert audit["raidwide_casts"][0]["planned_slots"] == []
        finally:
            s.execute(delete(FightModel).where(FightModel.encounter_id == 88888))
            s.execute(delete(Event).where(Event.fight_id == f.id))
            s.execute(delete(Fight).where(Fight.id == f.id))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code == code))
            s.execute(delete(Report).where(Report.code == code))
            s.commit()


def test_summary_aggregates(seeded):
    session, fid = seeded
    summary = mit_audit_summary(session, fid, 1)
    assert summary["raidwide_count"] == 2
    assert summary["with_plan"] == 2
    assert summary["missing_plan"] == 0
    assert summary["planned_slots_total"] == 2
    assert summary["missed_mits_total"] == 1
    assert summary["mit_hit_rate"] == 0.5


def test_no_raidwides_returns_note():
    """Fight whose encounter has no labeled raidwides → friendly note."""
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        code = "T303_NONE"
        s.add(Report(code=code, ingested_at=now))
        s.flush()
        f = Fight(report_code=code, fight_id_in_report=1,
                  encounter_id=77777, is_kill=False,
                  start_time=0, end_time=10_000, duration_ms=10_000)
        s.add(f)
        s.commit()
        try:
            audit = mit_audit_for_fight(s, f.id, 1)
            assert audit["raidwide_casts"] == []
            assert "note" in audit
        finally:
            s.execute(delete(Fight).where(Fight.id == f.id))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code == code))
            s.execute(delete(Report).where(Report.code == code))
            s.commit()


def test_unknown_fight_returns_note():
    with SessionLocal() as s:
        audit = mit_audit_for_fight(s, -1, 1)
    assert audit["raidwide_casts"] == []
