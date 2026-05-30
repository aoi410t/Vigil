"""T-206 failure cartography tests + live AC."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete

from analysis.cartography import cartography_for_encounter
from db.models import (
    Combatant, Event, Fight, IngestionLedger, Report, WatchedReport,
)
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)

ENC = 765432
CODES = ("T206_A",)


@pytest.fixture
def seeded_wipe():
    """One wipe pull: boss ability 555 kills 4 players; ability 666 kills 1."""
    fight_id_holder = []
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        s.add(Report(code=CODES[0], ingested_at=now))
        s.flush()
        f = Fight(report_code=CODES[0], fight_id_in_report=1,
                  encounter_id=ENC, is_kill=False,
                  start_time=0, end_time=60_000, duration_ms=60_000)
        s.add(f)
        s.flush()
        fight_id_holder.append(f.id)
        BOSS = 9999
        for pid in range(1, 6):  # 5 players
            s.add(Combatant(fight_id=f.id, player_id=pid, name=f"P{pid}", job="WAR"))
            # Each player casts once so _active_players includes them
            s.add(Event(fight_id=f.id, ts=500, type="cast",
                        source_id=pid, ability_game_id=8888))
        # 4 players die to ability 555
        for pid in range(1, 5):
            s.add(Event(fight_id=f.id, ts=30_000 + pid * 50, type="death",
                        source_id=BOSS, target_id=pid, ability_game_id=555))
        # 1 player dies to ability 666
        s.add(Event(fight_id=f.id, ts=40_000, type="death",
                    source_id=BOSS, target_id=5, ability_game_id=666))
        # Non-attributable death from FFLogs' source_id=-1 pattern
        s.add(Event(fight_id=f.id, ts=45_000, type="death",
                    source_id=-1, target_id=1, ability_game_id=None))
        s.commit()
        try:
            yield s, f.id
        finally:
            s.execute(delete(Event).where(Event.fight_id.in_(fight_id_holder)))
            s.execute(delete(Combatant).where(Combatant.fight_id.in_(fight_id_holder)))
            s.execute(delete(Fight).where(Fight.id.in_(fight_id_holder)))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code.in_(CODES)))
            s.execute(delete(Report).where(Report.code.in_(CODES)))
            s.commit()


def test_cartography_buckets_by_killing_ability(seeded_wipe):
    session, _ = seeded_wipe
    result = cartography_for_encounter(session, ENC)
    by_aid = {b["ability_game_id"]: b for b in result["buckets"]}
    assert by_aid[555]["deaths"] == 4
    assert by_aid[555]["fights_affected"] == 1
    assert by_aid[666]["deaths"] == 1


def test_non_attributable_death_bucketed_under_none(seeded_wipe):
    session, _ = seeded_wipe
    result = cartography_for_encounter(session, ENC)
    nones = [b for b in result["buckets"] if b["non_attributable"]]
    assert len(nones) == 1
    assert nones[0]["deaths"] == 1
    assert nones[0]["ability_game_id"] is None


def test_totals_match_buckets(seeded_wipe):
    session, _ = seeded_wipe
    result = cartography_for_encounter(session, ENC)
    bucket_total = sum(b["deaths"] for b in result["buckets"])
    assert bucket_total == result["total_deaths"]
    assert result["total_wipes"] == 1
    assert result["total_kills"] == 0


def test_buckets_sorted_by_deaths_desc(seeded_wipe):
    session, _ = seeded_wipe
    result = cartography_for_encounter(session, ENC)
    deaths = [b["deaths"] for b in result["buckets"]]
    assert deaths == sorted(deaths, reverse=True)


def test_unknown_encounter_returns_zero_shape():
    with SessionLocal() as s:
        result = cartography_for_encounter(s, 999_999_999)
    assert result["total_fights"] == 0
    assert result["buckets"] == []


# ---- v1.8.0: static_id-scoped cartography for consumer Home ----

ENC_SCOPED = 765_433
CODE_OURS = "T206_OURS"
CODE_THEIRS = "T206_THEIRS"


@pytest.fixture
def seeded_two_statics():
    """Two wipes for the same encounter: one in our watchlist (static_id=1),
    one in someone else's (static_id=2). Both kill via ability 555."""
    fight_ids = []
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        for code in (CODE_OURS, CODE_THEIRS):
            s.add(Report(code=code, ingested_at=now))
        s.flush()
        # static 1 watches CODE_OURS only.
        s.add(WatchedReport(static_id=1, code=CODE_OURS, active=True,
                            added_at=now))
        # static 2 watches CODE_THEIRS only (simulating another tenant).
        # NB: tests run against static_id=1 by default; we just need that
        # CODE_THEIRS is NOT in static 1's watchlist.
        BOSS = 9999
        for code in (CODE_OURS, CODE_THEIRS):
            f = Fight(report_code=code, fight_id_in_report=1,
                      encounter_id=ENC_SCOPED, is_kill=False,
                      start_time=0, end_time=60_000, duration_ms=60_000)
            s.add(f)
            s.flush()
            fight_ids.append(f.id)
            for pid in range(1, 4):
                s.add(Combatant(fight_id=f.id, player_id=pid,
                                name=f"P{pid}", job="WAR"))
                s.add(Event(fight_id=f.id, ts=500, type="cast",
                            source_id=pid, ability_game_id=8888))
                s.add(Event(fight_id=f.id, ts=30_000 + pid * 50, type="death",
                            source_id=BOSS, target_id=pid,
                            ability_game_id=555))
        s.commit()
        try:
            yield s, fight_ids
        finally:
            s.execute(delete(Event).where(Event.fight_id.in_(fight_ids)))
            s.execute(delete(Combatant).where(Combatant.fight_id.in_(fight_ids)))
            s.execute(delete(Fight).where(Fight.id.in_(fight_ids)))
            s.execute(delete(WatchedReport).where(
                WatchedReport.code.in_([CODE_OURS, CODE_THEIRS])))
            s.execute(delete(IngestionLedger).where(
                IngestionLedger.report_code.in_([CODE_OURS, CODE_THEIRS])))
            s.execute(delete(Report).where(
                Report.code.in_([CODE_OURS, CODE_THEIRS])))
            s.commit()


