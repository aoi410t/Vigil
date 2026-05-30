"""v1.17.1: fight_model auto-refresh.

Tests the helper directly (throttle / no_data / failure semantics) and
verifies the poll path's refresh wire-in only fires when new data was
ingested.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import delete, select

from analysis import _encounter as enc_mod
from analysis.fight_model_refresh import (
    DEFAULT_THROTTLE_SECONDS,
    encounter_ids_for_report,
    refresh_fight_model_for_encounter,
    refresh_for_report,
)
from db.models import (
    Combatant, Event, Fight, FightModel, IngestionLedger, Report,
    WatchedReport,
)
from db.session import SessionLocal, engine
from jobs.poll_watched import _poll_one_row

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)

# Fake test-only clone group — added via monkeypatch so we never touch
# real DSR rows in the dev DB.
FAKE_CANONICAL = 991_017
FAKE_LEGACY = 991_018
FAKE_NONCLONE = 991_019


@pytest.fixture
def fake_clone_group(monkeypatch):
    """Add (FAKE_CANONICAL, FAKE_LEGACY) to the cloned-group lookup."""
    monkeypatch.setitem(enc_mod._CANONICAL_OF, FAKE_CANONICAL, FAKE_CANONICAL)
    monkeypatch.setitem(enc_mod._CANONICAL_OF, FAKE_LEGACY, FAKE_CANONICAL)
    monkeypatch.setitem(enc_mod._GROUP_OF, FAKE_CANONICAL,
                        (FAKE_CANONICAL, FAKE_LEGACY))
    monkeypatch.setitem(enc_mod._GROUP_OF, FAKE_LEGACY,
                        (FAKE_CANONICAL, FAKE_LEGACY))
    yield


def _seed_kill(s, code: str, fight_id_in_report: int, enc_id: int, *,
               boss_cast_aids: list[int]):
    """Seed a kill fight that consensus can detect a phase from. Mirrors
    the v1.17.0 canonical-merge test seed."""
    now = datetime.now(timezone.utc)
    if s.get(Report, code) is None:
        s.add(Report(code=code, ingested_at=now))
        s.flush()
        s.add(IngestionLedger(
            report_code=code, fights_ingested=[fight_id_in_report],
            last_event_ts=0, status="open", last_polled_at=now,
        ))
        s.flush()
    f = Fight(
        report_code=code, fight_id_in_report=fight_id_in_report,
        encounter_id=enc_id, is_kill=True,
        fight_percentage=0.0, last_phase=0,
        start_time=0, end_time=60_000, duration_ms=60_000,
    )
    s.add(f); s.flush()
    boss_target_id = 100_000
    boss_source_id = 100_000
    for pid in range(1, 9):
        s.add(Combatant(fight_id=f.id, player_id=pid, name=f"p{pid}",
                        job="DRG", server=None))
    for i in range(30):
        s.add(Event(fight_id=f.id, ts=i * 1_000, type="damage",
                    source_id=1, target_id=boss_target_id,
                    ability_game_id=8888, amount=100, raw={}))
    for pid in range(1, 9):
        s.add(Event(fight_id=f.id, ts=1_000, type="damage",
                    source_id=pid, target_id=boss_target_id,
                    ability_game_id=8888, amount=1000, raw={}))
    for k, aid in enumerate(boss_cast_aids):
        s.add(Event(fight_id=f.id, ts=5_000 + k * 1_000, type="cast",
                    source_id=boss_source_id, target_id=None,
                    ability_game_id=aid, amount=None, raw={}))
    s.flush()
    return f.id


def _cleanup_codes(s, codes: list[str]):
    fids = list(s.execute(
        select(Fight.id).where(Fight.report_code.in_(codes))
    ).scalars().all())
    if fids:
        s.execute(delete(Event).where(Event.fight_id.in_(fids)))
        s.execute(delete(Combatant).where(Combatant.fight_id.in_(fids)))
        s.execute(delete(Fight).where(Fight.id.in_(fids)))
    s.execute(delete(IngestionLedger).where(IngestionLedger.report_code.in_(codes)))
    s.execute(delete(Report).where(Report.code.in_(codes)))


@pytest.fixture
def three_fake_kills(fake_clone_group):
    """3 kills across both halves of the fake clone group — enough for
    consensus to fire."""
    code_a = "T171_REFRESH_A"
    code_b = "T171_REFRESH_B"
    code_c = "T171_REFRESH_C"
    codes = [code_a, code_b, code_c]
    with SessionLocal() as s:
        _cleanup_codes(s, codes)
        s.execute(delete(FightModel).where(FightModel.encounter_id.in_(
            (FAKE_CANONICAL, FAKE_LEGACY))))
        s.commit()
        casts = [80_001, 80_002, 80_003]
        _seed_kill(s, code_a, 1, FAKE_CANONICAL, boss_cast_aids=casts)
        _seed_kill(s, code_b, 1, FAKE_LEGACY, boss_cast_aids=casts)
        _seed_kill(s, code_c, 1, FAKE_LEGACY, boss_cast_aids=casts)
        s.commit()
        try:
            yield s, codes
        finally:
            _cleanup_codes(s, codes)
            s.execute(delete(FightModel).where(FightModel.encounter_id.in_(
                (FAKE_CANONICAL, FAKE_LEGACY))))
            s.commit()


# --- refresh helper -------------------------------------------------------

def test_refresh_end_to_end_runs_all_three_steps(three_fake_kills):
    s, _ = three_fake_kills
    result = refresh_fight_model_for_encounter(s, FAKE_LEGACY, force=True)
    assert result["encounter_id"] == FAKE_CANONICAL
    assert result["skipped"] is None
    assert result["persist"]["abilities_written"] >= 3
    assert result["classify"]["labeled"] >= 3
    # Cactbot has no timeline for FAKE_CANONICAL → returns 0 annotated.
    assert "annotate" in result
    assert "annotated" in result["annotate"]

    # fight_model rows now exist under canonical
    rows = s.execute(
        select(FightModel).where(FightModel.encounter_id == FAKE_CANONICAL)
    ).scalars().all()
    assert len(rows) >= 3


def test_refresh_throttle_skips_recent_runs(three_fake_kills):
    s, _ = three_fake_kills
    first = refresh_fight_model_for_encounter(s, FAKE_CANONICAL, force=True)
    assert first["skipped"] is None

    # Immediately re-run with default 60s throttle — should skip.
    second = refresh_fight_model_for_encounter(s, FAKE_CANONICAL)
    assert second["skipped"] == "throttle"
    assert second["throttle_seconds"] == DEFAULT_THROTTLE_SECONDS
    assert second["last_refresh_age_seconds"] < DEFAULT_THROTTLE_SECONDS

    # force=True bypasses the throttle.
    third = refresh_fight_model_for_encounter(s, FAKE_CANONICAL, force=True)
    assert third["skipped"] is None


def test_refresh_no_data_when_too_few_kills(fake_clone_group):
    """Encounter with zero kills returns skipped=no_data, doesn't crash."""
    with SessionLocal() as s:
        s.execute(delete(FightModel).where(FightModel.encounter_id.in_(
            (FAKE_CANONICAL, FAKE_LEGACY))))
        s.commit()
        result = refresh_fight_model_for_encounter(s, FAKE_LEGACY, force=True)
        assert result["encounter_id"] == FAKE_CANONICAL
        assert result["skipped"] == "no_data"
        assert result["persist"]["abilities_written"] == 0


