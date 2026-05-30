"""Live AC verification for T-004: ingest a public report, prove rerun is delta-only.

Run: `python -m scripts.verify_delta` from the repo root with a populated .env.

Idempotent against the dev DB — the report row + fights + combatants land once
and stay; subsequent runs are no-ops on a `complete` ledger or only insert new
fights for `open`.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Combatant, Fight, IngestionLedger, Report
from db.session import engine
from ingest import FFLogsClient, ingest_report, mark_report_complete


def _discover_public_code(client: FFLogsClient) -> str:
    """Pick a current (non-frozen) zone so the events query isn't paywall-archived.

    FFLogs archives older reports' events behind the paid /user endpoint; the
    fights metadata still returns but `events()` errors. Current Savage / FRU
    reports stay open. We iterate non-frozen zones first and only fall back to
    frozen ones if nothing's accessible.
    """
    zones = client.graphql(
        "query { worldData { zones { id name frozen encounters { id name } } } }"
    )["worldData"]["zones"]
    ordered = [z for z in zones if not z.get("frozen")] + [z for z in zones if z.get("frozen")]
    for zone in ordered:
        for enc in zone.get("encounters") or []:
            rankings = client.graphql(
                """
                query ($id: Int!) {
                  worldData {
                    encounter(id: $id) {
                      fightRankings(metric: speed, page: 1)
                    }
                  }
                }
                """,
                {"id": enc["id"]},
            )
            payload = rankings["worldData"]["encounter"]["fightRankings"]
            ranks = payload.get("rankings") if isinstance(payload, dict) else None
            if ranks:
                code = ranks[0]["report"]["code"]
                print(
                    f"      using public report {code!r} "
                    f"(encounter {enc['id']} '{enc['name']}', "
                    f"zone '{zone['name']}', frozen={zone.get('frozen')})"
                )
                return code
    raise SystemExit("no public report code could be discovered")


def main() -> int:
    if engine is None:
        raise SystemExit("DATABASE_URL not configured; cannot run T-004 verify")

    with FFLogsClient() as client, Session(engine) as session:
        # 1. Discover a real code.
        code = _discover_public_code(client)

        # 2. First ingest.
        r1 = ingest_report(session, client, code)
        session.commit()
        print(f"[1/4] first ingest: {r1}")

        report = session.get(Report, code)
        fights = session.execute(
            select(Fight).where(Fight.report_code == code)
        ).scalars().all()
        combatants = session.execute(
            select(Combatant).join(Fight, Combatant.fight_id == Fight.id).where(
                Fight.report_code == code
            )
        ).scalars().all()
        ledger = session.get(IngestionLedger, code)
        print(
            f"      DB: report={report.code!r}, fights={len(fights)}, "
            f"combatants={len(combatants)}, ledger.status={ledger.status!r}"
        )

        # 3. Rerun: should add 0 fights (either no-op-complete, or open+no-new).
        r2 = ingest_report(session, client, code)
        session.commit()
        print(f"[2/4] rerun: {r2}")
        assert r2["new_fights"] == 0, "rerun added new fights (delta gate broken)"

        # 4. Force complete, rerun, prove zero-network no-op.
        mark_report_complete(session, code)
        session.commit()
        r3 = ingest_report(session, client, code)
        print(f"[3/4] complete-rerun: {r3}")
        assert r3["was_no_op"] is True, "complete report still hit the API"
        assert r3["new_fights"] == 0

        # 5. Sanity: counts unchanged.
        fights_after = session.execute(
            select(Fight).where(Fight.report_code == code)
        ).scalars().all()
        assert len(fights_after) == len(fights)
        print(f"[4/4] counts stable: {len(fights_after)} fights")

    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
