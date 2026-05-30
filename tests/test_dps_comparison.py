"""v1.10.0: per-encounter DPS comparison with static_id split + optional job
filter. Powers Home "Your DPS vs the field" section.

Tests:
- Two kills, one ours + one field → ours and field distributions both populated.
- Empty encounter returns zero shape.
- Job filter narrows to per-player DPS for that job; non-matching jobs ignored.
- jobs_available reflects every job seen across both sides.
- Static with no watched kills sees only field distribution.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete

from analysis.dps_check import dps_comparison_for_encounter
from db.models import (
    Combatant, Event, Fight, IngestionLedger, Report, WatchedReport,
)
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)

ENC = 5_432_188
CODE_OURS = "T1A0_OURS"
CODE_FIELD = "T1A0_FIELD"
STATIC_ID = 1


@pytest.fixture
def two_kills_one_each():
    """Two kill fights of the same encounter:
      - CODE_OURS, watched by static 1 → 'ours'. SAM does 800 damage events of 1000 each.
      - CODE_FIELD, not watched by static 1 → 'field'. SAM (1200 ea) + PCT (1000 ea).
    """
    fight_ids = []
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        for code in (CODE_OURS, CODE_FIELD):
            s.add(Report(code=code, ingested_at=now))
        s.flush()
        s.add(WatchedReport(static_id=STATIC_ID, code=CODE_OURS,
                            active=True, added_at=now))
        BOSS = 9999

        # ours: 1 SAM, 100 hits of 800 across 100s → 800 DPS
        f_ours = Fight(report_code=CODE_OURS, fight_id_in_report=1,
                       encounter_id=ENC, is_kill=True,
                       start_time=0, end_time=100_000, duration_ms=100_000)
        s.add(f_ours)
        s.flush()
        fight_ids.append(f_ours.id)
        s.add(Combatant(fight_id=f_ours.id, player_id=1,
                        name="OurSAM", job="SAM"))
        for j in range(100):
            s.add(Event(fight_id=f_ours.id, ts=j * 1000, type="damage",
                        source_id=1, target_id=BOSS,
                        ability_game_id=1, amount=800))

        # field: 1 SAM (1200 DPS) + 1 PCT (1000 DPS)
        f_field = Fight(report_code=CODE_FIELD, fight_id_in_report=1,
                        encounter_id=ENC, is_kill=True,
                        start_time=0, end_time=100_000, duration_ms=100_000)
        s.add(f_field)
        s.flush()
        fight_ids.append(f_field.id)
        s.add(Combatant(fight_id=f_field.id, player_id=10,
                        name="FieldSAM", job="SAM"))
        s.add(Combatant(fight_id=f_field.id, player_id=11,
                        name="FieldPCT", job="PCT"))
        for j in range(100):
            s.add(Event(fight_id=f_field.id, ts=j * 1000, type="damage",
                        source_id=10, target_id=BOSS,
                        ability_game_id=1, amount=1200))
            s.add(Event(fight_id=f_field.id, ts=j * 1000 + 500, type="damage",
                        source_id=11, target_id=BOSS,
                        ability_game_id=2, amount=1000))
        s.commit()
        try:
            yield s
        finally:
            s.execute(delete(Event).where(Event.fight_id.in_(fight_ids)))
            s.execute(delete(Combatant).where(Combatant.fight_id.in_(fight_ids)))
            s.execute(delete(Fight).where(Fight.id.in_(fight_ids)))
            s.execute(delete(WatchedReport).where(WatchedReport.code == CODE_OURS))
            s.execute(delete(IngestionLedger).where(
                IngestionLedger.report_code.in_([CODE_OURS, CODE_FIELD])))
            s.execute(delete(Report).where(Report.code.in_([CODE_OURS, CODE_FIELD])))
            s.commit()


def test_empty_encounter_returns_zero_shape():
    with SessionLocal() as s:
        r = dps_comparison_for_encounter(s, 99_999_999, STATIC_ID)
    assert r["ours"]["kills_aggregated"] == 0
    assert r["field"]["kills_aggregated"] == 0
    assert r["ours"]["phases"] == []
    assert r["field"]["phases"] == []
    assert r["jobs_available"] == []


def test_no_job_filter_raid_dps_distribution(two_kills_one_each):
    s = two_kills_one_each
    r = dps_comparison_for_encounter(s, ENC, STATIC_ID)
    assert r["ours"]["kills_aggregated"] == 1
    assert r["field"]["kills_aggregated"] == 1
    # Single phase aggregation for each side
    assert len(r["ours"]["phases"]) == 1
    assert len(r["field"]["phases"]) == 1
    our = r["ours"]["phases"][0]["dps"]
    field = r["field"]["phases"][0]["dps"]
    # Our raid DPS = 800; field raid DPS = 1200+1000 = 2200
    assert 750 <= our["p50"] <= 850
    assert 2100 <= field["p50"] <= 2300


def test_job_filter_narrows_to_per_player_distribution(two_kills_one_each):
    s = two_kills_one_each
    r = dps_comparison_for_encounter(s, ENC, STATIC_ID, job="SAM")
    # Only SAMs counted on each side
    assert r["ours"]["phases"][0]["dps"]["n"] == 1
    assert r["field"]["phases"][0]["dps"]["n"] == 1
    # Our SAM 800, field SAM 1200
    assert 750 <= r["ours"]["phases"][0]["dps"]["p50"] <= 850
    assert 1150 <= r["field"]["phases"][0]["dps"]["p50"] <= 1250


def test_job_filter_excludes_other_jobs(two_kills_one_each):
    """Filtering to PCT must drop the SAM contributions entirely. The 'ours'
    side has no PCT → phases list is empty for ours."""
    s = two_kills_one_each
    r = dps_comparison_for_encounter(s, ENC, STATIC_ID, job="PCT")
    # ours had no PCT — that side should be empty
    assert r["ours"]["phases"] == []
    # field's PCT was 1000 DPS
    assert r["field"]["phases"][0]["dps"]["n"] == 1
    assert 950 <= r["field"]["phases"][0]["dps"]["p50"] <= 1050


def test_jobs_available_reflects_both_sides(two_kills_one_each):
    s = two_kills_one_each
    r = dps_comparison_for_encounter(s, ENC, STATIC_ID)
    assert "SAM" in r["jobs_available"]
    assert "PCT" in r["jobs_available"]


def test_static_with_no_kills_sees_only_field(two_kills_one_each):
    """A different static (no overlap with watchlist) sees both fights as field."""
    s = two_kills_one_each
    r = dps_comparison_for_encounter(s, ENC, 9_999_999)  # nonexistent static
    assert r["ours"]["kills_aggregated"] == 0
    assert r["field"]["kills_aggregated"] == 2
