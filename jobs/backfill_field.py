"""T-201 field backfill — rankings → codes → ingest, rate-limited + ledger-deduped.

For each tracked encounter, query FFLogs `worldData.encounter(id).fightRankings`
for the top public reports, then call existing T-004 `ingest_report` on each.
The ingestion ledger short-circuits anything already `complete` (PLAN
Invariant 1), so re-runs only pull *new* reports.

For a configurable subset of the discovered reports (`events_top_n`), we also
pull events for the single ranked-fight in each — exactly the slice needed for
T-104 consensus + T-202 cross-group consensus + T-204 empirical DPS check.
Ingesting the full report's events would 200× the cost since Ultimate reports
average that many pulls each.

Deployment shape mirrors T-101: `python -m jobs.backfill_field [--dry-run]`
runs one pass; cron/Task Scheduler externally for periodic execution.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import Event, Fight, IngestionLedger
from db.session import SessionLocal, engine
from ingest.delta import ingest_report
from ingest.fflogs import FFLogsClient

# Default encounter set — current Savage tier (AAC Heavyweight, M9S–M12S) +
# the current Ultimate (FRU) + DSR + TOP. Easy to expand; M-CART/M-INFER
# want as many encounters as we're willing to pay storage for.
# Verified against FFLogs `worldData.encounter(id).name`:
#   1079 = Futures Rewritten (current ultimate)
#   1068 = The Omega Protocol
#   1076 = Dragonsong's Reprise (canonical; legacy alias 1065 lives in the
#          cloned-encounter group — backfill targets BOTH halves automatically)
#   101..105 = AAC Heavyweight tier (M9S..M12S-P2)
# Other ultimates available if useful later:
#   1075 = TEA, 1074 = UWU, 1073 = UCoB
DEFAULT_ENCOUNTERS = (1079, 1068, 1076, 1065, 101, 102, 103, 104, 105)

DEFAULT_REPORTS_PER_ENCOUNTER = 25
DEFAULT_EVENTS_TOP_N = 5
INTER_QUERY_SLEEP_S = 0.5  # polite pacing between FFLogs queries

_RANKINGS_QUERY = """
query ($encounterId: Int!, $page: Int!) {
  worldData { encounter(id: $encounterId) {
    fightRankings(page: $page)
  }}
}
"""

_FIGHT_EVENTS_QUERY = """
query ($code: String!, $fid: [Int]!, $dt: EventDataType!, $ht: HostilityType, $st: Float) {
  reportData { report(code: $code) {
    events(dataType: $dt, fightIDs: $fid, hostilityType: $ht, startTime: $st) {
      data nextPageTimestamp
    }
  }}
}
"""

# T-104 consensus needs Casts(Enemies) and DamageDone (for T-103 phase
# detection's non-player damage-target signal). DamageTaken + friendly Casts
# round out coverage without ballooning event count.
_EVENTS_DATA_TYPES = (
    ("Casts", "Enemies"),
    ("DamageDone", None),
    ("DamageTaken", None),
    ("Casts", "Friendlies"),
)


def _pace() -> None:
    if INTER_QUERY_SLEEP_S > 0:
        time.sleep(INTER_QUERY_SLEEP_S)


def fetch_rankings(client: FFLogsClient, encounter_id: int,
                   page: int = 1) -> list[dict[str, Any]]:
    """Return the parsed list of ranking entries for one encounter page."""
    resp = client.graphql(_RANKINGS_QUERY,
                          {"encounterId": encounter_id, "page": page})
    raw = resp["worldData"]["encounter"]["fightRankings"]
    if isinstance(raw, str):
        raw = json.loads(raw)
    return raw.get("rankings", [])


def fetch_rankings_paginated(client: FFLogsClient, encounter_id: int,
                             max_entries: int) -> list[dict[str, Any]]:
    """Collect rankings across pages until we have `max_entries` or pages dry up.

    Page 1 is sorted by all-time best parses (often archived for legacy fights).
    Deeper pages mix in more recent clears, so paginating broadens the candidate
    pool for encounters where the top-ranked reports are paywall-archived.
    """
    out: list[dict[str, Any]] = []
    page = 1
    while len(out) < max_entries:
        batch = fetch_rankings(client, encounter_id, page=page)
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 25:  # last page
            break
        page += 1
        _pace()
    return out[:max_entries]


def _ingest_fight_events(session: Session, client: FFLogsClient,
                         code: str, fight_in_report: int, fight_db_id: int) -> int:
    """Pull events for one fight (scoped, not the full report). Returns count."""
    have = session.execute(
        select(Event.id).where(Event.fight_id == fight_db_id).limit(1)
    ).scalar_one_or_none()
    if have is not None:
        return 0

    inserted = 0
    for dt, ht in _EVENTS_DATA_TYPES:
        cursor = 0
        while True:
            variables = {"code": code, "fid": [fight_in_report],
                         "dt": dt, "st": float(cursor)}
            if ht is not None:
                variables["ht"] = ht
            resp = client.graphql_with_archive_retry(session, _FIGHT_EVENTS_QUERY, variables)
            block = resp["reportData"]["report"]["events"]
            data = block.get("data") or []
            for e in data:
                ability_id = e.get("abilityGameID") or e.get("extraAbilityGameID")
                if e.get("type") == "death":
                    ability_id = e.get("killingAbilityGameID") or ability_id
                session.add(Event(
                    fight_id=fight_db_id,
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
            _pace()
        session.commit()
        _pace()
    return inserted


def backfill_once(
    session: Session,
    client: FFLogsClient,
    *,
    encounter_ids: tuple[int, ...] = DEFAULT_ENCOUNTERS,
    reports_per_encounter: int = DEFAULT_REPORTS_PER_ENCOUNTER,
    events_top_n: int = DEFAULT_EVENTS_TOP_N,
    dry_run: bool = False,
    auto_refresh_fight_model: bool = True,
) -> dict[int, dict[str, Any]]:
    """Run one backfill pass. Returns `{encounter_id: {summary…}}`.

    v1.17.1: after each encounter's reports are pulled, auto-refreshes the
    canonical fight_model for that encounter (force=True, since this is the
    main vehicle for adding kill data to less-progged ultimates and the
    nightly cadence makes the 60s debounce irrelevant). Disable with
    `auto_refresh_fight_model=False`.
    """
    # Local import keeps backfill_field a thin Postgres-only module.
    from analysis.fight_model_refresh import refresh_fight_model_for_encounter
    summary: dict[int, dict[str, Any]] = {}
    for enc_id in encounter_ids:
        rankings = fetch_rankings_paginated(client, enc_id, reports_per_encounter)
        _pace()
        per_enc: dict[str, Any] = {
            "rankings_seen": len(rankings),
            "reports_ingested": 0,
            "reports_skipped_complete": 0,
            "fights_with_events": 0,
            "events_inserted": 0,
            "errors": [],
        }
        for rank_i, r in enumerate(rankings):
            rep = r.get("report") or {}
            code = rep.get("code")
            fight_in_report = rep.get("fightID")
            if not code:
                continue

            ledger = session.get(IngestionLedger, code)
            if ledger is not None and ledger.status == "complete":
                per_enc["reports_skipped_complete"] += 1
            else:
                if dry_run:
                    per_enc["reports_ingested"] += 1
                    continue
                try:
                    ingest_report(session, client, code)
                    session.commit()
                    per_enc["reports_ingested"] += 1
                    _pace()
                except Exception as e:
                    session.rollback()
                    per_enc["errors"].append({"code": code, "stage": "meta",
                                              "error": f"{type(e).__name__}: {e}"})
                    continue

            # Events for the top N rankings only
            if rank_i < events_top_n and fight_in_report is not None and not dry_run:
                fight_row = session.execute(
                    select(Fight)
                    .where(Fight.report_code == code,
                           Fight.fight_id_in_report == fight_in_report)
                ).scalar_one_or_none()
                if fight_row is None:
                    continue
                try:
                    n = _ingest_fight_events(session, client, code,
                                             fight_in_report, fight_row.id)
                    if n > 0:
                        per_enc["fights_with_events"] += 1
                        per_enc["events_inserted"] += n
                except Exception as e:
                    session.rollback()
                    per_enc["errors"].append({"code": code, "stage": "events",
                                              "error": f"{type(e).__name__}: {e}"})

        # v1.17.1: refresh canonical fight_model for this encounter (force=True
        # — nightly cadence already debounces; we want every backfill pass to
        # land in the model). Refresh failure logs to per_enc["errors"] but
        # doesn't abort the encounter loop.
        if (auto_refresh_fight_model and not dry_run
                and (per_enc["reports_ingested"] > 0
                     or per_enc["fights_with_events"] > 0)):
            try:
                per_enc["fight_model_refresh"] = refresh_fight_model_for_encounter(
                    session, enc_id, force=True,
                )
            except Exception as e:
                session.rollback()
                per_enc["errors"].append({"stage": "fight_model_refresh",
                                          "error": f"{type(e).__name__}: {e}"})

        summary[enc_id] = per_enc
    return summary


def field_stats(session: Session,
                encounter_ids: tuple[int, ...] = DEFAULT_ENCOUNTERS,
                ) -> list[dict[str, Any]]:
    """Per-encounter: how many reports + how many fights with events we have.

    v1.17.0: when an input ID is part of a cloned group (e.g. DSR 1065),
    the row reports unified counts across the full group. Duplicate input
    IDs (1065 + 1076 both passed) collapse into one row keyed on the
    canonical ID.
    """
    from analysis._encounter import canonical_encounter_id, encounter_id_group
    # Dedupe canonical IDs while preserving input order.
    seen: set[int] = set()
    canonical_order: list[int] = []
    for enc_id in encounter_ids:
        canonical = canonical_encounter_id(enc_id)
        if canonical not in seen:
            seen.add(canonical)
            canonical_order.append(canonical)
    out = []
    for canonical in canonical_order:
        group = encounter_id_group(canonical)
        report_count = session.execute(
            select(func.count(func.distinct(Fight.report_code)))
            .where(Fight.encounter_id.in_(group))
        ).scalar() or 0
        fights_with_events = session.execute(
            select(func.count(func.distinct(Event.fight_id)))
            .select_from(Event)
            .join(Fight, Fight.id == Event.fight_id)
            .where(Fight.encounter_id.in_(group), Fight.is_kill.is_(True))
        ).scalar() or 0
        out.append({
            "encounter_id": canonical,
            "reports_ingested": int(report_count),
            "kills_with_events": int(fights_with_events),
        })
    return out


def main(argv: list[str]) -> int:
    if engine is None:
        raise SystemExit("DATABASE_URL not configured")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="discover + count, but don't write anything")
    ap.add_argument("--encounters", type=int, nargs="+",
                    default=list(DEFAULT_ENCOUNTERS))
    ap.add_argument("--reports-per-encounter", type=int,
                    default=DEFAULT_REPORTS_PER_ENCOUNTER)
    ap.add_argument("--events-top-n", type=int, default=DEFAULT_EVENTS_TOP_N)
    args = ap.parse_args(argv[1:])

    started = datetime.now(timezone.utc)
    with SessionLocal() as s, FFLogsClient() as c:
        summary = backfill_once(
            s, c,
            encounter_ids=tuple(args.encounters),
            reports_per_encounter=args.reports_per_encounter,
            events_top_n=args.events_top_n,
            dry_run=args.dry_run,
        )
        stats = field_stats(s, tuple(args.encounters))

    dur = (datetime.now(timezone.utc) - started).total_seconds()
    print(f"\n== backfill summary ({dur:.0f}s) ==")
    for enc_id, per_enc in summary.items():
        print(f"  encounter {enc_id}: seen={per_enc['rankings_seen']:>3} "
              f"new={per_enc['reports_ingested']:>2} "
              f"skipped={per_enc['reports_skipped_complete']:>2} "
              f"fights+events={per_enc['fights_with_events']:>2} "
              f"events+={per_enc['events_inserted']}")
        for err in per_enc["errors"]:
            print(f"    ERROR {err['code']} ({err['stage']}): {err['error']}")
    print("\n== field totals after pass ==")
    for s_row in stats:
        print(f"  encounter {s_row['encounter_id']}: "
              f"{s_row['reports_ingested']} reports · "
              f"{s_row['kills_with_events']} kills w/ events")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
