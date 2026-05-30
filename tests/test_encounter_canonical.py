"""Unit tests for analysis/_encounter.py canonical-encounter helpers (v1.17.0)."""
from __future__ import annotations

from analysis._encounter import (
    all_cloned_groups,
    canonical_encounter_id,
    encounter_id_group,
    is_cloned,
)


def test_canonical_of_canonical_is_itself():
    assert canonical_encounter_id(1076) == 1076


def test_canonical_of_legacy_returns_current():
    assert canonical_encounter_id(1065) == 1076


def test_canonical_of_non_cloned_is_identity():
    # FRU, TOP, current Savage tier — not cloned
    assert canonical_encounter_id(1079) == 1079
    assert canonical_encounter_id(1068) == 1068
    assert canonical_encounter_id(101) == 101


def test_group_of_cloned_returns_full_sorted_group():
    assert encounter_id_group(1065) == (1065, 1076)
    assert encounter_id_group(1076) == (1065, 1076)


def test_group_of_non_cloned_returns_single_tuple():
    assert encounter_id_group(1079) == (1079,)
    assert encounter_id_group(42) == (42,)


def test_is_cloned():
    assert is_cloned(1065) is True
    assert is_cloned(1076) is True
    assert is_cloned(1079) is False
    assert is_cloned(42) is False


def test_all_cloned_groups_is_sorted_tuples():
    groups = all_cloned_groups()
    assert (1065, 1076) in groups
    # Each group is sorted ascending (predictable diff in migration scripts)
    for grp in groups:
        assert list(grp) == sorted(grp)