def test_scoped_cartography_sees_only_our_wipes(seeded_two_statics):
    s, _ = seeded_two_statics
    scoped = cartography_for_encounter(s, ENC_SCOPED, static_id=1)
    assert scoped["total_wipes"] == 1  # only CODE_OURS in static 1
    assert scoped["total_deaths"] == 3
    by_aid = {b["ability_game_id"]: b for b in scoped["buckets"]}
    assert by_aid[555]["deaths"] == 3


def test_unscoped_cartography_sees_both_statics(seeded_two_statics):
    s, _ = seeded_two_statics
    unscoped = cartography_for_encounter(s, ENC_SCOPED)
    assert unscoped["total_wipes"] == 2  # both reports counted
    assert unscoped["total_deaths"] == 6


def test_scoped_cartography_with_no_watched_reports():
    """Static with empty watchlist gets an empty result, not all-fights."""
    with SessionLocal() as s:
        result = cartography_for_encounter(s, ENC_SCOPED, static_id=9_999_999)
    assert result["total_fights"] == 0
    assert result["buckets"] == []


# ---- Live AC against M5S (which has wipes) ----

def test_live_m5s_cartography_has_top_ability():
    """M5S has 32 wipes in dev DB — should produce a populated cartography
    where the top ability has multiple deaths and a fight_model_label
    (since T-203 ran on it)."""
    with SessionLocal() as s:
        result = cartography_for_encounter(s, 101)
    if result["total_wipes"] == 0:
        pytest.skip("no M5S wipes in dev DB")
    assert result["total_deaths"] > 0
    assert result["buckets"]
    top = result["buckets"][0]
    assert top["deaths"] >= 2
