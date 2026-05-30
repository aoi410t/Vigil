"""v1.14.5: phase-weighted fault aggregate scoring.

Tests:
- _phase_severity monotonically increases.
- _phase_severity is mild — P5 ≈ 1.7× P3 (not 100×).
- _within_phase_severity decreases as boss HP increases.
- _prog_relevance floor at 0.3; full weight at the prog wall.
- fight_score_multiplier composes correctly.
- Encounter aggregate weights a late-phase wipe more than an early one.
- Aggregate de-weights early-phase wipes when group's best is later.
- `raw_score` is preserved alongside the weighted `score`.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete

from analysis.fault_attribution import (
    _phase_severity,
    _within_phase_severity,
    _prog_relevance,
    fight_score_multiplier,
    compute_fault_scores_for_fight,
    fault_aggregate_for_encounter,
)
from db.models import (
    Combatant, Event, FaultScore, Fight, FightModel,
    IngestionLedger, Report, WatchedReport,
)
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)


# ---- pure helpers ----

def test_phase_severity_monotonic():
    last = -1.0
    for p in range(8):
        s = _phase_severity(p)
        assert s > last
        last = s


def test_phase_severity_mild_ratio():
    """P5 should be heavier than P3 but not absurdly so. ~1.5-2.5× is mild."""
    p3 = _phase_severity(3)
    p5 = _phase_severity(5)
    ratio = p5 / p3
    assert 1.5 <= ratio <= 2.5


def test_phase_severity_none_returns_one():
    assert _phase_severity(None) == 1.0


def test_within_phase_severity_high_hp_low_weight():
    """fp=100% (just entered phase) is the baseline 1.0."""
    assert _within_phase_severity(100.0) == pytest.approx(1.0)


def test_within_phase_severity_low_hp_high_weight():
    """fp=0% (boss almost dead) is the max 1.5."""
    assert _within_phase_severity(0.0) == pytest.approx(1.5)


def test_within_phase_severity_monotonic():
    """Lower boss HP = higher weight."""
    assert _within_phase_severity(80) < _within_phase_severity(50)
    assert _within_phase_severity(50) < _within_phase_severity(10)


def test_prog_relevance_at_wall_is_full():
    assert _prog_relevance(3, 3) == 1.0
    assert _prog_relevance(5, 5) == 1.0


def test_prog_relevance_past_wall_decreases():
    """v1.16.1: near-wall plateau (0.5) + exp decay. _prog_relevance(3,5)
    with no fp: our_prog=3.0, best_prog=5.0, delta=2.0, beyond tolerance
    by 1.5 → exp(-0.3 * 1.5) ≈ 0.638. Gentler than v1.16.0's 0.549."""
    assert _prog_relevance(3, 5) < 1.0
    assert _prog_relevance(3, 5) == pytest.approx(0.638, abs=0.01)


def test_prog_relevance_near_wall_plateau():
    """v1.16.1: within 0.5 prog units of the wall = no de-weighting. P4
    fp=1% (prog 4.99) vs wall=5.0 → delta=0.01 → full weight. P4 fp=50%
    (prog 4.5) → delta=0.5 → still full weight (boundary). P4 fp=80%
    (prog 4.2) → delta=0.8 → exp(-0.09) ≈ 0.91 (just past plateau)."""
    assert _prog_relevance(4, 5, 1.0) == 1.0      # delta ≈ 0.01
    assert _prog_relevance(4, 5, 50.0) == 1.0     # delta = 0.5 (at edge)
    assert _prog_relevance(4, 5, 80.0) == pytest.approx(0.91, abs=0.02)


def test_prog_relevance_floor():
    """Far past the wall never goes below 0.3 floor."""
    assert _prog_relevance(0, 7) == 0.3
    assert _prog_relevance(0, 100) == 0.3


def test_prog_relevance_ahead_of_best_doesnt_increase():
    """Wipe phase shouldn't exceed best phase (best IS max-seen)
    but if it does, still full weight, not super-weight."""
    assert _prog_relevance(5, 3) == 1.0


def test_fight_score_multiplier_composes():
    """P5 at prog wall, low boss HP → heavy multiplier."""
    m = fight_score_multiplier(5, 10.0, 5)
    # _phase_severity(5)=3.143, _within(10)=1.45, _prog_relevance(5,5)=1.0
    assert 4.0 <= m <= 5.0


def test_fight_score_multiplier_de_weighted_when_past_wall():
    """v1.16.1: P3 fp=50% (prog 3.5), best=P5 (prog 5.0), delta=1.5.
    Past plateau (0.5) by 1.0 → exp(-0.3) ≈ 0.741. Combined:
    _phase(3)=1.857, _within(50)=1.25, prog ≈ 0.741 → 1.72."""
    m = fight_score_multiplier(3, 50.0, 5)
    expected = 1.857 * 1.25 * 0.741
    assert m == pytest.approx(expected, abs=0.05)


