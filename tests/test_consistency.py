"""T-306 consistency-per-mechanic tests."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete

from analysis.consistency import consistency_for_encounter
from db.models import (
    Combatant, Event, Fight, FightModel, IngestionLedger,
    Report, WatchedReport,
)
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)

ENC = 50306
CODES = ("T306_OUR_1", "T306_OUR_2")


@pytest.fixture
def seeded():
    """2 of our pulls (in WatchedReport).
    Mechanic 200 fires once per pull, 5s after fight start.
    Mechanic 300 fires once per pull, 30s after fight start.
    P1 dies to mechanic 200 in pull 1 (=> 200 clean rate 0.5).
    Both pulls clear mechanic 300 (=> 300 clean rate 1.0).
    """
    fight_ids: list[int] = []
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        for code in CODES:
            s.add(Report(code=code, ingested_at=now))
        s.flush()
        for code in CODES:
            s.add(WatchedReport(static_id=1, code=code, active=True, added_at=now))
        # FightModel: 200 and 300 are canonical raidwides
        for aid, seq in [(200, 0), (300, 1)]:
            s.add(FightModel(encounter_id=ENC, version=1, phase=0, seq=seq,
                              ability_game_id=aid, type_label="raidwide",
                              relative_t_ms=5000 if aid == 200 else 30_000,
                              time_variance_ms=0, confidence=1.0,
                              meta={}, updated_at=now))
        BOSS = 9999
        for i, code in enumerate(CODES):
            f = Fight(report_code=code, fight_id_in_report=1,
                      encounter_id=ENC, is_kill=False,
                      start_time=0, end_time=60_000, duration_ms=60_000)
            s.add(f)
            s.flush()
            fight_ids.append(f.id)
            s.add(Combatant(fight_id=f.id, player_id=1, name="P1", job="WAR"))
            s.add(Event(fight_id=f.id, ts=0, type="cast", source_id=1, ability_game_id=999))
            # Boss casts both mechanics
            s.add(Event(fight_id=f.id, ts=5_000, type="cast",
                        source_id=BOSS, ability_game_id=200))
            s.add(Event(fight_id=f.id, ts=30_000, type="cast",
                        source_id=BOSS, ability_game_id=300))
        # Pull 1: P1 dies 200ms after mechanic 200
        s.add(Event(fight_id=fight_ids[0], ts=5_200, type="death",
                    source_id=BOSS, target_id=1, ability_game_id=200))
        s.commit()
        try:
            yield s
        finally:
            s.execute(delete(WatchedReport).where(WatchedReport.code.in_(CODES)))
            s.execute(delete(FightModel).where(FightModel.encounter_id == ENC))
            s.execute(delete(Event).where(Event.fight_id.in_(fight_ids)))
            s.execute(delete(Combatant).where(Combatant.fight_id.in_(fight_ids)))
            s.execute(delete(Fight).where(Fight.id.in_(fight_ids)))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code.in_(CODES)))
            s.execute(delete(Report).where(Report.code.in_(CODES)))
            s.commit()


def test_clean_rate_per_mechanic(seeded):
    session = seeded
    r = consistency_for_encounter(session, ENC, 1)
    by_aid = {m["ability_game_id"]: m for m in r["mechanics"]}
    assert by_aid[200]["occurrences_total"] == 2
    assert by_aid[200]["occurrences_clean"] == 1
    assert by_aid[200]["clean_rate"] == 0.5
    assert by_aid[300]["clean_rate"] == 1.0


def test_our_pulls_count(seeded):
    session = seeded
    r = consistency_for_encounter(session, ENC, 1)
    assert r["our_pulls"] == 2


def test_sorted_worst_first(seeded):
    session = seeded
    r = consistency_for_encounter(session, ENC, 1)
    rates = [m["clean_rate"] for m in r["mechanics"]]
    assert rates == sorted(rates)


def test_no_fight_model_returns_note():
    with SessionLocal() as s:
        r = consistency_for_encounter(s, 9_999_999, 1)
    assert r["mechanics"] == []
    assert "note" in r


def test_no_watched_reports_returns_note(seeded):
    session = seeded
    # Remove the watchlist rows mid-fixture
    session.execute(delete(WatchedReport).where(WatchedReport.code.in_(CODES)))
    session.commit()
    r = consistency_for_encounter(session, ENC, 1)
    assert r["our_pulls"] == 0
    assert "note" in r
