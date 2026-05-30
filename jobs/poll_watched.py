"""T-101: poll all active `watched_reports` once.

Wraps T-004's `ingest_report` + T-005's `ingest_events_for_report` so that for
each active watchlist entry we:
1. fetch report meta (auto-flips `open` → `complete` when FFLogs reports end_time)
2. fetch any new events past the ledger's `last_event_ts` cursor

A single pass is idempotent (delta-aware via the ingestion ledger — PLAN
Invariant 1) and skips reports that have already flipped to `complete`.

Deployment shape: `python -m jobs.poll_watched` runs one pass. Wrap in cron /
Task Scheduler / a while-loop wrapper for periodic execution. Keeping it as a
one-shot script rather than an in-process scheduler keeps the FastAPI server
focused on serving and lets the user choose the cadence externally.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analysis.fight_model_refresh import refresh_for_report
from db.models import IngestionLedger, WatchedReport
from db.session import SessionLocal, engine
from ingest.delta import ingest_report
from ingest.events import ingest_events_for_report
from ingest.fflogs import FFLogsClient


def _poll_one_row(session: Session, client: FFLogsClient,
                  w: WatchedReport, *,
                  auto_refresh_fight_model: bool = True) -> dict[str, Any]:
    """Ingest one watched report. Handles commits + per-row error capture
    so callers (the watchlist sweep, the Poll-now button, ad-hoc scripts)
    never see the half-committed-state footgun on `ingest_report`.

    v1.17.1: after a successful ingest that added new events, auto-refreshes
    the canonical fight_model for every encounter this report contributed to
    (persist → classify → annotate-cactbot). Throttled per-encounter at the
    refresh layer (default 60s) so a burst of polls during a prog session
    doesn't re-classify on every tick. Disable with `auto_refresh_fight_model=False`
    for tests or one-shots that want raw ingest only.
    """
    entry: dict[str, Any] = {"code": w.code, "label": w.label}
    try:
        ledger = session.get(IngestionLedger, w.code)
        if ledger is not None and ledger.status == "complete":
            entry["status"] = "skipped_complete"
            w.last_polled_at = datetime.now(timezone.utc)
            w.last_error = None
            session.commit()
            return entry

        meta = ingest_report(session, client, w.code)
        session.commit()
        entry["meta"] = meta

        ev = ingest_events_for_report(session, client, w.code)
        session.commit()
        entry["events"] = ev

        w.last_polled_at = datetime.now(timezone.utc)
        w.last_error = None
        session.commit()
        entry["status"] = "ok"

        # v1.17.1: refresh canonical fight_model for any encounters this
        # report touched. Skip when nothing new was ingested (avoids paying
        # the classifier cost on a no-op poll). Failures here are non-fatal —
        # the ingest already committed; we surface refresh errors per-encounter
        # in the response but don't flip status away from "ok".
        if (auto_refresh_fight_model
                and (meta.get("new_fights", 0) > 0
                     or ev.get("events_inserted", 0) > 0)):
            try:
                entry["fight_model_refresh"] = refresh_for_report(
                    session, w.code,
                )
            except Exception as e:  # belt-and-suspenders; refresh_for_report itself doesn't raise
                session.rollback()
                entry["fight_model_refresh_error"] = f"{type(e).__name__}: {e}"
    except Exception as e:
        session.rollback()
        w.last_error = f"{type(e).__name__}: {e}"
        w.last_polled_at = datetime.now(timezone.utc)
        session.commit()
        entry["status"] = "error"
        entry["error"] = str(e)
    return entry


def poll_one_by_code(session: Session, client: FFLogsClient,
                     code: str, *, static_id: int | None = None,
                    ) -> dict[str, Any] | None:
    """Poll just one watched report by code. Returns None if it's not on the
    watchlist.

    `static_id` (v1.6.0): when set, only look at that static's watch entry.
    When None (legacy CLI path), pick the first row across any static —
    fine for ad-hoc scripts but the API path always passes static_id.
    """
    q = select(WatchedReport).where(WatchedReport.code == code)
    if static_id is not None:
        q = q.where(WatchedReport.static_id == static_id)
    w = session.execute(q.limit(1)).scalar_one_or_none()
    if w is None:
        return None
    return _poll_one_row(session, client, w)


def poll_once(session: Session, client: FFLogsClient) -> list[dict[str, Any]]:
    """Run one pass over the active watchlist across ALL statics. Returns a
    per-report summary. The same report code may appear once per watching
    static; the ingestion ledger dedups raw-data work."""
    watched = session.execute(
        select(WatchedReport).where(WatchedReport.active.is_(True))
    ).scalars().all()
    return [_poll_one_row(session, client, w) for w in watched]


def main(argv: list[str]) -> int:
    if engine is None:
        raise SystemExit("DATABASE_URL not configured")
    with SessionLocal() as s, FFLogsClient() as c:
        summaries = poll_once(s, c)

    if not summaries:
        print("watchlist empty — nothing to poll")
        return 0

    print(f"polled {len(summaries)} watched reports:")
    for r in summaries:
        line = f"  {r['code']:>18} ({r.get('label') or '—'}): {r['status']}"
        if r["status"] == "ok":
            meta = r["meta"]
            ev = r["events"]
            line += (f" — fights+{meta.get('new_fights', 0)} "
                     f"events+{ev.get('events_inserted', 0)} "
                     f"({meta.get('status')})")
        elif r["status"] == "error":
            line += f" — {r['error']}"
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
