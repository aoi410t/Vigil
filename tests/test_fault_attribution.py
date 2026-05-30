"""T-302 fault-attribution tests + live AC on M5S."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select

from analysis.fault_attribution import (
    _death_kind,
    classify_wipe_type,
    compute_fault_scores_for_fight,
    fault_aggregate_for_encounter,
    fault_scores_for_fight,
)
from db.models import (
    Combatant, Event, FaultScore, Fight, FightModel,
    IngestionLedger, Report, WatchedReport,
)
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)

ENC = 70302
CODES = ("T302_A",)


# ---- pure-function _death_kind (v1.12.0 signature: cascade_pressure float + mit_audit) ----

def test_death_kind_non_attributable_is_cascade():
    assert _death_kind(None, None, 0.0) == "cascade"


def test_death_kind_enrage():
    assert _death_kind(555, "enrage", 0.0) == "enrage"


def test_death_kind_tankbuster_is_root():
    assert _death_kind(555, "tankbuster", 0.0) == "root"


def test_death_kind_aoe_party_is_root():
    assert _death_kind(555, "aoe_party", 0.0) == "root"


def test_death_kind_raidwide_no_pressure_is_root():
    """First death to raidwide (no cascade pressure) = unmitigated → root."""
    assert _death_kind(555, "raidwide", 0.0) == "root"


def test_death_kind_raidwide_with_pressure_is_cascade():
    """Raidwide death with cascade pressure above threshold = collateral."""
    assert _death_kind(555, "raidwide", 0.9) == "cascade"


def test_death_kind_raidwide_below_threshold_is_root():
    """Pressure just below threshold keeps the death classified as root —
    the continuous decay isn't a 50/50 coin flip at exactly the threshold."""
    assert _death_kind(555, "raidwide", 0.49) == "root"


def test_death_kind_raidwide_mits_missed_is_mit_failure():
    """v1.12.0 mit-aware path: raidwide death where the plan had missed
    mits is mit_failure, not cascade."""
    audit = {"no_plan": False, "missed_count": 2, "planned_slots": []}
    assert _death_kind(555, "raidwide", 0.9, audit) == "mit_failure"


def test_death_kind_raidwide_mits_all_fired_is_heal_failure():
    """v1.16.0: plan fired completely + raidwide still killed → heal_failure
    (raidwide should be heal-survivable from full HP w/ mits up)."""
    audit = {"no_plan": False, "missed_count": 0, "planned_slots": []}
    assert _death_kind(555, "raidwide", 0.0, audit) == "heal_failure"


def test_death_kind_raidwide_no_plan_falls_through_to_pressure():
    """no_plan=True means consult cascade_pressure as the fallback."""
    audit = {"no_plan": True, "missed_count": 0, "planned_slots": []}
    assert _death_kind(555, "raidwide", 0.9, audit) == "cascade"
    assert _death_kind(555, "raidwide", 0.1, audit) == "root"


def test_death_kind_unknown_label():
    assert _death_kind(555, "cosmetic", 0.0) == "unknown"


# ---- end-to-end with seeded DB ----

