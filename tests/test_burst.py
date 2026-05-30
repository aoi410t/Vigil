"""T-105 M-BURST tests — burst-window merge + per-player alignment counts."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete

from analysis.burst import (
    DEFAULT_RAID_BUFF_WINDOW_MS,
    burst_alignment_for_report,
    in_any_interval,
    merge_intervals,
)
from db.models import (
    Ability,
    AbilityLabel,
    Combatant,
    Event,
    Fight,
    IngestionLedger,
    Report,
)
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)

CODE = "T105_TEST"
# Disposable label IDs.
RAID_BUFF_ID = 991001
PERSONAL_BUFF_ID = 991002


# ---------- pure-function tests (no DB) ----------

def test_merge_intervals_empty():
    assert merge_intervals([]) == []


def test_merge_intervals_no_overlap():
    assert merge_intervals([(0, 10), (20, 30)]) == [(0, 10), (20, 30)]


def test_merge_intervals_touch_or_overlap():
    assert merge_intervals([(0, 10), (10, 20), (15, 25)]) == [(0, 25)]


def test_merge_intervals_unsorted_input():
    assert merge_intervals([(20, 30), (0, 10), (25, 40)]) == [(0, 10), (20, 40)]


def test_in_any_interval():
    ivs = [(0, 100), (200, 300)]
    assert in_any_interval(50, ivs)
    assert in_any_interval(0, ivs)
    assert in_any_interval(100, ivs)
    assert not in_any_interval(150, ivs)
    assert in_any_interval(250, ivs)
    assert not in_any_interval(301, ivs)


# ---------- end-to-end with seeded DB ----------

@pytest.fixture
def seeded():
    """Seed: 1 report, 1 fight, 1 raid buff cast at ts=0, 2 personal buff casts."""
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        s.add(Report(code=CODE, ingested_at=now))
        s.flush()
        s.add(Ability(ability_game_id=RAID_BUFF_ID, kind="action",
                      name="Test Raid Buff", fetched_at=now))
        s.add(Ability(ability_game_id=PERSONAL_BUFF_ID, kind="action",
                      name="Test Personal CD", fetched_at=now))
        s.add(AbilityLabel(ability_game_id=RAID_BUFF_ID, label="raid_buff",
                           confidence=0.95, source="auto", updated_at=now))
        s.add(AbilityLabel(ability_game_id=PERSONAL_BUFF_ID, label="personal_buff",
                           confidence=0.95, source="auto", updated_at=now))
        f = Fight(report_code=CODE, fight_id_in_report=1, encounter_id=1,
                  is_kill=False, start_time=0, end_time=120_000, duration_ms=120_000)
        s.add(f)
        s.flush()
        s.add(Combatant(fight_id=f.id, player_id=1, name="Alice", job="WAR"))
        s.add(Combatant(fight_id=f.id, player_id=2, name="Bob", job="PLD"))
        # Raid buff cast at ts=0 → window [0, 20000]
        s.add(Event(fight_id=f.id, ts=0, type="cast", source_id=1,
                    ability_game_id=RAID_BUFF_ID))
        # Alice personal CDs at ts=5000 (in window) and ts=60000 (drift)
        s.add(Event(fight_id=f.id, ts=5000, type="cast", source_id=1,
                    ability_game_id=PERSONAL_BUFF_ID))
        s.add(Event(fight_id=f.id, ts=60_000, type="cast", source_id=1,
                    ability_game_id=PERSONAL_BUFF_ID))
        # Bob personal CD at ts=15000 (in window)
        s.add(Event(fight_id=f.id, ts=15_000, type="cast", source_id=2,
                    ability_game_id=PERSONAL_BUFF_ID))
        s.commit()
        try:
            yield s, f.id
        finally:
            s.execute(delete(Event).where(Event.fight_id == f.id))
            s.execute(delete(Combatant).where(Combatant.fight_id == f.id))
            s.execute(delete(Fight).where(Fight.id == f.id))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code == CODE))
            s.execute(delete(Report).where(Report.code == CODE))
            s.execute(delete(AbilityLabel).where(
                AbilityLabel.ability_game_id.in_([RAID_BUFF_ID, PERSONAL_BUFF_ID])))
            s.execute(delete(Ability).where(
                Ability.ability_game_id.in_([RAID_BUFF_ID, PERSONAL_BUFF_ID])))
            s.commit()


def test_burst_alignment_basic(seeded):
    session, fight_id = seeded
    result = burst_alignment_for_report(session, CODE)
    assert len(result["fights"]) == 1
    fight = result["fights"][0]
    assert fight["fight_id"] == fight_id
    assert fight["burst_windows"] == [[0, DEFAULT_RAID_BUFF_WINDOW_MS]]
    players = {p["name"]: p for p in fight["players"]}
    assert players["Alice"]["personal_casts_total"] == 2
    assert players["Alice"]["in_window"] == 1
    assert players["Alice"]["drift"] == 1
    assert players["Alice"]["in_window_pct"] == 0.5
    assert players["Bob"]["in_window_pct"] == 1.0


def test_unknown_report_returns_empty_fights(seeded):
    session, _ = seeded
    result = burst_alignment_for_report(session, "NOT_A_REPORT")
    assert result["fights"] == []


def test_no_raid_buff_labels_yields_no_windows():
    """If the labels table has no raid_buff rows, burst windows are empty,
    every personal cast counts as drift."""
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        s.add(Report(code="T105_NOLBL", ingested_at=now))
        s.flush()
        f = Fight(report_code="T105_NOLBL", fight_id_in_report=1, encounter_id=1,
                  is_kill=False, start_time=0, end_time=10_000, duration_ms=10_000)
        s.add(f)
        s.flush()
        s.add(Combatant(fight_id=f.id, player_id=1, name="X", job="WAR"))
        s.add(Event(fight_id=f.id, ts=100, type="cast", source_id=1, ability_game_id=PERSONAL_BUFF_ID))
        s.commit()
        try:
            result = burst_alignment_for_report(s, "T105_NOLBL")
            # No labels = no raid_buff IDs, hence no windows and personal_buff_ids empty too
            # so the player has 0 personal casts counted.
            assert result["fights"][0]["burst_windows"] == []
            assert result["fights"][0]["players"] == []
        finally:
            s.execute(delete(Event).where(Event.fight_id == f.id))
            s.execute(delete(Combatant).where(Combatant.fight_id == f.id))
            s.execute(delete(Fight).where(Fight.id == f.id))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code == "T105_NOLBL"))
            s.execute(delete(Report).where(Report.code == "T105_NOLBL"))
            s.commit()


def test_overlapping_raid_buffs_merge_into_single_window(seeded):
    """Two raid buff casts 5s apart should merge into one [first_cast, last_cast + window] window."""
    session, fight_id = seeded
    # Add a second raid buff cast at ts=5000 — window should become [0, 25000]
    session.add(Event(fight_id=fight_id, ts=5000, type="cast",
                      source_id=1, ability_game_id=RAID_BUFF_ID))
    session.commit()
    result = burst_alignment_for_report(session, CODE)
    assert result["fights"][0]["burst_windows"] == [[0, DEFAULT_RAID_BUFF_WINDOW_MS + 5000]]


def test_per_ability_duration_override(seeded):
    """When abilities.duration_ms is set, the window for that ability uses
    the wiki-scraped duration instead of the 20s default. Real example:
    Reprisal (mit_boss_debuff) is 15s, not 20s."""
    session, fight_id = seeded
    # Simulate the wiki scrape having populated 15s on the raid buff ability
    ability = session.get(Ability, RAID_BUFF_ID)
    ability.duration_ms = 15_000
    session.commit()
    result = burst_alignment_for_report(session, CODE)
    # Window length is now 15s, not 20s. The single cast at ts=0 yields [0, 15000].
    assert result["fights"][0]["burst_windows"] == [[0, 15_000]]
    # Alice's CD at 5000 was in window; her CD at 60000 still drifts.
    players = {p["name"]: p for p in result["fights"][0]["players"]}
    assert players["Alice"]["in_window"] == 1
    assert players["Alice"]["drift"] == 1
    # Bob's CD at 15000 is now AT the window edge — should still count as in.
    # (in_any_interval uses <= boundaries)
    assert players["Bob"]["in_window"] == 1


def test_per_ability_duration_only_affects_that_ability(seeded):
    """When two raid buffs are involved and only one has duration_ms set,
    windows from the other still use the default."""
    session, fight_id = seeded
    # Add a second raid-buff ability with NO duration_ms
    second_id = 991009
    now = datetime.now(timezone.utc)
    try:
        session.add(Ability(ability_game_id=second_id, kind="action",
                            name="Other Raid Buff", fetched_at=now))
        session.add(AbilityLabel(ability_game_id=second_id, label="raid_buff",
                                 confidence=0.95, source="auto",
                                 updated_at=now))
        # Set duration on the first one only
        a1 = session.get(Ability, RAID_BUFF_ID)
        a1.duration_ms = 15_000
        # First raid-buff cast (15s window) at 0, second raid-buff cast (default
        # 20s window) at 50000 — should produce two disjoint windows.
        session.add(Event(fight_id=fight_id, ts=50_000, type="cast",
                          source_id=1, ability_game_id=second_id))
        session.commit()
        result = burst_alignment_for_report(session, CODE)
        wins = result["fights"][0]["burst_windows"]
        # First window: [0, 15000]. Second: [50000, 50000 + 20000].
        assert wins == [[0, 15_000], [50_000, 70_000]]
    finally:
        session.execute(delete(Event).where(
            Event.fight_id == fight_id,
            Event.ability_game_id == second_id))
        session.execute(delete(AbilityLabel).where(
            AbilityLabel.ability_game_id == second_id))
        session.execute(delete(Ability).where(
            Ability.ability_game_id == second_id))
        session.commit()
