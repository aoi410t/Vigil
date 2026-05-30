"""T-301 strat_config — shape, encoding helpers, and CRUD.

Per the 2026-05-24 UX decisions:
  - **mit_plan** = structured slots: list of `{ability_id, expected_role, window_offset_ms}`
  - **assignments** = role-based: dict of `{slot_name: ROLE}` where ROLE is one
    of the eight canonical FFXIV roles (MT/OT/H1/H2/D1/D2/D3/D4)
  - **mechanic_ref** = compound `"{ability_game_id}_{occurrence_idx}"` so
    recurring mechanics (Akh Morn x4) can carry distinct mit plans per cast.

The schema is `strat_config(encounter_id, mechanic_ref, assignments JSONB,
mit_plan JSONB)` — already present from T-003. This module just defines the
JSON shape, validates inputs, and exposes CRUD helpers consumed by the API.
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from analysis._encounter import canonical_encounter_id
from db.models import StratConfig

ROLES = ("MT", "OT", "H1", "H2", "D1", "D2", "D3", "D4")
ROLE_SET = set(ROLES)

_MECHANIC_REF_RE = re.compile(r"^(\d+)_(\d+)$")


def encode_mechanic_ref(ability_game_id: int, occurrence: int = 0) -> str:
    """`(16552, 0) → '16552_0'`. Occurrence 0 is the first cast in the phase,
    1 is the second, etc. — matches `T-104` consensus first-occurrence-wins
    semantics."""
    if ability_game_id < 0 or occurrence < 0:
        raise ValueError("ability_game_id and occurrence must be non-negative")
    return f"{ability_game_id}_{occurrence}"


def decode_mechanic_ref(ref: str) -> tuple[int, int]:
    """Inverse of `encode_mechanic_ref`. Raises ValueError on malformed."""
    m = _MECHANIC_REF_RE.match(ref)
    if m is None:
        raise ValueError(f"invalid mechanic_ref: {ref!r}")
    return int(m.group(1)), int(m.group(2))


def validate_mit_plan(plan: dict[str, Any] | None) -> dict[str, Any]:
    """Coerce a mit_plan payload to canonical shape; raise ValueError on
    structural problems. Empty/missing → `{"slots": []}`."""
    if plan is None:
        return {"slots": []}
    if not isinstance(plan, dict):
        raise ValueError("mit_plan must be an object")
    slots = plan.get("slots", [])
    if not isinstance(slots, list):
        raise ValueError("mit_plan.slots must be a list")
    out: list[dict[str, Any]] = []
    for i, s in enumerate(slots):
        if not isinstance(s, dict):
            raise ValueError(f"mit_plan.slots[{i}] must be an object")
        if "ability_id" not in s:
            raise ValueError(f"mit_plan.slots[{i}] missing ability_id")
        try:
            ability_id = int(s["ability_id"])
        except (TypeError, ValueError) as e:
            raise ValueError(f"mit_plan.slots[{i}].ability_id must be int") from e
        expected_role = s.get("expected_role")
        if expected_role is not None and expected_role != "any":
            if expected_role not in ROLE_SET:
                raise ValueError(
                    f"mit_plan.slots[{i}].expected_role must be one of "
                    f"{sorted(ROLE_SET)} or 'any' or null; got {expected_role!r}"
                )
        try:
            window_offset_ms = int(s.get("window_offset_ms", 0) or 0)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"mit_plan.slots[{i}].window_offset_ms must be int") from e
        out.append({
            "ability_id": ability_id,
            "expected_role": expected_role,
            "window_offset_ms": window_offset_ms,
        })
    return {"slots": out}


def validate_assignments(assignments: dict[str, Any] | None) -> dict[str, Any]:
    """Coerce assignments to canonical shape: `{"role_map": {slot_name: ROLE}}`.

    Empty/missing → `{"role_map": {}}`. Each value must be a canonical role
    (or "any" / null for unassigned). Keys are user-defined slot names like
    "tower_north", "tether_target_1".
    """
    if assignments is None:
        return {"role_map": {}}
    if not isinstance(assignments, dict):
        raise ValueError("assignments must be an object")
    role_map = assignments.get("role_map", {})
    if not isinstance(role_map, dict):
        raise ValueError("assignments.role_map must be an object")
    out: dict[str, str | None] = {}
    for slot, role in role_map.items():
        if not isinstance(slot, str) or not slot:
            raise ValueError("slot name must be a non-empty string")
        if role is not None and role != "any" and role not in ROLE_SET:
            raise ValueError(
                f"role for slot {slot!r} must be one of {sorted(ROLE_SET)} "
                f"or 'any' or null; got {role!r}"
            )
        out[slot] = role
    return {"role_map": out}


def list_for_encounter(session: Session, encounter_id: int,
                       static_id: int) -> list[dict[str, Any]]:
    # v1.17.0: strat_config lives at the canonical encounter ID so a strat
    # authored for DSR 1076 applies to fights from 1065 too.
    canonical = canonical_encounter_id(encounter_id)
    rows = session.execute(
        select(StratConfig).where(
            StratConfig.static_id == static_id,
            StratConfig.encounter_id == canonical,
        )
        .order_by(StratConfig.mechanic_ref)
    ).scalars().all()
    return [_row_to_dict(r) for r in rows]


def get_one(session: Session, encounter_id: int,
            mechanic_ref: str, static_id: int) -> dict[str, Any] | None:
    canonical = canonical_encounter_id(encounter_id)
    row = session.get(StratConfig, (static_id, canonical, mechanic_ref))
    return _row_to_dict(row) if row else None


def upsert(session: Session, encounter_id: int, mechanic_ref: str,
           *, assignments: dict[str, Any] | None,
           mit_plan: dict[str, Any] | None,
           static_id: int) -> dict[str, Any]:
    # Validate mechanic_ref shape early.
    decode_mechanic_ref(mechanic_ref)
    norm_assignments = validate_assignments(assignments)
    norm_mit_plan = validate_mit_plan(mit_plan)

    canonical = canonical_encounter_id(encounter_id)
    row = session.get(StratConfig, (static_id, canonical, mechanic_ref))
    if row is None:
        row = StratConfig(static_id=static_id, encounter_id=canonical,
                          mechanic_ref=mechanic_ref)
        session.add(row)
    row.assignments = norm_assignments
    row.mit_plan = norm_mit_plan
    session.commit()
    session.refresh(row)
    return _row_to_dict(row)


def delete_one(session: Session, encounter_id: int, mechanic_ref: str,
               static_id: int) -> bool:
    """Returns True if a row was deleted, False if it didn't exist."""
    canonical = canonical_encounter_id(encounter_id)
    result = session.execute(
        delete(StratConfig)
        .where(StratConfig.static_id == static_id,
               StratConfig.encounter_id == canonical,
               StratConfig.mechanic_ref == mechanic_ref)
    )
    session.commit()
    return (result.rowcount or 0) > 0


def _row_to_dict(row: StratConfig) -> dict[str, Any]:
    ability_id, occurrence = decode_mechanic_ref(row.mechanic_ref)
    return {
        "static_id": row.static_id,
        "encounter_id": row.encounter_id,
        "mechanic_ref": row.mechanic_ref,
        "ability_game_id": ability_id,
        "occurrence": occurrence,
        "assignments": row.assignments or {"role_map": {}},
        "mit_plan": row.mit_plan or {"slots": []},
    }
