"""T-204 empirical DPS check tests + live AC on FRU."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete

from analysis.dps_check import (
    _quartiles,
    compare_fight_to_target,
    dps_check_for_encounter,
)
from db.models import Combatant, Event, Fight, IngestionLedger, Report
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)


# ---- Pure-function quartile tests ----

def test_quartiles_empty():
    assert _quartiles([])["n"] == 0


def test_quartiles_single_value_all_equal():
    q = _quartiles([1000.0])
    assert q["p25"] == q["p50"] == q["p75"] == 1000.0
    assert q["n"] == 1


def test_quartiles_distribution():
    q = _quartiles([100.0, 200.0, 300.0, 400.0, 500.0])
    assert q["p25"] < q["p50"] < q["p75"]
    assert q["min"] == 100.0 and q["max"] == 500.0


# ---- End-to-end with seeded DB ----

ENC = 5432187
CODES = ("T204_A", "T204_B", "T204_C")


@pytest.fixture
def three_kills_known_dps():
    """3 kill pulls, each with one phase and known total raid damage so the
    aggregate DPS distribution is predictable.
    Per pull: 1 player × 100 events × 1000 dmg = 100,000 total / 100s = 1000 raid-DPS.
    But we'll vary slightly across pulls to produce a real distribution.
    """
    fight_ids = []
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        for code in CODES:
            s.add(Report(code=code, ingested_at=now))
        s.flush()

        BOSS, PLAYER = 9999, 1
        damages_per_pull = [800, 1000, 1200]  # → raid-DPS 800/1000/1200
        for i, code in enumerate(CODES):
            f = Fight(report_code=code, fight_id_in_report=1,
                      encounter_id=ENC, is_kill=True,
                      start_time=0, end_time=100_000, duration_ms=100_000)
            s.add(f)
            s.flush()
            fight_ids.append(f.id)
            s.add(Combatant(fight_id=f.id, player_id=PLAYER, name="P", job="WAR"))
            # 100 player damage events distributed across 100s, each = damages_per_pull[i]
            for j in range(100):
                s.add(Event(fight_id=f.id, ts=j * 1000, type="damage",
                            source_id=PLAYER, target_id=BOSS,
                            ability_game_id=1, amount=damages_per_pull[i]))
        s.commit()
        try:
            yield s, fight_ids
        finally:
            s.execute(delete(Event).where(Event.fight_id.in_(fight_ids)))
            s.execute(delete(Combatant).where(Combatant.fight_id.in_(fight_ids)))
            s.execute(delete(Fight).where(Fight.id.in_(fight_ids)))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code.in_(CODES)))
            s.execute(delete(Report).where(Report.code.in_(CODES)))
            s.commit()


def test_dps_check_aggregates_per_phase(three_kills_known_dps):
    session, _ = three_kills_known_dps
    r = dps_check_for_encounter(session, ENC)
    assert r["kills_aggregated"] == 3
    assert len(r["phases"]) == 1
    dist = r["phases"][0]["raid_dps"]
    # Median of (80000, 100000, 120000) / 100s = 1000
    assert 800 <= dist["p50"] <= 1200
    assert dist["p25"] <= dist["p50"] <= dist["p75"]
    assert dist["n"] == 3


def test_compare_fight_below_p25(three_kills_known_dps):
    session, fight_ids = three_kills_known_dps
    # The lowest-DPS pull (800 raid-DPS) should be at or below p25.
    r = compare_fight_to_target(session, fight_ids[0])
    assert r["phases"][0]["verdict"] in ("below_p25", "between_p25_p75")


def test_compare_fight_above_p75(three_kills_known_dps):
    session, fight_ids = three_kills_known_dps
    # The highest-DPS pull (1200 raid-DPS) should be at or above p75.
    r = compare_fight_to_target(session, fight_ids[2])
    assert r["phases"][0]["verdict"] in ("above_p75", "between_p25_p75")


def test_dps_check_empty_encounter_returns_note():
    with SessionLocal() as s:
        r = dps_check_for_encounter(s, 999_999_999)
    assert r["phases"] == []
    assert "note" in r


def test_compare_fight_unknown_returns_note():
    with SessionLocal() as s:
        r = compare_fight_to_target(s, -1)
    assert r["phases"] == []
    assert r["note"] == "fight not found"


# ---- Live AC against FRU ----

def test_live_fru_dps_check_returns_six_phases():
    """11 FRU kills should give a 6-phase DPS distribution where median is in
    the right magnitude (hundreds of thousands of raid-DPS for an Ultimate)."""
    with SessionLocal() as s:
        r = dps_check_for_encounter(s, 1079)
    if r["kills_aggregated"] < 3:
        pytest.skip("not enough FRU kills with events")
    assert len(r["phases"]) == 6
    for p in r["phases"]:
        dist = p["raid_dps"]
        # Sanity: an Ultimate party does at least 50k raid-DPS per phase.
        assert dist["p50"] is not None and dist["p50"] > 50_000
