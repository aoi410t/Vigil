"""Unit tests for event normalization (T-005). Mocked GraphQL, live Postgres."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from db.models import Combatant, Event, Fight, IngestionLedger, Report
from ingest.delta import IngestError, ingest_report
from ingest.events import DATA_TYPES, ingest_events_for_report


class FakeClient:
    def __init__(self, responses: dict[str, list[dict[str, Any]]]):
        # responses key: "<dataType>" or "<dataType>:<hostility>" — both supported.
        self.responses = {k: list(v) for k, v in responses.items()}
        # Used by ingest_report to set up the report+fights+combatants
        self._report_payload: dict[str, Any] | None = None
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def set_report_payload(self, payload: dict[str, Any]) -> None:
        self._report_payload = payload

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((query, variables))
        if variables and "dataType" not in variables and self._report_payload is not None:
            return self._report_payload
        dtype = variables.get("dataType") if variables else None
        hostility = variables.get("hostilityType") if variables else None
        key = f"{dtype}:{hostility}" if hostility else dtype
        pages = self.responses.get(key) or self.responses.get(dtype, [])
        if not pages:
            return {
                "reportData": {"report": {"events": {"data": [], "nextPageTimestamp": None}}}
            }
        page = pages.pop(0)
        return {"reportData": {"report": {"events": page}}}

    def graphql_with_archive_retry(self, session, query, variables=None):
        return self.graphql(query, variables)


def _report_payload(code: str, fights, end_ms: int, actors=None):
    return {
        "reportData": {
            "report": {
                "code": code,
                "title": "Test",
                "startTime": 1_000_000,
                "endTime": end_ms,
                "fights": fights,
                "masterData": {"actors": actors or []},
            }
        }
    }


def _seed_report(db_session, code: str, *, end_ms: int | None = None, actors=None):
    """Quickly set up report + fights + combatants via ingest_report() so events
    ingestion has something to attach to."""
    end_ms = end_ms or int(datetime.now(timezone.utc).timestamp() * 1000)
    fights = [
        {"id": 7, "encounterID": 1, "kill": False, "fightPercentage": 50.0,
         "lastPhase": 1, "startTime": end_ms - 120_000, "endTime": end_ms - 60_000},
        {"id": 8, "encounterID": 1, "kill": True, "fightPercentage": 0.0,
         "lastPhase": 3, "startTime": end_ms - 50_000, "endTime": end_ms},
    ]
    actors = actors or [
        {"id": 1, "name": "Alice", "server": "S", "type": "Player", "subType": "PLD"},
        {"id": 2, "name": "Bob", "server": "S", "type": "Player", "subType": "WHM"},
    ]
    client = FakeClient(responses={})
    client.set_report_payload(_report_payload(code, fights, end_ms, actors))
    ingest_report(db_session, client, code)
    return [f for f in db_session.query(Fight).filter_by(report_code=code).all()]


def test_ingest_events_writes_all_data_types(db_session):
    fights = _seed_report(db_session, "EV_T5_A")

    # One page per dataType, two events each, distinguishable by ability_game_id.
    def _page(events):
        return {"data": events, "nextPageTimestamp": None}

    responses = {
        "DamageDone": [_page([
            {"timestamp": 1000, "type": "damage", "sourceID": 1, "targetID": 99,
             "abilityGameID": 1001, "amount": 5000, "fight": 7},
            {"timestamp": 1100, "type": "damage", "sourceID": 2, "targetID": 99,
             "abilityGameID": 1002, "amount": 3000, "fight": 7},
        ])],
        "DamageTaken": [_page([
            {"timestamp": 1500, "type": "damage", "sourceID": 99, "targetID": 1,
             "abilityGameID": 2001, "amount": 8000, "fight": 7},
        ])],
        "Casts:Friendlies": [_page([
            {"timestamp": 2000, "type": "cast", "sourceID": 1, "targetID": 99,
             "abilityGameID": 3001, "fight": 7},
        ])],
        "Casts:Enemies": [_page([])],  # explicit empty page so the pass is queried
        "Buffs": [_page([
            {"timestamp": 2500, "type": "applybuff", "sourceID": 1, "targetID": 1,
             "abilityGameID": 4001, "fight": 7},
        ])],
        "Debuffs": [_page([
            {"timestamp": 3000, "type": "applydebuff", "sourceID": 1, "targetID": 99,
             "abilityGameID": 5001, "fight": 7},
        ])],
        "Deaths": [_page([
            {"timestamp": 3500, "type": "death", "sourceID": 1, "targetID": 1,
             "killingAbilityGameID": 6001, "fight": 7},
        ])],
        "CombatantInfo": [_page([
            {"timestamp": 0, "type": "combatantinfo", "sourceID": 1, "fight": 7,
             "stats": {"sks": 2470, "sps": 0}},
        ])],
    }
    client = FakeClient(responses=responses)
    result = ingest_events_for_report(db_session, client, "EV_T5_A")

    assert result["events_inserted"] == 8
    expected_keys = {
        f"{d}:{h}" if h else d for d, h in DATA_TYPES
    }
    assert set(result["by_data_type"].keys()) == expected_keys
    assert result["combatant_info_updates"] == 1
    assert result["last_event_ts"] == 3500

    # Spot-check normalization: ability IDs preserved for each type.
    abilities = {
        e.type: e.ability_game_id
        for e in db_session.query(Event).filter(Event.fight_id == fights[0].id).all()
    }
    assert abilities["damage"] in (1001, 2001)
    assert abilities["cast"] == 3001
    assert abilities["applybuff"] == 4001
    assert abilities["applydebuff"] == 5001
    assert abilities["death"] == 6001  # pulled from killingAbilityGameID fallback

    # CombatantInfo updated combatant.stats
    combatant = db_session.get(Combatant, (fights[0].id, 1))
    assert combatant.stats is not None
    assert combatant.stats["stats"]["sks"] == 2470

    # Ledger cursor advanced
    ledger = db_session.get(IngestionLedger, "EV_T5_A")
    assert ledger.last_event_ts == 3500


def test_pagination_follows_nextPageTimestamp(db_session):
    _seed_report(db_session, "EV_T5_PAG")
    pages = [
        {"data": [
            {"timestamp": 100, "type": "damage", "sourceID": 1, "targetID": 99,
             "abilityGameID": 10, "amount": 100, "fight": 7},
        ], "nextPageTimestamp": 200},
        {"data": [
            {"timestamp": 250, "type": "damage", "sourceID": 1, "targetID": 99,
             "abilityGameID": 11, "amount": 200, "fight": 7},
        ], "nextPageTimestamp": 300},
        {"data": [
            {"timestamp": 350, "type": "damage", "sourceID": 1, "targetID": 99,
             "abilityGameID": 12, "amount": 300, "fight": 7},
        ], "nextPageTimestamp": None},
    ]
    client = FakeClient(responses={"DamageDone": pages})
    result = ingest_events_for_report(db_session, client, "EV_T5_PAG")
    assert result["by_data_type"]["DamageDone"] == 3
    assert result["last_event_ts"] == 350


def test_resume_skips_already_seen(db_session):
    _seed_report(db_session, "EV_T5_RES")
    # First pass: timestamps 0-500
    pages_1 = [{"data": [
        {"timestamp": 100, "type": "damage", "sourceID": 1, "targetID": 99,
         "abilityGameID": 10, "amount": 100, "fight": 7},
        {"timestamp": 500, "type": "damage", "sourceID": 1, "targetID": 99,
         "abilityGameID": 11, "amount": 100, "fight": 7},
    ], "nextPageTimestamp": None}]
    client = FakeClient(responses={"DamageDone": pages_1})
    ingest_events_for_report(db_session, client, "EV_T5_RES")
    ledger = db_session.get(IngestionLedger, "EV_T5_RES")
    assert ledger.last_event_ts == 500

    # Second pass: API only returns events AFTER cursor (we simulate by handing
    # back the new tail). Resume should pass startTime=500 to the API.
    pages_2 = [{"data": [
        {"timestamp": 800, "type": "damage", "sourceID": 1, "targetID": 99,
         "abilityGameID": 12, "amount": 100, "fight": 7},
    ], "nextPageTimestamp": None}]
    client2 = FakeClient(responses={"DamageDone": pages_2})
    result = ingest_events_for_report(db_session, client2, "EV_T5_RES")
    assert result["by_data_type"]["DamageDone"] == 1
    # First events call should carry startTime=500
    first_events_call = next(c for c in client2.calls if c[1] and "dataType" in c[1])
    assert first_events_call[1]["startTime"] == 500.0


def test_missing_ledger_raises(db_session):
    client = FakeClient(responses={})
    with pytest.raises(IngestError):
        ingest_events_for_report(db_session, client, "NEVER_INGESTED_T5")


def test_events_with_unknown_fight_are_skipped(db_session):
    _seed_report(db_session, "EV_T5_SKIP")
    pages = [{"data": [
        {"timestamp": 100, "type": "damage", "sourceID": 1, "targetID": 99,
         "abilityGameID": 10, "amount": 100, "fight": 7},
        # fight=999 doesn't exist; should be ignored
        {"timestamp": 200, "type": "damage", "sourceID": 1, "targetID": 99,
         "abilityGameID": 11, "amount": 100, "fight": 999},
    ], "nextPageTimestamp": None}]
    client = FakeClient(responses={"DamageDone": pages})
    result = ingest_events_for_report(db_session, client, "EV_T5_SKIP")
    assert result["by_data_type"]["DamageDone"] == 1


def test_cursor_not_advancing_breaks_loop(db_session):
    """If FFLogs returns the same nextPageTimestamp twice, we bail rather than
    spin forever."""
    _seed_report(db_session, "EV_T5_LOOP")
    # nextPageTimestamp returns same value as before — defensive break required.
    pages = [
        {"data": [{"timestamp": 100, "type": "damage", "sourceID": 1, "targetID": 99,
                   "abilityGameID": 10, "amount": 100, "fight": 7}],
         "nextPageTimestamp": 0},  # 0 <= cursor (0) → break
    ]
    client = FakeClient(responses={"DamageDone": pages})
    result = ingest_events_for_report(db_session, client, "EV_T5_LOOP")
    assert result["by_data_type"]["DamageDone"] == 1
