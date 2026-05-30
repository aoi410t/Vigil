"""One-off: truncate events for a report, reset its event cursor, re-ingest.

Use after event-ingest changes (e.g. adding the enemy-casts pass landed with
T-006) so an already-ingested report picks up the new event coverage.
"""
from __future__ import annotations

import sys

from sqlalchemy import delete
from sqlalchemy.orm import Session

from db.models import Event, Fight, IngestionLedger
from db.session import engine
from ingest import FFLogsClient, ingest_events_for_report


def main(argv: list[str]) -> int:
    if engine is None:
        raise SystemExit("DATABASE_URL not configured")
    if len(argv) < 2:
        raise SystemExit("usage: python -m scripts.rescan_events <report_code>")
    code = argv[1]

    with FFLogsClient() as client, Session(engine) as session:
        fight_ids = [
            f.id for f in session.query(Fight).filter_by(report_code=code).all()
        ]
        if not fight_ids:
            raise SystemExit(f"no fights for {code!r}; run scripts/verify_delta first")

        deleted = session.execute(
            delete(Event).where(Event.fight_id.in_(fight_ids))
        ).rowcount
        print(f"deleted {deleted} events for {code!r}")

        ledger = session.get(IngestionLedger, code)
        if ledger is not None:
            ledger.last_event_ts = 0
        session.commit()

        result = ingest_events_for_report(session, client, code)
        session.commit()
        print(f"re-ingested: {result['events_inserted']} events")
        print(f"breakdown: {result['by_data_type']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
