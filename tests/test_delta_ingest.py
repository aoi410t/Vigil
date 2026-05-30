"""Delta ingestion unit tests (T-004). Mocks the FFLogs client; uses live Postgres."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from db.models import Combatant, Fight, IngestionLedger, Report
from ingest.delta import IngestError, ingest_report, mark_report_complete


class FakeClient:
    def __init__(self, responses: list[dict[str, Any]]):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((query, variables))
        if not self.responses:
            raise AssertionError("no canned response left")
        return self.responses.pop(0)


def _payload(code: str, fights: list[dict[str, Any]], end_ms: int,
             actors: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "reportData": {
            "report": {
                "code": code,
                "title": "TestReport",
                "startTime": 1_000_000,
                "endTime": end_ms,
                "fights": fights,
                "masterData": {"actors": actors or []},
            }
        }
    }


def test_first_ingest_writes_report_fights_combatants(db_session):
    actors = [
        {"id": 1, "name": "Alice", "server": "S", "type": "Player", "subType": "PLD"},
        {"id": 2, "name": "Bob", "server": "S", "type": "Player", "subType": "WHM"},
        {"id": 99, "name": "Boss", "server": None, "type": "NPC", "subType": "Boss"},
    ]
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    fights = [
        {"id": 1, "encounterID": 88, "kill": False, "fightPercentage": 5.0,
         "lastPhase": 1, "startTime": end_ms - 600_000, "endTime": end_ms - 540_000},
        {"id": 2, "encounterID": 88, "kill": True, "fightPercentage": 0.0,
         "lastPhase": 5, "startTime": end_ms - 60_000, "endTime": end_ms},
    ]
    client = FakeClient([_payload("ABC_T4", fights, end_ms=end_ms, actors=actors)])
    result = ingest_report(db_session, client, "ABC_T4")
    assert result == {
        "new_fights": 2, "new_combatants": 4, "total_fights": 2,
        "status": "open", "was_no_op": False,
    }

    assert db_session.get(Report, "ABC_T4") is not None
    fights_in_db = db_session.query(Fight).filter_by(report_code="ABC_T4").all()
    assert {f.fight_id_in_report for f in fights_in_db} == {1, 2}
    assert all(f.duration_ms is not None for f in fights_in_db)

    combatants = (
        db_session.query(Combatant)
        .join(Fight, Combatant.fight_id == Fight.id)
        .filter(Fight.report_code == "ABC_T4")
        .all()
    )
    assert len(combatants) == 4
    assert {c.job for c in combatants} == {"PLD", "WHM"}
    assert not any(c.name == "Boss" for c in combatants)  # NPCs skipped

    ledger = db_session.get(IngestionLedger, "ABC_T4")
    assert ledger.status == "open"
    assert ledger.fights_ingested == [1, 2]


def test_rerun_complete_is_noop_no_graphql_call(db_session):
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    fights = [{"id": 1, "encounterID": 1, "kill": True, "fightPercentage": 0.0,
               "lastPhase": 1, "startTime": now_ms - 60_000, "endTime": now_ms}]
    actors = [{"id": 1, "name": "A", "server": "S", "type": "Player", "subType": "PLD"}]
    client = FakeClient([_payload("RR_T4", fights, end_ms=now_ms, actors=actors)])
    ingest_report(db_session, client, "RR_T4")
    assert mark_report_complete(db_session, "RR_T4") is True

    silent_client = FakeClient([])
    result = ingest_report(db_session, silent_client, "RR_T4")
    assert result["was_no_op"] is True
    assert result["new_fights"] == 0
    assert silent_client.calls == []
    assert db_session.query(Fight).filter_by(report_code="RR_T4").count() == 1


def test_rerun_open_inserts_only_new_fights(db_session):
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    fights_v1 = [
        {"id": 1, "encounterID": 1, "kill": False, "fightPercentage": 50.0,
         "lastPhase": 1, "startTime": now_ms - 100_000, "endTime": now_ms - 90_000},
        {"id": 2, "encounterID": 1, "kill": False, "fightPercentage": 30.0,
         "lastPhase": 2, "startTime": now_ms - 80_000, "endTime": now_ms - 70_000},
    ]
    actors = [{"id": 1, "name": "A", "server": "S", "type": "Player", "subType": "PLD"}]
    client = FakeClient([_payload("OP_T4", fights_v1, end_ms=now_ms - 70_000, actors=actors)])
    r1 = ingest_report(db_session, client, "OP_T4")
    assert r1["new_fights"] == 2 and r1["new_combatants"] == 2

    fights_v2 = fights_v1 + [
        {"id": 3, "encounterID": 1, "kill": True, "fightPercentage": 0.0,
         "lastPhase": 3, "startTime": now_ms - 60_000, "endTime": now_ms - 50_000},
    ]
    client2 = FakeClient([_payload("OP_T4", fights_v2, end_ms=now_ms - 50_000, actors=actors)])
    r2 = ingest_report(db_session, client2, "OP_T4")
    assert r2["new_fights"] == 1
    assert r2["new_combatants"] == 1
    assert r2["total_fights"] == 3

    fight_ids = {
        f.fight_id_in_report
        for f in db_session.query(Fight).filter_by(report_code="OP_T4").all()
    }
    assert fight_ids == {1, 2, 3}
    ledger = db_session.get(IngestionLedger, "OP_T4")
    assert ledger.fights_ingested == [1, 2, 3]


def test_auto_flip_to_complete_when_report_is_old(db_session):
    end_dt = datetime.now(timezone.utc) - timedelta(days=2)
    end_ms = int(end_dt.timestamp() * 1000)
    fights = [{"id": 1, "encounterID": 1, "kill": True, "fightPercentage": 0.0,
               "lastPhase": 1, "startTime": end_ms - 60_000, "endTime": end_ms}]
    client = FakeClient([_payload("OLD_T4", fights, end_ms=end_ms)])
    result = ingest_report(db_session, client, "OLD_T4")
    assert result["status"] == "complete"
    assert db_session.get(IngestionLedger, "OLD_T4").status == "complete"


def test_unknown_report_raises(db_session):
    client = FakeClient([{"reportData": {"report": None}}])
    with pytest.raises(IngestError):
        ingest_report(db_session, client, "NOPE_T4")


def test_idempotent_rerun_on_open_with_no_new_fights(db_session):
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    fights = [{"id": 1, "encounterID": 1, "kill": False, "fightPercentage": 50.0,
               "lastPhase": 1, "startTime": now_ms - 60_000, "endTime": now_ms}]
    actors = [{"id": 1, "name": "A", "server": "S", "type": "Player", "subType": "PLD"}]
    client = FakeClient([_payload("IDEM_T4", fights, end_ms=now_ms, actors=actors)])
    r1 = ingest_report(db_session, client, "IDEM_T4")
    assert r1["new_fights"] == 1

    # Same payload again; status stays open, no new rows
    client2 = FakeClient([_payload("IDEM_T4", fights, end_ms=now_ms, actors=actors)])
    r2 = ingest_report(db_session, client2, "IDEM_T4")
    assert r2["new_fights"] == 0
    assert r2["new_combatants"] == 0
    assert db_session.query(Fight).filter_by(report_code="IDEM_T4").count() == 1
    assert db_session.query(Combatant).count() >= 1  # unchanged
