"""Unit tests for M-WIPE wipe histogram (T-006)."""
from __future__ import annotations

from datetime import datetime, timezone

from db.models import Combatant, Event, Fight, Report
from analysis.wipes import wipe_histogram_for_report


def _new_fight(db_session, code: str, fight_id: int, *, is_kill: bool, last_phase: int,
               start: int, end: int) -> Fight:
    f = Fight(
        report_code=code, fight_id_in_report=fight_id, encounter_id=1,
        is_kill=is_kill, fight_percentage=0.0, last_phase=last_phase,
        start_time=start, end_time=end, duration_ms=end - start,
    )
    db_session.add(f)
    db_session.flush()
    return f


def _new_report(db_session, code: str) -> Report:
    r = Report(code=code, is_public=True, ingested_at=datetime.now(timezone.utc))
    db_session.add(r)
    db_session.flush()
    return r


def test_no_fights_returns_empty(db_session):
    _new_report(db_session, "WIPE_E1")
    result = wipe_histogram_for_report(db_session, "WIPE_E1")
    assert result == {
        "report_code": "WIPE_E1", "total_wipes": 0, "total_kills": 0, "buckets": []
    }


def test_kill_only_returns_no_buckets(db_session):
    _new_report(db_session, "WIPE_K1")
    _new_fight(db_session, "WIPE_K1", 1, is_kill=True, last_phase=3,
               start=0, end=100_000)
    result = wipe_histogram_for_report(db_session, "WIPE_K1")
    assert result["total_kills"] == 1
    assert result["total_wipes"] == 0
    assert result["buckets"] == []


def test_buckets_by_phase_and_last_boss_cast(db_session):
    _new_report(db_session, "WIPE_H1")

    # Two wipes in phase 2, both killed by mechanic ability 555 (boss source 99)
    # One wipe in phase 3, killed by mechanic ability 777 (boss source 99)
    # One kill in phase 3 (should not appear in buckets)
    f1 = _new_fight(db_session, "WIPE_H1", 1, is_kill=False, last_phase=2, start=0, end=10_000)
    f2 = _new_fight(db_session, "WIPE_H1", 2, is_kill=False, last_phase=2, start=20_000, end=30_000)
    f3 = _new_fight(db_session, "WIPE_H1", 3, is_kill=False, last_phase=3, start=40_000, end=50_000)
    f4 = _new_fight(db_session, "WIPE_H1", 4, is_kill=True, last_phase=3, start=60_000, end=70_000)

    # Combatants — player IDs to filter out from "boss" cast logic.
    for fight in (f1, f2, f3, f4):
        for pid in (1, 2, 3, 4):
            db_session.add(Combatant(fight_id=fight.id, player_id=pid, name=f"P{pid}",
                                     server="S", job="DRG"))
    db_session.flush()

    # Cast events: last boss cast in lookback window before fight.end_time
    # For f1 (ends 10_000): boss cast 555 at 9_500 (within 15s window)
    # For f2 (ends 30_000): boss cast 555 at 29_500
    # For f3 (ends 50_000): boss cast 777 at 49_000
    # Also a player cast at 9_900 for f1 — should be ignored
    db_session.add_all([
        Event(fight_id=f1.id, ts=9_500, type="cast", source_id=99,
              ability_game_id=555, raw={}),
        Event(fight_id=f1.id, ts=9_900, type="cast", source_id=1,  # player, ignored
              ability_game_id=666, raw={}),
        Event(fight_id=f2.id, ts=29_500, type="cast", source_id=99,
              ability_game_id=555, raw={}),
        Event(fight_id=f3.id, ts=49_000, type="cast", source_id=99,
              ability_game_id=777, raw={}),
    ])
    db_session.flush()

    result = wipe_histogram_for_report(db_session, "WIPE_H1")
    assert result["total_wipes"] == 3
    assert result["total_kills"] == 1
    assert len(result["buckets"]) == 2

    # Bucket (phase=2, ability=555) should have 2 wipes; (phase=3, ability=777) 1
    by_key = {(b["phase"], b["ability_game_id"]): b for b in result["buckets"]}
    assert by_key[(2, 555)]["count"] == 2
    assert sorted(by_key[(2, 555)]["wipes"]) == sorted([f1.id, f2.id])
    assert by_key[(3, 777)]["count"] == 1
    assert by_key[(3, 777)]["wipes"] == [f3.id]


def test_no_cast_in_window_buckets_as_phase_only(db_session):
    _new_report(db_session, "WIPE_NC")
    f1 = _new_fight(db_session, "WIPE_NC", 1, is_kill=False, last_phase=1,
                    start=0, end=10_000)
    # Player cast inside the window, no boss cast.
    db_session.add(Combatant(fight_id=f1.id, player_id=1, name="A", server="S", job="PLD"))
    db_session.add(Event(fight_id=f1.id, ts=9_000, type="cast", source_id=1,
                         ability_game_id=42, raw={}))
    db_session.flush()
    result = wipe_histogram_for_report(db_session, "WIPE_NC")
    assert result["buckets"] == [
        {"phase": 1, "ability_game_id": None, "count": 1, "wipes": [f1.id]}
    ]


def test_cast_outside_lookback_window_ignored(db_session):
    _new_report(db_session, "WIPE_OUT")
    f1 = _new_fight(db_session, "WIPE_OUT", 1, is_kill=False, last_phase=4,
                    start=0, end=60_000)
    db_session.add(Combatant(fight_id=f1.id, player_id=1, name="A", server="S", job="PLD"))
    # Boss cast far before lookback (~16s) — should be excluded with default 15s lookback
    db_session.add(Event(fight_id=f1.id, ts=44_000, type="cast", source_id=99,
                         ability_game_id=888, raw={}))
    db_session.flush()
    result = wipe_histogram_for_report(db_session, "WIPE_OUT")
    assert result["buckets"][0]["ability_game_id"] is None


def test_sort_order_count_desc(db_session):
    _new_report(db_session, "WIPE_SORT")
    fights = []
    for i, (phase, ability) in enumerate([(1, 100), (1, 100), (2, 200), (1, 100), (3, 300)], start=1):
        f = _new_fight(db_session, "WIPE_SORT", i, is_kill=False, last_phase=phase,
                       start=i * 100_000, end=i * 100_000 + 10_000)
        fights.append(f)
        db_session.add(Event(fight_id=f.id, ts=i * 100_000 + 9_000, type="cast",
                             source_id=99, ability_game_id=ability, raw={}))
    db_session.flush()
    result = wipe_histogram_for_report(db_session, "WIPE_SORT")
    counts = [b["count"] for b in result["buckets"]]
    assert counts == sorted(counts, reverse=True)
    assert result["buckets"][0]["count"] == 3