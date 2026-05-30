"""T-104 consensus timeline tests."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select

from analysis.consensus import (
    DEFAULT_CONSENSUS_THRESHOLD,
    consensus_timeline_for_encounter,
)
from db.models import Combatant, Event, Fight, IngestionLedger, Report
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)


@pytest.fixture
def three_pulls():
    """3 pulls of encounter 7777 — each with a boss ability fired at ~10s,
    plus one outlier ability that only one pull sees."""
    codes = ("T104_A", "T104_B", "T104_C")
    fight_ids: list[int] = []
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        for code in codes:
            s.add(Report(code=code, ingested_at=now))
        s.flush()

        BOSS_ENEMY = 9999
        PLAYER = 1
        for i, code in enumerate(codes):
            f = Fight(report_code=code, fight_id_in_report=1, encounter_id=7777,
                     is_kill=True, start_time=0, end_time=30_000, duration_ms=30_000)
            s.add(f)
            s.flush()
            fight_ids.append(f.id)
            s.add(Combatant(fight_id=f.id, player_id=PLAYER, name="P1", job="WAR"))
            # Boss is hit ≥30 times (phase detection threshold)
            for j in range(30):
                s.add(Event(fight_id=f.id, ts=j * 800, type="damage",
                           source_id=PLAYER, target_id=BOSS_ENEMY,
                           ability_game_id=999, amount=100))
            # Canonical boss cast: ability 555 around 10s ± small jitter per pull
            s.add(Event(fight_id=f.id, ts=10_000 + i * 100,
                       type="cast", source_id=BOSS_ENEMY,
                       ability_game_id=555, amount=None))
            # Player cast at the same time — must be filtered out
            s.add(Event(fight_id=f.id, ts=10_000 + i * 100,
                       type="cast", source_id=PLAYER,
                       ability_game_id=12345, amount=None))
        # Outlier — only pull A sees ability 777
        s.add(Event(fight_id=fight_ids[0], ts=20_000, type="cast",
                   source_id=BOSS_ENEMY, ability_game_id=777, amount=None))
        s.commit()
        try:
            yield s, fight_ids
        finally:
            s.execute(delete(Event).where(Event.fight_id.in_(fight_ids)))
            s.execute(delete(Combatant).where(Combatant.fight_id.in_(fight_ids)))
            s.execute(delete(Fight).where(Fight.id.in_(fight_ids)))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code.in_(codes)))
            s.execute(delete(Report).where(Report.code.in_(codes)))
            s.commit()


def test_consensus_below_min_pulls_returns_note():
    """Without ≥3 ingested pulls, return early with a note."""
    with SessionLocal() as s:
        r = consensus_timeline_for_encounter(s, 999999)
    assert r["phases"] == []
    assert "note" in r


def test_canonical_ability_recognized_at_100pct(three_pulls):
    session, _ = three_pulls
    r = consensus_timeline_for_encounter(session, 7777)
    assert r["total_pulls"] == 3
    assert len(r["phases"]) == 1  # single-phase synthetic fight
    phase = r["phases"][0]
    canonical = {a["ability_game_id"]: a for a in phase["canonical_abilities"]}
    assert 555 in canonical
    assert canonical[555]["occurrence_rate"] == 1.0
    # Player ability 12345 must be excluded (filtered as player source)
    assert 12345 not in canonical


def test_outlier_ability_below_threshold(three_pulls):
    session, _ = three_pulls
    r = consensus_timeline_for_encounter(session, 7777)
    phase = r["phases"][0]
    canonical = {a["ability_game_id"] for a in phase["canonical_abilities"]}
    all_seen = {a["ability_game_id"] for a in phase["all_abilities"]}
    # Outlier 777 (1/3 pulls = 33%) is below the 70% default threshold
    assert 777 not in canonical
    assert 777 in all_seen


def test_median_time_aggregates_across_pulls(three_pulls):
    session, _ = three_pulls
    r = consensus_timeline_for_encounter(session, 7777)
    phase = r["phases"][0]
    cast_555 = next(a for a in phase["canonical_abilities"]
                    if a["ability_game_id"] == 555)
    # Pulls fired at 10000, 10100, 10200 → median 10100
    assert cast_555["median_relative_t_ms"] == 10_100


def test_threshold_can_be_tightened(three_pulls):
    session, _ = three_pulls
    r = consensus_timeline_for_encounter(session, 7777, consensus_threshold=0.99)
    phase = r["phases"][0]
    canonical = {a["ability_game_id"] for a in phase["canonical_abilities"]}
    # 555 is 100%, so still canonical at 0.99 threshold
    assert 555 in canonical


# ---- Live AC against the 11 FRU kills in dev DB ----

def test_live_fru_consensus_returns_six_phases_high_confidence():
    """11 FRU kills must produce a 6-phase consensus where most P5 abilities
    are at >=70% recurrence and tight variance (boss is deterministic)."""
    with SessionLocal() as s:
        kills = s.execute(
            select(Fight.id).where(Fight.encounter_id == 1079, Fight.is_kill.is_(True))
        ).scalars().all()
        with_events = []
        for fid in kills:
            if s.execute(select(Event.id).where(Event.fight_id == fid).limit(1)).scalar():
                with_events.append(fid)
        if len(with_events) < 5:
            pytest.skip(f"need ≥5 FRU kills with events; have {len(with_events)}")
        r = consensus_timeline_for_encounter(s, 1079)
    assert r["total_pulls"] >= 5
    assert len(r["phases"]) == 6
    p5 = r["phases"][-1]
    # The final phase should have ≥5 canonical abilities (lots of scripted casts)
    assert len(p5["canonical_abilities"]) >= 5
    # Most should have tight variance (boss is deterministic to ~frame)
    tight_variance = sum(1 for a in p5["canonical_abilities"] if a["variance_ms"] < 500)
    assert tight_variance / len(p5["canonical_abilities"]) >= 0.8
