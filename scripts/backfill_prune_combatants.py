"""One-off: prune NPC combatants from already-ingested reports (T-109).

T-004 over-includes combatants from FFLogs `masterData.actors` (~12k/fight on
Ultimates). T-109 prunes any combatant whose player_id never appears as a
source_id in cast/damage events for that fight. New ingests now do this
automatically inside `ingest_events_for_report`; this script catches up reports
that landed before the patch.

Usage:
    python -m scripts.backfill_prune_combatants                  # every report
    python -m scripts.backfill_prune_combatants <report_code>... # specific reports
"""
from __future__ import annotations

import sys

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import Combatant, Fight
from db.session import engine
from ingest.events import prune_inactive_combatants


def _report_codes(session: Session, argv: list[str]) -> list[str]:
    if len(argv) > 1:
        return list(argv[1:])
    return list(
        session.execute(
            select(Fight.report_code).distinct().order_by(Fight.report_code)
        ).scalars().all()
    )


def main(argv: list[str]) -> int:
    if engine is None:
        raise SystemExit("DATABASE_URL not configured")
    with Session(engine) as session:
        codes = _report_codes(session, argv)
        if not codes:
            print("no reports to prune")
            return 0

        grand_total = 0
        for code in codes:
            fights = session.execute(
                select(Fight.id).where(Fight.report_code == code)
            ).scalars().all()
            if not fights:
                print(f"{code}: no fights, skipping")
                continue

            before = session.execute(
                select(func.count(Combatant.fight_id)).where(Combatant.fight_id.in_(fights))
            ).scalar_one()

            deleted = 0
            for fid in fights:
                deleted += prune_inactive_combatants(session, fid)
            session.commit()

            after = session.execute(
                select(func.count(Combatant.fight_id)).where(Combatant.fight_id.in_(fights))
            ).scalar_one()

            grand_total += deleted
            print(
                f"{code}: fights={len(fights)} combatants {before} -> {after} (pruned {deleted})"
            )

        print(f"total pruned across {len(codes)} reports: {grand_total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
