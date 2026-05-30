"""One-off: ingest just the boss-cast events for a fixed set of public FRU
kills, so T-104 has enough cross-pull data to detect consensus.

Avoids `ingest_events_for_report` (which would pull all 200+ fights per report)
by scoping to a single fight per report and pulling only the data types T-104
needs — Casts (Enemies) + Deaths for end-of-fight bound.
"""
from __future__ import annotations

import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Event, Fight
from db.session import engine
from ingest.delta import ingest_report
from ingest.fflogs import FFLogsClient

# (report_code, fightID_in_report) — picked from worldData.encounter(1079).fightRankings
TARGETS = [
    ("q7ZQNHBDh8VvR2jC", 30),
    ("KFthYXTAnQVN8Ba6", 26),
    ("gPyM3b1NXRL8Kxmj", 2),
    ("qka3zX9LZVMbRcg6", 9),
    ("3NjqBPZ62Dry1hpc", 13),
    ("XhynQJ9aHKkc2bPd", 25),
    ("ZgCdJkKr2FmcqwvT", 26),
    ("AD2WQFbPLma4nHzK", 8),
    ("hvMmHTwPqGbYg317", 69),
    ("1HDmfAGM7Tzv3aXt", 6),
]

# Just the data types T-104 needs: boss casts to extract canonical timeline,
# plus DamageTaken so phase segmentation (T-103) works on the new pulls.
DATA_TYPES = [
    ("Casts", "Enemies"),
    ("DamageDone", None),     # player → boss damage drives T-103 phase detection
    ("DamageTaken", None),
    ("Casts", "Friendlies"),
]

QUERY = """
query ($code: String!, $fid: [Int]!, $dt: EventDataType!, $ht: HostilityType, $st: Float) {
  reportData { report(code: $code) {
    events(dataType: $dt, fightIDs: $fid, hostilityType: $ht, startTime: $st) {
      data nextPageTimestamp
    }
  }}
}
"""


def ingest_one_fight(s: Session, c: FFLogsClient, code: str, fight_in_report: int) -> int:
    # Ingest report meta if it's new (so the Fight row exists).
    existing_fight = s.execute(
        select(Fight)
        .where(Fight.report_code == code,
               Fight.fight_id_in_report == fight_in_report)
    ).scalar_one_or_none()
    if existing_fight is None:
        ingest_report(s, c, code)
        s.commit()
        existing_fight = s.execute(
            select(Fight)
            .where(Fight.report_code == code,
                   Fight.fight_id_in_report == fight_in_report)
        ).scalar_one_or_none()
        if existing_fight is None:
            print(f"  WARN: fight {fight_in_report} not found in {code}")
            return 0

    # Skip if we already have events for this fight.
    have = s.execute(
        select(Event.id).where(Event.fight_id == existing_fight.id).limit(1)
    ).scalar_one_or_none()
    if have is not None:
        print(f"  {code} fight #{fight_in_report}: already have events, skipping")
        return 0

    inserted = 0
    for dt, ht in DATA_TYPES:
        cursor = 0
        while True:
            variables = {"code": code, "fid": [fight_in_report], "dt": dt, "st": float(cursor)}
            if ht is not None:
                variables["ht"] = ht
            resp = c.graphql(QUERY, variables)
            block = resp["reportData"]["report"]["events"]
            data = block.get("data") or []
            for e in data:
                ability_id = e.get("abilityGameID") or e.get("extraAbilityGameID")
                if e.get("type") == "death":
                    ability_id = e.get("killingAbilityGameID") or ability_id
                s.add(Event(
                    fight_id=existing_fight.id,
                    ts=e.get("timestamp"),
                    type=e.get("type"),
                    source_id=e.get("sourceID"),
                    target_id=e.get("targetID"),
                    ability_game_id=ability_id,
                    amount=e.get("amount"),
                    raw=e,
                ))
            inserted += len(data)
            next_ts = block.get("nextPageTimestamp")
            if next_ts is None or next_ts == cursor:
                break
            cursor = next_ts
        s.commit()
    return inserted


def main(argv: list[str]) -> int:
    if engine is None:
        raise SystemExit("DATABASE_URL not configured")
    with Session(engine) as s, FFLogsClient() as c:
        for code, fid in TARGETS:
            print(f"\n== {code} fight #{fid} ==")
            try:
                n = ingest_one_fight(s, c, code, fid)
                print(f"  inserted {n} events")
            except Exception as e:
                print(f"  ERROR: {type(e).__name__}: {e}")
                s.rollback()
    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
