"""v1.8.0: /api/me/encounters auto-detects the active encounter from the
current static's watchlist. Consumer Home reads this to pick what to focus
on.

Edge cases:
- No watched reports → empty list, active=null (Home renders onboarding).
- Multiple encounters → 'active' = most recently watched.
- Reports for encounters NOT in our watchlist must not surface (cross-static
  isolation, same invariant as the v1.6.0 multi-static tests).
"""
from __future__ import annotations

import base64
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from api.main import app
from db.models import (
    Fight, IngestionLedger, Report, Static, StaticMembership,
    User, WatchedReport,
)
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)


def _auth(username: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:test".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def user_with_static():
    """Disposable user + a fresh empty static they own. Yields
    (username, static_id) + cleans up its own scoped rows."""
    username = f"test_my_enc_{uuid.uuid4().hex[:8]}"
    client = TestClient(app)
    client.get("/api/me", headers=_auth(username))
    r = client.post("/api/statics", json={"name": f"S_{username}"},
                    headers=_auth(username))
    assert r.status_code == 201
    static_id = r.json()["id"]

    yield username, static_id

    # Teardown
    with SessionLocal() as s:
        u = s.execute(select(User).where(User.username == username)).scalar_one_or_none()
        codes = list(s.execute(select(WatchedReport.code).where(
            WatchedReport.static_id == static_id
        )).scalars().all())
        s.execute(delete(WatchedReport).where(WatchedReport.static_id == static_id))
        # Sweep Fight + Report only if they were created exclusively for this
        # test (codes start with 'T180_'). The fixture's own reports use that
        # prefix.
        test_codes = [c for c in codes if c.startswith("T180_")]
        if test_codes:
            fight_ids = list(s.execute(select(Fight.id).where(
                Fight.report_code.in_(test_codes)
            )).scalars().all())
            if fight_ids:
                s.execute(delete(Fight).where(Fight.id.in_(fight_ids)))
            s.execute(delete(IngestionLedger).where(
                IngestionLedger.report_code.in_(test_codes)))
            s.execute(delete(Report).where(Report.code.in_(test_codes)))
        s.execute(delete(StaticMembership).where(StaticMembership.static_id == static_id))
        s.execute(delete(Static).where(Static.id == static_id))
        if u is not None:
            u.current_static_id = None
            s.flush()
            s.execute(delete(StaticMembership).where(StaticMembership.user_id == u.id))
            s.delete(u)
        s.commit()


def _add_watched_with_fights(session, static_id: int, code: str,
                              encounter_id: int, pulls: int, kills: int,
                              latest_end_time: int) -> None:
    """Helper: add a watched report + Fight rows for it."""
    now = datetime.now(timezone.utc)
    session.add(Report(code=code, ingested_at=now))
    session.add(WatchedReport(static_id=static_id, code=code,
                               active=True, added_at=now))
    session.flush()
    for i in range(pulls):
        session.add(Fight(
            report_code=code, fight_id_in_report=i + 1,
            encounter_id=encounter_id,
            is_kill=(i < kills),
            start_time=0, end_time=latest_end_time - (pulls - i - 1) * 60_000,
            duration_ms=60_000,
        ))


def test_no_watched_reports_returns_empty(user_with_static):
    username, _ = user_with_static
    client = TestClient(app)
    r = client.get("/api/me/encounters", headers=_auth(username))
    assert r.status_code == 200
    body = r.json()
    assert body == {"active": None, "encounters": []}


def test_single_encounter_picks_it_as_active(user_with_static):
    username, sid = user_with_static
    with SessionLocal() as s:
        _add_watched_with_fights(s, sid, "T180_A", encounter_id=1079,
                                  pulls=5, kills=1, latest_end_time=10_000_000)
        s.commit()
    client = TestClient(app)
    body = client.get("/api/me/encounters", headers=_auth(username)).json()
    assert body["active"] == 1079
    assert len(body["encounters"]) == 1
    enc = body["encounters"][0]
    assert enc["encounter_id"] == 1079
    assert enc["pulls"] == 5
    assert enc["kills"] == 1
    assert enc["wipes"] == 4


def test_active_is_most_recently_watched(user_with_static):
    username, sid = user_with_static
    with SessionLocal() as s:
        # Older encounter
        _add_watched_with_fights(s, sid, "T180_OLD", encounter_id=101,
                                  pulls=20, kills=0,
                                  latest_end_time=1_000_000)
        # Newer encounter (smaller pull count, but more recent)
        _add_watched_with_fights(s, sid, "T180_NEW", encounter_id=1079,
                                  pulls=3, kills=0,
                                  latest_end_time=99_000_000)
        s.commit()
    client = TestClient(app)
    body = client.get("/api/me/encounters", headers=_auth(username)).json()
    # FRU is the most recently watched → active
    assert body["active"] == 1079
    eids = [e["encounter_id"] for e in body["encounters"]]
    # FRU listed first (sorted by latest_end_time desc)
    assert eids[0] == 1079
    assert 101 in eids


def test_other_statics_dont_leak(user_with_static):
    """A Fight in a report NOT watched by this static must not appear."""
    username, sid = user_with_static
    with SessionLocal() as s:
        # Add a Report + Fight that's NOT in this static's watchlist.
        now = datetime.now(timezone.utc)
        foreign_code = "T180_FOREIGN"
        s.add(Report(code=foreign_code, ingested_at=now))
        s.flush()
        s.add(Fight(
            report_code=foreign_code, fight_id_in_report=1,
            encounter_id=1068,  # TOP
            is_kill=False, start_time=0, end_time=99_999_999_999,
            duration_ms=60_000,
        ))
        # Also seed one watched report for this static so the user isn't empty.
        _add_watched_with_fights(s, sid, "T180_OURS", encounter_id=1079,
                                  pulls=2, kills=0, latest_end_time=10_000_000)
        s.commit()
        try:
            client = TestClient(app)
            body = client.get("/api/me/encounters", headers=_auth(username)).json()
            eids = [e["encounter_id"] for e in body["encounters"]]
            assert 1068 not in eids, "foreign-static report leaked into /api/me/encounters"
            assert 1079 in eids
            assert body["active"] == 1079
        finally:
            # Foreign rows cleanup (the OURS rows are cleaned by the fixture).
            s.execute(delete(Fight).where(Fight.report_code == foreign_code))
            s.execute(delete(IngestionLedger).where(
                IngestionLedger.report_code == foreign_code))
            s.execute(delete(Report).where(Report.code == foreign_code))
            s.commit()
