"""Live AC verification for T-007 Mode-1 fault basics on real ingested data."""
from __future__ import annotations

import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from analysis.faults import mode1_faults_for_report
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

        result = mode1_faults_for_report(session, code)
        fights = result["fights"]
        total_deaths = sum(len(f["deaths"]) for f in fights)
        total_takers_rows = sum(len(f["damage_takers"]) for f in fights)
        wipes = [f for f in fights if not f["is_kill"]]
        kills = [f for f in fights if f["is_kill"]]

        print(f"report:       {result['report_code']}")
        print(f"fights:       {len(fights)}  (wipes={len(wipes)}, kills={len(kills)})")
        print(f"total deaths: {total_deaths}")
        print(f"taker rows:   {total_takers_rows}")

        # Show a representative wipe with deaths
        with_deaths = [f for f in wipes if f["deaths"]]
        if with_deaths:
            f = with_deaths[0]
            print(f"\nsample wipe fight_id={f['fight_id']} (in_report={f['fight_id_in_report']}) "
                  f"phase={f['last_phase']} pct={f['fight_percentage']}")
            print(f"  deaths ({len(f['deaths'])}):")
            for d in f['deaths']:
                print(f"    pid={d['player_id']} {d['name']!s:>12} ({d['job']}) "
                      f"@t={d['ts']}  killed by ability {d['killing_ability_game_id']}")
            print(f"  top 5 damage takers:")
            for t in f['damage_takers'][:5]:
                print(f"    pid={t['player_id']} {t['name']!s:>12} ({t['job']}) "
                      f"= {t['damage_taken_total']:>9,} damage")

    print("\nOK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
