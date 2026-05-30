"""T-207 M-GATE gated-vs-mechanics diagnostic tests."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select

from analysis.gate_diagnostic import gate_diagnostic_for_fight
from db.models import Combatant, Event, Fight, IngestionLedger, Report
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)

ENC = 8765432
CODES = ("T207_KILL_A", "T207_KILL_B", "T207_KILL_C", "T207_OUR_WIPE")


def _seed_kill(s, code: str, raid_dps_units: int):
    """Seed one kill pull with predictable raid-DPS so the target distribution
    is well-defined. 1 player x 100 events x raid_dps_units / 100s window."""
    s.add(Report(code=code, ingested_at=datetime.now(timezone.utc)))
    s.flush()
    f = Fight(report_code=code, fight_id_in_report=1,
              encounter_id=ENC, is_kill=True,
              start_time=0, end_time=100_000, duration_ms=100_000)
    s.add(f)
    s.flush()
    s.add(Combatant(fight_id=f.id, player_id=1, name="K", job="WAR"))
    for j in range(100):
        s.add(Event(fight_id=f.id, ts=j * 1000, type="damage",
                    source_id=1, target_id=9999,
                    ability_game_id=1, amount=raid_dps_units))
    return f.id


@pytest.fixture
def encounter_with_target():
    """3 kill pulls at raid-DPS ~1000 to establish a target distribution."""
    fight_ids = []
    with SessionLocal() as s:
        for code, dps_units in zip(CODES[:3], (900, 1000, 1100)):
            fight_ids.append(_seed_kill(s, code, dps_units))
        s.commit()
        try:
            yield s, fight_ids
        finally:
            s.execute(delete(Event).where(Event.fight_id.in_(fight_ids)))
            s.execute(delete(Combatant).where(Combatant.fight_id.in_(fight_ids)))
            s.execute(delete(Fight).where(Fight.id.in_(fight_ids)))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code.in_(CODES[:3])))
            s.execute(delete(Report).where(Report.code.in_(CODES[:3])))
            s.commit()


def _seed_our_wipe(s, *, raid_dps_units: int, num_deaths: int):
    """Our pull: 1 player, predictable DPS, optional deaths spread across phase."""
    s.add(Report(code="T207_OUR_WIPE", ingested_at=datetime.now(timezone.utc)))
    s.flush()
    f = Fight(report_code="T207_OUR_WIPE", fight_id_in_report=1,
              encounter_id=ENC, is_kill=False,
              start_time=0, end_time=100_000, duration_ms=100_000)
    s.add(f)
    s.flush()
    s.add(Combatant(fight_id=f.id, player_id=1, name="P1", job="WAR"))
    # Add the second player so we can have 2 deaths
    s.add(Combatant(fight_id=f.id, player_id=2, name="P2", job="PLD"))
    # Each player produces damage so _active_players includes them
    for pid in (1, 2):
        for j in range(100):
            s.add(Event(fight_id=f.id, ts=j * 1000, type="damage",
                        source_id=pid, target_id=9999,
                        ability_game_id=1, amount=raid_dps_units // 2))
    for d in range(num_deaths):
        s.add(Event(fight_id=f.id, ts=50_000 + d * 1000, type="death",
                    source_id=9999, target_id=(d % 2) + 1, ability_game_id=555))
    s.commit()
    return f.id


def test_dps_gated_when_below_p25_and_no_deaths(encounter_with_target):
    session, _ = encounter_with_target
    fid = _seed_our_wipe(session, raid_dps_units=500, num_deaths=0)
    try:
        r = gate_diagnostic_for_fight(session, fid)
        assert len(r["phases"]) >= 1
        verdict = r["phases"][0]["verdict"]
        assert verdict == "dps_gated", f"got {verdict!r}"
    finally:
        session.execute(delete(Event).where(Event.fight_id == fid))
        session.execute(delete(Combatant).where(Combatant.fight_id == fid))
        session.execute(delete(Fight).where(Fight.id == fid))
        session.execute(delete(IngestionLedger).where(IngestionLedger.report_code == "T207_OUR_WIPE"))
        session.execute(delete(Report).where(Report.code == "T207_OUR_WIPE"))
        session.commit()


def test_mechanics_gated_when_dps_ok_and_many_deaths(encounter_with_target):
    session, _ = encounter_with_target
    fid = _seed_our_wipe(session, raid_dps_units=1000, num_deaths=4)
    try:
        r = gate_diagnostic_for_fight(session, fid)
        verdict = r["phases"][0]["verdict"]
        assert verdict == "mechanics_gated", f"got {verdict!r}"
    finally:
        session.execute(delete(Event).where(Event.fight_id == fid))
        session.execute(delete(Combatant).where(Combatant.fight_id == fid))
        session.execute(delete(Fight).where(Fight.id == fid))
        session.execute(delete(IngestionLedger).where(IngestionLedger.report_code == "T207_OUR_WIPE"))
        session.execute(delete(Report).where(Report.code == "T207_OUR_WIPE"))
        session.commit()


def test_both_gated_when_low_dps_and_many_deaths(encounter_with_target):
    session, _ = encounter_with_target
    fid = _seed_our_wipe(session, raid_dps_units=500, num_deaths=4)
    try:
        r = gate_diagnostic_for_fight(session, fid)
        assert r["phases"][0]["verdict"] == "both_gated"
    finally:
        session.execute(delete(Event).where(Event.fight_id == fid))
        session.execute(delete(Combatant).where(Combatant.fight_id == fid))
        session.execute(delete(Fight).where(Fight.id == fid))
        session.execute(delete(IngestionLedger).where(IngestionLedger.report_code == "T207_OUR_WIPE"))
        session.execute(delete(Report).where(Report.code == "T207_OUR_WIPE"))
        session.commit()


def test_no_target_fallback_when_too_few_kills():
    """Encounter with <3 kills → no DPS target; verdict falls back to
    death-only (many_deaths / clean)."""
    with SessionLocal() as s:
        f_id = _seed_our_wipe(s, raid_dps_units=500, num_deaths=3)
        try:
            r = gate_diagnostic_for_fight(s, f_id)
            assert r["phases"][0]["verdict"] == "many_deaths"
        finally:
            s.execute(delete(Event).where(Event.fight_id == f_id))
            s.execute(delete(Combatant).where(Combatant.fight_id == f_id))
            s.execute(delete(Fight).where(Fight.id == f_id))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code == "T207_OUR_WIPE"))
            s.execute(delete(Report).where(Report.code == "T207_OUR_WIPE"))
            s.commit()


def test_unknown_fight_returns_note():
    with SessionLocal() as s:
        r = gate_diagnostic_for_fight(s, -1)
    assert r["phases"] == []
    assert r["note"] == "fight not found"


def test_live_fru_kill_gate_diagnostic():
    """For a FRU kill, expect mostly `not_gated` verdicts (they cleared)."""
    with SessionLocal() as s:
        # Find a FRU kill that actually has events ingested (T-201 backfilled
        # meta for many reports without events).
        fids_with_events = s.execute(
            select(Fight.id)
            .join(Event, Event.fight_id == Fight.id)
            .where(Fight.encounter_id == 1079, Fight.is_kill.is_(True))
            .distinct()
            .limit(1)
        ).scalar()
        if fids_with_events is None:
            pytest.skip("no FRU kill with events in dev DB")
        r = gate_diagnostic_for_fight(s, fids_with_events)
    assert len(r["phases"]) == 6
    verdicts = [p["verdict"] for p in r["phases"]]
    # On a kill, most phases should be not_gated. Some might be dps_gated due
    # to natural variance (this fight's DPS landed below the p25 of the rest).
    assert verdicts.count("mechanics_gated") + verdicts.count("both_gated") <= 1
