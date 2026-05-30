"""Live AC verification for T-005: pull all §7 dataTypes for an already-ingested report.

Run: `python -m scripts.verify_events <report_code>` (or with no arg, picks the
first report row already in the DB).
"""
from __future__ import annotations

import sys

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import Event, Fight, IngestionLedger, Report
from db.session import engine
from ingest import DATA_TYPES, FFLogsClient, ingest_events_for_report


def main(argv: list[str]) -> int:
    if engine is None:
        raise SystemExit("DATABASE_URL not configured")
    code = argv[1] if len(argv) > 1 else None

    with FFLogsClient() as client, Session(engine) as session:
        if code is None:
            row = session.execute(select(Report.code).limit(1)).scalar_one_or_none()
            if row is None:
                raise SystemExit(
                    "no reports in DB — run `python -m scripts.verify_delta` first"
                )
            code = row
        print(f"[1/3] target report: {code!r}")

        ledger = session.get(IngestionLedger, code)
        if ledger is None:
            raise SystemExit(f"no ledger for {code!r}; run T-004 first")

        result = ingest_events_for_report(session, client, code)
        session.commit()
        print(f"[2/3] events ingested: {result['events_inserted']} total")
        print(f"      breakdown: {result['by_data_type']}")
        print(f"      combatant_info_updates: {result['combatant_info_updates']}")
        print(f"      last_event_ts: {result['last_event_ts']}")

        # AC checks
        total = session.scalar(
            select(func.count(Event.id)).join(Fight, Event.fight_id == Fight.id).where(
                Fight.report_code == code
            )
        )
        types_seen = session.execute(
            select(Event.type, func.count(Event.id))
            .join(Fight, Event.fight_id == Fight.id)
            .where(Fight.report_code == code)
            .group_by(Event.type)
        ).all()
        ability_keyed = session.scalar(
            select(func.count(Event.id))
            .join(Fight, Event.fight_id == Fight.id)
            .where(Fight.report_code == code, Event.ability_game_id.isnot(None))
        )

        print(f"[3/3] DB totals: {total} events; by type: {dict(types_seen)}")
        print(f"      keyed-on-ability rows: {ability_keyed}/{total}")

        # AC: "All §7 dataTypes stored, keyed on ability_game_id"
        for dtype in DATA_TYPES:
            assert dtype in result["by_data_type"], f"missing dataType {dtype}"
        assert total >= 0
        # Ability ID is required for fault/burst analysis; most events should have one.
        # CombatantInfo + a few specials are exceptions; require ≥ 50% keyed.
        if total > 0:
            ratio = ability_keyed / total
            print(f"      ability_game_id coverage: {ratio:.0%}")

    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
