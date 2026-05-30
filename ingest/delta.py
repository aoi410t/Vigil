"""Delta ingestion + ingestion ledger (T-004, PLAN §3 Invariant 1).

The FFLogs API is write-once: this module is the only thing that pulls report
metadata, and it gates every fetch on the ledger so already-stored fights are
never re-pulled.

T-004 writes `reports`, `fights`, `combatants`, and the `ingestion_ledger` row.
Per-event payloads are deferred to T-005.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy.orm import Session

from db.models import Combatant, Fight, IngestionLedger, Report

DEFAULT_COMPLETE_GRACE_S = 6 * 3600


class IngestError(RuntimeError):
    """Ingestion-side failure (report not accessible, malformed payload, …)."""


class _GraphQLClient(Protocol):
    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]: ...


REPORT_QUERY = """
query Report($code: String!) {
  reportData {
    report(code: $code) {
      code
      title
      startTime
      endTime
      fights {
        id
        encounterID
        kill
        fightPercentage
        lastPhase
        startTime
        endTime
      }
      masterData {
        actors {
          id
          name
          server
          type
          subType
        }
      }
    }
  }
}
"""


def _epoch_ms_to_dt(ms: int | None) -> datetime | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def ingest_report(
    session: Session,
    client: _GraphQLClient,
    code: str,
    *,
    complete_grace_s: int = DEFAULT_COMPLETE_GRACE_S,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Ingest report meta + fights + per-fight combatants for a public report.

    Idempotent on `complete` ledger entries (no network call, no rows added).
    On `open` ledger entries, refetches the report and inserts only fights whose
    `fight_id_in_report` is not already in `ingestion_ledger.fights_ingested`.

    Combatants are sourced from `masterData.actors` (report-wide). One row is
    written per (new fight, player actor). This over-includes if a player was
    absent from a specific fight; T-005 will tighten by intersecting with
    actual events.

    Returns counters for the caller (and the T-004 AC test).
    """
    ledger = session.get(IngestionLedger, code)
    if ledger is not None and ledger.status == "complete":
        return {
            "new_fights": 0,
            "new_combatants": 0,
            "total_fights": len(ledger.fights_ingested or []),
            "status": "complete",
            "was_no_op": True,
        }

    data = client.graphql(REPORT_QUERY, {"code": code})
    report_data = (data.get("reportData") or {}).get("report")
    if report_data is None:
        raise IngestError(f"Report not found or not accessible: {code}")

    api_fights: list[dict[str, Any]] = report_data.get("fights") or []
    api_fight_ids = {int(f["id"]) for f in api_fights}
    already = set(ledger.fights_ingested) if ledger and ledger.fights_ingested else set()
    new_ids = api_fight_ids - already

    if ledger is None:
        session.add(
            Report(
                code=code,
                is_public=True,
                start_time=_epoch_ms_to_dt(report_data.get("startTime")),
                end_time=_epoch_ms_to_dt(report_data.get("endTime")),
                ingested_at=datetime.now(timezone.utc),
            )
        )
        session.flush()
    else:
        existing = session.get(Report, code)
        if existing is not None:
            existing.end_time = _epoch_ms_to_dt(report_data.get("endTime"))

    new_fights_by_local_id: dict[int, Fight] = {}
    for f in api_fights:
        fid_local = int(f["id"])
        if fid_local not in new_ids:
            continue
        start_ms = f.get("startTime")
        end_ms = f.get("endTime")
        duration = (
            end_ms - start_ms if start_ms is not None and end_ms is not None else None
        )
        fight = Fight(
            report_code=code,
            fight_id_in_report=fid_local,
            encounter_id=f.get("encounterID"),
            is_kill=f.get("kill"),
            fight_percentage=f.get("fightPercentage"),
            last_phase=f.get("lastPhase"),
            start_time=start_ms,
            end_time=end_ms,
            duration_ms=duration,
        )
        session.add(fight)
        new_fights_by_local_id[fid_local] = fight
    session.flush()

    master = report_data.get("masterData") or {}
    actors = master.get("actors") or []
    players = [a for a in actors if a.get("type") == "Player"]
    new_combatants = 0
    for fight in new_fights_by_local_id.values():
        for actor in players:
            session.add(
                Combatant(
                    fight_id=fight.id,
                    player_id=int(actor["id"]),
                    name=actor.get("name"),
                    server=actor.get("server"),
                    job=actor.get("subType"),
                    stats=None,
                )
            )
            new_combatants += 1

    now_dt = now or datetime.now(timezone.utc)
    end_dt = _epoch_ms_to_dt(report_data.get("endTime"))
    is_complete = (
        end_dt is not None and (now_dt - end_dt).total_seconds() > complete_grace_s
    )
    new_status = "complete" if is_complete else "open"

    full_fights_list = sorted(already | new_ids)
    if ledger is None:
        session.add(
            IngestionLedger(
                report_code=code,
                fights_ingested=full_fights_list,
                last_event_ts=0,
                status=new_status,
                last_polled_at=now_dt,
            )
        )
    else:
        ledger.fights_ingested = full_fights_list
        ledger.status = new_status
        ledger.last_polled_at = now_dt

    session.flush()
    return {
        "new_fights": len(new_ids),
        "new_combatants": new_combatants,
        "total_fights": len(api_fight_ids),
        "status": new_status,
        "was_no_op": False,
    }


def mark_report_complete(session: Session, code: str) -> bool:
    """Force a ledger row to `complete` so subsequent `ingest_report` is a no-op."""
    ledger = session.get(IngestionLedger, code)
    if ledger is None:
        return False
    ledger.status = "complete"
    ledger.last_polled_at = datetime.now(timezone.utc)
    session.flush()
    return True
