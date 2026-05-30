"""Minimum-cost rectangular assignment (Hungarian / Jonker-Volgenant).

Used by `analysis/timeline_diff.py` to match cactbot timeline slots to boss
cast events optimally instead of with the per-slot nearest-unused greedy that
shipped in v1.4.1. Greedy is optimal for monotonic same-ability sequences
(the typical case) but can mis-assign when several slots have overlapping
multi-ID candidate pools and a closer-in-time slot eats a cast that some
later slot needed.

This module is self-contained — no external deps. Single O(n³) call site so
performance is fine for the realistic per-phase sizes (≤50 slots, ≤50 casts).

Algorithm: O(n³) Jonker-Volgenant on a square matrix. For rectangular inputs
we pad with dummy rows/cols charged at the skip penalty. Forbidden pairs
(cast's ability not in slot's id set) also use the skip penalty, so they're
chosen only if every other assignment for that slot is worse.
"""
from __future__ import annotations

from math import inf


def min_cost_assignment(
    cost: list[list[float]],
    *,
    skip_penalty: float,
) -> list[int]:
    """Solve a rectangular minimum-cost assignment problem.

    Each row may be assigned to at most one column, each column to at most
    one row. A row is "unassigned" (`-1` in the result) when its best
    available column has cost >= `skip_penalty`.

    Args:
        cost: `n_rows x n_cols` matrix of non-negative costs. Forbidden
            (row, col) pairs should have cost = `skip_penalty` (or higher).
        skip_penalty: per-row penalty used to threshold what counts as a
            real match. Should exceed every legitimate match cost in the
            input. The solver itself treats this as a regular cost; the
            threshold is applied only when reading out the result.

    Returns:
        `result[r]` = column assigned to row r, or -1 if unassigned.
        Length = `n_rows`.
    """
    n_rows = len(cost)
    if n_rows == 0:
        return []
    n_cols = len(cost[0])
    if n_cols == 0:
        return [-1] * n_rows

    # Pad to square by adding dummy rows/cols charged at skip_penalty.
    n = max(n_rows, n_cols)
    padded = [[skip_penalty] * n for _ in range(n)]
    for r in range(n_rows):
        for c in range(n_cols):
            padded[r][c] = cost[r][c]

    # JV-style O(n^3) solver on square matrix.
    # Adapted from the standard textbook formulation; 1-indexed internally.
    u = [0.0] * (n + 1)
    v = [0.0] * (n + 1)
    p = [0] * (n + 1)     # p[j] = row currently assigned to column j
    way = [0] * (n + 1)

    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [inf] * (n + 1)
        used = [False] * (n + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = inf
            j1 = 0
            for j in range(1, n + 1):
                if used[j]:
                    continue
                cur = padded[i0 - 1][j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j] = cur
                    way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            for j in range(n + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0 != 0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1

    # Read off: row -> column. Drop dummies and threshold by skip_penalty.
    row_to_col = [-1] * n_rows
    for j in range(1, n + 1):
        r = p[j] - 1
        c = j - 1
        if r < n_rows and c < n_cols and cost[r][c] < skip_penalty:
            row_to_col[r] = c
    return row_to_col
