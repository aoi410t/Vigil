"""Canonical encounter unification (v1.17.0).

FFLogs occasionally cuts the same logical encounter into a new
`encounter_id` after a re-cut — DSR has both 1065 (legacy) and 1076
(current). For analytics we want them treated as **one entity**: a kill
under either ID feeds the same fight_model; a strat_config authored
once applies to fights from either ID.

Two helpers:

  - `canonical_encounter_id(eid)` — returns the canonical ID for any
    member of a cloned group (1065 → 1076), or `eid` unchanged for
    non-cloned encounters.
  - `encounter_id_group(eid)` — returns the sorted tuple of every ID in
    the group (e.g. `(1065, 1076)`), or `(eid,)` for non-cloned.

Usage pattern across the analysis layer:
  - When filtering `Fight.encounter_id`: use `.in_(encounter_id_group(eid))`
    so both halves of a cloned group are unioned at read time.
  - When reading/writing `FightModel`: use `canonical_encounter_id(eid)` —
    we only store one consensus model per canonical group.
  - Same for `StratConfig` — user-curated strats live at the canonical ID.

Adding a new cloned group: append to `_CLONED_GROUPS` below. The first
element is the canonical (current) ID; subsequent elements are legacy
aliases. `_CANONICAL_OF` and `_GROUP_OF` are built at import time.
"""
from __future__ import annotations

# Each tuple lists the canonical ID FIRST, then legacy aliases.
# Append a new tuple when FFLogs re-cuts an existing encounter.
_CLONED_GROUPS: tuple[tuple[int, ...], ...] = (
    # DSR: 1076 is current, 1065 is legacy (pre-re-cut).
    (1076, 1065),
)


def _build_lookups() -> tuple[dict[int, int], dict[int, tuple[int, ...]]]:
    canonical_of: dict[int, int] = {}
    group_of: dict[int, tuple[int, ...]] = {}
    for grp in _CLONED_GROUPS:
        canonical = grp[0]
        full = tuple(sorted(grp))
        for eid in grp:
            canonical_of[eid] = canonical
            group_of[eid] = full
    return canonical_of, group_of


_CANONICAL_OF, _GROUP_OF = _build_lookups()


def canonical_encounter_id(eid: int) -> int:
    """Return the canonical encounter_id for a cloned group, else `eid`."""
    return _CANONICAL_OF.get(eid, eid)


def encounter_id_group(eid: int) -> tuple[int, ...]:
    """Return the sorted tuple of every encounter_id in the cloned group,
    or `(eid,)` for non-cloned encounters."""
    return _GROUP_OF.get(eid, (eid,))


def is_cloned(eid: int) -> bool:
    """True iff `eid` belongs to any cloned group (has at least one alias)."""
    return eid in _CANONICAL_OF


def all_cloned_groups() -> tuple[tuple[int, ...], ...]:
    """Every cloned group as defined. Useful for migration scripts + tests."""
    return tuple(tuple(sorted(grp)) for grp in _CLONED_GROUPS)
