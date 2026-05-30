"""T-307 session report tests + live AC against M5S."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete

from analysis.session_report import generate_session_report
from db.models import (
    Combatant, Event, Fight, IngestionLedger, Report,
)
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)


def test_unknown_report_returns_note():
    with SessionLocal() as s:
        r = generate_session_report(s, "__NOPE__", 1)
    assert r["markdown"] == ""
    assert "note" in r


CODE = "T307_REP"
ENC = 50307


@pytest.fixture
def seeded():
    fight_ids = []
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        s.add(Report(code=CODE, ingested_at=now, start_time=now))
        s.flush()
        # 3 pulls: 1 kill, 2 wipes with deaths
        for i in range(3):
            f = Fight(report_code=CODE, fight_id_in_report=i + 1,
                      encounter_id=ENC, is_kill=(i == 0),
                      fight_percentage=0 if i == 0 else (20 + i * 10),
                      last_phase=5, start_time=i * 100_000,
                      end_time=i * 100_000 + 60_000, duration_ms=60_000)
            s.add(f)
            s.flush()
            fight_ids.append(f.id)
            s.add(Combatant(fight_id=f.id, player_id=1, name="P1", job="WAR"))
            s.add(Event(fight_id=f.id, ts=0, type="cast",
                        source_id=1, ability_game_id=999))
            if i > 0:
                s.add(Event(fight_id=f.id, ts=30_000, type="death",
                            source_id=9999, target_id=1, ability_game_id=555))
        s.commit()
        try:
            yield s
        finally:
            s.execute(delete(Event).where(Event.fight_id.in_(fight_ids)))
            s.execute(delete(Combatant).where(Combatant.fight_id.in_(fight_ids)))
            s.execute(delete(Fight).where(Fight.id.in_(fight_ids)))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code == CODE))
            s.execute(delete(Report).where(Report.code == CODE))
            s.commit()


def test_basic_summary_renders(seeded):
    session = seeded
    r = generate_session_report(session, CODE, 1)
    md = r["markdown"]
    assert CODE in md
    assert "3 pulls" in md
    assert "1K / 2W" in md
    assert r["pulls"] == 3
    assert r["kills"] == 1
    assert r["wipes"] == 2


def test_kill_present_marks_best_phase(seeded):
    session = seeded
    r = generate_session_report(session, CODE, 1)
    assert "KILL" in r["markdown"]


def test_top_killing_abilities_included(seeded):
    session = seeded
    r = generate_session_report(session, CODE, 1)
    assert "Top killing abilities" in r["markdown"]
    assert "ability 555" in r["markdown"]


# ---- Live AC on M5S ----

def test_live_m5s_session_report():
    with SessionLocal() as s:
        r = generate_session_report(s, "mVCt9aDdzq2Q8BLJ", 1)
    if not r.get("markdown"):
        pytest.skip("M5S report not in dev DB")
    md = r["markdown"]
    assert "mVCt9aDdzq2Q8BLJ" in md
    # Should show wipes/kills counts
    assert "pulls" in md
