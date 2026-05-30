"""Live AC verification for T-008 — M-GCD drop detection on real ingested data.

AC requires the output to be sane on a sample. Heuristic check: a current Savage
prog session should have non-zero drops on most pulls (deaths/downtime cause
dropped GCDs); a perfect run would have ~0-3 per player per fight.
"""
from __future__ import annotations

import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from analysis.gcd import mode1_gcd_for_report
from db.models import Report
from db.session import engine


def main(argv):
    if engine is None:
        raise SystemExit("DATABASE_URL not configured")
    code = argv[1] if len(argv) > 1 else None

    with Session(engine) as session:
        if code is None:
            code = session.execute(select(Report.code).limit(1)).scalar_one_or_none()
            if code is None:
                raise SystemExit("no reports in DB")

        result = mode1_gcd_for_report(session, code)
        fights = result["fights"]
        print(f"report: {result['report_code']}  fights: {len(fights)}")

        total_drops = sum(
            p["dropped_count"] for f in fights for p in f["players"]
        )
        total_player_fights = sum(len(f["players"]) for f in fights)
        avg_drops_per_player_fight = (
            total_drops / total_player_fights if total_player_fights else 0
        )
        print(f"player-fights: {total_player_fights}  total drops: {total_drops}  "
              f"avg/player-fight: {avg_drops_per_player_fight:.1f}")

        # Show first wipe + one kill
        wipe = next((f for f in fights if not f["is_kill"] and f["players"]), None)
        kill = next((f for f in fights if f["is_kill"] and f["players"]), None)
        for label, f in (("WIPE", wipe), ("KILL", kill)):
            if f is None:
                continue
            print(f"\n--- {label} fight {f['fight_id_in_report']} "
                  f"(duration {f['duration_ms']/1000:.1f}s) ---")
            for p in f["players"]:
                rate = p["dropped_count"] / (f["duration_ms"] / 60_000) if f["duration_ms"] else 0
                print(
                    f"  {p['name']!s:>20} ({p['job']!s:>14}) "
                    f"GCD~{p['gcd_ms']}ms  casts={p['casts_total']:>3}  "
                    f"GCDs={p['gcds_cast']:>3}  drops={p['dropped_count']:>3}  "
                    f"({rate:.1f}/min)"
                )

    print("\nOK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
