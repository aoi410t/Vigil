"""v1.17.0 one-shot migration: canonicalize encounter-scoped tables.

After v1.17.0 unifies cloned encounter IDs into one canonical group
(e.g. DSR 1065 ≡ 1076 → canonical 1076), the analytics layer reads
fight_model + strat_config ONLY at the canonical ID. Rows under a
legacy alias become orphans — they're never read again.

This script:
  1. Walks every cloned-encounter group (`analysis._encounter._CLONED_GROUPS`).
  2. Reports row counts under each non-canonical alias for
     `fight_model` and `strat_config`.
  3. Asks for confirmation before deleting them.
  4. Deletes only the alias rows. Canonical rows are untouched.

Run with `--dry-run` to inspect without deleting, or `--yes` to skip the
prompt.

Usage:
  python -m scripts.migrate_canonical_encounters [--dry-run] [--yes]
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import delete, select, func

from analysis._encounter import _CANONICAL_OF, all_cloned_groups
from db.models import FightModel, StratConfig
from db.session import SessionLocal, engine


def _row_count(session, table, encounter_id: int) -> int:
    return int(session.execute(
        select(func.count()).select_from(table)
        .where(table.encounter_id == encounter_id)
    ).scalar() or 0)


def _table_row_summary(session, table, alias: int, canonical: int) -> dict:
    """Per-(alias, canonical) counts for one table."""
    return {
        "alias_rows": _row_count(session, table, alias),
        "canonical_rows": _row_count(session, table, canonical),
    }


def main(argv: list[str]) -> int:
    if engine is None:
        print("DATABASE_URL not configured; cannot run migration.", file=sys.stderr)
        return 2

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Report orphan rows but don't delete anything.")
    ap.add_argument("--yes", action="store_true",
                    help="Skip the interactive confirmation prompt.")
    args = ap.parse_args(argv[1:])

    groups = all_cloned_groups()
    if not groups:
        print("No cloned encounter groups defined; nothing to do.")
        return 0

    print(f"Cloned-encounter groups: {len(groups)}\n")

    deletion_plan: list[tuple[type, int, int, int]] = []  # (table, alias, canonical, rows)

    with SessionLocal() as session:
        for group in groups:
            # Canonical is whatever _CANONICAL_OF says for any member.
            canonical = _CANONICAL_OF.get(group[0])
            aliases = [eid for eid in group if eid != canonical]

            print(f"Group {group} (canonical = {canonical}):")
            for alias in aliases:
                for label, table in (("fight_model", FightModel),
                                      ("strat_config", StratConfig)):
                    counts = _table_row_summary(session, table, alias, canonical)
                    alias_n = counts["alias_rows"]
                    canonical_n = counts["canonical_rows"]
                    marker = "  <- ORPHAN" if alias_n > 0 else ""
                    print(f"  {label:<14} alias {alias}: {alias_n:>5} rows"
                          f"   canonical {canonical}: {canonical_n:>5} rows{marker}")
                    if alias_n > 0:
                        deletion_plan.append((table, alias, canonical, alias_n))
            print()

        if not deletion_plan:
            print("No orphan rows found — nothing to migrate.")
            return 0

        total_to_delete = sum(rows for _, _, _, rows in deletion_plan)
        print(f"Would delete {total_to_delete} orphan rows across "
              f"{len(deletion_plan)} (table, alias) pairs.\n")

        if args.dry_run:
            print("--dry-run set; no rows deleted.")
            return 0

        if not args.yes:
            print("Proceed with deletion? [y/N] ", end="", flush=True)
            answer = input().strip().lower()
            if answer not in ("y", "yes"):
                print("Aborted.")
                return 1

        for table, alias, _canonical, expected_n in deletion_plan:
            result = session.execute(
                delete(table).where(table.encounter_id == alias)
            )
            actual = result.rowcount or 0
            print(f"  Deleted {actual} rows from {table.__tablename__} at "
                  f"alias {alias} (expected {expected_n}).")
        session.commit()
        print("\nMigration complete.")
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
