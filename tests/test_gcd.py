"""Unit tests for M-GCD gcd-drop detection (T-008)."""
from __future__ import annotations

from datetime import datetime, timezone

from db.models import Combatant, Event, Fight, Report
from analysis.gcd import (
    DEFAULT_GCD_MS,
    detect_gcd_drops,
    estimate_gcd_ms,
    mode1_gcd_for_report,
)


def test_estimate_gcd_ms_from_clean_stream():
    timestamps = [0, 2500, 5000, 7500, 10_000, 12_500]
    assert estimate_gcd_ms(timestamps) == 2500


def test_estimate_gcd_ms_with_ogcd_weaves():
    # GCDs at 0, 2500, 5000; oGCDs at 1000 and 3500
    timestamps = [0, 1000, 2500, 3500, 5000, 7500, 10_000]
    # The 1000ms and 1500ms gaps are oGCD weaves and outside [1800, 2800].
    assert estimate_gcd_ms(timestamps) == 2500


def test_estimate_gcd_ms_falls_back_when_too_few_casts():
    assert estimate_gcd_ms([0, 2500]) == DEFAULT_GCD_MS


def test_estimate_gcd_ms_handles_skill_speed():
    # Player with skill speed → ~2300 ms GCD
    timestamps = list(range(0, 25_000, 2300))
    est = estimate_gcd_ms(timestamps)
    assert 2200 <= est <= 2400


def test_detect_no_drops_in_clean_stream():
    timestamps = [0, 2500, 5000, 7500, 10_000]
    result = detect_gcd_drops(timestamps, gcd_ms=2500)
    assert result["gcds_cast"] == 5
    assert result["dropped_count"] == 0
    assert result["drop_positions"] == []


def test_detect_one_dropped_gcd():
    # GCDs at 0, 2500, then a 5000ms gap (one slot missed at ~5000), then 7500
    timestamps = [0, 2500, 7500, 10_000]
    result = detect_gcd_drops(timestamps, gcd_ms=2500)
    assert result["gcds_cast"] == 4
    assert result["dropped_count"] == 1
    assert result["drop_positions"] == [5000]


def test_detect_multiple_drops_in_one_gap():
    # 10s gap = 4 slots wide → 3 drops
    timestamps = [0, 2500, 12_500]
    result = detect_gcd_drops(timestamps, gcd_ms=2500)
    assert result["dropped_count"] == 3
    assert result["drop_positions"] == [5000, 7500, 10_000]


def test_ogcd_weaves_dont_inflate_spine_or_drops():
    # GCDs at 0, 2500, 5000; oGCD at 1000 between first two; no drops
    timestamps = [0, 1000, 2500, 5000]
    result = detect_gcd_drops(timestamps, gcd_ms=2500)
    assert result["gcds_cast"] == 3
    assert result["dropped_count"] == 0


def test_short_stream_returns_zero_drops():
    assert detect_gcd_drops([], gcd_ms=2500)["dropped_count"] == 0
    assert detect_gcd_drops([1000], gcd_ms=2500)["dropped_count"] == 0


def _seed(db_session, code: str, *, fight_kwargs=None):
    db_session.add(Report(code=code, is_public=True,
                          ingested_at=datetime.now(timezone.utc)))
    db_session.flush()
    fk = {"is_kill": False, "fight_percentage": 0.0, "last_phase": 1,
          "start_time": 0, "end_time": 60_000, "duration_ms": 60_000}
    fk.update(fight_kwargs or {})
    f = Fight(report_code=code, fight_id_in_report=1, encounter_id=1, **fk)
    db_session.add(f)
    db_session.flush()
    return f


def test_report_level_rollup_per_player(db_session):
    f = _seed(db_session, "G_RR")
    # Two players, both with 2.5s GCD; player 1 has 1 drop, player 2 has 0.
    db_session.add_all([
        Combatant(fight_id=f.id, player_id=1, name="Alice", server="S", job="DRG"),
        Combatant(fight_id=f.id, player_id=2, name="Bob", server="S", job="BLM"),
    ])
    casts_p1 = [0, 2500, 7500, 10_000]  # drop at 5000
    casts_p2 = [0, 2500, 5000, 7500, 10_000]
    db_session.add_all([
        Event(fight_id=f.id, ts=t, type="cast", source_id=1, ability_game_id=100, raw={})
        for t in casts_p1
    ] + [
        Event(fight_id=f.id, ts=t, type="cast", source_id=2, ability_game_id=200, raw={})
        for t in casts_p2
    ])
    db_session.flush()
    result = mode1_gcd_for_report(db_session, "G_RR")
    players = result["fights"][0]["players"]
    by_pid = {p["player_id"]: p for p in players}
    assert by_pid[1]["dropped_count"] == 1
    assert by_pid[1]["drop_positions"] == [5000]
    assert by_pid[2]["dropped_count"] == 0
    # Sort: player with more drops first
    assert players[0]["player_id"] == 1


def test_non_player_casts_excluded(db_session):
    f = _seed(db_session, "G_NP")
    db_session.add(Combatant(fight_id=f.id, player_id=1, name="Alice", server="S", job="DRG"))
    # Boss casts (source 99, no combatant row) should not appear in output.
    db_session.add_all([
        Event(fight_id=f.id, ts=0, type="cast", source_id=99, ability_game_id=900, raw={}),
        Event(fight_id=f.id, ts=5000, type="cast", source_id=99, ability_game_id=901, raw={}),
        Event(fight_id=f.id, ts=0, type="cast", source_id=1, ability_game_id=100, raw={}),
        Event(fight_id=f.id, ts=2500, type="cast", source_id=1, ability_game_id=100, raw={}),
    ])
    db_session.flush()
    players = mode1_gcd_for_report(db_session, "G_NP")["fights"][0]["players"]
    assert [p["player_id"] for p in players] == [1]
    assert players[0]["gcds_cast"] == 2
    assert players[0]["dropped_count"] == 0


def test_empty_report(db_session):
    assert mode1_gcd_for_report(db_session, "G_NONE") == {
        "report_code": "G_NONE", "fights": []
    }