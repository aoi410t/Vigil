"""v1.16.3: cartography uses inference to bucket non-attributable deaths
under the closest cactbot/cast-proximity mechanic instead of lumping
them into a single 'non-attributable' bucket.
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

ENC = 70_330


def test_cartography_attributes_nonattributable_via_cast_proximity():
    """Non-attributable death lands under the boss cast that fired ~100ms
    before it. The bucket reports `inferred_deaths == 1`."""
    code = "T330_CART"
    fid_holder: list[int] = []
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        s.add(Report(code=code, ingested_at=now))
        s.flush()
        s.add(WatchedReport(static_id=1, code=code, active=True,
                             added_at=now))
        s.add(FightModel(encounter_id=ENC, version=1, phase=0, seq=0,
                         ability_game_id=222, type_label="raidwide",
                         relative_t_ms=10_000, time_variance_ms=0,
                         confidence=1.0, meta={}, updated_at=now))
        f = Fight(report_code=code, fight_id_in_report=1,
                  encounter_id=ENC, is_kill=False,
                  fight_percentage=50.0, last_phase=0,
                  start_time=0, end_time=60_000, duration_ms=60_000)
        s.add(f); s.flush(); fid_holder.append(f.id)
        # Two active players
        for pid in (1, 2):
            s.add(Combatant(fight_id=f.id, player_id=pid, name=f"P{pid}",
                            job="WAR"))
            s.add(Event(fight_id=f.id, ts=0, type="cast",
                        source_id=pid, ability_game_id=8888))
        # Boss casts raidwide 222 at t=10000
        s.add(Event(fight_id=f.id, ts=10_000, type="cast",
                    source_id=9999, ability_game_id=222))
        # Player 1 dies to it (real attribution)
        s.add(Event(fight_id=f.id, ts=10_100, type="death",
                    source_id=9999, target_id=1, ability_game_id=222))
        # Player 2 dies non-attributable 200ms after the same cast — should
        # be inferred to ability 222 via cast proximity.
        s.add(Event(fight_id=f.id, ts=10_300, type="death",
                    source_id=-1, target_id=2, ability_game_id=None))
        s.commit()
        try:
            r = cartography_for_encounter(s, ENC, static_id=1)
            # Should be ONE bucket with both deaths attributed to 222.
            buckets = {b["ability_game_id"]: b for b in r["buckets"]}
            assert 222 in buckets
            assert buckets[222]["deaths"] == 2
            assert buckets[222]["inferred_deaths"] == 1
            # No leftover non-attributable bucket — both got assigned.
            non_attrib = next((b for b in r["buckets"]
                                if b["non_attributable"]), None)
            assert non_attrib is None
        finally:
            s.execute(delete(Event).where(Event.fight_id.in_(fid_holder)))
            s.execute(delete(Combatant).where(Combatant.fight_id.in_(fid_holder)))
            s.execute(delete(Fight).where(Fight.id.in_(fid_holder)))
            s.execute(delete(FightModel).where(FightModel.encounter_id == ENC))
            s.execute(delete(WatchedReport).where(WatchedReport.code == code))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code == code))
            s.execute(delete(Report).where(Report.code == code))
            s.commit()


def test_cartography_keeps_unattributable_when_inference_fails():
    """If inference can't match (no cactbot, no boss cast nearby), the
    death stays in the non-attributable bucket so we don't over-claim."""
    code = "T330_CART_NOINFER"
    fid_holder: list[int] = []
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        s.add(Report(code=code, ingested_at=now))
        s.flush()
        s.add(WatchedReport(static_id=1, code=code, active=True,
                             added_at=now))
        # No FightModel for this encounter -> no labels -> no cast-proximity
        # inference. And no cactbot timeline for this fake encounter id.
        f = Fight(report_code=code, fight_id_in_report=1,
                  encounter_id=ENC + 1, is_kill=False,
                  fight_percentage=50.0, last_phase=0,
                  start_time=0, end_time=60_000, duration_ms=60_000)
        s.add(f); s.flush(); fid_holder.append(f.id)
        s.add(Combatant(fight_id=f.id, player_id=1, name="P1", job="WAR"))
        s.add(Event(fight_id=f.id, ts=0, type="cast",
                    source_id=1, ability_game_id=8888))
        # Non-attributable death with no nearby boss cast
        s.add(Event(fight_id=f.id, ts=30_000, type="death",
                    source_id=-1, target_id=1, ability_game_id=None))
        s.commit()
        try:
            r = cartography_for_encounter(s, ENC + 1, static_id=1)
            non_attrib = next((b for b in r["buckets"]
                                if b["non_attributable"]), None)
            assert non_attrib is not None
            assert non_attrib["deaths"] == 1
        finally:
            s.execute(delete(Event).where(Event.fight_id.in_(fid_holder)))
            s.execute(delete(Combatant).where(Combatant.fight_id.in_(fid_holder)))
            s.execute(delete(Fight).where(Fight.id.in_(fid_holder)))
            s.execute(delete(WatchedReport).where(WatchedReport.code == code))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code == code))
            s.execute(delete(Report).where(Report.code == code))
            s.commit()
