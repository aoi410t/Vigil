"""v1.16.2: regression test for the cross-report player_id collision bug.

FFLogs player_ids are scoped per-report — the same numeric pid can refer
to "Alice on Paladin" in report A and "Bob on Dancer" in report B. The
v1.16.1 aggregate keyed everything by pid alone, so the per-job
breakdown and per-character attendance were both broken: Alice would
inherit Bob's fights and their job, inflating Alice's totals.

v1.16.2 keys by (combatant.name, combatant.server) resolved per-(fight,
pid) — this test exercises that fix.
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

ENC = 60_220
STATIC_ID = 1


def test_same_pid_different_characters_does_not_conflate_attendance():
    """Two reports. pid=42 in report A is 'Alice on Paladin'. pid=42 in
    report B is 'Bob on Dancer'. After aggregate, Alice has only the
    fights from report A, and Bob has only the fights from report B —
    and their primary_job reflects the right one."""
    code_a = "T220_REPA"
    code_b = "T220_REPB"
    fid_holder: list[int] = []
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        s.add(Report(code=code_a, ingested_at=now))
        s.add(Report(code=code_b, ingested_at=now))
        s.flush()
        s.add(WatchedReport(static_id=STATIC_ID, code=code_a, active=True,
                             added_at=now))
        s.add(WatchedReport(static_id=STATIC_ID, code=code_b, active=True,
                             added_at=now))
        s.add(FightModel(encounter_id=ENC, version=1, phase=0, seq=0,
                         ability_game_id=111, type_label="tankbuster",
                         relative_t_ms=5_000, time_variance_ms=0,
                         confidence=1.0, meta={}, updated_at=now))

        def make_wipe(code, seq, pid, name, job):
            f = Fight(report_code=code, fight_id_in_report=seq,
                      encounter_id=ENC, is_kill=False,
                      fight_percentage=50.0, last_phase=0,
                      start_time=seq * 100_000,
                      end_time=seq * 100_000 + 60_000, duration_ms=60_000)
            s.add(f); s.flush(); fid_holder.append(f.id)
            s.add(Combatant(fight_id=f.id, player_id=pid, name=name,
                            job=job, server="Test"))
            s.add(Event(fight_id=f.id, ts=0, type="cast",
                        source_id=pid, ability_game_id=8888))
            s.add(Event(fight_id=f.id, ts=5_000, type="death",
                        source_id=9999, target_id=pid, ability_game_id=111))

        # Report A: pid=42 is Alice on PLD across 5 wipes
        for i in range(5):
            make_wipe(code_a, i + 1, 42, "Alice", "Paladin")
        # Report B: pid=42 is Bob on DNC across 3 wipes
        for i in range(3):
            make_wipe(code_b, 100 + i, 42, "Bob", "Dancer")
        s.commit()

        for fid in fid_holder:
            compute_fault_scores_for_fight(s, fid, STATIC_ID)

        try:
            agg = fault_aggregate_for_encounter(s, ENC, STATIC_ID)
            by_name = {p["name"]: p for p in agg["players"]}
            assert "Alice" in by_name
            assert "Bob" in by_name
            # Alice should have ONLY the 5 fights from report A
            assert by_name["Alice"]["fights"] == 5
            # Bob should have ONLY the 3 fights from report B
            assert by_name["Bob"]["fights"] == 3
            # Primary jobs reflect the actual jobs played
            assert by_name["Alice"]["job"] == "Paladin"
            assert by_name["Bob"]["job"] == "Dancer"
            # jobs_breakdown gives the same answer
            alice_jobs = by_name["Alice"]["jobs_breakdown"]
            bob_jobs = by_name["Bob"]["jobs_breakdown"]
            assert alice_jobs["Paladin"]["fights"] == 5
            assert "Dancer" not in alice_jobs
            assert bob_jobs["Dancer"]["fights"] == 3
            assert "Paladin" not in bob_jobs
        finally:
            s.execute(delete(FaultScore).where(FaultScore.fight_id.in_(fid_holder)))
            s.execute(delete(Event).where(Event.fight_id.in_(fid_holder)))
            s.execute(delete(Combatant).where(Combatant.fight_id.in_(fid_holder)))
            s.execute(delete(Fight).where(Fight.id.in_(fid_holder)))
            s.execute(delete(FightModel).where(FightModel.encounter_id == ENC))
            s.execute(delete(WatchedReport).where(
                WatchedReport.code.in_((code_a, code_b))))
            s.execute(delete(IngestionLedger).where(
                IngestionLedger.report_code.in_((code_a, code_b))))
            s.execute(delete(Report).where(Report.code.in_((code_a, code_b))))
            s.commit()


def test_same_pid_same_character_multiple_jobs_breakdown():
    """One character (Alice) plays both PLD (4 wipes) and DRK (2 wipes)
    within the same report under the same pid. The aggregate should
    surface both jobs in `jobs_breakdown` with correct counts."""
    code = "T220_MIXED"
    fid_holder: list[int] = []
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        s.add(Report(code=code, ingested_at=now))
        s.flush()
        s.add(WatchedReport(static_id=STATIC_ID, code=code, active=True,
                             added_at=now))
        s.add(FightModel(encounter_id=ENC + 1, version=1, phase=0, seq=0,
                         ability_game_id=111, type_label="tankbuster",
                         relative_t_ms=5_000, time_variance_ms=0,
                         confidence=1.0, meta={}, updated_at=now))

        def make_wipe(seq, job):
            f = Fight(report_code=code, fight_id_in_report=seq,
                      encounter_id=ENC + 1, is_kill=False,
                      fight_percentage=50.0, last_phase=0,
                      start_time=seq * 100_000,
                      end_time=seq * 100_000 + 60_000, duration_ms=60_000)
            s.add(f); s.flush(); fid_holder.append(f.id)
            s.add(Combatant(fight_id=f.id, player_id=42, name="Alice",
                            job=job, server="Test"))
            s.add(Event(fight_id=f.id, ts=0, type="cast",
                        source_id=42, ability_game_id=8888))

        for i in range(4):
            make_wipe(i + 1, "Paladin")
        for i in range(2):
            make_wipe(100 + i, "DarkKnight")
        s.commit()

        for fid in fid_holder:
            compute_fault_scores_for_fight(s, fid, STATIC_ID)

        try:
            agg = fault_aggregate_for_encounter(s, ENC + 1, STATIC_ID)
            by_name = {p["name"]: p for p in agg["players"]}
            assert by_name["Alice"]["fights"] == 6
            jb = by_name["Alice"]["jobs_breakdown"]
            assert jb["Paladin"]["fights"] == 4
            assert jb["DarkKnight"]["fights"] == 2
            # Primary job = most-played (Paladin)
            assert by_name["Alice"]["job"] == "Paladin"
        finally:
            s.execute(delete(FaultScore).where(FaultScore.fight_id.in_(fid_holder)))
            s.execute(delete(Event).where(Event.fight_id.in_(fid_holder)))
            s.execute(delete(Combatant).where(Combatant.fight_id.in_(fid_holder)))
            s.execute(delete(Fight).where(Fight.id.in_(fid_holder)))
            s.execute(delete(FightModel).where(FightModel.encounter_id == ENC + 1))
            s.execute(delete(WatchedReport).where(WatchedReport.code == code))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code == code))
            s.execute(delete(Report).where(Report.code == code))
            s.commit()
