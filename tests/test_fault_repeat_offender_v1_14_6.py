"""v1.14.6: repeat-offender amplifier for past-wall root deaths.

Tests:
- repeat_offender_multiplier math: rate-based exp scaling + cap.
- Same absolute count, different totals → different multipliers.
- 100-wipe vs 1000-wipe static comparison.
- Aggregate: 5 past-wall roots in 10 wipes (50% rate) hits the cap.
- Aggregate: 5 past-wall roots in 1000 wipes (0.5% rate) ≈ baseline.
- Time-ordered: 3rd offense in a row gets bigger multiplier than 1st.
- Only past-wall ROOT deaths count toward the rate (not cascades, not
  at-wall roots).
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete

from analysis.fault_attribution import (
    REPEAT_PENALTY_CAP,
    REPEAT_PENALTY_K,
    compute_fault_scores_for_fight,
    fault_aggregate_for_encounter,
    repeat_offender_multiplier,
)
from db.models import (
    Combatant, Event, FaultScore, Fight, FightModel,
    IngestionLedger, Report, WatchedReport,
)
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)


# ---- pure helper ----

def test_repeat_zero_offenses_is_one():
    assert repeat_offender_multiplier(0, 100) == 1.0


def test_repeat_one_offense_in_thousand_is_near_baseline():
    m = repeat_offender_multiplier(1, 1000)
    # rate = 0.001, exp(4 * 0.001) ≈ 1.004
    assert 1.0 < m < 1.01


def test_repeat_one_offense_in_hundred_is_modest():
    m = repeat_offender_multiplier(1, 100)
    # rate = 0.01, exp(0.04) ≈ 1.041
    assert 1.03 < m < 1.05


def test_repeat_same_count_different_totals():
    """5 offenses in 100 wipes (5%) should be MUCH higher than 5/1000 (0.5%)."""
    small_static = repeat_offender_multiplier(5, 100)   # rate 0.05
    big_static = repeat_offender_multiplier(5, 1000)    # rate 0.005
    # exp(0.2)=1.22 vs exp(0.02)=1.02 — big difference
    assert small_static > big_static + 0.1


def test_repeat_caps_at_max():
    """50% rate would give exp(2)≈7.4 but the cap brings it to 5.0."""
    m = repeat_offender_multiplier(50, 100)
    assert m == REPEAT_PENALTY_CAP


def test_repeat_floored_denominator_kills_cold_start():
    """v1.16.0: 1 offense in 2 wipes used to score 50% rate → cap. Now the
    floor at 20 wipes brings rate to 1/20 = 5% → modest multiplier."""
    m = repeat_offender_multiplier(1, 2)
    # rate = 1/20 = 0.05, exp(0.2) ≈ 1.22
    assert 1.20 < m < 1.25


def test_repeat_grows_exponentially_with_rate():
    """Each successive bump in rate produces a multiplicatively-larger
    multiplier — that's what 'exponential' means here."""
    r1 = repeat_offender_multiplier(5, 100)   # 5%
    r2 = repeat_offender_multiplier(10, 100)  # 10%
    r3 = repeat_offender_multiplier(15, 100)  # 15%
    # The ratio between successive steps should be approximately constant
    # because rate increases linearly and multiplier is exp(k * rate).
    ratio_12 = r2 / r1
    ratio_23 = r3 / r2
    assert ratio_12 == pytest.approx(ratio_23, abs=0.001)


# ---- end-to-end aggregate ----

ENC = 50_146


@pytest.fixture
def serial_offender_fixture():
    """20 total wipes for one player: 5 P5 wipes (establishes prog wall
    at P5), then 15 P3 wipes (all past-wall) where 5 have a root death.
    Past-wall root rate = 5/20 = 25%. Expected repeat multiplier on the
    5th offense ≈ exp(4 × 0.25) ≈ 2.72."""
    code = "T146_SERIAL"
    fid_holder = []
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        s.add(Report(code=code, ingested_at=now))
        s.flush()
        s.add(WatchedReport(static_id=1, code=code,
                             active=True, added_at=now))
        # Tankbuster ability used for root deaths
        s.add(FightModel(encounter_id=ENC, version=1, phase=0, seq=0,
                         ability_game_id=111, type_label="tankbuster",
                         relative_t_ms=5_000, time_variance_ms=0,
                         confidence=1.0, meta={}, updated_at=now))

        def make_fight(seq, phase, with_root):
            f = Fight(report_code=code, fight_id_in_report=seq,
                      encounter_id=ENC, is_kill=False,
                      fight_percentage=50.0, last_phase=phase,
                      start_time=1_000 + seq * 100,
                      end_time=10_000, duration_ms=9_000)
            s.add(f); s.flush(); fid_holder.append(f.id)
            s.add(Combatant(fight_id=f.id, player_id=1, name="P1", job="WAR"))
            s.add(Event(fight_id=f.id, ts=0, type="cast",
                        source_id=1, ability_game_id=8888))
            if with_root:
                s.add(Event(fight_id=f.id, ts=5_000, type="death",
                            source_id=9999, target_id=1,
                            ability_game_id=111))

        # 5 P5 wipes (sets running-best=5; no past-wall deaths possible)
        for i in range(5):
            make_fight(i + 1, 5, with_root=False)
        # 15 P3 wipes (all past-wall); 5 of them have a root death
        for i in range(15):
            with_root = i < 5  # first 5 are offenses
            make_fight(100 + i, 3, with_root=with_root)
        s.commit()
        for fid in fid_holder:
            compute_fault_scores_for_fight(s, fid, 1)
        try:
            yield s
        finally:
            s.execute(delete(FaultScore).where(FaultScore.fight_id.in_(fid_holder)))
            s.execute(delete(Event).where(Event.fight_id.in_(fid_holder)))
            s.execute(delete(Combatant).where(Combatant.fight_id.in_(fid_holder)))
            s.execute(delete(Fight).where(Fight.id.in_(fid_holder)))
            s.execute(delete(FightModel).where(FightModel.encounter_id == ENC))
            s.execute(delete(WatchedReport).where(WatchedReport.code == code))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code == code))
            s.execute(delete(Report).where(Report.code == code))
            s.commit()


