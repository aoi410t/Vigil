"""Live verification for T-002 AC: obtain token, force-refresh, fetch a public report.

Run: `python -m scripts.verify_fflogs` from the repo root with a populated .env.
"""
from __future__ import annotations

from ingest import FFLogsClient


def main() -> int:
    with FFLogsClient() as c:
        # 1. Obtain token.
        t1 = c.get_token()
        print(f"[1/4] obtained token (len={len(t1)})")

        # 2. Force-refresh (re-exchange creds for a fresh token).
        t2 = c.get_token(force_refresh=True)
        print(f"[2/4] refreshed token (len={len(t2)})")

        # 3. Auth proof + rate-limit visibility.
        rl = c.rate_limit()["rateLimitData"]
        print(
            f"[3/4] rate limit: {rl['pointsSpentThisHour']}/{rl['limitPerHour']} pts, "
            f"resets in {rl['pointsResetIn']}s"
        )

        # 4. Discover a real public report via worldData → fightRankings,
        #    then fetch it through reportData.report (PLAN §7 shape).
        encounters = c.graphql(
            """
            query { worldData { zones { id name encounters { id name } } } }
            """
        )["worldData"]["zones"]
        # pick the most recent zone that has at least one encounter
        zone = next((z for z in reversed(encounters) if z["encounters"]), None)
        if zone is None:
            raise SystemExit("no zones with encounters returned")
        enc = zone["encounters"][0]
        print(f"      probing encounter {enc['id']} '{enc['name']}' (zone '{zone['name']}')")

        rankings = c.graphql(
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
        )["worldData"]["encounter"]["fightRankings"]
        ranks = rankings.get("rankings") if isinstance(rankings, dict) else None
        if not ranks:
            raise SystemExit(f"no fightRankings for encounter {enc['id']}: {rankings!r}")
        code = ranks[0]["report"]["code"]
        print(f"      sample public report code: {code}")

        report = c.fetch_report(code)["reportData"]["report"]
        fights = report.get("fights") or []
        print(
            f"[4/4] fetched report {report['code']!r} — "
            f"{len(fights)} fights, title={report.get('title')!r}"
        )
        if fights:
            f = fights[0]
            print(
                f"      first fight: encounter {f['encounterID']}, "
                f"kill={f['kill']}, lastPhase={f['lastPhase']}, "
                f"fightPercentage={f['fightPercentage']}"
            )

    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
