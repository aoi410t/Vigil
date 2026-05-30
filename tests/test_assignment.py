"""Tests for the Hungarian/JV min-cost assignment helper used by
analysis/timeline_diff.py for multi-cast slot matching."""
from __future__ import annotations

from analysis._assignment import min_cost_assignment


SKIP = 1_000_000.0


def test_empty_matrix():
    assert min_cost_assignment([], skip_penalty=SKIP) == []


def test_zero_cols_returns_all_unassigned():
    # 3 rows, 0 cols
    out = min_cost_assignment([[], [], []], skip_penalty=SKIP)
    assert out == [-1, -1, -1]


def test_trivial_square_unique_assignment():
    # 2x2, diagonal is cheapest
    cost = [
        [1.0, 10.0],
        [10.0, 1.0],
    ]
    assert min_cost_assignment(cost, skip_penalty=SKIP) == [0, 1]


def test_swap_preferred_when_globally_optimal():
    # Greedy nearest would pair row 0 -> col 0 (cost 0), forcing row 1 -> col 1
    # (cost 100). Optimal swap: row 0 -> col 1 (cost 5), row 1 -> col 0 (cost 5).
    # Greedy total = 100, optimal total = 10. Hungarian must pick the swap.
    cost = [
        [0.0, 5.0],
        [5.0, 100.0],
    ]
    out = min_cost_assignment(cost, skip_penalty=SKIP)
    # Either assignment is valid in terms of "every row matched"; check total
    total = sum(cost[r][out[r]] for r in range(len(cost)) if out[r] >= 0)
    assert total == 10.0  # optimal


def test_rectangular_more_rows_than_cols():
    # 3 rows, 2 cols. One row must go unassigned (cost = SKIP for its dummy).
    cost = [
        [1.0, 100.0],
        [100.0, 1.0],
        [50.0, 50.0],
    ]
    out = min_cost_assignment(cost, skip_penalty=SKIP)
    # Two rows match real cols (sum = 2); third row should be unassigned.
    assigned = [out[r] for r in range(3) if out[r] >= 0]
    unassigned = [r for r in range(3) if out[r] < 0]
    assert len(assigned) == 2
    assert len(unassigned) == 1
    # The unassigned should be the third row (its best real cost was 50, vs.
    # rows 0/1 at cost 1 each — total 2 + 0 unassigned = 2 < 51).
    assert unassigned == [2]


def test_rectangular_more_cols_than_rows():
    # 2 rows, 3 cols. All rows should be matched to their best col.
    cost = [
        [10.0, 1.0, 10.0],
        [10.0, 10.0, 1.0],
    ]
    out = min_cost_assignment(cost, skip_penalty=SKIP)
    assert out == [1, 2]


def test_forbidden_pair_becomes_unassigned():
    # 2 rows, 2 cols. Row 0 can match col 0 cheaply but col 1 is forbidden.
    # Row 1 can match col 1 cheaply but col 0 is forbidden.
    cost = [
        [1.0, SKIP],
        [SKIP, 1.0],
    ]
    out = min_cost_assignment(cost, skip_penalty=SKIP)
    assert out == [0, 1]


def test_all_forbidden_returns_all_unassigned():
    cost = [
        [SKIP, SKIP],
        [SKIP, SKIP],
    ]
    out = min_cost_assignment(cost, skip_penalty=SKIP)
    assert out == [-1, -1]


def test_multi_cast_realistic_pathological_case():
    """The motivating example from v1.5.4 design notes.

    Slot A (expected 10s) accepts ability IDs {X, Y}.
    Slot B (expected 20s) accepts ability ID {X} only.
    Cast X at 11s. Cast Y at 25s.

    Greedy (slot-by-slot, expected-time order) would assign A -> X@11s
    (closest), leaving B with only Y@25s — but B doesn't accept Y, so B is
    unmatched. Total cost = 1 + missing_penalty.

    Hungarian sees the global picture and assigns A -> Y@25s (cost 15), B
    -> X@11s (cost 9). Total = 24 — better than 1 + skip_penalty if
    skip_penalty > 23, which it will be in real use.
    """
    # rows = slots A, B. cols = casts X@11, Y@25.
    # cost[A][X] = |11 - 10| = 1, cost[A][Y] = |25 - 10| = 15
    # cost[B][X] = |11 - 20| = 9, cost[B][Y] = SKIP (forbidden)
    cost = [
        [1.0, 15.0],
        [9.0, SKIP],
    ]
    out = min_cost_assignment(cost, skip_penalty=SKIP)
    # Optimal: A -> Y (col 1), B -> X (col 0). Total 15 + 9 = 24.
    assert out == [1, 0]


def test_single_row_picks_best_col():
    cost = [[5.0, 1.0, 10.0]]
    assert min_cost_assignment(cost, skip_penalty=SKIP) == [1]
