"""Tests for the cactbot timeline parser + fight_model annotator."""
from __future__ import annotations

from pathlib import Path

import pytest

from db.models import FightModel
from ingest import cactbot
from ingest.cactbot import (
    CactbotEntry,
    _best_match,
    _is_phase_marker,
    _parse_hex_ids,
    annotate_fight_model_for_encounter,
    load_timeline_for_encounter,
    parse_timeline_file,
)


def test_parse_hex_ids_single():
    assert _parse_hex_ids('"B375"') == [int("B375", 16)]


def test_parse_hex_ids_array():
    out = _parse_hex_ids('["B377", "B378", "B37D"]')
    assert out == [int("B377", 16), int("B378", 16), int("B37D", 16)]


def test_parse_hex_ids_skips_invalid():
    assert _parse_hex_ids('["B377", "XYZ", "B378"]') == [
        int("B377", 16), int("B378", 16),
    ]


@pytest.mark.parametrize("line, want_is, want_label", [
    ("# Phase Two", True, "P2"),
    ("# Phase Three", True, "P3"),
    ("# Phase 5", True, "P5"),
    ("# Adds Phase", True, "Adds"),
    ("# P2: setup", True, "P2"),
    ("# Just a comment", False, None),
    ("# Coffinmaker", False, None),
])
def test_phase_marker_detection(line, want_is, want_label):
    is_marker, label = _is_phase_marker(line)
    assert is_marker == want_is
    assert label == want_label


def test_parse_timeline_extracts_expected_shape(tmp_path: Path):
    sample = """
# AAC HEAVYWEIGHT (M1) (SAVAGE)
hideall "--sync--"
0.0 "--sync--" InCombat { inGameCombat: "1" } window 0,1
5.0 "Killer Voice" Ability { id: "B384", source: "Vamp Fatale" }
10.5 "Half Moon" Ability { id: ["B377", "B379", "B37B", "B37D"], source: "Vamp Fatale" }
15.0 "Brutal Rain" #Ability { id: "B383", source: "Vamp Fatale" }
# Phase Two
40.0 "Tankbuster" Ability { id: "B380", source: "Usurper" }
"""
    p = tmp_path / "sample.txt"
    p.write_text(sample, encoding="utf-8")
    timeline = parse_timeline_file(p)
    labels = [e.label for e in timeline.entries]
    assert "Killer Voice" in labels
    assert "Half Moon" in labels
    assert "Tankbuster" in labels
    # commented `#Ability` line is NOT extracted
    assert "Brutal Rain" not in labels
    # marker lines (`--sync--`) skipped
    assert all(not e.label.startswith("--") for e in timeline.entries)
    # Phase index increments on `# Phase Two`
    phase_of = {e.label: e.phase_index for e in timeline.entries}
    assert phase_of["Killer Voice"] == 0
    assert phase_of["Tankbuster"] == 1


def test_parse_timeline_computes_phase_relative_time(tmp_path: Path):
    sample = """
0.0 "First" Ability { id: "A001", source: "Boss" }
30.0 "Second" Ability { id: "A002", source: "Boss" }
# Phase Two
100.0 "Third" Ability { id: "A003", source: "Boss" }
130.0 "Fourth" Ability { id: "A004", source: "Boss" }
"""
    p = tmp_path / "sample.txt"
    p.write_text(sample, encoding="utf-8")
    timeline = parse_timeline_file(p)
    by_label = {e.label: e for e in timeline.entries}
    assert by_label["First"].phase_relative_t_s == 0.0
    assert by_label["Second"].phase_relative_t_s == 30.0
    # Third starts a new phase -> phase-relative t = 0
    assert by_label["Third"].phase_relative_t_s == 0.0
    assert by_label["Fourth"].phase_relative_t_s == 30.0


