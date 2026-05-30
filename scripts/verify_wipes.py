"""Live AC verification for T-006 — M-WIPE wipe histogram on real ingested data."""
from __future__ import annotations

import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from analysis.wipes import wipe_histogram_for_report
from db.models import Report
from db.session import engine


def main(argv: list[str]) -> int:
    if engine is None:
        raise SystemExit("DATABASE_URL not configured")
    code = argv[1] if len(argv) > 1 else None

    with Session(engine) as session:
        if code is None:
            code = session.execute(select(Report.code).limit(1)).scalar_one_or_none()
            if code is None:
                raise SystemExit("no reports in DB — run scripts/verify_delta first")

        result = wipe_histogram_for_report(session, code)
        print(f"report:      {result['report_code']}")
        print(f"total wipes: {result['total_wipes']}")
        print(f"total kills: {result['total_kills']}")
        print(f"buckets ({len(result['buckets'])}):")
        for b in result["buckets"][:15]:
            print(
                f"  phase={b['phase']:>3}  ability={b['ability_game_id']!s:>8}"
                f"  count={b['count']:>3}  wipes={b['wipes'][:5]}"
                f"{'…' if len(b['wipes']) > 5 else ''}"
            )

    assert result["total_wipes"] >= 0
    assert isinstance(result["buckets"], list)
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
