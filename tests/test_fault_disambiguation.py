"""T-304 fault-disambiguation tests.

v1.12.0 note: mit-aware classification was moved upstream into T-302's
`_death_kind`, so T-304 now finds nothing to upgrade in the normal flow
(its work is done by the initial compute). T-304 remains as a backward-
compat pass for fault_scores rows persisted before v1.12.0. These tests
verify the final state is correct regardless of which pass produced
the mit_failure classification."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select

from analysis.fault_attribution import compute_fault_scores_for_fight
from analysis.fault_disambiguation import disambiguate_for_fight
from analysis.strat_config import encode_mechanic_ref, upsert as strat_upsert
from db.models import (
    Combatant, Event, FaultScore, Fight, FightModel,
    IngestionLedger, Report, StratConfig,
)
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)

ENC = 50304
CODE = "T304_TEST"


@pytest.fixture
def seeded():
    """One wipe with two raidwide casts:
      - First: planned mit FIRED, player A dies cascade → stays cascade
      - Second: planned mit MISSED, player B dies cascade → should upgrade to mit_failure
    """
    fid_holder = []
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        s.add(Report(code=CODE, ingested_at=now))
        s.flush()
        f = Fight(report_code=CODE, fight_id_in_report=1,
                  encounter_id=ENC, is_kill=False,
                  start_time=0, end_time=60_000, duration_ms=60_000)
        s.add(f)
        s.flush()
        fid_holder.append(f.id)
        BOSS = 9999
        for pid in (1, 2):
            s.add(Combatant(fight_id=f.id, player_id=pid, name=f"P{pid}", job="WAR"))
            s.add(Event(fight_id=f.id, ts=0, type="cast",
                        source_id=pid, ability_game_id=8888))

        # Raidwide 200 cast at t=10s, applied to player 1 at t=10.2s (death)
        # Player 0 root death at t=9s to force the second-death-to-raidwide-with-cascade rule
        s.add(Event(fight_id=f.id, ts=9_000, type="death",
                    source_id=BOSS, target_id=1, ability_game_id=300))  # root cause
        s.add(Event(fight_id=f.id, ts=10_000, type="cast",
                    source_id=BOSS, ability_game_id=200))
        s.add(Event(fight_id=f.id, ts=10_200, type="death",
                    source_id=BOSS, target_id=2, ability_game_id=200))
        # Mit FIRED at t=8s for the first raidwide
        s.add(Event(fight_id=f.id, ts=8_000, type="applybuff",
                    source_id=1, target_id=BOSS, ability_game_id=7535))

        # Second raidwide 200 at t=30s — player 1 (resurrected) dies cascade
        # We need an earlier root death to put this into cascade per _death_kind.
        s.add(Event(fight_id=f.id, ts=29_500, type="death",
                    source_id=BOSS, target_id=1, ability_game_id=301))  # earlier root
        s.add(Event(fight_id=f.id, ts=30_000, type="cast",
                    source_id=BOSS, ability_game_id=200))
        s.add(Event(fight_id=f.id, ts=30_200, type="death",
                    source_id=BOSS, target_id=2, ability_game_id=200))
        # NO mit for the second raidwide → missed

        # FightModel: 200 = raidwide, 300/301 = tankbuster (root)
        for aid, label in [(200, "raidwide"), (300, "tankbuster"),
                           (301, "tankbuster")]:
            s.add(FightModel(encounter_id=ENC, version=1,
                              phase=0, seq=aid - 200,
                              ability_game_id=aid, type_label=label,
                              relative_t_ms=0, time_variance_ms=0,
                              confidence=1.0, meta={}, updated_at=now))
        s.commit()

        # Strat: both raidwide occurrences expect Reprisal
        strat_upsert(s, ENC, encode_mechanic_ref(200, 0),
                     assignments=None,
                     mit_plan={"slots": [{"ability_id": 7535,
                                          "expected_role": "MT",
                                          "window_offset_ms": -2000}]},
                     static_id=1)
        strat_upsert(s, ENC, encode_mechanic_ref(200, 1),
                     assignments=None,
                     mit_plan={"slots": [{"ability_id": 7535,
                                          "expected_role": "MT",
                                          "window_offset_ms": -2000}]},
                     static_id=1)
        try:
            yield s, f.id
        finally:
            s.execute(delete(StratConfig).where(StratConfig.encounter_id == ENC))
            s.execute(delete(FaultScore).where(FaultScore.fight_id.in_(fid_holder)))
            s.execute(delete(FightModel).where(FightModel.encounter_id == ENC))
            s.execute(delete(Event).where(Event.fight_id.in_(fid_holder)))
            s.execute(delete(Combatant).where(Combatant.fight_id.in_(fid_holder)))
            s.execute(delete(Fight).where(Fight.id.in_(fid_holder)))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code == CODE))
            s.execute(delete(Report).where(Report.code == CODE))
            s.commit()


def test_missed_mit_classified_as_mit_failure_after_full_pipeline(seeded):
    """End-to-end: T-302 (v1.12.0 mit-aware) + T-304 (backward-compat
    no-op) should produce mit_failure for the second raidwide where the
    Reprisal plan was missed."""
    session, fid = seeded
    compute_fault_scores_for_fight(session, fid, 1)
    disambiguate_for_fight(session, fid, 1)

    rows = session.execute(
        select(FaultScore).where(FaultScore.fight_id == fid)
    ).scalars().all()
    total_mit_failures = sum(
        (r.reasons or {}).get("mit_failure", 0) for r in rows
    )
    assert total_mit_failures >= 1, (
        "second raidwide (missed Reprisal) should classify as mit_failure"
    )


def test_disambiguate_does_not_upgrade_when_mit_fired(seeded):
    """The first raidwide had Reprisal applied → no upgrade for its victim."""
    session, fid = seeded
    compute_fault_scores_for_fight(session, fid, 1)
    disambiguate_for_fight(session, fid, 1)
    # Find the first cascade death (was at t=10200 on player 2)
    rows = session.execute(
        select(FaultScore).where(FaultScore.fight_id == fid)
    ).scalars().all()
    found_first_cascade_kept = False
    for r in rows:
        for d in (r.reasons or {}).get("deaths", []):
            if d["ts"] == 10200:
                assert d["kind"] == "cascade", (
                    "first raidwide had mit → should NOT upgrade to mit_failure"
                )
                found_first_cascade_kept = True
    assert found_first_cascade_kept


def test_disambiguate_no_fault_rows_returns_note():
    """No compute = no rows → friendly note."""
    with SessionLocal() as s:
        out = disambiguate_for_fight(s, -1, 1)
    assert out["reclassified"] == 0
    assert "note" in out


def test_mit_failure_carries_full_root_weight(seeded):
    """A mit_failure death contributes 1.0 to the score (same as root,
    not 0.1 like cascade). Combined with the first raidwide's cascade
    score (0.1), Player 2's total should reflect both signals."""
    session, fid = seeded
    compute_fault_scores_for_fight(session, fid, 1)
    disambiguate_for_fight(session, fid, 1)
    # Player 2: 1 cascade (first raidwide, mit fired) + 1 mit_failure
    # (second raidwide, no mit) = 0.1 + 1.0 = 1.1 from deaths.
    score = session.execute(
        select(FaultScore.score).where(FaultScore.fight_id == fid,
                                       FaultScore.player_id == 2)
    ).scalar()
    assert float(score) >= 1.1
    # And the mit_failure count should be exactly 1.
    reasons = session.execute(
        select(FaultScore.reasons).where(FaultScore.fight_id == fid,
                                          FaultScore.player_id == 2)
    ).scalar()
    assert reasons["mit_failure"] == 1
    assert reasons["cascade"] == 1
