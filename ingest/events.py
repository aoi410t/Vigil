"""Event normalization into the `events` table (T-005, PLAN §6 + §7).

For each report already ingested by T-004, pull the §7 dataTypes one at a time,
paginate on `nextPageTimestamp`, and store one row per event keyed on
`ability_game_id` (Invariant 2). The raw payload lives in `events.raw`.

Resumable via `ingestion_ledger.last_event_ts`: only events with `timestamp`
strictly after the stored cursor are fetched, so reruns on an `open` report
fetch only the new tail.

CombatantInfo events double-duty as the source for `combatants.stats` — when one
arrives, the corresponding combatant row's `stats` JSONB is filled from the raw.
"""
from __future__ import annotations

from typing import Any, Iterable, Protocol

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from db.models import Combatant, Event, Fight, IngestionLedger
from ingest.delta import IngestError

# Event types treated as evidence a combatant actually played in this fight.
# Same set the downstream "active players" filter uses (analysis/mechanic_classifier).
_ACTIVE_EVENT_TYPES: tuple[str, ...] = ("cast", "damage", "calculateddamage")

# §7 dataTypes we care about. Each entry is (dataType, hostilityType | None).
# FFLogs' `Casts` defaults to friendly casters; we add an explicit Enemies pass
# so M-WIPE / M-INFER can see boss casts (the analysis modules key on these).
DATA_TYPES: tuple[tuple[str, str | None], ...] = (
    ("DamageDone", None),
    ("DamageTaken", None),
    ("Casts", "Friendlies"),
    ("Casts", "Enemies"),
    ("Buffs", None),
    ("Debuffs", None),
    ("Deaths", None),
    ("CombatantInfo", None),
)


def _by_type_key(dtype: str, hostility: str | None) -> str:
    return f"{dtype}:{hostility}" if hostility else dtype


EVENTS_QUERY = """
query Events($code: String!, $dataType: EventDataType!, $startTime: Float!,
             $endTime: Float!, $fightIDs: [Int], $hostilityType: HostilityType) {
  reportData {
    report(code: $code) {
      events(
        dataType: $dataType
        startTime: $startTime
        endTime: $endTime
        fightIDs: $fightIDs
        hostilityType: $hostilityType
      ) {
        data
        nextPageTimestamp
      }
    }
  }
}
"""


class _GraphQLClient(Protocol):
    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]: ...


def _coerce_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _ability_id_from_event(ev: dict[str, Any]) -> int | None:
    """Pull the ability game id out of an event payload (Invariant 2).

    FFLogs uses `abilityGameID` in most event shapes. Death events sometimes
    carry the killing blow as `killingAbilityGameID` instead. Fall through both.
    """
    return (
        _coerce_int(ev.get("abilityGameID"))
        or _coerce_int(ev.get("killingAbilityGameID"))
    )


def prune_inactive_combatants(session: Session, fight_id: int) -> int:
    """Delete combatant rows whose player_id never appears as a source_id in
    this fight's cast/damage events (T-109).

    FFLogs `masterData.actors` on Ultimate reports lists every actor in the
    zone — ~12k per fight on FRU — and T-004 seeds combatants from that. This
    prune restores the invariant that `combatants` only contains actors who
    actually played in this specific fight, matching the active-players filter
    that T-203/T-206/T-207 already use downstream.

    Safety: no-op when the fight has no events yet (T-005 hasn't run for it),
    so we never delete the speculative roster before events exist.

    Returns the number of rows deleted.
    """
    has_events = session.execute(
        select(Event.id).where(Event.fight_id == fight_id).limit(1)
    ).first()
    if has_events is None:
        return 0

    active = set(
        session.execute(
            select(Event.source_id)
            .where(
                Event.fight_id == fight_id,
                Event.source_id.is_not(None),
                Event.type.in_(_ACTIVE_EVENT_TYPES),
            )
            .distinct()
        ).scalars().all()
    )
    if not active:
        return 0

    result = session.execute(
        delete(Combatant).where(
            Combatant.fight_id == fight_id,
            Combatant.player_id.not_in(active),
        )
    )
    return result.rowcount or 0


