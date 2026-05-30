"""Tests for cactbot Stage 2 (slot-driven) — per-pull expected-vs-actual diff.

Tests inject a synthetic `ParsedTimeline` so they don't depend on the vendored
cactbot files. See `_timeline(...)` helper below for the slot shape.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from analysis.timeline_diff import timeline_diff_for_fight
from db.models import (
    Combatant, Event, Fight, FightModel, IngestionLedger, Report,
)
from ingest.cactbot import CactbotEntry, ParsedTimeline


def _seed_fight(session, *, code: str, encounter_id: int = 9999) -> int:
    """Insert a minimal fight + ledger + report. Returns fight PK."""
    now_dt = datetime.now(timezone.utc)
    session.add(Report(code=code, is_public=True, ingested_at=now_dt))
    session.flush()
    session.add(IngestionLedger(
        report_code=code, fights_ingested=[1], last_event_ts=0,
        status="open", last_polled_at=now_dt,
    ))
    f = Fight(
        report_code=code, fight_id_in_report=1, encounter_id=encounter_id,
        is_kill=False, start_time=0, end_time=200_000, duration_ms=200_000,
    )
    session.add(f)
    session.flush()
    return f.id


def _add_combatants(session, fight_id: int, ids: list[int]) -> None:
    for pid in ids:
        session.add(Combatant(fight_id=fight_id, player_id=pid, name=f"p{pid}"))
    session.flush()


def _add_player_damage_to_boss(session, fight_id: int, ts: int,
                                source_player: int = 1,
                                target_boss: int = 999,
                                aid: int = 9001) -> None:
    """Drives T-103 phase detection via enemy-target activity window."""
    session.add(Event(
        fight_id=fight_id, ts=ts, type="damage",
        source_id=source_player, target_id=target_boss, ability_game_id=aid,
        amount=100, raw={},
    ))


def _add_boss_cast(session, fight_id: int, ts: int, aid: int) -> None:
    session.add(Event(
        fight_id=fight_id, ts=ts, type="cast",
        source_id=999, target_id=None, ability_game_id=aid,
        amount=None, raw={},
    ))


def _add_fight_model_row(session, encounter_id: int, phase: int, seq: int,
                        ability_game_id: int, *,
                        type_label: str = "raidwide") -> None:
    """Type-label-only stub; cactbot annotation comes from the timeline injection."""
    session.add(FightModel(
        encounter_id=encounter_id, version=1, phase=phase, seq=seq,
        ability_game_id=ability_game_id, relative_t_ms=0,
        type_label=type_label,
    ))


def _timeline(*entries) -> ParsedTimeline:
    """Build a ParsedTimeline from `(phase_index, phase_label, abs_time_s, label, ability_ids)` tuples.

    The first entry of each phase anchors the phase-relative clock at 0 for
    phase 0, then at the entry's abs_time for subsequent phases (matches the
    real parser's behavior).
    """
    cb_entries = []
    phase_starts: dict[int, float] = {0: 0.0}
    for phase_index, phase_label, abs_t, label, ids in entries:
        cb_entries.append(CactbotEntry(
            abs_time_s=abs_t, label=label, ability_ids=list(ids),
            phase_index=phase_index, phase_label=phase_label,
        ))
    # Compute phase_relative_t_s the same way parse_timeline_file does
    for e in cb_entries:
        if e.phase_index not in phase_starts:
            phase_starts[e.phase_index] = e.abs_time_s
    for e in cb_entries:
        e.phase_relative_t_s = e.abs_time_s - phase_starts[e.phase_index]
    return ParsedTimeline(encounter_file="<synthetic>", entries=cb_entries,
                          phase_labels={0: "P1"})


def test_unknown_fight_returns_note(db_session):
    out = timeline_diff_for_fight(db_session, fight_id=999_999_999)
    assert out["phases"] == []
    assert "not found" in out.get("note", "").lower()


def test_no_phases_returns_empty(db_session):
    fid = _seed_fight(db_session, code="TDIFF_A", encounter_id=8001)
    out = timeline_diff_for_fight(db_session, fid,
                                  _timeline=_timeline((0, "P1", 10.0, "x", [42])))
    assert out["phases"] == []
    assert "no phases" in out.get("note", "").lower()


def test_no_cactbot_timeline_returns_note(db_session):
    fid = _seed_fight(db_session, code="TDIFF_NCB", encounter_id=8099)
    _add_combatants(db_session, fid, [1])
    for i in range(40):
        _add_player_damage_to_boss(db_session, fid, ts=1000 + i * 1000)
    # No fight_model rows either, so the function returns the no-fight_model
    # note (it's checked before timeline load in the slot-driven version).
    db_session.flush()
    out = timeline_diff_for_fight(db_session, fid)
    assert out["phases"] == []
    assert "cactbot timeline" in out.get("note", "") or "fight_model" in out.get("note", "")


def test_fired_with_drift(db_session):
    """A mechanic that fired late shows a positive drift."""
    fid = _seed_fight(db_session, code="TDIFF_B", encounter_id=8002)
    _add_combatants(db_session, fid, [1, 2])
    for i in range(40):
        _add_player_damage_to_boss(db_session, fid, ts=1000 + i * 1000)
    _add_boss_cast(db_session, fid, ts=11_500, aid=4242)
    _add_fight_model_row(db_session, 8002, phase=0, seq=0,
                          ability_game_id=4242, type_label="raidwide")
    db_session.flush()

    tl = _timeline((0, "P1", 10.0, "Killer Voice", [4242]))
    out = timeline_diff_for_fight(db_session, fid, _timeline=tl)
    p0 = out["phases"][0]
    assert p0["entries_fired"] == 1
    assert p0["entries_missing"] == 0
    e = p0["entries"][0]
    assert e["cactbot_label"] == "Killer Voice"
    assert e["fired"] is True
    # phase_start = 1000. actual_t = 11500 - 1000 = 10_500. expected = 10_000. drift = +500.
    assert e["actual_t_ms"] == 10_500
    assert e["drift_ms"] == 500


def test_missing_mechanic(db_session):
    fid = _seed_fight(db_session, code="TDIFF_C", encounter_id=8003)
    _add_combatants(db_session, fid, [1])
    for i in range(40):
        _add_player_damage_to_boss(db_session, fid, ts=1000 + i * 1000)
    # No cast events for 7777
    _add_fight_model_row(db_session, 8003, phase=0, seq=0,
                          ability_game_id=7777, type_label="raidwide")
    db_session.flush()

    tl = _timeline((0, "P1", 5.0, "Never Fires", [7777]))
    out = timeline_diff_for_fight(db_session, fid, _timeline=tl)
    p0 = out["phases"][0]
    assert p0["entries_fired"] == 0
    assert p0["entries_missing"] == 1
    e = p0["entries"][0]
    assert e["fired"] is False
    assert e["actual_t_ms"] is None


def test_cosmetic_slot_still_included(db_session):
    """Slot-driven trusts cactbot's curation: even if all our fight_model rows
    for a slot's IDs are typed 'cosmetic' (sub-cast damage attribution
    classifies the headline cast as no-damage), we still show the slot — the
    cast time is real strat information. The slot just gets `type_label:
    cosmetic` in the output for UI shading."""
    fid = _seed_fight(db_session, code="TDIFF_D", encounter_id=8004)
    _add_combatants(db_session, fid, [1])
    for i in range(40):
        _add_player_damage_to_boss(db_session, fid, ts=1000 + i * 1000)
    _add_boss_cast(db_session, fid, ts=5_500, aid=1111)
    _add_fight_model_row(db_session, 8004, phase=0, seq=0,
                          ability_game_id=1111, type_label="cosmetic")
    db_session.flush()

    tl = _timeline((0, "P1", 4.0, "Cosmetic Effect", [1111]))
    out = timeline_diff_for_fight(db_session, fid, _timeline=tl)
    entries = out["phases"][0]["entries"]
    assert len(entries) == 1
    assert entries[0]["cactbot_label"] == "Cosmetic Effect"
    assert entries[0]["type_label"] == "cosmetic"
    assert entries[0]["fired"] is True


def test_multi_cast_no_collision(db_session):
    """Two same-ability slots match distinct casts (nearest-unused greedy)."""
    fid = _seed_fight(db_session, code="TDIFF_MC1", encounter_id=8010)
    _add_combatants(db_session, fid, [1])
    for i in range(40):
        _add_player_damage_to_boss(db_session, fid, ts=1000 + i * 1000)
    _add_boss_cast(db_session, fid, ts=11_000, aid=5555)
    _add_boss_cast(db_session, fid, ts=21_000, aid=5555)
    _add_fight_model_row(db_session, 8010, phase=0, seq=0,
                          ability_game_id=5555, type_label="raidwide")
    db_session.flush()

    tl = _timeline(
        (0, "P1", 10.0, "Recurring 1", [5555]),
        (0, "P1", 20.0, "Recurring 2", [5555]),
    )
    out = timeline_diff_for_fight(db_session, fid, _timeline=tl)
    entries = out["phases"][0]["entries"]
    assert entries[0]["actual_t_ms"] == 10_000
    assert entries[1]["actual_t_ms"] == 20_000
    assert entries[0]["actual_t_ms"] != entries[1]["actual_t_ms"]


def test_multi_cast_extra_slots_marked_missing(db_session):
    """More cactbot slots than casts -> extra slots are missing."""
    fid = _seed_fight(db_session, code="TDIFF_MC2", encounter_id=8011)
    _add_combatants(db_session, fid, [1])
    for i in range(40):
        _add_player_damage_to_boss(db_session, fid, ts=1000 + i * 1000)
    _add_boss_cast(db_session, fid, ts=11_000, aid=5556)
    _add_fight_model_row(db_session, 8011, phase=0, seq=0,
                          ability_game_id=5556, type_label="raidwide")
    db_session.flush()

    tl = _timeline(
        (0, "P1", 10.0, "Akh Morn 1", [5556]),
        (0, "P1", 30.0, "Akh Morn 2", [5556]),
    )
    out = timeline_diff_for_fight(db_session, fid, _timeline=tl)
    entries = out["phases"][0]["entries"]
    # Slot 1 (expected 10s) wins the cast, slot 2 (expected 30s) misses
    assert entries[0]["fired"] is True
    assert entries[0]["actual_t_ms"] == 10_000
    assert entries[1]["fired"] is False


def test_multi_id_slot_variant_a_fires(db_session):
    """Cactbot slot `id: [X, Y]`: variant X fires, slot still matches."""
    fid = _seed_fight(db_session, code="TDIFF_VID_A", encounter_id=8020)
    _add_combatants(db_session, fid, [1])
    for i in range(40):
        _add_player_damage_to_boss(db_session, fid, ts=1000 + i * 1000)
    # Only X (9CD3) fires; Y (9CD5) does not
    _add_boss_cast(db_session, fid, ts=11_000, aid=0x9CD3)
    # fight_model has only the X row (consensus didn't pick Y up)
    _add_fight_model_row(db_session, 8020, phase=0, seq=0,
                          ability_game_id=0x9CD3, type_label="raidwide")
    db_session.flush()

    tl = _timeline((0, "P1", 10.0, "Sinsmoke/Sinsmite", [0x9CD3, 0x9CD5]))
    out = timeline_diff_for_fight(db_session, fid, _timeline=tl)
    e = out["phases"][0]["entries"][0]
    assert e["fired"] is True
    assert e["cactbot_label"] == "Sinsmoke/Sinsmite"
    assert e["ability_game_id"] == 0x9CD3  # the variant that fired
    assert e["drift_ms"] == 0  # 11_000 - 1_000 - 10_000 = 0


def test_multi_id_slot_variant_b_fires(db_session):
    """Cactbot slot `id: [X, Y]`: variant Y fires, slot still matches.

    The fix-this-ship case: with the old row-driven matching, the row for
    9CD3 would hunt around because no cast of 9CD3 exists in the pull.
    Slot-driven correctly sees the 9CD5 cast as filling the slot.
    """
    fid = _seed_fight(db_session, code="TDIFF_VID_B", encounter_id=8021)
    _add_combatants(db_session, fid, [1])
    for i in range(40):
        _add_player_damage_to_boss(db_session, fid, ts=1000 + i * 1000)
    # Only Y (9CD5) fires; X (9CD3) does not
    _add_boss_cast(db_session, fid, ts=11_000, aid=0x9CD5)
    # fight_model still has a row for X (consensus across many pulls)
    _add_fight_model_row(db_session, 8021, phase=0, seq=0,
                          ability_game_id=0x9CD3, type_label="raidwide")
    db_session.flush()

    tl = _timeline((0, "P1", 10.0, "Sinsmoke/Sinsmite", [0x9CD3, 0x9CD5]))
    out = timeline_diff_for_fight(db_session, fid, _timeline=tl)
    e = out["phases"][0]["entries"][0]
    assert e["fired"] is True
    assert e["cactbot_label"] == "Sinsmoke/Sinsmite"
    assert e["ability_game_id"] == 0x9CD5  # the variant that actually fired
    assert e["drift_ms"] == 0


def test_multi_id_slot_two_slots_two_variants(db_session):
    """Two slots, each accepting [X, Y]. In this pull, X fires first and Y fires
    second. Slot 1 -> X, slot 2 -> Y, no collision."""
    fid = _seed_fight(db_session, code="TDIFF_VID_C", encounter_id=8022)
    _add_combatants(db_session, fid, [1])
    # Spread damage events out to ts=80_000 so both casts land in the phase
    for i in range(80):
        _add_player_damage_to_boss(db_session, fid, ts=1000 + i * 1000)
    _add_boss_cast(db_session, fid, ts=11_000, aid=0x9CD3)
    _add_boss_cast(db_session, fid, ts=51_000, aid=0x9CD5)
    _add_fight_model_row(db_session, 8022, phase=0, seq=0,
                          ability_game_id=0x9CD3, type_label="raidwide")
    db_session.flush()

    tl = _timeline(
        (0, "P1", 10.0, "Sinsmoke/Sinsmite (1)", [0x9CD3, 0x9CD5]),
        (0, "P1", 50.0, "Sinsmoke/Sinsmite (2)", [0x9CD3, 0x9CD5]),
    )
    out = timeline_diff_for_fight(db_session, fid, _timeline=tl)
    entries = out["phases"][0]["entries"]
    assert entries[0]["ability_game_id"] == 0x9CD3
    assert entries[0]["drift_ms"] == 0
    assert entries[1]["ability_game_id"] == 0x9CD5
    assert entries[1]["drift_ms"] == 0


def test_variant_collapsing_shared_label(db_session):
    """Two slots with same label base (parens stripped) at close times — only
    one fires per pull. The non-firing one is marked alternate_variant, not missing."""
    fid = _seed_fight(db_session, code="TDIFF_VAR_A", encounter_id=8030)
    _add_combatants(db_session, fid, [1])
    for i in range(40):
        _add_player_damage_to_boss(db_session, fid, ts=1000 + i * 1000)
    # Only the fire variant fires this pull
    _add_boss_cast(db_session, fid, ts=11_000, aid=0xB5C0)
    _add_fight_model_row(db_session, 8030, phase=0, seq=0,
                          ability_game_id=0xB5C0, type_label="raidwide")
    _add_fight_model_row(db_session, 8030, phase=0, seq=1,
                          ability_game_id=0xB5C1, type_label="raidwide")
    db_session.flush()

    tl = _timeline(
        (0, "P1", 10.0, "Alley-oop Inferno (fire)", [0xB5C0]),
        (0, "P1", 10.0, "Alley-oop Inferno (lightning)", [0xB5C1]),
    )
    out = timeline_diff_for_fight(db_session, fid, _timeline=tl)
    p0 = out["phases"][0]
    assert p0["entries_fired"] == 1
    assert p0["entries_missing"] == 0  # not counted as a miss
    assert p0["entries_alternate"] == 1
    # The non-firing slot is the one marked alternate
    fired = [e for e in p0["entries"] if e["fired"]][0]
    alt = [e for e in p0["entries"] if e["alternate_variant"]][0]
    assert fired["cactbot_label"] == "Alley-oop Inferno (fire)"
    assert alt["cactbot_label"] == "Alley-oop Inferno (lightning)"
    assert alt["fired"] is False


def test_variant_collapsing_shared_ability_id(db_session):
    """Two slots sharing an ability ID in their sets — if one fires, the other
    is a variant, not a miss."""
    fid = _seed_fight(db_session, code="TDIFF_VAR_B", encounter_id=8031)
    _add_combatants(db_session, fid, [1])
    for i in range(40):
        _add_player_damage_to_boss(db_session, fid, ts=1000 + i * 1000)
    _add_boss_cast(db_session, fid, ts=11_000, aid=4001)
    _add_fight_model_row(db_session, 8031, phase=0, seq=0,
                          ability_game_id=4001, type_label="raidwide")
    db_session.flush()

    # Two slots; both list ability 4001 along with their own alt
    tl = _timeline(
        (0, "P1", 10.0, "Variant A", [4001, 4002]),
        (0, "P1", 10.5, "Variant B", [4001, 4003]),
    )
    out = timeline_diff_for_fight(db_session, fid, _timeline=tl)
    p0 = out["phases"][0]
    # 1 fired (whichever wins the cast), 1 alternate (the other)
    assert p0["entries_fired"] == 1
    assert p0["entries_missing"] == 0
    assert p0["entries_alternate"] == 1


def test_variant_collapsing_both_miss_no_variant_mark(db_session):
    """If no sibling fired, both stay marked as plain missing, not variants."""
    fid = _seed_fight(db_session, code="TDIFF_VAR_C", encounter_id=8032)
    _add_combatants(db_session, fid, [1])
    for i in range(40):
        _add_player_damage_to_boss(db_session, fid, ts=1000 + i * 1000)
    # Neither variant fires
    _add_fight_model_row(db_session, 8032, phase=0, seq=0,
                          ability_game_id=0xC001, type_label="raidwide")
    _add_fight_model_row(db_session, 8032, phase=0, seq=1,
                          ability_game_id=0xC002, type_label="raidwide")
    db_session.flush()

    tl = _timeline(
        (0, "P1", 10.0, "Wishful Thing (a)", [0xC001]),
        (0, "P1", 10.0, "Wishful Thing (b)", [0xC002]),
    )
    out = timeline_diff_for_fight(db_session, fid, _timeline=tl)
    p0 = out["phases"][0]
    assert p0["entries_fired"] == 0
    assert p0["entries_missing"] == 2  # both genuinely missing
    assert p0["entries_alternate"] == 0


def test_variant_collapsing_different_labels_not_collapsed(db_session):
    """Distinct-label, distinct-ID slots near in time are sequential mechanics,
    not variants. A non-firing slot stays as a true miss."""
    fid = _seed_fight(db_session, code="TDIFF_VAR_D", encounter_id=8033)
    _add_combatants(db_session, fid, [1])
    for i in range(40):
        _add_player_damage_to_boss(db_session, fid, ts=1000 + i * 1000)
    _add_boss_cast(db_session, fid, ts=11_000, aid=0xD001)
    # only D001 fires; D002 doesn't
    _add_fight_model_row(db_session, 8033, phase=0, seq=0,
                          ability_game_id=0xD001, type_label="raidwide")
    _add_fight_model_row(db_session, 8033, phase=0, seq=1,
                          ability_game_id=0xD002, type_label="raidwide")
    db_session.flush()

    tl = _timeline(
        (0, "P1", 10.0, "Spirit Taker", [0xD001]),
        (0, "P1", 10.5, "Holy Bladedance", [0xD002]),
    )
    out = timeline_diff_for_fight(db_session, fid, _timeline=tl)
    p0 = out["phases"][0]
    assert p0["entries_fired"] == 1
    # Different labels + different IDs -> not a variant, count as a real miss
    assert p0["entries_missing"] == 1
    assert p0["entries_alternate"] == 0


def test_phase_alignment_one_cactbot_two_fight_phases(db_session):
    """M9S-style case: T-103 detects 2 phases, cactbot has only 1. Both fight
    phases should still pull mechanic labels + drift from the single cactbot
    phase rather than fight phase 1 silently dropping all matches."""
    fid = _seed_fight(db_session, code="TDIFF_PHASE_1", encounter_id=8040)
    _add_combatants(db_session, fid, [1])
    # Two damage windows separated by a gap → T-103 detects 2 phases
    for i in range(40):  # phase 0: ts 1000..40000
        _add_player_damage_to_boss(db_session, fid, ts=1000 + i * 1000,
                                   target_boss=999)
    for i in range(40):  # phase 1: ts 60000..99000 (different target)
        _add_player_damage_to_boss(db_session, fid, ts=60000 + i * 1000,
                                   target_boss=998)
    # Boss casts in each detected phase
    _add_boss_cast(db_session, fid, ts=11_000, aid=6001)
    _add_boss_cast(db_session, fid, ts=71_000, aid=6002)
    _add_fight_model_row(db_session, 8040, phase=0, seq=0,
                          ability_game_id=6001, type_label="raidwide")
    _add_fight_model_row(db_session, 8040, phase=0, seq=1,
                          ability_game_id=6002, type_label="raidwide")
    db_session.flush()

    # Cactbot has only one phase but both mechanics in it
    tl = _timeline(
        (0, "P1", 10.0, "Killer Voice", [6001]),
        (0, "P1", 70.0, "Hardcore", [6002]),
    )
    out = timeline_diff_for_fight(db_session, fid, _timeline=tl)
    # Both fight phases should have a fired entry
    assert len(out["phases"]) == 2
    assert out["phases"][0]["entries_fired"] == 1
    assert out["phases"][1]["entries_fired"] == 1
    # The fight-phase-1 entry should carry its cactbot label
    labels = [e["cactbot_label"] for p in out["phases"] for e in p["entries"]]
    assert "Killer Voice" in labels
    assert "Hardcore" in labels


def test_phase_alignment_helper_identity_when_counts_match():
    """1:1 mapping when both sides have the same phase count (M9S/M10S/etc.)."""
    from analysis.timeline_diff import _align_phases
    assert _align_phases(3, 3) == {0: 0, 1: 1, 2: 2}
    assert _align_phases(1, 1) == {0: 0}


def test_phase_alignment_helper_one_cactbot_many_fight():
    """Multiple fight phases all map to cactbot phase 0."""
    from analysis.timeline_diff import _align_phases
    assert _align_phases(3, 1) == {0: 0, 1: 0, 2: 0}


def test_phase_alignment_helper_proportional():
    """Proportional interpolation when both counts differ but >1."""
    from analysis.timeline_diff import _align_phases
    # 5 fight phases, 3 cactbot phases:
    # f=0 → 0, f=1 → round(0.5) (banker's: 0), f=2 → 1, f=3 → round(1.5) (banker's: 2), f=4 → 2
    m = _align_phases(5, 3)
    # Anchor first/last and monotonic non-decreasing
    assert m[0] == 0
    assert m[4] == 2
    assert all(m[i] <= m[i + 1] for i in range(4))


def test_median_drift_per_phase(db_session):
    fid = _seed_fight(db_session, code="TDIFF_G", encounter_id=8007)
    _add_combatants(db_session, fid, [1])
    for i in range(40):
        _add_player_damage_to_boss(db_session, fid, ts=1000 + i * 1000)
    _add_boss_cast(db_session, fid, ts=5_200, aid=4242)   # expected 4_000 → drift 200 (after phase_start=1000: actual 4_200, drift 200)
    _add_boss_cast(db_session, fid, ts=15_500, aid=4243)
    _add_boss_cast(db_session, fid, ts=25_800, aid=4244)
    for seq, aid in enumerate((4242, 4243, 4244)):
        _add_fight_model_row(db_session, 8007, phase=0, seq=seq,
                              ability_game_id=aid, type_label="raidwide")
    db_session.flush()

    tl = _timeline(
        (0, "P1", 4.0, "A", [4242]),
        (0, "P1", 14.0, "B", [4243]),
        (0, "P1", 24.0, "C", [4244]),
    )
    out = timeline_diff_for_fight(db_session, fid, _timeline=tl)
    # Drifts: A 200, B 500, C 800 -> median 500
    assert out["phases"][0]["median_drift_ms"] == 500