# ---- end-to-end aggregate weighting ----

ENC = 50_145
CODE = "T145_PHASE_WEIGHT"
STATIC_ID = 1


@pytest.fixture
def seeded_two_wipes_diff_phases():
    """Two wipes:
      - Wipe A: P3, fp=50%, P1 dies (root)
      - Wipe B: P5, fp=10%, P1 dies (root)
    Both later-phase + closer-to-clear weight should make wipe B heavier
    than wipe A. Running-best at time of each: depends on order.

    For this test, wipes are ordered A then B (B is the new prog), so
    best at A = 3, best at B = 5. Both at prog wall, no de-weighting.
    """
    fid_holder = []
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        s.add(Report(code=CODE, ingested_at=now))
        s.flush()
        s.add(WatchedReport(static_id=STATIC_ID, code=CODE,
                             active=True, added_at=now))
        # Two wipes, A starts at t=1000, B starts at t=2000
        f_a = Fight(report_code=CODE, fight_id_in_report=1,
                    encounter_id=ENC, is_kill=False,
                    fight_percentage=50.0, last_phase=3,
                    start_time=1_000, end_time=20_000, duration_ms=19_000)
        s.add(f_a)
        s.flush()
        fid_holder.append(f_a.id)
        f_b = Fight(report_code=CODE, fight_id_in_report=2,
                    encounter_id=ENC, is_kill=False,
                    fight_percentage=10.0, last_phase=5,
                    start_time=2_000, end_time=40_000, duration_ms=38_000)
        s.add(f_b)
        s.flush()
        fid_holder.append(f_b.id)

        # P1 = same player in both fights. Single root death per wipe.
        for f in (f_a, f_b):
            s.add(Combatant(fight_id=f.id, player_id=1, name="P1", job="WAR"))
            s.add(Event(fight_id=f.id, ts=0, type="cast",
                        source_id=1, ability_game_id=8888))
            s.add(Event(fight_id=f.id, ts=10_000, type="death",
                        source_id=9999, target_id=1, ability_game_id=111))

        # Tankbuster ability 111 -> root via _death_kind
        s.add(FightModel(encounter_id=ENC, version=1, phase=0, seq=0,
                         ability_game_id=111, type_label="tankbuster",
                         relative_t_ms=10_000, time_variance_ms=0,
                         confidence=1.0, meta={}, updated_at=now))
        s.commit()
        compute_fault_scores_for_fight(s, f_a.id, STATIC_ID)
        compute_fault_scores_for_fight(s, f_b.id, STATIC_ID)
        try:
            yield s, f_a.id, f_b.id
        finally:
            s.execute(delete(FaultScore).where(FaultScore.fight_id.in_(fid_holder)))
            s.execute(delete(Event).where(Event.fight_id.in_(fid_holder)))
            s.execute(delete(Combatant).where(Combatant.fight_id.in_(fid_holder)))
            s.execute(delete(Fight).where(Fight.id.in_(fid_holder)))
            s.execute(delete(FightModel).where(FightModel.encounter_id == ENC))
            s.execute(delete(WatchedReport).where(WatchedReport.code == CODE))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code == CODE))
            s.execute(delete(Report).where(Report.code == CODE))
            s.commit()


def test_aggregate_weights_later_phase_heavier(seeded_two_wipes_diff_phases):
    s, fid_a, fid_b = seeded_two_wipes_diff_phases
    agg = fault_aggregate_for_encounter(s, ENC, STATIC_ID)
    p1 = next(p for p in agg["players"] if p["player_id"] == 1)
    # raw_score = 1.0 (root) × 2 wipes = 2.0
    assert p1["raw_score"] == pytest.approx(2.0)
    # Weighted: A=phase3@fp50,prog=3 → 1.857×1.25×1.0=2.32
    #           B=phase5@fp10,prog=5 → 3.143×1.45×1.0=4.56
    # Total weighted ≈ 6.88
    assert p1["score"] > p1["raw_score"]
    assert 6.0 < p1["score"] < 8.0


