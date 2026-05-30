"""v1.9.0: per-encounter mit-audit aggregate. Rolls T-303's per-fight summary
up to the encounter level, scoped to the static's watched reports.

Tests:
- Empty case (no watched fights) returns zero shape.
- Two-raidwide fight with one mit fired, one missed → hit_rate 0.5, worst mit
  ranked by miss rate.
- Worst-mechanic surfaces the raidwide with absolute miss count.
- Fights NOT in our watchlist don't count.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete

from analysis.mit_audit import mit_audit_aggregate_for_encounter
from analysis.strat_config import encode_mechanic_ref, upsert as strat_upsert
from db.models import (
    Combatant, Event, Fight, FightModel, IngestionLedger,
    Report, StratConfig, WatchedReport,
)
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)

ENC = 50_309
CODE_WATCHED = "T190_WATCHED"
CODE_OTHER = "T190_OTHER"
STATIC_ID = 1


@pytest.fixture
def seeded_two_raidwides_one_missed():
    """One watched wipe with two raidwide casts of ability 200 (Reprisal-able).
    The first has Reprisal (7535) applied 2s prior; the second does not.
    Strat expects Reprisal on both occurrences. Static 1 watches the report."""
    fid_holder = []
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        s.add(Report(code=CODE_WATCHED, ingested_at=now))
        s.flush()
        s.add(WatchedReport(static_id=STATIC_ID, code=CODE_WATCHED,
                            active=True, added_at=now))
        f = Fight(report_code=CODE_WATCHED, fight_id_in_report=1,
                  encounter_id=ENC, is_kill=False,
                  start_time=0, end_time=60_000, duration_ms=60_000)
        s.add(f)
        s.flush()
        fid_holder.append(f.id)
        # Players + 1 cast each so they show up in active-players intersect.
        for pid in (1, 2):
            s.add(Combatant(fight_id=f.id, player_id=pid,
                            name=f"P{pid}", job="WAR"))
            s.add(Event(fight_id=f.id, ts=0, type="cast",
                        source_id=pid, ability_game_id=8888))
        BOSS = 9999
        # Two raidwide casts
        s.add(Event(fight_id=f.id, ts=10_000, type="cast",
                    source_id=BOSS, ability_game_id=200))
        s.add(Event(fight_id=f.id, ts=30_000, type="cast",
                    source_id=BOSS, ability_game_id=200))
        # Reprisal applied at t=8000 — within first raidwide's window only
        s.add(Event(fight_id=f.id, ts=8_000, type="applybuff",
                    source_id=1, target_id=BOSS, ability_game_id=7535))
        # FightModel: ability 200 = raidwide
        s.add(FightModel(encounter_id=ENC, version=1, phase=0, seq=0,
                         ability_game_id=200, type_label="raidwide",
                         relative_t_ms=10_000, time_variance_ms=0,
                         confidence=1.0, meta={}, updated_at=now))
        s.commit()
        # Strat expects Reprisal on both occurrences
        for occ in (0, 1):
            strat_upsert(s, ENC, encode_mechanic_ref(200, occ),
                         assignments=None,
                         mit_plan={"slots": [{"ability_id": 7535,
                                              "expected_role": "MT",
                                              "window_offset_ms": -2000}]},
                         static_id=STATIC_ID)
        try:
            yield s
        finally:
            s.execute(delete(StratConfig).where(StratConfig.encounter_id == ENC))
            s.execute(delete(FightModel).where(FightModel.encounter_id == ENC))
            s.execute(delete(Event).where(Event.fight_id.in_(fid_holder)))
            s.execute(delete(Combatant).where(Combatant.fight_id.in_(fid_holder)))
            s.execute(delete(Fight).where(Fight.id.in_(fid_holder)))
            s.execute(delete(WatchedReport).where(WatchedReport.code == CODE_WATCHED))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code == CODE_WATCHED))
            s.execute(delete(Report).where(Report.code == CODE_WATCHED))
            s.commit()


def test_aggregate_empty_when_no_watched_fights():
    with SessionLocal() as s:
        result = mit_audit_aggregate_for_encounter(s, 99_999_999, STATIC_ID)
    assert result["fights_aggregated"] == 0
    assert result["raidwide_casts"] == 0
    assert result["planned_slots_total"] == 0
    assert result["mit_hit_rate"] is None
    assert result["worst_mits"] == []
    assert result["worst_mechanics"] == []


def test_aggregate_counts_planned_and_missed(seeded_two_raidwides_one_missed):
    s = seeded_two_raidwides_one_missed
    result = mit_audit_aggregate_for_encounter(s, ENC, STATIC_ID)
    assert result["fights_aggregated"] == 1
    assert result["raidwide_casts"] == 2
    assert result["planned_slots_total"] == 2  # 2 occurrences × 1 mit each
    assert result["missed_mits_total"] == 1
    assert result["mit_hit_rate"] == 0.5


def test_worst_mit_surfaces_reprisal(seeded_two_raidwides_one_missed):
    s = seeded_two_raidwides_one_missed
    result = mit_audit_aggregate_for_encounter(s, ENC, STATIC_ID)
    assert len(result["worst_mits"]) == 1
    m = result["worst_mits"][0]
    assert m["ability_id"] == 7535
    assert m["planned"] == 2
    assert m["missed"] == 1
    assert m["miss_rate"] == 0.5


def test_worst_mechanic_surfaces_raidwide(seeded_two_raidwides_one_missed):
    s = seeded_two_raidwides_one_missed
    result = mit_audit_aggregate_for_encounter(s, ENC, STATIC_ID)
    assert len(result["worst_mechanics"]) == 1
    m = result["worst_mechanics"][0]
    assert m["ability_id"] == 200
    assert m["occurrences"] == 2
    assert m["planned_slots"] == 2
    assert m["missed"] == 1
    assert m["miss_rate"] == 0.5


def test_aggregate_ignores_unwatched_fights(seeded_two_raidwides_one_missed):
    """A fight in CODE_OTHER (NOT in our watchlist) must not affect totals."""
    s = seeded_two_raidwides_one_missed
    extra_fid = None
    try:
        now = datetime.now(timezone.utc)
        s.add(Report(code=CODE_OTHER, ingested_at=now))
        s.flush()
        # No WatchedReport row for CODE_OTHER — this is the negative case.
        f = Fight(report_code=CODE_OTHER, fight_id_in_report=1,
                  encounter_id=ENC, is_kill=False,
                  start_time=0, end_time=60_000, duration_ms=60_000)
        s.add(f)
        s.flush()
        extra_fid = f.id
        # 5 raidwide casts in the foreign fight, no mits — would dominate
        # the aggregate if we accidentally counted it.
        for i in range(5):
            s.add(Event(fight_id=f.id, ts=10_000 * (i + 1), type="cast",
                        source_id=9999, ability_game_id=200))
        s.commit()

        result = mit_audit_aggregate_for_encounter(s, ENC, STATIC_ID)
        # Still the 2 raidwides from CODE_WATCHED, not 7
        assert result["raidwide_casts"] == 2
        assert result["planned_slots_total"] == 2
        assert result["missed_mits_total"] == 1
    finally:
        if extra_fid is not None:
            s.execute(delete(Event).where(Event.fight_id == extra_fid))
            s.execute(delete(Fight).where(Fight.id == extra_fid))
        s.execute(delete(IngestionLedger).where(IngestionLedger.report_code == CODE_OTHER))
        s.execute(delete(Report).where(Report.code == CODE_OTHER))
        s.commit()