def test_best_match_prefers_same_phase():
    e_p0_close = CactbotEntry(abs_time_s=0, label="P0 ability", ability_ids=[1], phase_index=0, phase_label="P1", phase_relative_t_s=5.0)
    e_p1_closer = CactbotEntry(abs_time_s=0, label="P1 ability", ability_ids=[1], phase_index=1, phase_label="P2", phase_relative_t_s=4.0)
    best = _best_match([e_p0_close, e_p1_closer], fight_model_phase=0, fight_model_rel_t_ms=4500)
    # Same phase preferred even though P1 entry is closer numerically
    assert best.label == "P0 ability"


def test_best_match_falls_back_across_phases_when_no_same_phase():
    e1 = CactbotEntry(abs_time_s=0, label="P1 ability", ability_ids=[1], phase_index=1, phase_label="P2", phase_relative_t_s=10.0)
    e2 = CactbotEntry(abs_time_s=0, label="P2 ability", ability_ids=[1], phase_index=2, phase_label="Adds", phase_relative_t_s=4.0)
    best = _best_match([e1, e2], fight_model_phase=0, fight_model_rel_t_ms=5000)
    # No same-phase candidates → picks closest by time across all
    assert best.label == "P2 ability"


def test_load_timeline_returns_none_for_unmapped_encounter():
    assert load_timeline_for_encounter(99999) is None


def test_load_timeline_loads_real_vendored_file():
    # Live vendored file. r9s.txt is included in vendor/cactbot/.
    timeline = load_timeline_for_encounter(101)
    assert timeline is not None
    assert timeline.encounter_file == "r9s.txt"
    assert len(timeline.entries) > 0
    # Sanity: "Killer Voice" + "Vamp Stomp" exist in r9s
    labels = {e.label for e in timeline.entries}
    assert "Killer Voice" in labels
    assert "Vamp Stomp" in labels
    # Each entry has at least one ability ID
    for e in timeline.entries:
        assert len(e.ability_ids) >= 1


def test_load_timeline_fru_has_multiple_phases():
    timeline = load_timeline_for_encounter(1079)
    assert timeline is not None
    phases = {e.phase_index for e in timeline.entries}
    # FRU has at least 4 phases (P1, P2, Adds, P3, P4, P5 in cactbot)
    assert len(phases) >= 4


def test_fallback_names_parsed_from_comment_block(tmp_path: Path):
    sample = """
0.0 "Killer Voice" Ability { id: "B384", source: "Boss" }

# IGNORED ABILITIES
# B333 Sadistic Screech: VFX
# B35A Hardcore: VFX, tankbuster
# B383 Brutal Rain
"""
    p = tmp_path / "sample.txt"
    p.write_text(sample, encoding="utf-8")
    timeline = parse_timeline_file(p)
    # Three entries from the comment block, names without trailing context
    assert timeline.fallback_names[int("B333", 16)].startswith("Sadistic Screech")
    assert timeline.fallback_names[int("B35A", 16)].startswith("Hardcore")
    assert timeline.fallback_names[int("B383", 16)] == "Brutal Rain"


def test_commented_ability_lines_become_fallback_names(tmp_path: Path):
    """`<time> "<label>" #Ability { id: "<HEX>" }` lines are sub-cast docs.
    Their (id, label) pairs feed `fallback_names` so we can label the ability."""
    sample = """
0.0 "Cosmo Memory" Ability { id: "7BA1", source: "Alpha Omega" }
1.5 "Wave Cannon Puddle 1" #Ability { id: "7BAF", source: "Alpha Omega" }
2.0 "Cosmo Dive Far" #Ability { id: "7BA8", source: "Alpha Omega" }
"""
    p = tmp_path / "sample.txt"
    p.write_text(sample, encoding="utf-8")
    timeline = parse_timeline_file(p)
    # Body entry stays in `entries` list
    assert any(e.label == "Cosmo Memory" for e in timeline.entries)
    # Commented entries are NOT in `entries` (they don't have firm timing)
    assert not any(e.label == "Wave Cannon Puddle 1" for e in timeline.entries)
    # but their labels show up in fallback_names
    assert timeline.fallback_names[int("7BAF", 16)] == "Wave Cannon Puddle 1"
    assert timeline.fallback_names[int("7BA8", 16)] == "Cosmo Dive Far"


