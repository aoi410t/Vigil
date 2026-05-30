"""T-009 dashboard: list-reports endpoint covers fight/kill/wipe counts.

The dev DB persists data ingested by `scripts/verify_*` runs; these tests
seed disposable rows under known codes and assert against the slice they
own, instead of wiping the whole table (events/combatants would orphan).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from api.main import app
from db.models import Fight, IngestionLedger, Report
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)

CODES = ("T009_A", "T009_B")


@pytest.fixture(autouse=True)
def _clean():
    yield
    with SessionLocal() as s:
        s.execute(delete(Fight).where(Fight.report_code.in_(CODES)))
        s.execute(delete(IngestionLedger).where(IngestionLedger.report_code.in_(CODES)))
        s.execute(delete(Report).where(Report.code.in_(CODES)))
        s.commit()


client = TestClient(app)


def _seed_report(s, code: str, *, kills: int, wipes: int, encounter: int):
    s.add(Report(
        code=code,
        start_time=datetime(2026, 5, 23, 18, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 5, 23, 21, 0, tzinfo=timezone.utc),
        ingested_at=datetime(2026, 5, 23, 21, 5, tzinfo=timezone.utc),
    ))
    s.flush()
    fid = 1
    for _ in range(kills):
        s.add(Fight(report_code=code, fight_id_in_report=fid,
                    encounter_id=encounter, is_kill=True))
        fid += 1
    for _ in range(wipes):
        s.add(Fight(report_code=code, fight_id_in_report=fid,
                    encounter_id=encounter, is_kill=False))
        fid += 1


def test_list_reports_returns_ok():
    r = client.get("/api/reports")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_list_reports_counts_match_seeded_rows():
    with SessionLocal() as s:
        _seed_report(s, "T009_A", kills=1, wipes=4, encounter=1079)
        _seed_report(s, "T009_B", kills=0, wipes=2, encounter=101)
        s.commit()
    rows = client.get("/api/reports").json()
    by_code = {r["code"]: r for r in rows}
    assert "T009_A" in by_code and "T009_B" in by_code
    assert by_code["T009_A"]["fight_count"] == 5
    assert by_code["T009_A"]["kill_count"] == 1
    assert by_code["T009_A"]["wipe_count"] == 4
    assert by_code["T009_A"]["encounter_id"] == 1079
    assert by_code["T009_B"]["kill_count"] == 0
    assert by_code["T009_B"]["wipe_count"] == 2
