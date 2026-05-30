"""v1.13.0: per-(player, ability) fault breakdown.

Tests:
- Empty encounter → empty rows.
- One wipe with multiple deaths produces correct (player, ability) pairs.
- Same player dying twice to the same ability collapses into one row, deaths=2.
- Multi-wipe aggregation: fights_affected counts distinct fight_ids.
- by_kind correctly counts root vs cascade vs mit_failure vs unknown.
- Rows sort by deaths desc.
- Ability name lookup populated when ability exists in `abilities`.
- Cross-static isolation: foreign static's wipes don't leak.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete

from analysis.fault_attribution import compute_fault_scores_for_fight
from analysis.fault_breakdown import fault_breakdown_for_encounter
from db.models import (
    Ability, Combatant, Event, FaultScore, Fight, FightModel,
    IngestionLedger, Report,
)
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)

ENC = 5_013_130
CODE_A = "T13A_WIPE"
CODE_B = "T13B_WIPE"
STATIC_ID = 1
ABILITY_TANKBUSTER = 100_013
ABILITY_RAIDWIDE = 100_014


@pytest.fixture
def seeded_two_wipes():
    """Two wipes of the same encounter (both watched by static 1):
      - Wipe A: P1 dies to tankbuster (root), P2 dies to tankbuster (root)
      - Wipe B: P1 dies to tankbuster again (root), P3 dies to raidwide
        (mit_failure if no plan but pressure low → root; we keep no plan
        and pressure 0 so it stays root). Then P2 dies to raidwide w/
        pressure → cascade.

    Net per-player breakdown:
      P1: 2 deaths to tankbuster (2 roots) — repeat offender on tankbuster
      P2: 1 death to tankbuster + 1 death to raidwide (cascade)
      P3: 1 death to raidwide (root, no preceding raid-wounding death)
    """
    fid_holder = []
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        for code in (CODE_A, CODE_B):
            s.add(Report(code=code, ingested_at=now))
        s.flush()
        # WatchedReport rows so the breakdown sees these as ours.
        from db.models import WatchedReport
        for code in (CODE_A, CODE_B):
            s.add(WatchedReport(static_id=STATIC_ID, code=code,
                                active=True, added_at=now))
        # Register the ability so name lookup works.
        s.add(Ability(ability_game_id=ABILITY_TANKBUSTER,
                      name="Test Tankbuster", kind="action",
                      description="t"))
        s.add(Ability(ability_game_id=ABILITY_RAIDWIDE,
                      name="Test Raidwide", kind="action",
                      description="t"))
        s.add(FightModel(encounter_id=ENC, version=1, phase=0, seq=0,
                         ability_game_id=ABILITY_TANKBUSTER,
                         type_label="tankbuster", relative_t_ms=10_000,
                         time_variance_ms=0, confidence=1.0, meta={},
                         updated_at=now))
        s.add(FightModel(encounter_id=ENC, version=1, phase=0, seq=1,
                         ability_game_id=ABILITY_RAIDWIDE,
                         type_label="raidwide", relative_t_ms=20_000,
                         time_variance_ms=0, confidence=1.0, meta={},
                         updated_at=now))
        s.flush()

        BOSS = 9999
        # Wipe A
        f_a = Fight(report_code=CODE_A, fight_id_in_report=1,
                    encounter_id=ENC, is_kill=False,
                    start_time=0, end_time=60_000, duration_ms=60_000)
        s.add(f_a)
        s.flush()
        fid_holder.append(f_a.id)
        for pid in (1, 2):
            s.add(Combatant(fight_id=f_a.id, player_id=pid,
                            name=f"P{pid}", job="WAR"))
            s.add(Event(fight_id=f_a.id, ts=0, type="cast",
                        source_id=pid, ability_game_id=8888))
        # Both die to tankbuster, separated by 15s so neither pressures the other
        s.add(Event(fight_id=f_a.id, ts=10_000, type="death",
                    source_id=BOSS, target_id=1,
                    ability_game_id=ABILITY_TANKBUSTER))
        s.add(Event(fight_id=f_a.id, ts=25_000, type="death",
                    source_id=BOSS, target_id=2,
                    ability_game_id=ABILITY_TANKBUSTER))

        # Wipe B
        f_b = Fight(report_code=CODE_B, fight_id_in_report=1,
                    encounter_id=ENC, is_kill=False,
                    start_time=0, end_time=60_000, duration_ms=60_000)
        s.add(f_b)
        s.flush()
        fid_holder.append(f_b.id)
        for pid in (1, 2, 3):
            s.add(Combatant(fight_id=f_b.id, player_id=pid,
                            name=f"P{pid}", job="WAR"))
            s.add(Event(fight_id=f_b.id, ts=0, type="cast",
                        source_id=pid, ability_game_id=8888))
        # P1 tankbuster, P3 raidwide (first → root), P2 raidwide 1s later → cascade
        s.add(Event(fight_id=f_b.id, ts=10_000, type="death",
                    source_id=BOSS, target_id=1,
                    ability_game_id=ABILITY_TANKBUSTER))
        s.add(Event(fight_id=f_b.id, ts=30_000, type="death",
                    source_id=BOSS, target_id=3,
                    ability_game_id=ABILITY_RAIDWIDE))
        s.add(Event(fight_id=f_b.id, ts=31_000, type="death",
                    source_id=BOSS, target_id=2,
                    ability_game_id=ABILITY_RAIDWIDE))
        s.commit()
        # Compute fault scores so the breakdown has data to read.
        compute_fault_scores_for_fight(s, f_a.id, STATIC_ID)
        compute_fault_scores_for_fight(s, f_b.id, STATIC_ID)
        try:
            yield s, fid_holder
        finally:
            s.execute(delete(FaultScore).where(FaultScore.fight_id.in_(fid_holder)))
            s.execute(delete(WatchedReport).where(WatchedReport.code.in_([CODE_A, CODE_B])))
            s.execute(delete(Event).where(Event.fight_id.in_(fid_holder)))
            s.execute(delete(Combatant).where(Combatant.fight_id.in_(fid_holder)))
            s.execute(delete(Fight).where(Fight.id.in_(fid_holder)))
            s.execute(delete(FightModel).where(FightModel.encounter_id == ENC))
            s.execute(delete(Ability).where(Ability.ability_game_id.in_(
                [ABILITY_TANKBUSTER, ABILITY_RAIDWIDE])))
            s.execute(delete(IngestionLedger).where(
                IngestionLedger.report_code.in_([CODE_A, CODE_B])))
            s.execute(delete(Report).where(Report.code.in_([CODE_A, CODE_B])))
            s.commit()


def test_empty_encounter_returns_empty_rows():
    with SessionLocal() as s:
        r = fault_breakdown_for_encounter(s, 99_999_999, STATIC_ID)
    assert r["rows"] == []
    assert r["wipes_aggregated"] == 0


def test_repeat_offender_collapses_to_single_row(seeded_two_wipes):
    """P1 dies to tankbuster in both wipes — should appear as ONE row
    with deaths=2, fights_affected=2."""
    s, _ = seeded_two_wipes
    r = fault_breakdown_for_encounter(s, ENC, STATIC_ID)
    p1_tb = [row for row in r["rows"]
             if row["player_id"] == 1
             and row["ability_game_id"] == ABILITY_TANKBUSTER]
    assert len(p1_tb) == 1
    assert p1_tb[0]["deaths"] == 2
    assert p1_tb[0]["fights_affected"] == 2


def test_ability_name_resolved(seeded_two_wipes):
    s, _ = seeded_two_wipes
    r = fault_breakdown_for_encounter(s, ENC, STATIC_ID)
    p1_tb = next(row for row in r["rows"]
                 if row["player_id"] == 1
                 and row["ability_game_id"] == ABILITY_TANKBUSTER)
    assert p1_tb["ability_name"] == "Test Tankbuster"


def test_by_kind_counts_correctly(seeded_two_wipes):
    """P2's raidwide death in wipe B should be cascade (preceded by P3
    raidwide 1s earlier)."""
    s, _ = seeded_two_wipes
    r = fault_breakdown_for_encounter(s, ENC, STATIC_ID)
    p2_rw = [row for row in r["rows"]
             if row["player_id"] == 2
             and row["ability_game_id"] == ABILITY_RAIDWIDE]
    assert len(p2_rw) == 1
    assert p2_rw[0]["by_kind"]["cascade"] == 1
    assert p2_rw[0]["by_kind"]["root"] == 0


def test_rows_sorted_by_deaths_desc(seeded_two_wipes):
    s, _ = seeded_two_wipes
    r = fault_breakdown_for_encounter(s, ENC, STATIC_ID)
    deaths = [row["deaths"] for row in r["rows"]]
    assert deaths == sorted(deaths, reverse=True)


def test_wipes_aggregated_count(seeded_two_wipes):
    s, _ = seeded_two_wipes
    r = fault_breakdown_for_encounter(s, ENC, STATIC_ID)
    assert r["wipes_aggregated"] == 2


def test_other_static_doesnt_leak(seeded_two_wipes):
    """A different static_id sees no rows for the same encounter."""
    s, _ = seeded_two_wipes
    r = fault_breakdown_for_encounter(s, ENC, 9_999_999)
    assert r["rows"] == []
    assert r["wipes_aggregated"] == 0