def test_real_fru_comment_block_picks_up_hiemal_ray():
    """FRU's Adds-phase abilities (e.g. 9D41 Hiemal Ray) live only in the
    comment block, not the active timeline. Fix 2 picks them up."""
    timeline = load_timeline_for_encounter(1079)
    assert timeline is not None
    # 9D41 = 40257 = Hiemal Ray (commented only)
    assert 40257 in timeline.fallback_names
    assert "hiemal ray" in timeline.fallback_names[40257].lower()


def test_annotate_uses_fallback_name_when_no_body_entry(db_session):
    """Ability 9D41 (40257) is in FRU's comment block only. Annotator should
    use the fallback name and leave expected_t_ms as None."""
    from db.models import FightModel
    test_version = 98
    db_session.add(FightModel(
        encounter_id=1079, version=test_version, phase=2, seq=0,
        ability_game_id=40257, relative_t_ms=15_000,
        type_label="raidwide",
    ))
    db_session.flush()
    result = annotate_fight_model_for_encounter(db_session, 1079, version=test_version)
    db_session.flush()
    assert result["annotated_fallback"] >= 1
    row = db_session.query(FightModel).filter(
        FightModel.encounter_id == 1079, FightModel.version == test_version,
    ).first()
    assert row.cactbot_label is not None
    assert "hiemal ray" in row.cactbot_label.lower()
    # No expected time from a fallback-only annotation
    assert row.cactbot_expected_t_ms is None
    # Cleanup
    db_session.query(FightModel).filter(
        FightModel.encounter_id == 1079, FightModel.version == test_version,
    ).delete()
    db_session.flush()


def test_annotate_fight_model_persists_labels(db_session):
    """End-to-end: insert a few fight_model rows under a test-only version
    against an encounter we have a timeline for (M9S = 101), run the
    annotator, verify labels persist. Uses version=99 so it doesn't collide
    with the dev DB's real persisted consensus at version=1."""
    test_version = 99
    # Pick a real M9S ability: B384 = "Killer Voice" (phase 0, t=5.0 in r9s.txt)
    db_session.add(FightModel(
        encounter_id=101, version=test_version, phase=0, seq=0,
        ability_game_id=int("B384", 16), relative_t_ms=10_400,
        type_label="raidwide",
    ))
    # B374 = "Vamp Stomp" (phase 0, t=30.6 in r9s.txt)
    db_session.add(FightModel(
        encounter_id=101, version=test_version, phase=0, seq=1,
        ability_game_id=int("B374", 16), relative_t_ms=30_600,
        type_label="aoe_party",
    ))
    # A bogus ability that's not in r9s.txt
    db_session.add(FightModel(
        encounter_id=101, version=test_version, phase=0, seq=2,
        ability_game_id=999999, relative_t_ms=99_999,
    ))
    db_session.flush()

    result = annotate_fight_model_for_encounter(db_session, 101, version=test_version)
    db_session.flush()

    assert result["annotated"] == 2
    assert result["missed_no_match"] == 1

    rows = {r.seq: r for r in db_session.query(FightModel).filter(
        FightModel.encounter_id == 101, FightModel.version == test_version,
    ).all()}
    assert rows[0].cactbot_label == "Killer Voice"
    assert rows[1].cactbot_label == "Vamp Stomp"
    assert rows[2].cactbot_label is None  # bogus ability stayed unannotated
    # Phase labels populated
    assert rows[0].cactbot_phase_label == "P1"
    # Expected times match cactbot's r9s.txt (phase 0 anchored at abs t=0)
    assert rows[0].cactbot_expected_t_ms == 10400  # "10.4 Killer Voice"
    assert rows[1].cactbot_expected_t_ms == 30600  # "30.6 Vamp Stomp"

    # Cleanup
    db_session.query(FightModel).filter(
        FightModel.encounter_id == 101, FightModel.version == test_version,
    ).delete()
    db_session.flush()
