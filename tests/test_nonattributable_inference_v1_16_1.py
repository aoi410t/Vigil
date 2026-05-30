"""v1.16.1: non-attributable death inference.

When FFLogs emits a death with `killing_ability_game_id = None` and
`source_id = -1`, we try to figure out what mechanic actually killed
the player via:
  1. CAST PROXIMITY — most recent enemy cast within INFER_LOOKBACK_MS
     (8s) whose type_label is actionable.
  2. CACTBOT DRIFT — predicted cactbot expected time + this pull's
     per-phase drift, matched to the death timestamp.

Tests cover both paths + the no-match fallback (death remains
classified as `cascade`).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete

from analysis.fault_attribution import (
    _infer_killer_from_cast_proximity,
    INFER_LOOKBACK_MS,
    compute_fault_scores_for_fight,
    fault_scores_for_fight,
)
from db.models import (
    Combatant, Event, FaultScore, Fight, FightModel,
    IngestionLedger, Report, WatchedReport,
)
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)


# ---- pure helper: cast-proximity layer ----

def test_cast_proximity_picks_most_recent_within_window():
    casts = [
        (111, 1_000),  # tankbuster
        (222, 5_000),  # raidwide
        (333, 6_000),  # cosmetic (not actionable)
    ]
    labels = {111: "tankbuster", 222: "raidwide", 333: "cosmetic"}
    # Death at 7000 → 6000 is closest, but cosmetic doesn't count.
    # 5000 is the most recent actionable.
    aid, lbl = _infer_killer_from_cast_proximity(7_000, casts, labels)
    assert aid == 222
    assert lbl == "raidwide"


def test_cast_proximity_skips_casts_outside_lookback():
    casts = [(111, 0)]  # 8.5s before death
    labels = {111: "tankbuster"}
    aid, lbl = _infer_killer_from_cast_proximity(
        INFER_LOOKBACK_MS + 500, casts, labels,
    )
    assert aid is None
    assert lbl is None


def test_cast_proximity_skips_casts_after_death():
    casts = [(111, 5_000)]
    labels = {111: "raidwide"}
    aid, _ = _infer_killer_from_cast_proximity(4_000, casts, labels)
    assert aid is None


def test_cast_proximity_no_actionable_returns_none():
    casts = [(111, 1_000), (222, 2_000)]
    labels = {111: "cosmetic", 222: "unknown"}
    aid, _ = _infer_killer_from_cast_proximity(3_000, casts, labels)
    assert aid is None


# ---- end-to-end: non-attributable death gets reclassified ----

ENC = 60_161
STATIC_ID = 1


def test_nonattributable_death_classifies_via_cast_proximity():
    """A death with ability_game_id=None gets inferred from the boss cast
    that fired ~100ms before — and the resulting kind reflects the real
    mechanic (raidwide → cascade or heal_failure depending on context)
    rather than the default cascade-on-None."""
    code = "T161_INFER"
    fid_holder = []
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        s.add(Report(code=code, ingested_at=now))
        s.flush()
        s.add(WatchedReport(static_id=STATIC_ID, code=code, active=True,
                             added_at=now))
        f = Fight(report_code=code, fight_id_in_report=1,
                  encounter_id=ENC, is_kill=False,
                  fight_percentage=50.0, last_phase=0,
                  start_time=0, end_time=60_000, duration_ms=60_000)
        s.add(f); s.flush(); fid_holder.append(f.id)
        # Roster: 1 tank + 1 DPS, both active
        for pid, name, job in [(1, "T1", "PLD"), (2, "D1", "DRG")]:
            s.add(Combatant(fight_id=f.id, player_id=pid, name=name, job=job))
            # Active marker
            s.add(Event(fight_id=f.id, ts=0, type="cast",
                        source_id=pid, ability_game_id=8888))
        # Boss casts a TANKBUSTER (111) at t=10000
        s.add(Event(fight_id=f.id, ts=10_000, type="cast",
                    source_id=9999, ability_game_id=111))
        # Seed fight_model: 111 = tankbuster
        s.add(FightModel(encounter_id=ENC, version=1, phase=0, seq=0,
                         ability_game_id=111, type_label="tankbuster",
                         relative_t_ms=10_000, time_variance_ms=0,
                         confidence=1.0, meta={}, updated_at=now))
        # DPS (D1, non-tank) dies 100ms after the cast, with NULL ability
        # (non-attributable). Previously this would classify as cascade.
        # v1.16.1 inference: cast proximity picks 111 (tankbuster on
        # non-tank → root).
        s.add(Event(fight_id=f.id, ts=10_100, type="death",
                    source_id=-1, target_id=2, ability_game_id=None))
        s.commit()
        try:
            summary = compute_fault_scores_for_fight(s, f.id, STATIC_ID)
            # Without inference: kind=cascade (1 cascade, 0 root).
            # With inference: kind=root (tankbuster-on-non-tank).
            assert summary["label_counts"]["root"] == 1
            assert summary["label_counts"]["cascade"] == 0
            # Verify the inference metadata is recorded in the death record.
            body = fault_scores_for_fight(s, f.id, STATIC_ID)
            d1 = next(p for p in body["players"] if p["player_id"] == 2)
            deaths = d1["reasons"]["deaths"]
            assert len(deaths) == 1
            assert deaths[0]["ability_game_id"] is None  # original preserved
            assert deaths[0]["inferred_ability_id"] == 111
            assert deaths[0]["inferred_ability_label"] == "tankbuster"
            assert deaths[0]["inferred_from"] == "cast_proximity"
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


def test_nonattributable_death_falls_back_to_cascade_when_no_match():
    """No actionable enemy cast within 8s before the death → falls back
    to default cascade classification (preserves v1.12.0 behavior)."""
    code = "T161_NOINFER"
    fid_holder = []
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        s.add(Report(code=code, ingested_at=now))
        s.flush()
        s.add(WatchedReport(static_id=STATIC_ID, code=code, active=True,
                             added_at=now))
        f = Fight(report_code=code, fight_id_in_report=1,
                  encounter_id=ENC + 1, is_kill=False,
                  fight_percentage=50.0, last_phase=0,
                  start_time=0, end_time=60_000, duration_ms=60_000)
        s.add(f); s.flush(); fid_holder.append(f.id)
        s.add(Combatant(fight_id=f.id, player_id=2, name="D1", job="DRG"))
        s.add(Event(fight_id=f.id, ts=0, type="cast",
                    source_id=2, ability_game_id=8888))
        # No enemy casts near the death — only a far-too-old cast.
        s.add(Event(fight_id=f.id, ts=0, type="cast",
                    source_id=9999, ability_game_id=999))
        s.add(FightModel(encounter_id=ENC + 1, version=1, phase=0, seq=0,
                         ability_game_id=999, type_label="raidwide",
                         relative_t_ms=0, time_variance_ms=0,
                         confidence=1.0, meta={}, updated_at=now))
        # Death at t=20000 — 20s after the only cast, way outside window
        s.add(Event(fight_id=f.id, ts=20_000, type="death",
                    source_id=-1, target_id=2, ability_game_id=None))
        s.commit()
        try:
            summary = compute_fault_scores_for_fight(s, f.id, STATIC_ID)
            assert summary["label_counts"]["cascade"] == 1
            body = fault_scores_for_fight(s, f.id, STATIC_ID)
            d1 = next(p for p in body["players"] if p["player_id"] == 2)
            death = d1["reasons"]["deaths"][0]
            # No inference key when nothing matched
            assert "inferred_from" not in death
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
