"""T-108 rule-based classifier tests with realistic XIVAPI description text."""
from __future__ import annotations

from analysis.ability_classifier import (
    AUTO_HIGH_THRESHOLD,
    classify,
    clean_description,
)


def test_clean_strips_html_and_templates():
    s = ('Damage taken is reduced<If(GreaterThanOrEqualTo(...))> and HP recovery '
         'is increased<Else/></If>. <span style="color:#00cc22;">Duration: </span>15s')
    out = clean_description(s)
    assert "<" not in out and ">" not in out
    assert "damage taken is reduced" in out


def test_damage_down_by_name():
    assert classify("Damage Down", "anything")[0] == "damage_down"


def test_rampart_mit_self():
    desc = "Damage taken is reduced. Duration: 20s"
    label, conf = classify("Rampart", desc)
    assert label == "mit_self"
    assert conf >= AUTO_HIGH_THRESHOLD


def test_shake_it_off_mit_party():
    desc = ("Creates a barrier around self and nearby party members that reduces "
            "damage taken.")
    label, conf = classify("Shake It Off", desc)
    assert label == "mit_party"
    assert conf >= AUTO_HIGH_THRESHOLD


def test_reprisal_mit_boss_debuff():
    desc = "Reduces damage dealt by nearby enemies by 10%."
    label, conf = classify("Reprisal", desc)
    assert label == "mit_boss_debuff"
    assert conf >= AUTO_HIGH_THRESHOLD


def test_feint_mit_boss_debuff():
    desc = "Reduces target's physical damage dealt by 10% and magic damage dealt by 5%."
    label, conf = classify("Feint", desc)
    assert label == "mit_boss_debuff"


def test_divination_raid_buff():
    desc = "Increases damage dealt by self and nearby party members by 6%."
    label, conf = classify("Divination", desc)
    assert label == "raid_buff"
    assert conf >= AUTO_HIGH_THRESHOLD


def test_inner_release_personal_buff():
    desc = "Allows you to execute three weaponskills, increasing your damage."
    label, _ = classify("Inner Release", desc)
    assert label == "personal_buff"


def test_terse_status_personal_buff_promoted_for_kind_status():
    """Status descriptions like 'Damage dealt is increased.' are personal
    buffs in the overwhelming majority — we promote them at kind='status'."""
    label, conf = classify("Some Personal Status", "Damage dealt is increased.", kind="status")
    assert label == "personal_buff"
    assert conf >= AUTO_HIGH_THRESHOLD


def test_terse_action_personal_buff_stays_low_for_review():
    """Same description on kind='action' is ambiguous — leave for review."""
    label, conf = classify("Some Action", "Damage dealt is increased.", kind="action")
    assert label == "personal_buff"
    assert conf < AUTO_HIGH_THRESHOLD


def test_unknown_when_no_description():
    label, conf = classify("Mystery", "")
    assert label == "unknown"
    assert conf == 0.0


def test_player_damage_down_via_description():
    desc = "Your damage dealt is reduced."
    label, _ = classify("Strange Penalty", desc)
    assert label == "damage_down"


def test_potency_attack_ignored_at_high_confidence():
    desc = "Delivers an attack with a potency of 200."
    label, conf = classify("Heavy Swing", desc)
    assert label == "ignore"
    assert conf >= AUTO_HIGH_THRESHOLD


def test_unrecognized_ability_falls_to_ignore_low_confidence():
    desc = "Some weird obscure description with no game keywords."
    label, conf = classify("Mystery Action", desc)
    assert label == "ignore"
    assert conf < AUTO_HIGH_THRESHOLD
