"""v1.12.0: classifier overhaul — mit-aware primary, strict causality,
continuous decay.

Tests:
- _cascade_pressure decay: linear from 1.0 at t-0 to 0 at t-5s.
- _cascade_pressure ignores non-raid-wounding preceding deaths (#3).
- _cascade_pressure sums multiple preceding deaths.
- _death_kind raidwide mit_failure path: missed mits → mit_failure (#4).
- _death_kind raidwide cascade despite mit firing → cascade (heal/mit
  overwhelm).
- _death_kind raidwide no-plan falls through to cascade pressure heuristic.
- End-to-end: tankbuster doesn't cascade follow-up raidwides (strict #3).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete

from analysis.fault_attribution import (
    CASCADE_PRESSURE_THRESHOLD,
    PRECEDING_DEATH_WINDOW_MS,
    _cascade_pressure,
    _death_kind,
    compute_fault_scores_for_fight,
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


# ---- pure-function _cascade_pressure ----

def test_cascade_pressure_no_preceding_deaths():
    assert _cascade_pressure(10_000, []) == 0.0


def test_cascade_pressure_decays_linearly():
    """A raidwide death 2.5s ago contributes 0.5 (halfway through decay)."""
    pressure = _cascade_pressure(10_000, [(7_500, "raidwide")])
    assert pressure == pytest.approx(0.5)


def test_cascade_pressure_tankbuster_doesnt_contribute():
    """Strict causality (#3): tankbuster preceding death is targeted, not
    raid-wounding — must NOT contribute to cascade pressure."""
    pressure = _cascade_pressure(10_000, [(9_000, "tankbuster")])
    assert pressure == 0.0


def test_cascade_pressure_outside_window_doesnt_contribute():
    """5.5s ago is past the window."""
    pressure = _cascade_pressure(10_000, [(4_500, "raidwide")])
    assert pressure == 0.0


def test_cascade_pressure_sums_multiple_qualifying_deaths():
    pressure = _cascade_pressure(
        10_000,
        [(9_000, "raidwide"),    # 1s ago → 0.8
         (8_000, "aoe_party")],  # 2s ago → 0.6
    )
    assert pressure == pytest.approx(1.4)


def test_cascade_pressure_filters_mixed_labels():
    pressure = _cascade_pressure(
        10_000,
        [(9_500, "tankbuster"),  # not raid-wounding → 0
         (9_000, "raidwide"),    # raid-wounding, 1s ago → 0.8
         (8_500, "cosmetic"),    # not raid-wounding → 0
         (7_000, "aoe_party")],  # raid-wounding, 3s ago → 0.4
    )
    assert pressure == pytest.approx(1.2)


def test_cascade_pressure_threshold_is_what_changes_classification():
    """Just-at-threshold pressure flips raidwide root → cascade."""
    assert _death_kind(555, "raidwide", CASCADE_PRESSURE_THRESHOLD) == "cascade"
    # Just below threshold stays root
    below = CASCADE_PRESSURE_THRESHOLD - 0.01
    assert _death_kind(555, "raidwide", below) == "root"


# ---- end-to-end: strict causality fixture ----

ENC = 50_312
CODE = "T312_STRICT"


@pytest.fixture
def tankbuster_then_raidwide():
    """Player 1 dies to tankbuster (single-target); 1s later Player 2 dies
    to raidwide. Under v1.12.0 strict causality, P2 should be ROOT, not
    cascade — the tankbuster didn't pressure the raidwide."""
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
        BOSS = 9999
        for pid in (1, 2):
            s.add(Combatant(fight_id=f.id, player_id=pid,
                            name=f"P{pid}", job="WAR"))
            s.add(Event(fight_id=f.id, ts=0, type="cast",
                        source_id=pid, ability_game_id=8888))
        # P1 tankbuster death at t=10s
        s.add(Event(fight_id=f.id, ts=10_000, type="death",
                    source_id=BOSS, target_id=1, ability_game_id=111))
        # P2 raidwide death at t=11s (1s later — well inside old 5s cliff,
        # but should NOT cascade because tankbuster isn't raid-wounding)
        s.add(Event(fight_id=f.id, ts=11_000, type="death",
                    source_id=BOSS, target_id=2, ability_game_id=222))
        # FightModel
        s.add(FightModel(encounter_id=ENC, version=1, phase=0, seq=0,
                         ability_game_id=111, type_label="tankbuster",
                         relative_t_ms=10_000, time_variance_ms=0,
                         confidence=1.0, meta={}, updated_at=now))
        s.add(FightModel(encounter_id=ENC, version=1, phase=0, seq=1,
                         ability_game_id=222, type_label="raidwide",
                         relative_t_ms=11_000, time_variance_ms=0,
                         confidence=1.0, meta={}, updated_at=now))
        s.commit()
        try:
            yield s, f.id
        finally:
            s.execute(delete(FaultScore).where(FaultScore.fight_id.in_(fid_holder)))
            s.execute(delete(Event).where(Event.fight_id.in_(fid_holder)))
            s.execute(delete(Combatant).where(Combatant.fight_id.in_(fid_holder)))
            s.execute(delete(Fight).where(Fight.id.in_(fid_holder)))
            s.execute(delete(FightModel).where(FightModel.encounter_id == ENC))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code == CODE))
            s.execute(delete(Report).where(Report.code == CODE))
            s.commit()


