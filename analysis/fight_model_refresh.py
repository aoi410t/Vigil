"""v1.17.1: auto-refresh the canonical fight_model after report ingest.

Runs the 3-step pipeline `write_consensus → classify → annotate-cactbot`
for a single canonical encounter. Wired into:

  - `jobs/poll_watched._poll_one_row` (live polling + Poll-now button)
  - `jobs/backfill_field.backfill_once` (per-encounter nightly job)

Throttled per-encounter via `FightModel.updated_at` so a burst of polls
during a prog session doesn't re-classify every 30 seconds. Default
60s debounce; tunable via `throttle_seconds`. Pass `force=True` to
bypass.

Failure semantics: never raise. Returns a summary dict with an `error`
field on failure so the caller can log it without aborting the
surrounding ingest pass.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from analysis._encounter import canonical_encounter_id
from analysis.consensus import write_consensus_to_fight_model
from analysis.mechanic_classifier import classify_canonical_abilities
from db.models import Fight, FightModel
from ingest.cactbot import annotate_fight_model_for_encounter

DEFAULT_THROTTLE_SECONDS = 60


def encounter_ids_for_report(session: Session, report_code: str) -> set[int]:
    """Distinct canonical encounter IDs that this report has fights for."""
    raw_ids = session.execute(
        select(Fight.encounter_id)
        .where(Fight.report_code == report_code,
               Fight.encounter_id.is_not(None))
        .distinct()
    ).scalars().all()
    return {canonical_encounter_id(int(eid)) for eid in raw_ids if eid is not None}


def _last_refresh_age_seconds(session: Session, canonical: int) -> float | None:
    last_updated = session.execute(
        select(func.max(FightModel.updated_at))
        .where(FightModel.encounter_id == canonical)
    ).scalar()
    if last_updated is None:
        return None
    # FightModel.updated_at is TIMESTAMPTZ → tz-aware
    return (datetime.now(timezone.utc) - last_updated).total_seconds()


def refresh_fight_model_for_encounter(
    session: Session, encounter_id: int, *,
    throttle_seconds: int = DEFAULT_THROTTLE_SECONDS,
    force: bool = False,
) -> dict[str, Any]:
    """Run persist → classify → annotate-cactbot for one canonical encounter.

    Returns: {encounter_id, skipped (str|None), persist?, classify?, annotate?, error?}.

    `skipped` reasons:
      - "throttle" — last refresh was < throttle_seconds ago
      - "no_data" — consensus produced no rows (need more kills with events)
      - "error_persist" / "error_classify" / "error_annotate" — captured below
    """
    canonical = canonical_encounter_id(encounter_id)
    out: dict[str, Any] = {"encounter_id": canonical}

    if not force:
        age = _last_refresh_age_seconds(session, canonical)
        if age is not None and age < throttle_seconds:
            out["skipped"] = "throttle"
            out["throttle_seconds"] = throttle_seconds
            out["last_refresh_age_seconds"] = round(age, 1)
            return out

    # Step 1: consensus → fight_model rows
    try:
        persist = write_consensus_to_fight_model(session, canonical)
        out["persist"] = persist
    except Exception as e:
        session.rollback()
        out["skipped"] = "error_persist"
        out["error"] = f"{type(e).__name__}: {e}"
        return out

    if persist.get("abilities_written", 0) == 0:
        # No canonical abilities yet (need ≥3 kills with events).
        # Don't classify/annotate on empty rows.
        out["skipped"] = "no_data"
        return out

    # Step 2: mechanic classification
    try:
        out["classify"] = classify_canonical_abilities(session, canonical)
    except Exception as e:
        session.rollback()
        out["skipped"] = "error_classify"
        out["error"] = f"{type(e).__name__}: {e}"
        return out

    # Step 3: cactbot annotation
    try:
        annotate = annotate_fight_model_for_encounter(session, canonical)
        session.commit()
        out["annotate"] = annotate
    except Exception as e:
        session.rollback()
        out["skipped"] = "error_annotate"
        out["error"] = f"{type(e).__name__}: {e}"
        return out

    out["skipped"] = None
    return out


def refresh_for_report(
    session: Session, report_code: str, *,
    throttle_seconds: int = DEFAULT_THROTTLE_SECONDS,
    force: bool = False,
) -> list[dict[str, Any]]:
    """Refresh every canonical encounter the report has fights for.

    Returns one summary per encounter touched. Empty list if the report
    has no ingested fights yet.
    """
    encounters = encounter_ids_for_report(session, report_code)
    return [
        refresh_fight_model_for_encounter(
            session, eid, throttle_seconds=throttle_seconds, force=force,
        )
        for eid in sorted(encounters)
    ]