def test_aggregate_de_weights_past_wall_wipes_with_low_rate():
    """1 P3 backslide root in a sea of non-offense wipes. With many
    no-root wipes diluting the past-wall-root rate, the repeat-offender
    amplifier stays near 1.0 and we can observe the prog-relevance
    de-weighting clearly. (When the rate is high — as in a 2-wipe
    fixture with 1 offense — the repeat amplifier overpowers the
    de-weighting; that's the v1.14.6 behavior tested separately.)"""
    code = "T145_BACKSLIDE"
    fid_holder = []
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        s.add(Report(code=code, ingested_at=now))
        s.flush()
        s.add(WatchedReport(static_id=STATIC_ID, code=code,
                             active=True, added_at=now))
        # Wipe 1: P5 reach (sets running-best to 5)
        f1 = Fight(report_code=code, fight_id_in_report=1,
                   encounter_id=99_999, is_kill=False,
                   fight_percentage=20.0, last_phase=5,
                   start_time=1_000, end_time=10_000, duration_ms=9_000)
        s.add(f1); s.flush(); fid_holder.append(f1.id)
        # 19 more P5 wipes (no deaths) to dilute the rate to ~5%
        for i in range(19):
            f = Fight(report_code=code, fight_id_in_report=10 + i,
                      encounter_id=99_999, is_kill=False,
                      fight_percentage=20.0, last_phase=5,
                      start_time=1_500 + i * 100,
                      end_time=10_000, duration_ms=9_000)
            s.add(f); s.flush(); fid_holder.append(f.id)
            s.add(Combatant(fight_id=f.id, player_id=1, name="P1", job="WAR"))
            s.add(Event(fight_id=f.id, ts=0, type="cast",
                        source_id=1, ability_game_id=8888))
        # Wipe 21: P3 backslide (the offense)
        f2 = Fight(report_code=code, fight_id_in_report=2,
                   encounter_id=99_999, is_kill=False,
                   fight_percentage=80.0, last_phase=3,
                   start_time=5_000, end_time=8_000, duration_ms=6_000)
        s.add(f2); s.flush(); fid_holder.append(f2.id)
        for f in (f1, f2):
            s.add(Combatant(fight_id=f.id, player_id=1, name="P1", job="WAR"))
            s.add(Event(fight_id=f.id, ts=0, type="cast",
                        source_id=1, ability_game_id=8888))
        # P1 only dies in the P3 backslide
        s.add(Event(fight_id=f2.id, ts=5_000, type="death",
                    source_id=9999, target_id=1, ability_game_id=222))
        s.add(FightModel(encounter_id=99_999, version=1, phase=0, seq=0,
                         ability_game_id=222, type_label="tankbuster",
                         relative_t_ms=5_000, time_variance_ms=0,
                         confidence=1.0, meta={}, updated_at=now))
        s.commit()
        for f in fid_holder:
            compute_fault_scores_for_fight(s, f, STATIC_ID)
        try:
            agg = fault_aggregate_for_encounter(s, 99_999, STATIC_ID)
            p1 = next(p for p in agg["players"] if p["player_id"] == 1)
            assert p1["raw_score"] == pytest.approx(1.0)
            # v1.16.0: prog_distance(3,80) = 3.2, prog_distance(5,None) = 5.0,
            # delta = 1.8, exp(-0.54) ≈ 0.583.
            # within_phase(80) = 1.1. phase_severity(3) = 1.857.
            # Rate = 1 offense / max(21, 20) = 0.0476, repeat ≈ 1.21.
            # weighted ≈ 1.857 × 1.1 × 0.583 × 1.21 ≈ 1.44
            # Same at wall (delta=0, prog=1.0) ≈ 1.857 × 1.1 × 1.0 × 1.21 ≈ 2.47
            same_at_wall = 1.857 * 1.1 * 1.0 * 1.21  # ≈ 2.47
            assert p1["score"] < same_at_wall  # de-weighting still applies
            assert p1["past_wall_offenses"] == 1
        finally:
            s.execute(delete(FaultScore).where(FaultScore.fight_id.in_(fid_holder)))
            s.execute(delete(Event).where(Event.fight_id.in_(fid_holder)))
            s.execute(delete(Combatant).where(Combatant.fight_id.in_(fid_holder)))
            s.execute(delete(Fight).where(Fight.id.in_(fid_holder)))
            s.execute(delete(FightModel).where(FightModel.encounter_id == 99_999))
            s.execute(delete(WatchedReport).where(WatchedReport.code == code))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code == code))
            s.execute(delete(Report).where(Report.code == code))
            s.commit()


def test_raw_score_preserved_alongside_weighted(seeded_two_wipes_diff_phases):
    """raw_score should be the unweighted sum so the UI can show both
    if needed."""
    s, _, _ = seeded_two_wipes_diff_phases
    agg = fault_aggregate_for_encounter(s, ENC, STATIC_ID)
    p1 = next(p for p in agg["players"] if p["player_id"] == 1)
    assert "raw_score" in p1
    assert p1["raw_score"] != p1["score"]
