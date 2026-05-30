"""T-305 recovery/resilience tests."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete

from analysis.recovery import (
    FAST_REZ_THRESHOLD_MS,
    recovery_for_fight,
)
from db.models import Combatant, Event, Fight, IngestionLedger, Report
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)

CODE = "T305_REC"


@pytest.fixture
def seeded():
    """A pull with 3 deaths:
      - P1 dies at 10s, never recovered (fatal)
      - P2 dies at 20s, recovered at 23s (fast — likely Swiftcast)
      - P3 dies at 30s, recovered at 38s (slow — normal cast)
    """
    fid = [None]
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        s.add(Report(code=CODE, ingested_at=now))
        s.flush()
        f = Fight(report_code=CODE, fight_id_in_report=1,
                  encounter_id=999, is_kill=False,
                  start_time=0, end_time=60_000, duration_ms=60_000)
        s.add(f)
        s.flush()
        fid[0] = f.id
        for pid in (1, 2, 3):
            s.add(Combatant(fight_id=f.id, player_id=pid,
                            name=f"P{pid}", job="WAR"))
            # Each player casts at t=0 so the active-players filter includes them
            s.add(Event(fight_id=f.id, ts=0, type="cast",
                        source_id=pid, ability_game_id=999))
        # P1 death at 10s, no recovery
        s.add(Event(fight_id=f.id, ts=10_000, type="death",
                    source_id=9999, target_id=1, ability_game_id=555))
        # P2 death at 20s, recovery cast at 23s (fast)
        s.add(Event(fight_id=f.id, ts=20_000, type="death",
                    source_id=9999, target_id=2, ability_game_id=555))
        s.add(Event(fight_id=f.id, ts=23_000, type="cast",
                    source_id=2, ability_game_id=888))
        # P3 death at 30s, recovery cast at 38s (slow)
        s.add(Event(fight_id=f.id, ts=30_000, type="death",
                    source_id=9999, target_id=3, ability_game_id=555))
        s.add(Event(fight_id=f.id, ts=38_000, type="cast",
                    source_id=3, ability_game_id=888))
        s.commit()
        try:
            yield s, f.id
        finally:
            s.execute(delete(Event).where(Event.fight_id == fid[0]))
            s.execute(delete(Combatant).where(Combatant.fight_id == fid[0]))
            s.execute(delete(Fight).where(Fight.id == fid[0]))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code == CODE))
            s.execute(delete(Report).where(Report.code == CODE))
            s.commit()


def test_total_deaths_and_resilience(seeded):
    session, fid = seeded
    r = recovery_for_fight(session, fid)
    assert r["total_deaths"] == 3
    assert r["recovered_deaths"] == 2
    assert r["fatal_deaths"] == 1
    assert r["resilience_pct"] == round(2 / 3 * 100, 1)


def test_fast_rez_detected(seeded):
    session, fid = seeded
    r = recovery_for_fight(session, fid)
    # P2 recovered in 3s (≤5s threshold), P3 in 8s. Exactly 1 fast.
    assert r["fast_rez_count"] == 1


def test_per_event_recovery_times(seeded):
    session, fid = seeded
    r = recovery_for_fight(session, fid)
    by_pid = {e["player_id"]: e for e in r["events"]}
    assert by_pid[1]["recovered"] is False
    assert by_pid[2]["time_to_recovery_ms"] == 3_000
    assert by_pid[2]["fast"] is True
    assert by_pid[3]["time_to_recovery_ms"] == 8_000
    assert by_pid[3]["fast"] is False


def test_per_player_aggregate(seeded):
    session, fid = seeded
    r = recovery_for_fight(session, fid)
    by_pid = {p["player_id"]: p for p in r["players"]}
    assert by_pid[1]["deaths"] == 1
    assert by_pid[1]["recovered"] == 0
    assert by_pid[1]["fatal"] == 1
    assert by_pid[2]["avg_recovery_ms"] == 3_000


def test_avg_recovery_ms(seeded):
    session, fid = seeded
    r = recovery_for_fight(session, fid)
    # Avg of (3000, 8000) = 5500
    assert r["avg_recovery_ms"] == 5500


def test_no_deaths_returns_zero():
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        s.add(Report(code="T305_NONE", ingested_at=now))
        s.flush()
        f = Fight(report_code="T305_NONE", fight_id_in_report=1,
                  encounter_id=1, is_kill=True,
                  start_time=0, end_time=1000, duration_ms=1000)
        s.add(f)
        s.commit()
        try:
            r = recovery_for_fight(s, f.id)
            assert r["total_deaths"] == 0
            assert r["resilience_pct"] is None
        finally:
            s.execute(delete(Fight).where(Fight.id == f.id))
            s.execute(delete(IngestionLedger).where(
                IngestionLedger.report_code == "T305_NONE"))
            s.execute(delete(Report).where(Report.code == "T305_NONE"))
            s.commit()


def test_unknown_fight_returns_note():
    with SessionLocal() as s:
        r = recovery_for_fight(s, -1)
    assert r["total_deaths"] == 0
    assert "note" in r