def ingest_events_for_report(
    session: Session,
    client: _GraphQLClient,
    code: str,
    *,
    data_types: Iterable[tuple[str, str | None]] = DATA_TYPES,
) -> dict[str, Any]:
    """Pull all configured dataTypes for `code`, normalize into `events`.

    Returns counters: total inserted, per-dataType breakdown, combatant_info
    updates applied, and the new ledger cursor.
    """
    ledger = session.get(IngestionLedger, code)
    if ledger is None:
        raise IngestError(
            f"No ingestion_ledger row for {code}; call ingest_report() first (T-004)."
        )

    fights: list[Fight] = (
        session.query(Fight).filter(Fight.report_code == code).all()
    )
    if not fights:
        return {
            "events_inserted": 0,
            "by_data_type": {},
            "combatant_info_updates": 0,
            "fights_seen": 0,
            "combatants_pruned": 0,
            "last_event_ts": ledger.last_event_ts or 0,
        }

    local_to_pk: dict[int, int] = {f.fight_id_in_report: f.id for f in fights}
    fight_ids_in_report = sorted(local_to_pk.keys())

    start_ts = ledger.last_event_ts or 0
    end_ts = max((f.end_time for f in fights if f.end_time is not None), default=0)
    if end_ts <= start_ts:
        return {
            "events_inserted": 0,
            "by_data_type": {},
            "combatant_info_updates": 0,
            "fights_seen": len(fights),
            "combatants_pruned": 0,
            "last_event_ts": start_ts,
        }

    inserted_total = 0
    by_type: dict[str, int] = {}
    ci_updates = 0
    max_ts_seen = start_ts

    for dtype, hostility in data_types:
        cursor = start_ts
        dtype_count = 0
        while True:
            resp = client.graphql_with_archive_retry(
                session,
                EVENTS_QUERY,
                {
                    "code": code,
                    "dataType": dtype,
                    "startTime": float(cursor),
                    "endTime": float(end_ts),
                    "fightIDs": fight_ids_in_report,
                    "hostilityType": hostility,
                },
            )
            page = (
                (((resp.get("reportData") or {}).get("report") or {}).get("events"))
                or {}
            )
            events_list: list[dict[str, Any]] = page.get("data") or []

            for ev in events_list:
                fight_local = _coerce_int(ev.get("fight"))
                if fight_local is None:
                    continue
                fight_pk = local_to_pk.get(fight_local)
                if fight_pk is None:
                    continue

                ts = _coerce_int(ev.get("timestamp"))
                if ts is not None and ts > max_ts_seen:
                    max_ts_seen = ts

                etype = ev.get("type")
                src = _coerce_int(ev.get("sourceID"))
                tgt = _coerce_int(ev.get("targetID"))
                ability = _ability_id_from_event(ev)
                amount = _coerce_int(ev.get("amount"))

                if etype == "combatantinfo" and src is not None:
                    combatant = session.get(Combatant, (fight_pk, src))
                    if combatant is not None:
                        combatant.stats = ev
                        ci_updates += 1

                session.add(
                    Event(
                        fight_id=fight_pk,
                        ts=ts,
                        type=etype,
                        source_id=src,
                        target_id=tgt,
                        ability_game_id=ability,
                        amount=amount,
                        raw=ev,
                    )
                )
                dtype_count += 1
                inserted_total += 1

            next_ts = page.get("nextPageTimestamp")
            if next_ts is None:
                break
            # FFLogs cursor is the next page's start; if it didn't advance, bail.
            next_cursor = _coerce_int(next_ts) or int(next_ts)
            if next_cursor <= cursor:
                break
            cursor = next_cursor

        by_type[_by_type_key(dtype, hostility)] = dtype_count
        session.flush()

    ledger.last_event_ts = max_ts_seen
    session.flush()

    pruned_total = 0
    for fight in fights:
        pruned_total += prune_inactive_combatants(session, fight.id)
    session.flush()

    return {
        "events_inserted": inserted_total,
        "by_data_type": by_type,
        "combatant_info_updates": ci_updates,
        "fights_seen": len(fights),
        "combatants_pruned": pruned_total,
        "last_event_ts": max_ts_seen,
    }
