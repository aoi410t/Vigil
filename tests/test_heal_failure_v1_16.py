"""v1.16.0: heal-failure attribution.

When a raidwide kills a player but the planned mits successfully fired,
we classify the death as `heal_failure`:
  - The dying player gets ZERO score weight
  - The HEALERS active at death_ts split HEAL_FAILURE_TOTAL_WEIGHT
    (1.0) equally between them

Tests:
- Basic: 2 alive healers → each gets 0.5 heal_failure_caused_score
- One healer dead at death_ts → other gets full 1.0
- Both healers dead → falls back to cascade (the chain is broken)
- Victim gets heal_failure count in their reasons, with zero death_score
  contribution
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete

from analysis.fault_attribution import (
    compute_fault_scores_for_fight,
    fault_scores_for_fight,
)
from analysis.mit_audit import mit_audit_for_fight  # noqa — proves import
from analysis.strat_config import encode_mechanic_ref
from db.models import (
    Combatant, Event, FaultScore, Fight, FightModel,
    IngestionLedger, Report, StratConfig, WatchedReport,
)
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)

ENC = 60_160
STATIC_ID = 1


def _seed_heal_failure_fight(s, code: str, healers_dying_at: dict[int, int] | None = None):
    """Seed a fight with:
      - 1 raidwide (ability 555) cast at t=10000
      - A strat plan saying "Rampart fires at t=10s"
      - Rampart actually fires from PLD (player 1)
      - 1 DPS dies (player 5, DRG) at t=10100
      - 2 healers: WHM (player 3), SCH (player 4)
      - Optional: healer death timestamps before the DPS dies
    """
    now = datetime.now(timezone.utc)
    s.add(Report(code=code, ingested_at=now))
    s.flush()
    s.add(WatchedReport(static_id=STATIC_ID, code=code,
                         active=True, added_at=now))
    f = Fight(report_code=code, fight_id_in_report=1,
              encounter_id=ENC, is_kill=False,
              fight_percentage=50.0, last_phase=2,
              start_time=0, end_time=60_000, duration_ms=60_000)
    s.add(f); s.flush()

    # Combatants — 8 player roster: 2 tanks, 2 healers, 4 dps
    roster = [
        (1, "Aoi", "PLD"), (2, "Beta", "WAR"),
        (3, "Wm", "WHM"), (4, "Sc", "SCH"),
        (5, "Dra", "DRG"), (6, "Sam", "SAM"),
        (7, "Bard", "BRD"), (8, "Blm", "BLM"),
    ]
    for pid, name, job in roster:
        s.add(Combatant(fight_id=f.id, player_id=pid, name=name, job=job))
        s.add(Event(fight_id=f.id, ts=0, type="cast",
                    source_id=pid, ability_game_id=8888))

    # Seed fight_model: raidwide ability 555, mit ability 7531 (Rampart)
    s.add(FightModel(encounter_id=ENC, version=1, phase=0, seq=0,
                     ability_game_id=555, type_label="raidwide",
                     relative_t_ms=10_000, time_variance_ms=0,
                     confidence=1.0, meta={}, updated_at=now))

    # Strat plan: Rampart (ability 7531) at offset 0 for the raidwide
    s.add(StratConfig(
        encounter_id=ENC,
        static_id=STATIC_ID,
        mechanic_ref=encode_mechanic_ref(555, 0),
        mit_plan={"slots": [
            {"ability_id": 7531, "expected_role": "MT", "window_offset_ms": 0}
        ]},
        assignments={"role_map": {}},
    ))

    # Boss casts raidwide at t=10000
    s.add(Event(fight_id=f.id, ts=10_000, type="cast",
                source_id=9999, ability_game_id=555))
    # Rampart fires from PLD (player 1) at t=9000 (within mit window)
    s.add(Event(fight_id=f.id, ts=9_000, type="cast",
                source_id=1, ability_game_id=7531))
    s.add(Event(fight_id=f.id, ts=9_000, type="applybuff",
                source_id=1, target_id=1, ability_game_id=7531))

    # Healer deaths (optional)
    if healers_dying_at:
        for healer_pid, death_ts in healers_dying_at.items():
            s.add(Event(fight_id=f.id, ts=death_ts, type="death",
                        source_id=9999, target_id=healer_pid,
                        ability_game_id=666))

    # DPS dies to the raidwide at t=10100
    s.add(Event(fight_id=f.id, ts=10_100, type="death",
                source_id=9999, target_id=5, ability_game_id=555))

    s.commit()
    return f.id


def _cleanup_fight(s, fid: int, code: str):
    s.execute(delete(FaultScore).where(FaultScore.fight_id == fid))
    s.execute(delete(Event).where(Event.fight_id == fid))
    s.execute(delete(Combatant).where(Combatant.fight_id == fid))
    s.execute(delete(Fight).where(Fight.id == fid))
    s.execute(delete(FightModel).where(FightModel.encounter_id == ENC))
    s.execute(delete(StratConfig).where(StratConfig.encounter_id == ENC))
    s.execute(delete(WatchedReport).where(WatchedReport.code == code))
    s.execute(delete(IngestionLedger).where(IngestionLedger.report_code == code))
    s.execute(delete(Report).where(Report.code == code))
    s.commit()


def test_heal_failure_splits_blame_between_two_healers():
    code = "T160_TWO_HEALERS"
    with SessionLocal() as s:
        fid = _seed_heal_failure_fight(s, code)
        try:
            summary = compute_fault_scores_for_fight(s, fid, STATIC_ID)
            assert summary["label_counts"]["heal_failure"] == 1
            body = fault_scores_for_fight(s, fid, STATIC_ID)
            by_pid = {p["player_id"]: p for p in body["players"]}
            # DPS who died: heal_failure count 1, score = 0 from this
            dps = by_pid.get(5, {})
            reasons = dps.get("reasons", {})
            assert reasons.get("heal_failure", 0) == 1
            assert dps.get("score", 0) == 0
            # WHM (3) and SCH (4) each get 0.5
            whm = by_pid[3]
            sch = by_pid[4]
            assert whm["reasons"]["heal_failure_caused"] == 1
            assert sch["reasons"]["heal_failure_caused"] == 1
            assert whm["score"] == pytest.approx(0.5)
            assert sch["score"] == pytest.approx(0.5)
        finally:
            _cleanup_fight(s, fid, code)


def test_heal_failure_full_blame_when_only_one_healer_alive():
    """If WHM died at t=9500 (before the DPS dies at t=10100), only SCH
    is alive — SCH carries the full 1.0 weight."""
    code = "T160_ONE_HEALER"
    with SessionLocal() as s:
        fid = _seed_heal_failure_fight(s, code, healers_dying_at={3: 9_500})
        try:
            compute_fault_scores_for_fight(s, fid, STATIC_ID)
            body = fault_scores_for_fight(s, fid, STATIC_ID)
            by_pid = {p["player_id"]: p for p in body["players"]}
            sch = by_pid[4]
            assert sch["reasons"]["heal_failure_caused"] == 1
            assert sch["score"] == pytest.approx(1.0)
            # WHM died — they didn't cause the heal_failure
            whm = by_pid.get(3)
            if whm is not None:
                assert whm["reasons"].get("heal_failure_caused", 0) == 0
        finally:
            _cleanup_fight(s, fid, code)


def test_heal_failure_falls_back_to_cascade_when_both_healers_dead():
    """Chain is broken — don't blame the healers when neither was alive
    to react. Falls back to cascade weight on the dying player."""
    code = "T160_NO_HEALERS"
    with SessionLocal() as s:
        fid = _seed_heal_failure_fight(s, code,
                                        healers_dying_at={3: 9_500, 4: 9_700})
        try:
            summary = compute_fault_scores_for_fight(s, fid, STATIC_ID)
            # No heal_failure recorded — got remapped to cascade
            assert summary["label_counts"]["heal_failure"] == 0
            assert summary["label_counts"]["cascade"] >= 1
        finally:
            _cleanup_fight(s, fid, code)


def test_heal_failure_repeat_offender_with_mit_failure():
    """v1.16.0: amplifier now counts mit_failure as a past-wall offense."""
    from analysis.fault_attribution import repeat_offender_multiplier
    # 5 mit_failures in 10 wipes — same denominator floor as before
    m = repeat_offender_multiplier(5, 10)
    # max(10, 20) = 20, rate = 5/20 = 0.25, exp(1.0) ≈ 2.72
    assert 2.5 < m < 3.0
