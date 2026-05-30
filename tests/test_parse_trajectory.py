"""T-106 parse trajectory tests — unit + live AC on FRU kill."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select

from analysis.parse_trajectory import parse_per_phase_for_fight
from db.models import Combatant, Event, Fight, IngestionLedger, Report
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)

CODE = "T106_TEST"


@pytest.fixture
def two_phase_fight():
    """Two-phase fight: P1 = [0, 100s] on boss 999, P2 = [110s, 200s] on boss 998.
    Player 100 deals 30 damage events of 1000 each spread across both phases."""
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        s.add(Report(code=CODE, ingested_at=now))
        s.flush()
        f = Fight(report_code=CODE, fight_id_in_report=1, encounter_id=1,
                  is_kill=True, start_time=0, end_time=200_000, duration_ms=200_000)
        s.add(f)
        s.flush()
        s.add(Combatant(fight_id=f.id, player_id=100, name="Alice", job="WAR"))
        s.add(Combatant(fight_id=f.id, player_id=101, name="Bob", job="PLD"))
        # Boss A hits (phase 1 marker)
        for i in range(30):
            s.add(Event(fight_id=f.id, ts=i * 3333, type="damage",
                        source_id=100, target_id=999, ability_game_id=1, amount=1000))
        # Boss B hits (phase 2 marker)
        for i in range(30):
            s.add(Event(fight_id=f.id, ts=110_000 + i * 3000, type="damage",
                        source_id=100, target_id=998, ability_game_id=1, amount=1000))
        # Bob: only deals damage in phase 1 (5 events, 500 each)
        for i in range(30):
            s.add(Event(fight_id=f.id, ts=i * 3333, type="damage",
                        source_id=101, target_id=999, ability_game_id=2, amount=500))
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


def test_per_phase_sums_only_within_phase(two_phase_fight):
    session, fid = two_phase_fight
    result = parse_per_phase_for_fight(session, fid)
    assert len(result["phases"]) == 2
    p1, p2 = result["phases"]
    alice_p1 = next(p for p in p1["players"] if p["name"] == "Alice")
    alice_p2 = next(p for p in p2["players"] if p["name"] == "Alice")
    # Each Alice phase has 30 hits × 1000 = 30000 of damage (boss-hit events
    # double as her damage events here since she's the source).
    assert alice_p1["damage_total"] == 30_000
    assert alice_p2["damage_total"] == 30_000


def test_dps_is_per_second(two_phase_fight):
    session, fid = two_phase_fight
    result = parse_per_phase_for_fight(session, fid)
    p1 = result["phases"][0]
    alice = next(p for p in p1["players"] if p["name"] == "Alice")
    # Phase 1 spans roughly 0..96.6s (29 * 3333 ms), 30 hits inside.
    # damage_total / (duration / 1000) — sanity-check the DPS is in the right ballpark.
    expected_dps_lower = alice["damage_total"] / (p1["duration_ms"] / 1000) - 1
    expected_dps_upper = alice["damage_total"] / (p1["duration_ms"] / 1000) + 1
    assert expected_dps_lower <= alice["dps"] <= expected_dps_upper


def test_players_sorted_by_damage_desc(two_phase_fight):
    session, fid = two_phase_fight
    result = parse_per_phase_for_fight(session, fid)
    for p in result["phases"]:
        totals = [pp["damage_total"] for pp in p["players"]]
        assert totals == sorted(totals, reverse=True)


def test_phase_offset_metadata_present(two_phase_fight):
    session, fid = two_phase_fight
    result = parse_per_phase_for_fight(session, fid)
    assert result["phases"][0]["start_offset_ms"] == 0
    assert result["phases"][1]["start_offset_ms"] > 0


def test_no_phases_returns_empty():
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        s.add(Report(code="T106_EMPTY", ingested_at=now))
        s.flush()
        f = Fight(report_code="T106_EMPTY", fight_id_in_report=1, encounter_id=1,
                  is_kill=False, start_time=0, end_time=1000, duration_ms=1000)
        s.add(f)
        s.commit()
        try:
            result = parse_per_phase_for_fight(s, f.id)
            assert result == {"fight_id": f.id, "phases": []}
        finally:
            s.execute(delete(Fight).where(Fight.id == f.id))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code == "T106_EMPTY"))
            s.execute(delete(Report).where(Report.code == "T106_EMPTY"))
            s.commit()


def test_live_fru_kill_yields_per_phase_dps():
    """FRU kill fight 1500 should produce 6 phases each with a populated
    per-player DPS table where damage_total > 0."""
    with SessionLocal() as s:
        if s.execute(select(Event.id).where(Event.fight_id == 1500).limit(1)).scalar() is None:
            pytest.skip("FRU fight 1500 not ingested")
        result = parse_per_phase_for_fight(s, 1500)
        assert len(result["phases"]) == 6
        for p in result["phases"]:
            assert p["players"], f"phase {p['phase_index']} has no players"
            top = p["players"][0]
            assert top["damage_total"] > 0
            assert top["dps"] > 0
