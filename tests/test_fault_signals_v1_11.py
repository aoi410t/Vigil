"""v1.11.0: expanded fault signals (PLAN §3 Invariant 5).

Tests:
- Tankbuster damage to a non-tank surfaces as avoidable_damage.
- Tankbuster damage to a tank does NOT surface as avoidable.
- Damage Down applybuff applications count via T-108-labeled abilities.
- Confidence (classified_fraction) reflects classified vs unknown deaths.
- Survivors with avoidable damage / Damage Down get a fault row (not just
  dead players).
- Composite score blends death + survive-fault signals.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete

from analysis.fault_attribution import (
    AVOIDABLE_DAMAGE_PER_POINT,
    compute_fault_scores_for_fight,
    fault_aggregate_for_encounter,
    fault_scores_for_fight,
)
from db.models import (
    Ability, AbilityLabel, Combatant, Event, FaultScore, Fight,
    FightModel, IngestionLedger, Report, WatchedReport,
)
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)

ENC = 5_011_100
CODE = "T1B0_FAULT"
STATIC_ID = 1
DAMAGE_DOWN_ABILITY = 999_911  # synthetic; we register a label row for it


@pytest.fixture
def seeded_fight_with_signals():
    """One wipe fight with three players:
      - P1 (tank, WAR) takes 500k from a tankbuster — expected, not avoidable.
      - P2 (dps, SAM) takes 500k from same tankbuster — avoidable.
      - P3 (healer, WHM) gets Damage Down applied twice (survive-fault).
      - P2 also dies to ability 555 (unknown label, classifies as unknown).
    No raidwide casts; no preceding-death cascades; clean signal isolation.
    """
    fid_holder = []
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        s.add(Report(code=CODE, ingested_at=now))
        s.flush()
        # WatchedReport so encounter aggregate sees the fight.
        s.add(WatchedReport(static_id=STATIC_ID, code=CODE,
                             active=True, added_at=now))
        # Register a damage-down label on synthetic ability 999_911.
        s.add(Ability(ability_game_id=DAMAGE_DOWN_ABILITY,
                      name="Damage Down (test)", kind="status",
                      description="test"))
        s.flush()
        s.add(AbilityLabel(ability_game_id=DAMAGE_DOWN_ABILITY,
                           label="damage_down", confidence=1.0,
                           source="user"))
        f = Fight(report_code=CODE, fight_id_in_report=1,
                  encounter_id=ENC, is_kill=False,
                  start_time=0, end_time=60_000, duration_ms=60_000)
        s.add(f)
        s.flush()
        fid_holder.append(f.id)

        # Players + 1 cast each (active-players intersect)
        s.add(Combatant(fight_id=f.id, player_id=1, name="Tank", job="WAR"))
        s.add(Combatant(fight_id=f.id, player_id=2, name="Sammy", job="SAM"))
        s.add(Combatant(fight_id=f.id, player_id=3, name="Heals", job="WHM"))
        for pid in (1, 2, 3):
            s.add(Event(fight_id=f.id, ts=0, type="cast",
                        source_id=pid, ability_game_id=8888))

        BOSS = 9999
        # Tankbuster (ability 100, labeled tankbuster in fight_model). Hits
        # P1 (tank — expected) and P2 (DPS — avoidable). 500k each.
        s.add(FightModel(encounter_id=ENC, version=1, phase=0, seq=0,
                         ability_game_id=100, type_label="tankbuster",
                         relative_t_ms=5_000, time_variance_ms=0,
                         confidence=1.0, meta={}, updated_at=now))
        s.add(Event(fight_id=f.id, ts=5_000, type="damage",
                    source_id=BOSS, target_id=1, ability_game_id=100,
                    amount=500_000))
        s.add(Event(fight_id=f.id, ts=5_050, type="damage",
                    source_id=BOSS, target_id=2, ability_game_id=100,
                    amount=500_000))

        # Damage Down applied to P3 twice (two botched body-checks).
        s.add(Event(fight_id=f.id, ts=20_000, type="applydebuff",
                    source_id=BOSS, target_id=3,
                    ability_game_id=DAMAGE_DOWN_ABILITY))
        s.add(Event(fight_id=f.id, ts=40_000, type="applydebuff",
                    source_id=BOSS, target_id=3,
                    ability_game_id=DAMAGE_DOWN_ABILITY))

        # P2 dies to ability 555 (no fight_model label → kind=unknown).
        s.add(Event(fight_id=f.id, ts=50_000, type="death",
                    source_id=BOSS, target_id=2, ability_game_id=555))

        s.commit()
        try:
            yield s, f.id
        finally:
            s.execute(delete(FaultScore).where(FaultScore.fight_id.in_(fid_holder)))
            s.execute(delete(Event).where(Event.fight_id.in_(fid_holder)))
            s.execute(delete(Combatant).where(Combatant.fight_id.in_(fid_holder)))
            s.execute(delete(Fight).where(Fight.id.in_(fid_holder)))
            s.execute(delete(WatchedReport).where(WatchedReport.code == CODE))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code == CODE))
            s.execute(delete(Report).where(Report.code == CODE))
            s.execute(delete(FightModel).where(FightModel.encounter_id == ENC))
            s.execute(delete(AbilityLabel).where(
                AbilityLabel.ability_game_id == DAMAGE_DOWN_ABILITY))
            s.execute(delete(Ability).where(
                Ability.ability_game_id == DAMAGE_DOWN_ABILITY))
            s.commit()


def test_tankbuster_to_non_tank_surfaces_as_avoidable(seeded_fight_with_signals):
    s, fid = seeded_fight_with_signals
    compute_fault_scores_for_fight(s, fid, STATIC_ID)
    scores = fault_scores_for_fight(s, fid, STATIC_ID)
    sammy = next(p for p in scores["players"] if p["reasons"]["job"] == "SAM")
    assert sammy["reasons"]["avoidable_damage"] == 500_000


def test_tankbuster_to_tank_not_counted_as_avoidable(seeded_fight_with_signals):
    s, fid = seeded_fight_with_signals
    compute_fault_scores_for_fight(s, fid, STATIC_ID)
    scores = fault_scores_for_fight(s, fid, STATIC_ID)
    tank_rows = [p for p in scores["players"] if p["reasons"]["job"] == "WAR"]
    # Tank may or may not have a row depending on whether they have any fault
    # signal. With no avoidable damage / Damage Down / death, they shouldn't.
    if tank_rows:
        assert tank_rows[0]["reasons"]["avoidable_damage"] == 0


def test_damage_down_count_surfaces(seeded_fight_with_signals):
    s, fid = seeded_fight_with_signals
    compute_fault_scores_for_fight(s, fid, STATIC_ID)
    scores = fault_scores_for_fight(s, fid, STATIC_ID)
    heals = next(p for p in scores["players"] if p["reasons"]["job"] == "WHM")
    assert heals["reasons"]["damage_downs"] == 2


def test_survivor_with_survive_fault_gets_a_row(seeded_fight_with_signals):
    """P3 (WHM) never dies — only signal is two Damage Downs. Must still
    appear in fault_scores so the consumer Home surfaces them."""
    s, fid = seeded_fight_with_signals
    compute_fault_scores_for_fight(s, fid, STATIC_ID)
    scores = fault_scores_for_fight(s, fid, STATIC_ID)
    jobs = {p["reasons"]["job"] for p in scores["players"]}
    assert "WHM" in jobs


def test_classified_fraction_reports_confidence(seeded_fight_with_signals):
    """Sammy has 1 death of kind=unknown → classified_fraction=0."""
    s, fid = seeded_fight_with_signals
    compute_fault_scores_for_fight(s, fid, STATIC_ID)
    scores = fault_scores_for_fight(s, fid, STATIC_ID)
    sammy = next(p for p in scores["players"] if p["reasons"]["job"] == "SAM")
    assert sammy["reasons"]["classified_fraction"] == 0.0
    assert sammy["reasons"]["unknown"] == 1


def test_score_blends_death_avoidable_damage_down(seeded_fight_with_signals):
    """Score should be > 0 even for survivors with avoidable / Damage Down."""
    s, fid = seeded_fight_with_signals
    compute_fault_scores_for_fight(s, fid, STATIC_ID)
    scores = fault_scores_for_fight(s, fid, STATIC_ID)
    heals = next(p for p in scores["players"] if p["reasons"]["job"] == "WHM")
    # 2 damage downs × 0.5 = 1.0
    assert heals["score"] == pytest.approx(1.0, abs=0.01)

    sammy = next(p for p in scores["players"] if p["reasons"]["job"] == "SAM")
    # 500k avoidable → 500000 / 100000 = 5.0 (capped at 5.0)
    # Plus 1 unknown death (score 0). Total ~5.0.
    assert sammy["score"] >= 5.0
    assert sammy["score"] <= 5.5


def test_encounter_aggregate_carries_new_signals(seeded_fight_with_signals):
    s, fid = seeded_fight_with_signals
    compute_fault_scores_for_fight(s, fid, STATIC_ID)
    agg = fault_aggregate_for_encounter(s, ENC, STATIC_ID)
    by_job = {p["job"]: p for p in agg["players"]}
    assert by_job["SAM"]["avoidable_damage"] == 500_000
    assert by_job["WHM"]["damage_downs"] == 2
    # Each player has classified_fraction key
    for p in agg["players"]:
        assert "classified_fraction" in p