@pytest.fixture
def seeded_wipe_with_model():
    """1 wipe pull: 3 players, 3 deaths — root tankbuster, then 2 cascade raidwides."""
    fight_id_holder = []
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        s.add(Report(code=CODES[0], ingested_at=now))
        s.flush()
        # v1.14.6: fault_aggregate_for_encounter scopes attendance via
        # WatchedReport (static_id=1) — fixture must mark the report watched.
        s.add(WatchedReport(static_id=1, code=CODES[0],
                             active=True, added_at=now))
        f = Fight(report_code=CODES[0], fight_id_in_report=1,
                  encounter_id=ENC, is_kill=False,
                  start_time=0, end_time=60_000, duration_ms=60_000)
        s.add(f)
        s.flush()
        fight_id_holder.append(f.id)
        BOSS = 9999
        for pid in (1, 2, 3):
            s.add(Combatant(fight_id=f.id, player_id=pid,
                            name=f"P{pid}", job="WAR"))
            s.add(Event(fight_id=f.id, ts=500, type="cast",
                        source_id=pid, ability_game_id=8888))
        # Tankbuster kills player 1 at t=10s
        s.add(Event(fight_id=f.id, ts=10_000, type="death",
                    source_id=BOSS, target_id=1, ability_game_id=111))
        # Raidwide cascades 1s and 2s later kill players 2 and 3
        s.add(Event(fight_id=f.id, ts=11_000, type="death",
                    source_id=BOSS, target_id=2, ability_game_id=222))
        s.add(Event(fight_id=f.id, ts=12_000, type="death",
                    source_id=BOSS, target_id=3, ability_game_id=222))
        # Seed fight_model rows so the classifier knows the labels
        s.add(FightModel(encounter_id=ENC, version=1, phase=0, seq=0,
                         ability_game_id=111, type_label="tankbuster",
                         relative_t_ms=10_000, time_variance_ms=0,
                         confidence=1.0, meta={}, updated_at=now))
        s.add(FightModel(encounter_id=ENC, version=1, phase=0, seq=1,
                         ability_game_id=222, type_label="raidwide",
                         relative_t_ms=11_000, time_variance_ms=0,
                         confidence=1.0, meta={}, updated_at=now))
        s.commit()
        try:
            yield s, f.id
        finally:
            s.execute(delete(FaultScore).where(FaultScore.fight_id.in_(fight_id_holder)))
            s.execute(delete(Event).where(Event.fight_id.in_(fight_id_holder)))
            s.execute(delete(Combatant).where(Combatant.fight_id.in_(fight_id_holder)))
            s.execute(delete(Fight).where(Fight.id.in_(fight_id_holder)))
            s.execute(delete(FightModel).where(FightModel.encounter_id == ENC))
            s.execute(delete(WatchedReport).where(WatchedReport.code.in_(CODES)))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code.in_(CODES)))
            s.execute(delete(Report).where(Report.code.in_(CODES)))
            s.commit()


def test_compute_classifies_root_vs_cascade(seeded_wipe_with_model):
    """v1.12.0 strict causality: tankbuster preceding a raidwide doesn't
    propagate cascade pressure (tankbuster is targeted, not raid-wounding).
    P1 root (tankbuster) → P2 root (raidwide, P1 doesn't pressure it)
    → P3 cascade (raidwide w/ preceding P2 raidwide at 0.8 pressure)."""
    session, fid = seeded_wipe_with_model
    summary = compute_fault_scores_for_fight(session, fid, 1)
    assert summary["labeled"] == 3
    assert summary["label_counts"]["root"] == 2
    assert summary["label_counts"]["cascade"] == 1


def test_persisted_scores_reflect_classification(seeded_wipe_with_model):
    session, fid = seeded_wipe_with_model
    compute_fault_scores_for_fight(session, fid, 1)
    body = fault_scores_for_fight(session, fid, 1)
    by_pid = {p["player_id"]: p for p in body["players"]}
    # Player 1 ate the tankbuster — root, score = 1.0
    assert by_pid[1]["score"] == pytest.approx(1.0)
    # Player 2 raidwide as first raid-wounding death — root under v1.12.0
    # strict causality, score = 1.0
    assert by_pid[2]["score"] == pytest.approx(1.0)
    # Player 3 raidwide with preceding P2 raidwide → cascade, score = 0.1
    assert by_pid[3]["score"] == pytest.approx(0.1)


def test_compute_is_idempotent(seeded_wipe_with_model):
    session, fid = seeded_wipe_with_model
    compute_fault_scores_for_fight(session, fid, 1)
    compute_fault_scores_for_fight(session, fid, 1)
    body = fault_scores_for_fight(session, fid, 1)
    assert len(body["players"]) == 3


def test_classify_wipe_type_body_check(seeded_wipe_with_model):
    """1 tankbuster + 2 raidwides → tank-buster makes up 33%, raidwide 67%
    → wipe_type='mechanics'."""
    session, fid = seeded_wipe_with_model
    r = classify_wipe_type(session, fid)
    assert r["wipe_type"] == "mechanics"
    assert r["deaths"] == 3


