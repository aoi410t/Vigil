"""XIVAPI client + ability bootstrap (T-108, PLAN §11).

Pulls FFXIV action/status metadata from XIVAPI keyed on `ability_game_id` so
rows join straight to FFLogs events (Invariant 2). The Action and Status
namespaces overlap on some IDs — we pick the namespace matching the dominant
event type for each ID in our `events` table (cast/damage → action; buff/debuff
events → status).

The classic xivapi.com endpoints (`/Action/{id}`, `/Status/{id}`) are stable
and don't require auth. Polite request pacing (`min_interval_s`) keeps us off
their rate limiter. Rows already present in `abilities` are skipped by default;
pass `force=True` to refresh.
"""
from __future__ import annotations

import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Ability, Event

XIVAPI_BASE = "https://xivapi.com"
DEFAULT_TIMEOUT_S = 10.0
DEFAULT_MIN_INTERVAL_S = 0.06  # ≈ 16 req/s, well under XIVAPI's posted ~20

# FFLogs event types that imply the ability_game_id is a *status* (buff/debuff)
# rather than an *action*.
STATUS_EVENT_TYPES = frozenset({
    "applybuff", "removebuff", "refreshbuff", "applybuffstack", "removebuffstack",
    "applydebuff", "removedebuff", "applydebuffstack", "removedebuffstack",
    "refreshdebuff",
})
# Event types whose ability_game_id is always an action.
ACTION_EVENT_TYPES = frozenset({
    "cast", "begincast", "damage", "calculateddamage", "death",
})

# FFLogs synthesizes ability IDs by offsetting the underlying XIVAPI id:
#   status:  fflogs_id = xivapi_status_id + 1_000_000
#   ~500k:   environmental/fall damage that doesn't map back to XIVAPI cleanly.
FFLOGS_STATUS_OFFSET = 1_000_000
FFLOGS_STATUS_OFFSET_MAX = 2_000_000


def xivapi_status_id_from_fflogs(fflogs_id: int) -> int | None:
    """Strip the FFLogs +1,000,000 offset on status applybuff/refresh events."""
    if FFLOGS_STATUS_OFFSET <= fflogs_id < FFLOGS_STATUS_OFFSET_MAX:
        return fflogs_id - FFLOGS_STATUS_OFFSET
    return None


class XIVAPIClient:
    """Thin HTTP client with built-in pacing and 404 handling.

    `fetch_action` / `fetch_status` return the parsed JSON dict on 200, `None`
    on 404, and raise on anything else. The pacing happens per-request, not
    just on hits, so 404s still count toward the rate limit.
    """

    def __init__(
        self,
        base_url: str = XIVAPI_BASE,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url, timeout=timeout_s, transport=transport
        )
        self._min_interval_s = min_interval_s
        self._last_call_at = 0.0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "XIVAPIClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _pace(self) -> None:
        if self._min_interval_s <= 0:
            return
        elapsed = time.monotonic() - self._last_call_at
        wait = self._min_interval_s - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_call_at = time.monotonic()

    def _get(self, path: str) -> dict[str, Any] | None:
        self._pace()
        r = self._client.get(path)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def fetch_action(self, ability_id: int) -> dict[str, Any] | None:
        return self._get(f"/Action/{ability_id}")

    def fetch_status(self, ability_id: int) -> dict[str, Any] | None:
        return self._get(f"/Status/{ability_id}")


def classify_namespace(event_type_counts: dict[str, int]) -> str:
    """Pick 'action' vs 'status' based on the dominant event type for this ID."""
    if not event_type_counts:
        return "unknown"
    status_score = sum(c for t, c in event_type_counts.items() if t in STATUS_EVENT_TYPES)
    action_score = sum(c for t, c in event_type_counts.items() if t in ACTION_EVENT_TYPES)
    if status_score > action_score:
        return "status"
    if action_score > 0:
        return "action"
    return "unknown"


def distinct_ability_ids(session: Session) -> dict[int, dict[str, int]]:
    """Return `{ability_game_id: {event_type: count, …}}` over all stored events."""
    rows = session.execute(
        select(Event.ability_game_id, Event.type)
        .where(Event.ability_game_id.is_not(None))
    ).all()
    out: dict[int, Counter] = defaultdict(Counter)
    for ability_id, etype in rows:
        out[ability_id][etype or ""] += 1
    return {aid: dict(c) for aid, c in out.items()}


def _row_from_payload(payload: dict[str, Any], kind: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "name": payload.get("Name_en") or payload.get("Name"),
        "description": (payload.get("Description_en") or payload.get("Description")
                        or "").strip() or None,
        "icon": payload.get("Icon"),
        "raw": payload,
        "fetched_at": datetime.now(timezone.utc),
    }


def fetch_one(
    client: XIVAPIClient, ability_id: int, event_type_counts: dict[str, int]
) -> tuple[str, dict[str, Any] | None]:
    """Fetch a single ability from the appropriate XIVAPI namespace.

    Returns (kind, payload). `kind` is what we stored — 'action', 'status', or
    'unknown' if both endpoints 404'd. `payload` is the row dict for insert/update.

    Handles FFLogs' +1,000,000 status-id offset: an FFLogs ability id in the
    [1M, 2M) range is a Status with the offset stripped. We try that lookup
    first when applicable, then fall back to plain Action/Status.
    """
    offset_status = xivapi_status_id_from_fflogs(ability_id)
    if offset_status is not None:
        payload = client.fetch_status(offset_status)
        if payload is not None:
            return "status", _row_from_payload(payload, "status")
        # Fall through — synthetic ID but XIVAPI doesn't recognize the stripped id.

    primary = classify_namespace(event_type_counts)
    order: list[str] = (
        ["status", "action"] if primary == "status"
        else ["action", "status"]
    )
    for kind in order:
        fetcher = client.fetch_action if kind == "action" else client.fetch_status
        payload = fetcher(ability_id)
        if payload is not None:
            return kind, _row_from_payload(payload, kind)
    return "unknown", {
        "kind": "unknown",
        "name": None,
        "description": None,
        "icon": None,
        "raw": None,
        "fetched_at": datetime.now(timezone.utc),
    }


def bootstrap_abilities_from_events(
    session: Session,
    client: XIVAPIClient,
    *,
    force: bool = False,
    only_ids: Iterable[int] | None = None,
) -> dict[str, int]:
    """Populate `abilities` for every distinct ability id in `events`.

    Skips IDs already in `abilities` unless `force=True`. Returns a small
    summary dict for the verify script: `fetched_action`, `fetched_status`,
    `unknown`, `skipped`, `total_seen`.
    """
    id_to_types = distinct_ability_ids(session)
    if only_ids is not None:
        keep = set(only_ids)
        id_to_types = {k: v for k, v in id_to_types.items() if k in keep}

    existing: set[int] = set(
        session.execute(select(Ability.ability_game_id)).scalars().all()
    )

    summary = {
        "fetched_action": 0,
        "fetched_status": 0,
        "unknown": 0,
        "skipped": 0,
        "total_seen": len(id_to_types),
    }

    for ability_id, type_counts in id_to_types.items():
        if not force and ability_id in existing:
            summary["skipped"] += 1
            continue
        kind, row = fetch_one(client, ability_id, type_counts)
        if kind == "action":
            summary["fetched_action"] += 1
        elif kind == "status":
            summary["fetched_status"] += 1
        else:
            summary["unknown"] += 1

        existing_row = session.get(Ability, ability_id)
        if existing_row is None:
            session.add(Ability(ability_game_id=ability_id, **row))
        else:
            for k, v in row.items():
                setattr(existing_row, k, v)
        session.commit()

    return summary