def test_tankbuster_doesnt_cascade_raidwide(tankbuster_then_raidwide):
    """The signature v1.12.0 outcome: tankbuster-then-raidwide produces
    two ROOTs, not one root + one cascade."""
    session, fid = tankbuster_then_raidwide
    summary = compute_fault_scores_for_fight(session, fid, 1)
    assert summary["label_counts"]["root"] == 2
    assert summary["label_counts"]["cascade"] == 0


# ---- end-to-end: mit-aware primary classification ----

ENC_M = 50_313
CODE_M = "T312_MIT"


@pytest.fixture
def raidwide_with_missed_mit():
    """One raidwide cast at t=10s; no mit fires anywhere; strat expects
    Reprisal. Should classify as mit_failure (not root, not cascade)."""
    fid_holder = []
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        s.add(Report(code=CODE_M, ingested_at=now))
        s.flush()
        f = Fight(report_code=CODE_M, fight_id_in_report=1,
                  encounter_id=ENC_M, is_kill=False,
                  start_time=0, end_time=60_000, duration_ms=60_000)
        s.add(f)
        s.flush()
        fid_holder.append(f.id)
        BOSS = 9999
        for pid in (1, 2):
            s.add(Combatant(fight_id=f.id, player_id=pid,
                            name=f"P{pid}", job="WAR"))
            s.add(Event(fight_id=f.id, ts=0, type="cast",
                        source_id=pid, ability_game_id=8888))
        # Raidwide cast at t=10s, kills P1 at t=10.2s. No mit fires.
        s.add(Event(fight_id=f.id, ts=10_000, type="cast",
                    source_id=BOSS, ability_game_id=200))
        s.add(Event(fight_id=f.id, ts=10_200, type="death",
                    source_id=BOSS, target_id=1, ability_game_id=200))
        s.add(FightModel(encounter_id=ENC_M, version=1, phase=0, seq=0,
                         ability_game_id=200, type_label="raidwide",
                         relative_t_ms=10_000, time_variance_ms=0,
                         confidence=1.0, meta={}, updated_at=now))
        s.commit()
        # Strat expects Reprisal on first occurrence
        strat_upsert(s, ENC_M, encode_mechanic_ref(200, 0),
                     assignments=None,
                     mit_plan={"slots": [{"ability_id": 7535,
                                          "expected_role": "MT",
                                          "window_offset_ms": -2000}]},
                     static_id=1)
        try:
            yield s, f.id
        finally:
            s.execute(delete(StratConfig).where(StratConfig.encounter_id == ENC_M))
            s.execute(delete(FaultScore).where(FaultScore.fight_id.in_(fid_holder)))
            s.execute(delete(Event).where(Event.fight_id.in_(fid_holder)))
            s.execute(delete(Combatant).where(Combatant.fight_id.in_(fid_holder)))
            s.execute(delete(Fight).where(Fight.id.in_(fid_holder)))
            s.execute(delete(FightModel).where(FightModel.encounter_id == ENC_M))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code == CODE_M))
            s.execute(delete(Report).where(Report.code == CODE_M))
            s.commit()


def test_missed_mit_directly_classified_as_mit_failure(raidwide_with_missed_mit):
    """v1.12.0 mit-aware path: missed mit goes straight to mit_failure in
    T-302, no T-304 disambiguation pass needed."""
    session, fid = raidwide_with_missed_mit
    summary = compute_fault_scores_for_fight(session, fid, 1)
    assert summary["label_counts"]["mit_failure"] == 1
    assert summary["label_counts"]["root"] == 0
    assert summary["label_counts"]["cascade"] == 0