def test_kill_classified_as_kill():
    """is_kill=True → wipe_type='kill', regardless of death pattern."""
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        s.add(Report(code="T302_KILL", ingested_at=now))
        s.flush()
        f = Fight(report_code="T302_KILL", fight_id_in_report=1,
                  encounter_id=ENC, is_kill=True,
                  start_time=0, end_time=10, duration_ms=10)
        s.add(f)
        s.commit()
        try:
            r = classify_wipe_type(s, f.id)
            assert r["wipe_type"] == "kill"
        finally:
            s.execute(delete(Fight).where(Fight.id == f.id))
            s.execute(delete(IngestionLedger).where(
                IngestionLedger.report_code == "T302_KILL"))
            s.execute(delete(Report).where(Report.code == "T302_KILL"))
            s.commit()


def test_aggregate_for_encounter(seeded_wipe_with_model):
    """v1.12.0: P1 tankbuster=root, P2 raidwide=root (no raid-wounding
    preceding death), P3 raidwide=cascade (P2 raidwide preceded it)."""
    session, fid = seeded_wipe_with_model
    compute_fault_scores_for_fight(session, fid, 1)
    agg = fault_aggregate_for_encounter(session, ENC, 1)
    assert agg["wipes_aggregated"] == 1
    by_pid = {p["player_id"]: p for p in agg["players"]}
    assert by_pid[1]["root"] == 1
    assert by_pid[2]["root"] == 1
    assert by_pid[3]["cascade"] == 1


def test_aggregate_emits_member_resolution(seeded_wipe_with_model):
    """v1.15.1: each player row carries member_id/member_name when a roster
    alias matches the combatant. Lets the Home UI merge main + sub accounts
    into one row in the fault contributors table."""
    from db.models import CharacterAlias, Member
    session, fid = seeded_wipe_with_model
    compute_fault_scores_for_fight(session, fid, 1)
    # Seed: "Alice" owns P1; "Bob" owns P2 + P3 (main + sub-account).
    now = datetime.now(timezone.utc)
    alice = Member(static_id=1, name="Alice", kind="core", created_at=now)
    bob = Member(static_id=1, name="Bob", kind="core", created_at=now)
    session.add_all([alice, bob])
    session.flush()
    session.add_all([
        CharacterAlias(member_id=alice.id, character_name="P1", created_at=now),
        CharacterAlias(member_id=bob.id, character_name="P2", created_at=now),
        CharacterAlias(member_id=bob.id, character_name="P3", created_at=now),
    ])
    session.commit()
    try:
        agg = fault_aggregate_for_encounter(session, ENC, 1)
        by_pid = {p["player_id"]: p for p in agg["players"]}
        assert by_pid[1]["member_name"] == "Alice"
        assert by_pid[1]["member_id"] == alice.id
        # Bob's main + sub: both player rows resolve to the same member.
        assert by_pid[2]["member_name"] == "Bob"
        assert by_pid[3]["member_name"] == "Bob"
        assert by_pid[2]["member_id"] == by_pid[3]["member_id"] == bob.id
    finally:
        session.execute(delete(CharacterAlias).where(
            CharacterAlias.member_id.in_([alice.id, bob.id])))
        session.execute(delete(Member).where(
            Member.id.in_([alice.id, bob.id])))
        session.commit()


def test_unknown_fight_returns_note():
    with SessionLocal() as s:
        r = compute_fault_scores_for_fight(s, -1, 1)
    assert r["labeled"] == 0
    assert "note" in r


# ---- Live AC against M5S ----

def test_live_m5s_fault_compute_runs():
    """Compute fault scores on a real M5S wipe and verify the structure is
    populated. Doesn't assert verdicts (real data has noise) — just that the
    code runs end-to-end on live data."""
    with SessionLocal() as s:
        wipe_id = s.execute(
            select(Fight.id)
            .join(Event, Event.fight_id == Fight.id)
            .where(Fight.encounter_id == 101, Fight.is_kill.is_(False),
                   Event.type == "death")
            .distinct()
            .limit(1)
        ).scalar()
        if wipe_id is None:
            pytest.skip("no M5S wipe with deaths in dev DB")
        summary = compute_fault_scores_for_fight(s, wipe_id, 1)
        # Clean up the rows we created
        s.execute(delete(FaultScore).where(FaultScore.fight_id == wipe_id))
        s.commit()
    assert summary["labeled"] > 0
    total = sum(summary["label_counts"].values())
    assert total == summary["labeled"]
