"""v1.16.1: scoped top-N contributors restricted to fights the focal
player attended. Emitted as part of fault_aggregate_for_encounter so the
Home expansion can show "in the wipes Alice attended, here are the top
contributors."

Tests:
- A player with full attendance sees the same ranking as the global aggregate.
- A player who only attended a subset of wipes sees a scoped ranking that
  excludes contributors who only scored in wipes they weren't in.
- Self is excluded.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete

from analysis.fault_attribution import (
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

ENC = 60_170
STATIC_ID = 1


@pytest.fixture
def three_wipes_two_subbed():
    """3 wipes. Alice (P1) attended all 3; Bob (P2) attended all 3; Carol
    (P3) only attended wipe 0. Each wipe has one root death:
      - wipe 0: Bob dies → Bob's root
      - wipe 1: Alice dies → Alice's root
      - wipe 2: Bob dies → Bob's root

    Global ranking: Bob (2 roots) > Alice (1 root) > Carol (0 roots).
    Scoped from Carol's POV (attended only wipe 0): Bob (1 root in wipe 0)
    — Alice was active in wipe 0 too so should appear too if she had a
    fault, but she didn't (her root is in wipe 1, which Carol didn't attend).
    """
    code = "T170_SCOPED"
    fid_holder = []
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        s.add(Report(code=code, ingested_at=now))
        s.flush()
        s.add(WatchedReport(static_id=STATIC_ID, code=code, active=True,
                             added_at=now))
        s.add(FightModel(encounter_id=ENC, version=1, phase=0, seq=0,
                         ability_game_id=111, type_label="tankbuster",
                         relative_t_ms=5_000, time_variance_ms=0,
                         confidence=1.0, meta={}, updated_at=now))

        def make_wipe(seq, deaths_by_pid, present_pids):
            f = Fight(report_code=code, fight_id_in_report=seq,
                      encounter_id=ENC, is_kill=False,
                      fight_percentage=50.0, last_phase=0,
                      start_time=seq * 1_000_000,
                      end_time=seq * 1_000_000 + 60_000,
                      duration_ms=60_000)
            s.add(f); s.flush(); fid_holder.append(f.id)
            for pid, name in present_pids:
                s.add(Combatant(fight_id=f.id, player_id=pid, name=name,
                                job="DRG"))
                s.add(Event(fight_id=f.id, ts=0, type="cast",
                            source_id=pid, ability_game_id=8888))
            for pid in deaths_by_pid:
                s.add(Event(fight_id=f.id, ts=5_000, type="death",
                            source_id=9999, target_id=pid,
                            ability_game_id=111))

        # Wipe 0: Alice + Bob + Carol present; Bob dies
        make_wipe(1, [2], [(1, "Alice"), (2, "Bob"), (3, "Carol")])
        # Wipe 1: Alice + Bob present (Carol subbed out); Alice dies
        make_wipe(2, [1], [(1, "Alice"), (2, "Bob")])
        # Wipe 2: Alice + Bob present; Bob dies
        make_wipe(3, [2], [(1, "Alice"), (2, "Bob")])
        s.commit()
        for fid in fid_holder:
            compute_fault_scores_for_fight(s, fid, STATIC_ID)
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


def test_scoped_contributors_attached_to_each_player(three_wipes_two_subbed):
    s = three_wipes_two_subbed
    agg = fault_aggregate_for_encounter(s, ENC, STATIC_ID)
    by_pid = {p["player_id"]: p for p in agg["players"]}
    for pid in (1, 2, 3):
        assert "scoped_top_contributors" in by_pid[pid]
        assert "scoped_wipes_count" in by_pid[pid]


def test_scoped_full_attendance_sees_global_ranking(three_wipes_two_subbed):
    """Alice attended all 3 wipes — her scoped ranking should include
    everyone else who has fault rows."""
    s = three_wipes_two_subbed
    agg = fault_aggregate_for_encounter(s, ENC, STATIC_ID)
    by_pid = {p["player_id"]: p for p in agg["players"]}
    alice = by_pid[1]
    assert alice["scoped_wipes_count"] == 3
    names = [c["name"] for c in alice["scoped_top_contributors"]]
    assert "Bob" in names
    # Alice excluded (self)
    assert "Alice" not in names


def test_scoped_partial_attendance_filters_out_unseen_wipes(three_wipes_two_subbed):
    """Carol only attended wipe 0 (where Bob died). Her scoped view should
    rank Bob (1 root) but NOT Alice (whose only root was in wipe 1, which
    Carol didn't attend)."""
    s = three_wipes_two_subbed
    agg = fault_aggregate_for_encounter(s, ENC, STATIC_ID)
    by_pid = {p["player_id"]: p for p in agg["players"]}
    carol = by_pid[3]
    assert carol["scoped_wipes_count"] == 1
    names = [c["name"] for c in carol["scoped_top_contributors"]]
    assert "Bob" in names
    assert "Alice" not in names  # Alice's root was in a wipe Carol missed
    assert "Carol" not in names  # self excluded