def test_aggregate_carries_past_wall_offense_count(serial_offender_fixture):
    s = serial_offender_fixture
    agg = fault_aggregate_for_encounter(s, ENC, 1)
    p1 = next(p for p in agg["players"] if p["player_id"] == 1)
    # v1.16.0: renamed past_wall_roots → past_wall_offenses (now counts
    # mit_failures too, though this fixture only has roots).
    assert p1["past_wall_offenses"] == 5
    assert p1["fights"] == 20


def test_aggregate_amplifies_serial_past_wall_root(serial_offender_fixture):
    """The 5 root deaths should each be weighted by an escalating
    repeat-multiplier. The 5th offense lands at rate 5/10 (because by
    then they've attended 5 P5 + 5 P3 wipes), so its multiplier hits the
    cap. Total weighted score should be substantially HIGHER than the
    base de-weighted score."""
    s = serial_offender_fixture
    agg = fault_aggregate_for_encounter(s, ENC, 1)
    p1 = next(p for p in agg["players"] if p["player_id"] == 1)
    # 5 roots × ROOT_SCORE=1.0 → raw_score = 5.0
    assert p1["raw_score"] == pytest.approx(5.0)
    # Phase-weighted (P3 past-wall, fp=50, best=5):
    # _phase(3)=1.857, _within(50)=1.25, _prog(3,5)=0.7 → 1.625 per wipe
    # Without amplifier, 5 × 1.625 = 8.13
    base_de_weighted = 5 * 1.857 * 1.25 * 0.7
    assert p1["score"] > base_de_weighted * 1.5, (
        "repeat amplifier should bump the score significantly above the "
        "base de-weighted total"
    )


def test_aggregate_no_past_wall_offenses_no_amplification():
    """If a player has no past-wall root deaths, the repeat amplifier
    stays at 1.0 throughout — they only get phase-weighting."""
    code = "T146_CLEAN"
    fid_holder = []
    enc = 50_147
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        s.add(Report(code=code, ingested_at=now))
        s.flush()
        s.add(WatchedReport(static_id=1, code=code,
                             active=True, added_at=now))
        s.add(FightModel(encounter_id=enc, version=1, phase=0, seq=0,
                         ability_game_id=111, type_label="tankbuster",
                         relative_t_ms=5_000, time_variance_ms=0,
                         confidence=1.0, meta={}, updated_at=now))
        # 10 P5 wipes, P1 dies in 3 of them (all at the prog wall = NOT past-wall)
        for i in range(10):
            f = Fight(report_code=code, fight_id_in_report=i + 1,
                      encounter_id=enc, is_kill=False,
                      fight_percentage=20.0, last_phase=5,
                      start_time=1_000 + i * 100, end_time=10_000,
                      duration_ms=9_000)
            s.add(f); s.flush(); fid_holder.append(f.id)
            s.add(Combatant(fight_id=f.id, player_id=1, name="P1", job="WAR"))
            s.add(Event(fight_id=f.id, ts=0, type="cast",
                        source_id=1, ability_game_id=8888))
            if i < 3:
                s.add(Event(fight_id=f.id, ts=5_000, type="death",
                            source_id=9999, target_id=1,
                            ability_game_id=111))
        s.commit()
        for fid in fid_holder:
            compute_fault_scores_for_fight(s, fid, 1)
        try:
            agg = fault_aggregate_for_encounter(s, enc, 1)
            p1 = next(p for p in agg["players"] if p["player_id"] == 1)
            assert p1["past_wall_offenses"] == 0
            assert p1["repeat_multiplier_avg"] == 1.0
            # Score is straight phase-weighted: 3 × 1.0 × _phase(5) × _within(20) × _prog(5,5)
            # = 3 × 3.143 × 1.4 × 1.0 ≈ 13.2
            assert 12.0 < p1["score"] < 15.0
        finally:
            s.execute(delete(FaultScore).where(FaultScore.fight_id.in_(fid_holder)))
            s.execute(delete(Event).where(Event.fight_id.in_(fid_holder)))
            s.execute(delete(Combatant).where(Combatant.fight_id.in_(fid_holder)))
            s.execute(delete(Fight).where(Fight.id.in_(fid_holder)))
            s.execute(delete(FightModel).where(FightModel.encounter_id == enc))
            s.execute(delete(WatchedReport).where(WatchedReport.code == code))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code == code))
            s.execute(delete(Report).where(Report.code == code))
            s.commit()
