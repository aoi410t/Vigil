"""v1.16.5: cartography phase inference + wipes_by_phase.

- `wipes_by_phase` is a per-Fight.last_phase tally returned alongside totals.
- Per-bucket `phase` falls back to T-103-inferred when `fight_model_phase`
  is None; `phase_source` says which.
- `phase_inferred_deaths` counts how many of the bucket's deaths got
  their phase via T-103 rather than fight_model.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete

from analysis.cartography import cartography_for_encounter
from db.models import (
    Combatant, Event, Fight, FightModel, IngestionLedger, Report,
    WatchedReport,
)
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)

ENC = 70_550


def test_wipes_by_phase_returned_in_response():
    """Two wipes ending in P2, one in P5 → wipes_by_phase = {2: 2, 5: 1}."""
    code = "T550_WBP"
    fid_holder: list[int] = []
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        s.add(Report(code=code, ingested_at=now))
        s.flush()
        s.add(WatchedReport(static_id=1, code=code, active=True,
                             added_at=now))
        # Seed 3 wipes with different last_phase values.
        for i, lp in enumerate([2, 2, 5]):
            f = Fight(report_code=code, fight_id_in_report=i + 1,
                      encounter_id=ENC, is_kill=False,
                      fight_percentage=50.0, last_phase=lp,
                      start_time=i * 1000, end_time=i * 1000 + 5000,
                      duration_ms=5000)
            s.add(f); s.flush(); fid_holder.append(f.id)
        s.commit()
        try:
            r = cartography_for_encounter(s, ENC, static_id=1)
            wbp = r["wipes_by_phase"]
            assert wbp.get(2) == 2
            assert wbp.get(5) == 1
            assert r["total_wipes"] == 3
        finally:
            s.execute(delete(Fight).where(Fight.id.in_(fid_holder)))
            s.execute(delete(WatchedReport).where(WatchedReport.code == code))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code == code))
            s.execute(delete(Report).where(Report.code == code))
            s.commit()


def test_bucket_phase_inherits_fight_model_phase():
    """Ability has a `fight_model_phase` → bucket's `phase` reflects it,
    `phase_source = 'fight_model'`, `phase_inferred_deaths = 0`."""
    code = "T550_FMPHASE"
    fid_holder: list[int] = []
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        s.add(Report(code=code, ingested_at=now))
        s.flush()
        s.add(WatchedReport(static_id=1, code=code, active=True,
                             added_at=now))
        # fight_model says ability 222 is in phase 3
        s.add(FightModel(encounter_id=ENC + 1, version=1, phase=3, seq=0,
                         ability_game_id=222, type_label="raidwide",
                         relative_t_ms=10_000, time_variance_ms=0,
                         confidence=1.0, meta={}, updated_at=now))
        f = Fight(report_code=code, fight_id_in_report=1,
                  encounter_id=ENC + 1, is_kill=False,
                  fight_percentage=50.0, last_phase=3,
                  start_time=0, end_time=60_000, duration_ms=60_000)
        s.add(f); s.flush(); fid_holder.append(f.id)
        s.add(Combatant(fight_id=f.id, player_id=1, name="P1", job="WAR"))
        s.add(Event(fight_id=f.id, ts=0, type="cast",
                    source_id=1, ability_game_id=8888))
        s.add(Event(fight_id=f.id, ts=10_000, type="death",
                    source_id=9999, target_id=1, ability_game_id=222))
        s.commit()
        try:
            r = cartography_for_encounter(s, ENC + 1, static_id=1)
            b = next(x for x in r["buckets"] if x["ability_game_id"] == 222)
            # bucket.phase is 1-indexed; fight_model raw stored 0-indexed
            # value (3) → +1 = 4.
            assert b["phase"] == 4
            assert b["fight_model_phase"] == 3
            assert b["phase_source"] == "fight_model"
            assert b["phase_inferred_deaths"] == 0
        finally:
            s.execute(delete(Event).where(Event.fight_id.in_(fid_holder)))
            s.execute(delete(Combatant).where(Combatant.fight_id.in_(fid_holder)))
            s.execute(delete(Fight).where(Fight.id.in_(fid_holder)))
            s.execute(delete(FightModel).where(FightModel.encounter_id == ENC + 1))
            s.execute(delete(WatchedReport).where(WatchedReport.code == code))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code == code))
            s.execute(delete(Report).where(Report.code == code))
            s.commit()


def test_bucket_phase_unknown_when_no_fight_model_no_inference():
    """Ability has no fight_model row → fight_model_phase is None. With
    no cactbot/T-103 phase data either → phase stays None, phase_source
    = 'unknown', phase_inferred_deaths = 0."""
    code = "T550_NOPHASE"
    fid_holder: list[int] = []
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        s.add(Report(code=code, ingested_at=now))
        s.flush()
        s.add(WatchedReport(static_id=1, code=code, active=True,
                             added_at=now))
        f = Fight(report_code=code, fight_id_in_report=1,
                  encounter_id=ENC + 2, is_kill=False,
                  fight_percentage=50.0, last_phase=None,
                  start_time=0, end_time=60_000, duration_ms=60_000)
        s.add(f); s.flush(); fid_holder.append(f.id)
        s.add(Combatant(fight_id=f.id, player_id=1, name="P1", job="WAR"))
        s.add(Event(fight_id=f.id, ts=0, type="cast",
                    source_id=1, ability_game_id=8888))
        # Ability 999 has no fight_model row
        s.add(Event(fight_id=f.id, ts=10_000, type="death",
                    source_id=9999, target_id=1, ability_game_id=999))
        s.commit()
        try:
            r = cartography_for_encounter(s, ENC + 2, static_id=1)
            b = next(x for x in r["buckets"] if x["ability_game_id"] == 999)
            assert b["phase"] is None
            assert b["phase_source"] == "unknown"
            assert b["phase_inferred_deaths"] == 0
        finally:
            s.execute(delete(Event).where(Event.fight_id.in_(fid_holder)))
            s.execute(delete(Combatant).where(Combatant.fight_id.in_(fid_holder)))
            s.execute(delete(Fight).where(Fight.id.in_(fid_holder)))
            s.execute(delete(WatchedReport).where(WatchedReport.code == code))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code == code))
            s.execute(delete(Report).where(Report.code == code))
            s.commit()