def test_refresh_for_report_returns_one_per_encounter(three_fake_kills):
    s, codes = three_fake_kills
    # All 3 seeded reports point at the FAKE clone group → one canonical eid.
    results = refresh_for_report(s, codes[0], force=True)
    assert len(results) == 1
    assert results[0]["encounter_id"] == FAKE_CANONICAL


def test_encounter_ids_for_report_canonicalizes(three_fake_kills):
    s, codes = three_fake_kills
    # codes[1] was seeded under FAKE_LEGACY — must surface as FAKE_CANONICAL.
    eids = encounter_ids_for_report(s, codes[1])
    assert eids == {FAKE_CANONICAL}


def test_refresh_for_unknown_report_returns_empty():
    with SessionLocal() as s:
        results = refresh_for_report(s, "NO_SUCH_CODE_171", force=True)
    assert results == []


# --- poll path wire-in ----------------------------------------------------

def test_poll_skips_refresh_when_no_new_data():
    """A poll that ingests zero new fights/events doesn't trigger refresh.
    Uses an already-`complete` ledger to short-circuit the network call."""
    code = "T171_POLL_COMPLETE"
    with SessionLocal() as s:
        try:
            s.add(Report(code=code, ingested_at=datetime.now(timezone.utc)))
            s.flush()
            s.add(IngestionLedger(report_code=code, status="complete",
                                  fights_ingested=[1, 2], last_event_ts=1000,
                                  last_polled_at=datetime.now(timezone.utc)))
            w = WatchedReport(static_id=1, code=code, active=True,
                              added_at=datetime.now(timezone.utc))
            s.add(w)
            s.commit()

            client = MagicMock()
            entry = _poll_one_row(s, client, w)
            assert entry["status"] == "skipped_complete"
            # No refresh field because the early-return path doesn't even
            # consider it.
            assert "fight_model_refresh" not in entry
            client.graphql.assert_not_called()
        finally:
            s.execute(delete(WatchedReport).where(WatchedReport.code == code))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code == code))
            s.execute(delete(Report).where(Report.code == code))
            s.commit()


def test_poll_auto_refresh_can_be_disabled():
    """The `auto_refresh_fight_model=False` knob bypasses the refresh wire-in
    even when new events were ingested. Useful for tests + one-shots."""
    code = "T171_POLL_DISABLED"
    with SessionLocal() as s:
        try:
            s.add(Report(code=code, ingested_at=datetime.now(timezone.utc)))
            s.flush()
            s.add(IngestionLedger(report_code=code, status="complete",
                                  fights_ingested=[], last_event_ts=0,
                                  last_polled_at=datetime.now(timezone.utc)))
            w = WatchedReport(static_id=1, code=code, active=True,
                              added_at=datetime.now(timezone.utc))
            s.add(w)
            s.commit()

            entry = _poll_one_row(s, MagicMock(), w,
                                  auto_refresh_fight_model=False)
            assert entry["status"] == "skipped_complete"
            assert "fight_model_refresh" not in entry
        finally:
            s.execute(delete(WatchedReport).where(WatchedReport.code == code))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code == code))
            s.execute(delete(Report).where(Report.code == code))
            s.commit()
