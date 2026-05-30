"""Job → role lookup tables shared across analysis modules (v1.11.0).

FFXIV jobs partition cleanly into 4 roles. We use 3-letter abbrevs because
that's what FFLogs emits in CombatantInfo and what we store in `combatants.job`.

Used by:
- T-302 fault attribution (avoidable damage = tankbuster on a non-tank, etc.)
- Body-check fault attribution (v1.14.0) — role-vs-assigned-role mismatches.

Endwalker / Dawntrail jobs only — older job abbrevs (e.g. ACN/THM/CRP) are
class-not-job and wouldn't appear in combatant data for current content.
"""
from __future__ import annotations

TANK_JOBS = frozenset({"WAR", "PLD", "DRK", "GNB"})
HEALER_JOBS = frozenset({"WHM", "SCH", "AST", "SGE"})
MELEE_DPS = frozenset({"MNK", "DRG", "NIN", "SAM", "RPR", "VPR"})
PHYS_RANGED_DPS = frozenset({"BRD", "MCH", "DNC"})
CASTER_DPS = frozenset({"BLM", "SMN", "RDM", "PCT"})

DPS_JOBS = MELEE_DPS | PHYS_RANGED_DPS | CASTER_DPS


def role_of(job: str | None) -> str | None:
    """Map a job abbreviation to its role. None for unknown jobs."""
    if job is None:
        return None
    if job in TANK_JOBS:
        return "tank"
    if job in HEALER_JOBS:
        return "healer"
    if job in DPS_JOBS:
        return "dps"
    return None
