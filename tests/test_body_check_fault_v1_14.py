"""v1.14.0: body-check fault attribution by missing role.

aoe_party deaths previously always classified as root. Now, if strat_config
has a role_map for the killing mechanic's occurrence, a player whose job
role isn't in the expected target set gets reclassified root → cascade
(it wasn't their job; their fault is elsewhere, and the actual assigned
player's absence is the missing-slot signal).

Tests:
- _expected_job_roles_from_role_map: tank-only roles → {tank}; 4-role assignment → {tank,healer}.
- _expected_job_roles_from_role_map: 'any' → wildcard, returns all 3 roles.
- _expected_job_roles_from_role_map: empty / no role_map → None.
- End-to-end: DPS dies to aoe_party assigned to MT+OT → reclassified to cascade.
- End-to-end: tank dies to same aoe_party → stays root (assigned).
- End-to-end: no strat_config for the mechanic → keeps existing root.
- Reclassification surfaces in summary.body_check_reclassified count.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete

from analysis.fault_attribution import (
    _expected_job_roles_from_role_map,
    compute_fault_scores_for_fight,
    fault_scores_for_fight,
)
from analysis.strat_config import encode_mechanic_ref, upsert as strat_upsert
from db.models import (
    Combatant, Event, FaultScore, Fight, FightModel,
    IngestionLedger, Report, StratConfig,
)
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)


# ---- pure-function _expected_job_roles_from_role_map ----

def test_expected_roles_empty_returns_none():
    assert _expected_job_roles_from_role_map(None) is None
    assert _expected_job_roles_from_role_map({"role_map": {}}) is None


def test_expected_roles_tanks_only():
    r = _expected_job_roles_from_role_map(
        {"role_map": {"tower_n": "MT", "tower_s": "OT"}}
    )
    assert r == {"tank"}


def test_expected_roles_mixed_tank_healer():
    r = _expected_job_roles_from_role_map(
        {"role_map": {"a": "MT", "b": "OT", "c": "H1", "d": "H2"}}
    )
    assert r == {"tank", "healer"}


def test_expected_roles_any_is_wildcard():
    r = _expected_job_roles_from_role_map(
        {"role_map": {"a": "any"}}
    )
    assert r == {"tank", "healer", "dps"}


def test_expected_roles_full_party():
    r = _expected_job_roles_from_role_map(
        {"role_map": {"a": "MT", "b": "OT", "c": "H1", "d": "H2",
                      "e": "D1", "f": "D2", "g": "D3", "h": "D4"}}
    )
    assert r == {"tank", "healer", "dps"}


def test_expected_roles_with_null_role_skipped():
    r = _expected_job_roles_from_role_map(
        {"role_map": {"a": "MT", "b": None, "c": "H1"}}
    )
    assert r == {"tank", "healer"}


# ---- end-to-end ----

ENC = 5_014_140
CODE = "T14_BODY"
STATIC_ID = 1
AOE_ABILITY = 100_141


@pytest.fixture
def seeded_aoe_with_tank_assigned():
    """One wipe fight with an aoe_party cast assigned to MT+OT (tank-only).
    Two deaths to the AoE: P1 (DPS) and P2 (Tank). With strat assignment,
    P1's death should reclassify to cascade (DPS wasn't supposed to be
    hit); P2's stays root (tank was supposed to handle it but died doing
    so).
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

        s.add(Combatant(fight_id=f.id, player_id=1, name="P1_DPS",
                        job="SAM"))
        s.add(Combatant(fight_id=f.id, player_id=2, name="P2_Tank",
                        job="WAR"))
        for pid in (1, 2):
            s.add(Event(fight_id=f.id, ts=0, type="cast",
                        source_id=pid, ability_game_id=8888))

        BOSS = 9999
        # aoe_party cast at t=10s
        s.add(Event(fight_id=f.id, ts=10_000, type="cast",
                    source_id=BOSS, ability_game_id=AOE_ABILITY))
        # Both die to it at t=10.2s
        s.add(Event(fight_id=f.id, ts=10_200, type="death",
                    source_id=BOSS, target_id=1,
                    ability_game_id=AOE_ABILITY))
        s.add(Event(fight_id=f.id, ts=10_300, type="death",
                    source_id=BOSS, target_id=2,
                    ability_game_id=AOE_ABILITY))

        s.add(FightModel(encounter_id=ENC, version=1, phase=0, seq=0,
                         ability_game_id=AOE_ABILITY,
                         type_label="aoe_party",
                         relative_t_ms=10_000, time_variance_ms=0,
                         confidence=1.0, meta={}, updated_at=now))
        s.commit()

        # Strat: tank-only assignment for occurrence 0
        strat_upsert(s, ENC, encode_mechanic_ref(AOE_ABILITY, 0),
                     assignments={"role_map": {"tower_n": "MT",
                                                "tower_s": "OT"}},
                     mit_plan=None,
                     static_id=STATIC_ID)
        try:
            yield s, f.id
        finally:
            s.execute(delete(StratConfig).where(StratConfig.encounter_id == ENC))
            s.execute(delete(FaultScore).where(FaultScore.fight_id.in_(fid_holder)))
            s.execute(delete(Event).where(Event.fight_id.in_(fid_holder)))
            s.execute(delete(Combatant).where(Combatant.fight_id.in_(fid_holder)))
            s.execute(delete(Fight).where(Fight.id.in_(fid_holder)))
            s.execute(delete(FightModel).where(FightModel.encounter_id == ENC))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code == CODE))
            s.execute(delete(Report).where(Report.code == CODE))
            s.commit()


