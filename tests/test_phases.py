"""T-103 phase segmentation tests — unit + live AC against FRU + M5S."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select

from analysis.phases import detect_phase_boundaries
from db.models import Combatant, Event, Fight, IngestionLedger, Report
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)

CODE = "T103_TEST"


@pytest.fixture
def seeded():
    """Two-phase synthetic fight: boss A hit 0..100s, boss B hit 110..200s."""
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        s.add(Report(code=CODE, ingested_at=now))
        s.flush()
        f = Fight(report_code=CODE, fight_id_in_report=1, encounter_id=1,
                  is_kill=True, start_time=0, end_time=200_000, duration_ms=200_000)
        s.add(f)
        s.flush()
        # Players (so we know what's "non-player")
        s.add(Combatant(fight_id=f.id, player_id=100, name="P1", job="WAR"))
        s.add(Combatant(fight_id=f.id, player_id=101, name="P2", job="PLD"))
        # Boss A: hit 30 times over [0, 100_000]
        for i in range(30):
            s.add(Event(fight_id=f.id, ts=i * 3333, type="damage",
                        source_id=100, target_id=999, ability_game_id=1, amount=1000))
        # Boss B: hit 30 times over [110_000, 200_000]
        for i in range(30):
            s.add(Event(fight_id=f.id, ts=110_000 + i * 3000, type="damage",
                        source_id=100, target_id=998, ability_game_id=1, amount=1000))
        # Trivial add (only 5 hits) — must be filtered out
        for i in range(5):
            s.add(Event(fight_id=f.id, ts=50_000 + i * 100, type="damage",
                        source_id=100, target_id=997, ability_game_id=1, amount=100))
        s.commit()
        try:
            yield s, f.id
        finally:
            s.execute(delete(Event).where(Event.fight_id == f.id))
            s.execute(delete(Combatant).where(Combatant.fight_id == f.id))
            s.execute(delete(Fight).where(Fight.id == f.id))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code == CODE))
            s.execute(delete(Report).where(Report.code == CODE))
            s.commit()


def test_two_distinct_bosses_become_two_phases(seeded):
    session, fid = seeded
    result = detect_phase_boundaries(session, fid)
    assert len(result["phases"]) == 2
    p0, p1 = result["phases"]
    assert p0["boss_target_ids"] == [999]
    assert p1["boss_target_ids"] == [998]
    assert p0["end_ts"] < p1["start_ts"]
    # One transition between two phases
    assert len(result["transitions"]) == 1
    assert result["transitions"][0]["after_phase"] == 0
    assert result["transitions"][0]["gap_ms"] == p1["start_ts"] - p0["end_ts"]


def test_trivial_adds_below_min_hits_dropped(seeded):
    session, fid = seeded
    # Default min_hits=30 — the 5-hit add at target 997 must not appear.
    result = detect_phase_boundaries(session, fid)
    all_targets = [t for p in result["phases"] for t in p["boss_target_ids"]]
    assert 997 not in all_targets


def test_lower_min_hits_picks_up_adds(seeded):
    session, fid = seeded
    result = detect_phase_boundaries(session, fid, min_hits=3)
    all_targets = [t for p in result["phases"] for t in p["boss_target_ids"]]
    assert 997 in all_targets


def test_merge_gap_collapses_concurrent_bosses(seeded):
    """Phase 4 in FRU has two simultaneous boss actors — they should merge
    into one phase, not appear as two close-by phases."""
    session, fid = seeded
    # Inject a second concurrent boss (target 996) overlapping target 998's window.
    for i in range(30):
        session.add(Event(fight_id=fid, ts=120_000 + i * 2500, type="damage",
                          source_id=100, target_id=996, ability_game_id=1, amount=500))
    session.commit()
    result = detect_phase_boundaries(session, fid)
    # Should still be 2 phases (998 + 996 merged into the second)
    assert len(result["phases"]) == 2
    assert sorted(result["phases"][1]["boss_target_ids"]) == [996, 998]


def test_empty_fight_returns_no_phases():
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        s.add(Report(code="T103_EMPTY", ingested_at=now))
        s.flush()
        f = Fight(report_code="T103_EMPTY", fight_id_in_report=1, encounter_id=1,
                  is_kill=False, start_time=0, end_time=1000, duration_ms=1000)
        s.add(f)
        s.commit()
        try:
            result = detect_phase_boundaries(s, f.id)
            assert result == {"fight_id": f.id, "phases": [], "transitions": []}
        finally:
            s.execute(delete(Fight).where(Fight.id == f.id))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code == "T103_EMPTY"))
            s.execute(delete(Report).where(Report.code == "T103_EMPTY"))
            s.commit()


# ---------- Live AC against the FRU kill we ingested for T-103 ----------

FRU_FIGHT_DB_ID = 1500   # report 4RVNq7drBDLG3JZw, fight #163 (FRU kill)


def test_live_fru_kill_detects_six_phases():
    """FRU is a 6-phase fight (last_phase=5 in FFLogs). The detector should
    surface roughly that many distinct phase intervals."""
    with SessionLocal() as s:
        f = s.get(Fight, FRU_FIGHT_DB_ID)
        if f is None or s.execute(
            select(Event.id).where(Event.fight_id == FRU_FIGHT_DB_ID).limit(1)
        ).scalar() is None:
            pytest.skip("FRU dev fight 1500 not ingested")
        result = detect_phase_boundaries(s, FRU_FIGHT_DB_ID)
        # Expect 5-7 phases (FRU has 5+intermissions; merge_gap may collapse some)
        assert 5 <= len(result["phases"]) <= 7
        # Each phase should span >5s (filter spurious blips)
        for p in result["phases"]:
            span = p["end_ts"] - p["start_ts"]
            assert span > 5_000, f"phase {p['index']} too short: {span}ms"
