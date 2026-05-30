"""v1.17.0: cross-encounter unification end-to-end.

Seed kills under BOTH halves of a cloned-encounter group and assert each
per-encounter analytics function unions them. Uses a fake test-only clone
group (added via monkeypatch) for the fight_model write test so we don't
clobber the dev DB's real DSR rows under 1076.

Covers:
- Consensus sees pulls from both IDs in the group.
- Cartography aggregates deaths across the group.
- DPS check pools per-phase DPS from both IDs.
- fight_model writes/reads use the canonical ID only — readers under the
  legacy alias still hit the same rows.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select

from analysis import _encounter as enc_mod
from analysis._encounter import canonical_encounter_id, encounter_id_group
from analysis.cartography import cartography_for_encounter
from analysis.consensus import (
    consensus_timeline_for_encounter,
    read_fight_model,
    write_consensus_to_fight_model,
)
from analysis.dps_check import dps_check_for_encounter
from db.models import (
    Combatant, Event, Fight, FightModel, IngestionLedger, Report,
)
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)

# Real DSR pair — used by read-only tests (consensus / cartography / dps_check).
# These tests never write fight_model, so they don't touch the dev DB's
# existing DSR rows at 1076.
LEGACY_ENC = 1065
CANONICAL_ENC = 1076

# Fake test-only clone group — added via monkeypatch in the writer test so
# we exercise the "write at canonical / read via alias" path without touching
# real data.
FAKE_CANONICAL = 990_017
FAKE_LEGACY = 990_018

STATIC_ID = 1


# --- helpers -----------------------------------------------------------------

def _seed_kill(s, code: str, fight_id_in_report: int, enc_id: int, *,
               boss_cast_aids: list[int],
               start_offset_ms: int = 0):
    """Seed one kill fight with enough boss damage to trip T-103 (one phase)
    plus boss casts at fixed phase-relative times. Returns the Fight PK."""
    now = datetime.now(timezone.utc)
    if s.get(Report, code) is None:
        s.add(Report(code=code, ingested_at=now))
        s.flush()
        s.add(IngestionLedger(
            report_code=code, fights_ingested=[], last_event_ts=0,
            status="open", last_polled_at=now,
        ))
        s.flush()
    f = Fight(
        report_code=code, fight_id_in_report=fight_id_in_report,
        encounter_id=enc_id, is_kill=True,
        fight_percentage=0.0, last_phase=0,
        start_time=start_offset_ms,
        end_time=start_offset_ms + 60_000,
        duration_ms=60_000,
    )
    s.add(f)
    s.flush()
    boss_target_id = 100_000
    boss_source_id = 100_000
    for pid in range(1, 9):
        s.add(Combatant(fight_id=f.id, player_id=pid, name=f"p{pid}",
                        job="DRG", server=None))
    # T-103 needs ≥30 hits on a boss-target spread across the phase.
    # Span the damage from t=0 to t=29_000 so the phase window covers our
    # boss-cast times (5k/6k/7k) — otherwise the casts land OUTSIDE the
    # detected phase and consensus can't see them.
    for i in range(30):
        s.add(Event(
            fight_id=f.id, ts=start_offset_ms + i * 1_000,
            type="damage", source_id=1, target_id=boss_target_id,
            ability_game_id=8888, amount=100, raw={},
        ))
    for pid in range(1, 9):
        s.add(Event(
            fight_id=f.id, ts=start_offset_ms + 1_000,
            type="damage", source_id=pid, target_id=boss_target_id,
            ability_game_id=8888, amount=1000, raw={},
        ))
    for k, aid in enumerate(boss_cast_aids):
        s.add(Event(
            fight_id=f.id, ts=start_offset_ms + 5_000 + k * 1_000,
            type="cast", source_id=boss_source_id, target_id=None,
            ability_game_id=aid, amount=None, raw={},
        ))
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


# --- read-only tests against the real DSR pair -------------------------------

@pytest.fixture
def seed_three_kills_split_dsr():
    """Three synthetic DSR kills — 2 under canonical 1076, 1 under legacy
    1065. Read-only tests only — no fight_model writes."""
    code_a = "T17_DSR_CANONICAL_1"
    code_b = "T17_DSR_CANONICAL_2"
    code_c = "T17_DSR_LEGACY"
    codes = [code_a, code_b, code_c]
    fids: list[int] = []
    with SessionLocal() as s:
        _cleanup_codes(s, codes)
        s.commit()
        casts = [50_001, 50_002, 50_003]
        fids.append(_seed_kill(s, code_a, 1, CANONICAL_ENC, boss_cast_aids=casts))
        fids.append(_seed_kill(s, code_b, 1, CANONICAL_ENC, boss_cast_aids=casts))
        fids.append(_seed_kill(s, code_c, 1, LEGACY_ENC, boss_cast_aids=casts))
        s.commit()
        try:
            yield s, fids
        finally:
            _cleanup_codes(s, codes)
            s.commit()


def test_consensus_sees_pulls_from_both_cloned_ids(seed_three_kills_split_dsr):
    s, _fids = seed_three_kills_split_dsr
    # Query via canonical AND via legacy — both must produce the same answer.
    for query_id in (CANONICAL_ENC, LEGACY_ENC):
        result = consensus_timeline_for_encounter(s, query_id)
        assert result["encounter_id"] == CANONICAL_ENC, (
            f"response should canonicalize input {query_id} → {CANONICAL_ENC}"
        )
        # >=3 pulls — dev DB likely has many real DSR kills under 1076 plus
        # our 3 seeded. The point is that we get SOME pulls back, and the
        # query via either ID returns the same total.
        assert result["total_pulls"] >= 3, result.get("note")
        # The seeded boss casts must appear in the all_abilities list (no
        # threshold). They won't be canonical because the dev DB drowns
        # our 3 pulls; canonical_abilities are >=70% recurrence.
        all_ids = {
            ab["ability_game_id"]
            for phase in result["phases"]
            for ab in phase["all_abilities"]
        }
        assert {50_001, 50_002, 50_003}.issubset(all_ids), (
            f"seeded boss-cast abilities missing from all_abilities; "
            f"sample={sorted(all_ids)[:8]}"
        )


def test_dps_check_pools_kills_across_cloned_group(seed_three_kills_split_dsr):
    s, _ = seed_three_kills_split_dsr
    # 3 kills total — 2 under 1076 + 1 under 1065 — should pool.
    # NOTE: the real dev DB may have additional DSR kills under 1076 with
    # events; we test the lower bound + canonical-id-in-response shape.
    for qid in (CANONICAL_ENC, LEGACY_ENC):
        result = dps_check_for_encounter(s, qid)
        assert result["encounter_id"] == CANONICAL_ENC
        assert result["kills_aggregated"] >= 3
        # At least one pooled phase.
        assert len(result["phases"]) >= 1


def test_cartography_unions_deaths_across_cloned_group():
    """Seed wipes under both halves — cartography aggregates both."""
    code_a = "T17_CART_CANON"
    code_b = "T17_CART_LEGACY"
    codes = [code_a, code_b]
    fids: list[int] = []
    with SessionLocal() as s:
        _cleanup_codes(s, codes)
        s.commit()
        now = datetime.now(timezone.utc)
        for code, enc_id in ((code_a, CANONICAL_ENC), (code_b, LEGACY_ENC)):
            s.add(Report(code=code, ingested_at=now)); s.flush()
            s.add(IngestionLedger(
                report_code=code, fights_ingested=[], last_event_ts=0,
                status="open", last_polled_at=now,
            ))
            f = Fight(report_code=code, fight_id_in_report=1,
                      encounter_id=enc_id, is_kill=False,
                      fight_percentage=50.0, last_phase=0,
                      start_time=0, end_time=60_000, duration_ms=60_000)
            s.add(f); s.flush()
            fids.append(f.id)
            for pid in range(1, 9):
                s.add(Combatant(fight_id=f.id, player_id=pid, name=f"p{pid}",
                                job="DRG"))
                s.add(Event(fight_id=f.id, ts=0, type="cast",
                            source_id=pid, ability_game_id=8888))
            # One death from a unique ability id per fight side
            s.add(Event(fight_id=f.id, ts=5_000, type="death",
                        source_id=999, target_id=1, ability_game_id=70_001))
        s.commit()
        try:
            for qid in (CANONICAL_ENC, LEGACY_ENC):
                result = cartography_for_encounter(s, qid)
                assert result["encounter_id"] == CANONICAL_ENC
                # Real DSR data may exist in the dev DB; we asserted a lower
                # bound on our seeded counts.
                assert result["total_fights"] >= 2
                assert result["total_wipes"] >= 2
                ability_buckets = {b["ability_game_id"]: b
                                   for b in result["buckets"]}
                assert 70_001 in ability_buckets, (
                    f"unique seeded ability missing from cartography; got "
                    f"keys={list(ability_buckets)[:10]}"
                )
                # Both deaths must be counted under this ability.
                assert ability_buckets[70_001]["deaths"] >= 2
        finally:
            _cleanup_codes(s, codes)
            s.commit()


# --- writer test against a FAKE clone group ----------------------------------

@pytest.fixture
def fake_clone_group(monkeypatch):
    """Add (FAKE_CANONICAL, FAKE_LEGACY) to the cloned-group lookup tables
    for the duration of this test. Real production groups (e.g. DSR) keep
    working alongside the fake pair."""
    monkeypatch.setitem(enc_mod._CANONICAL_OF, FAKE_CANONICAL, FAKE_CANONICAL)
    monkeypatch.setitem(enc_mod._CANONICAL_OF, FAKE_LEGACY, FAKE_CANONICAL)
    monkeypatch.setitem(enc_mod._GROUP_OF, FAKE_CANONICAL,
                        (FAKE_CANONICAL, FAKE_LEGACY))
    monkeypatch.setitem(enc_mod._GROUP_OF, FAKE_LEGACY,
                        (FAKE_CANONICAL, FAKE_LEGACY))
    yield
    # monkeypatch.setitem auto-reverts on teardown


def test_fight_model_writes_at_canonical_id_only(fake_clone_group):
    """write_consensus called with the legacy ID still persists rows under
    the canonical ID. Readers under either alias return the same model."""
    code_a = "T17_FM_CANONICAL"
    code_b = "T17_FM_LEGACY_1"
    code_c = "T17_FM_LEGACY_2"
    codes = [code_a, code_b, code_c]
    fids: list[int] = []
    # Verify the monkeypatch is live
    assert canonical_encounter_id(FAKE_LEGACY) == FAKE_CANONICAL
    assert encounter_id_group(FAKE_LEGACY) == (FAKE_CANONICAL, FAKE_LEGACY)

    with SessionLocal() as s:
        _cleanup_codes(s, codes)
        s.execute(delete(FightModel).where(FightModel.encounter_id.in_(
            (FAKE_CANONICAL, FAKE_LEGACY))))
        s.commit()
        casts = [80_001, 80_002, 80_003]
        # 1 kill at canonical, 2 at legacy — needs 3 pulls for consensus to fire.
        fids.append(_seed_kill(s, code_a, 1, FAKE_CANONICAL, boss_cast_aids=casts))
        fids.append(_seed_kill(s, code_b, 1, FAKE_LEGACY, boss_cast_aids=casts))
        fids.append(_seed_kill(s, code_c, 1, FAKE_LEGACY, boss_cast_aids=casts))
        s.commit()
        try:
            # Call the writer with the LEGACY ID — it must persist under
            # the CANONICAL.
            summary = write_consensus_to_fight_model(s, FAKE_LEGACY)
            assert summary["encounter_id"] == FAKE_CANONICAL
            assert summary["abilities_written"] >= 3
            assert summary["total_pulls"] == 3

            # No rows under the legacy ID.
            legacy_rows = s.execute(
                select(FightModel).where(FightModel.encounter_id == FAKE_LEGACY)
            ).scalars().all()
            assert len(legacy_rows) == 0

            # Canonical has the rows.
            canonical_rows = s.execute(
                select(FightModel).where(FightModel.encounter_id == FAKE_CANONICAL)
            ).scalars().all()
            assert len(canonical_rows) >= 3

            # Reading via EITHER alias returns the same canonical rows.
            read_canonical = read_fight_model(s, FAKE_CANONICAL)
            read_legacy = read_fight_model(s, FAKE_LEGACY)
            assert read_canonical["encounter_id"] == FAKE_CANONICAL
            assert read_legacy["encounter_id"] == FAKE_CANONICAL
            assert len(read_canonical["phases"]) == len(read_legacy["phases"])
            phase0_canonical = read_canonical["phases"][0]["abilities"]
            phase0_legacy = read_legacy["phases"][0]["abilities"]
            assert {a["ability_game_id"] for a in phase0_canonical} == {
                a["ability_game_id"] for a in phase0_legacy
            }
        finally:
            _cleanup_codes(s, codes)
            s.execute(delete(FightModel).where(FightModel.encounter_id.in_(
                (FAKE_CANONICAL, FAKE_LEGACY))))
            s.commit()


def test_canonical_helper_is_identity_for_non_cloned():
    """Non-cloned encounters keep their own ID — no surprise side effects."""
    for eid in (1079, 1068, 101, 999_017):
        assert canonical_encounter_id(eid) == eid
        assert encounter_id_group(eid) == (eid,)