def test_dps_dying_to_tank_assigned_aoe_reclassifies_to_cascade(
    seeded_aoe_with_tank_assigned,
):
    s, fid = seeded_aoe_with_tank_assigned
    summary = compute_fault_scores_for_fight(s, fid, STATIC_ID)
    assert summary["body_check_reclassified"] == 1
    # 1 reclassified root → cascade (P1 DPS), 1 root remains (P2 Tank)
    assert summary["label_counts"]["root"] == 1
    assert summary["label_counts"]["cascade"] == 1


def test_tank_dying_to_tank_assigned_aoe_stays_root(
    seeded_aoe_with_tank_assigned,
):
    s, fid = seeded_aoe_with_tank_assigned
    compute_fault_scores_for_fight(s, fid, STATIC_ID)
    scores = fault_scores_for_fight(s, fid, STATIC_ID)
    p2 = next(p for p in scores["players"] if p["player_id"] == 2)
    assert p2["reasons"]["root"] == 1
    assert p2["reasons"]["cascade"] == 0


def test_dps_now_classified_as_cascade(seeded_aoe_with_tank_assigned):
    s, fid = seeded_aoe_with_tank_assigned
    compute_fault_scores_for_fight(s, fid, STATIC_ID)
    scores = fault_scores_for_fight(s, fid, STATIC_ID)
    p1 = next(p for p in scores["players"] if p["player_id"] == 1)
    assert p1["reasons"]["root"] == 0
    assert p1["reasons"]["cascade"] == 1


def test_no_strat_config_keeps_existing_root():
    """Same fixture pattern but no strat upsert — both deaths stay as root
    (the body-check refinement is opt-in via strat_config)."""
    fid_holder = []
    enc = ENC + 1
    code = CODE + "_NOSTRAT"
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        s.add(Report(code=code, ingested_at=now))
        s.flush()
        f = Fight(report_code=code, fight_id_in_report=1,
                  encounter_id=enc, is_kill=False,
                  start_time=0, end_time=60_000, duration_ms=60_000)
        s.add(f)
        s.flush()
        fid_holder.append(f.id)
        s.add(Combatant(fight_id=f.id, player_id=1, name="P1_DPS", job="SAM"))
        s.add(Combatant(fight_id=f.id, player_id=2, name="P2_Tank", job="WAR"))
        for pid in (1, 2):
            s.add(Event(fight_id=f.id, ts=0, type="cast",
                        source_id=pid, ability_game_id=8888))
        BOSS = 9999
        s.add(Event(fight_id=f.id, ts=10_000, type="cast",
                    source_id=BOSS, ability_game_id=AOE_ABILITY))
        s.add(Event(fight_id=f.id, ts=10_200, type="death",
                    source_id=BOSS, target_id=1, ability_game_id=AOE_ABILITY))
        s.add(Event(fight_id=f.id, ts=10_300, type="death",
                    source_id=BOSS, target_id=2, ability_game_id=AOE_ABILITY))
        s.add(FightModel(encounter_id=enc, version=1, phase=0, seq=0,
                         ability_game_id=AOE_ABILITY,
                         type_label="aoe_party",
                         relative_t_ms=10_000, time_variance_ms=0,
                         confidence=1.0, meta={}, updated_at=now))
        s.commit()
        try:
            summary = compute_fault_scores_for_fight(s, f.id, STATIC_ID)
            # Both deaths stay as root — no strat means no reclassification.
            assert summary["body_check_reclassified"] == 0
            assert summary["label_counts"]["root"] == 2
        finally:
            s.execute(delete(FaultScore).where(FaultScore.fight_id.in_(fid_holder)))
            s.execute(delete(Event).where(Event.fight_id.in_(fid_holder)))
            s.execute(delete(Combatant).where(Combatant.fight_id.in_(fid_holder)))
            s.execute(delete(Fight).where(Fight.id.in_(fid_holder)))
            s.execute(delete(FightModel).where(FightModel.encounter_id == enc))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code == code))
            s.execute(delete(Report).where(Report.code == code))
            s.commit()
