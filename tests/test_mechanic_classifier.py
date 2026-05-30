"""T-203 mechanic classifier tests + live AC."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select

from analysis.consensus import write_consensus_to_fight_model
from analysis.mechanic_classifier import (
    _label,
    classify_canonical_abilities,
)
from db.models import (
    Combatant, Event, Fight, FightModel, IngestionLedger, Report,
)
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)


# ---- Pure-function label tests ----

def test_label_cosmetic_when_no_damage():
    assert _label({"cast_count": 0, "max_targets": 0, "mean_targets": 0,
                   "mean_amount_per_target": 0}, False) == "cosmetic"


def test_label_raidwide_when_many_targets():
    assert _label({"cast_count": 3, "max_targets": 8, "mean_targets": 7.5,
                   "mean_amount_per_target": 20_000}, False) == "raidwide"


def test_label_tankbuster_single_target_big_hit():
    assert _label({"cast_count": 2, "max_targets": 1, "mean_targets": 1.0,
                   "mean_amount_per_target": 150_000}, False) == "tankbuster"


def test_label_aoe_party_2_to_5_targets():
    assert _label({"cast_count": 1, "max_targets": 3, "mean_targets": 3.0,
                   "mean_amount_per_target": 10_000}, False) == "aoe_party"


def test_label_enrage_overrides_everything():
    sig = {"cast_count": 1, "max_targets": 8, "mean_targets": 8.0,
           "mean_amount_per_target": 999_999}
    assert _label(sig, True) == "enrage"


# ---- End-to-end with seeded DB ----

ENC = 432109
CODES = ("T203_A", "T203_B", "T203_C")


@pytest.fixture
def three_pulls_with_signatures():
    """3 kill pulls of encounter ENC, each containing:
      - canonical ability 1 fires at ~5s, hits 8 players (raidwide)
      - canonical ability 2 fires at ~10s, hits player 1 only (tankbuster)
      - canonical ability 3 fires at ~20s, no damage (cosmetic)
    """
    fight_ids: list[int] = []
    with SessionLocal() as s:
        now = datetime.now(timezone.utc)
        for code in CODES:
            s.add(Report(code=code, ingested_at=now))
        s.flush()

        BOSS = 9999
        PLAYERS = list(range(1, 9))  # 8 players
        for i, code in enumerate(CODES):
            f = Fight(report_code=code, fight_id_in_report=1,
                      encounter_id=ENC, is_kill=True,
                      start_time=0, end_time=30_000, duration_ms=30_000)
            s.add(f)
            s.flush()
            fight_ids.append(f.id)
            for pid in PLAYERS:
                s.add(Combatant(fight_id=f.id, player_id=pid,
                                name=f"P{pid}", job="WAR"))
                # Each player produces ≥1 cast so _active_players includes them.
                s.add(Event(fight_id=f.id, ts=1000, type="cast",
                            source_id=pid, ability_game_id=9001))
                # And enough boss-target damage for phase detection (need ≥30
                # spread across the whole fight so canonical casts at 5/10/20s
                # all fall inside the detected phase window).
                for j in range(8):
                    s.add(Event(fight_id=f.id, ts=2000 + j * 3000, type="damage",
                                source_id=pid, target_id=BOSS,
                                ability_game_id=8888, amount=1000))
            # Canonical 1: raidwide cast at 5000
            s.add(Event(fight_id=f.id, ts=5000, type="cast",
                        source_id=BOSS, ability_game_id=111))
            for pid in PLAYERS:
                s.add(Event(fight_id=f.id, ts=5050, type="damage",
                            source_id=BOSS, target_id=pid,
                            ability_game_id=111, amount=15_000))
            # Canonical 2: tankbuster on player 1 at 10000
            s.add(Event(fight_id=f.id, ts=10_000, type="cast",
                        source_id=BOSS, ability_game_id=222))
            s.add(Event(fight_id=f.id, ts=10_050, type="damage",
                        source_id=BOSS, target_id=1,
                        ability_game_id=222, amount=80_000))
            # Canonical 3: cosmetic cast at 20000 with no damage
            s.add(Event(fight_id=f.id, ts=20_000, type="cast",
                        source_id=BOSS, ability_game_id=333))
        s.commit()
        try:
            write_consensus_to_fight_model(s, ENC)
            yield s
        finally:
            s.execute(delete(FightModel).where(FightModel.encounter_id == ENC))
            s.execute(delete(Event).where(Event.fight_id.in_(fight_ids)))
            s.execute(delete(Combatant).where(Combatant.fight_id.in_(fight_ids)))
            s.execute(delete(Fight).where(Fight.id.in_(fight_ids)))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code.in_(CODES)))
            s.execute(delete(Report).where(Report.code.in_(CODES)))
            s.commit()


def test_classifier_labels_synthetic_fight(three_pulls_with_signatures):
    session = three_pulls_with_signatures
    summary = classify_canonical_abilities(session, ENC)
    rows = session.execute(
        select(FightModel).where(FightModel.encounter_id == ENC)
        .order_by(FightModel.phase, FightModel.seq)
    ).scalars().all()
    by_aid = {r.ability_game_id: r.type_label for r in rows}
    assert by_aid[111] == "raidwide"
    assert by_aid[222] == "tankbuster"
    assert by_aid[333] == "cosmetic"
    assert summary["label_counts"]["raidwide"] >= 1
    assert summary["label_counts"]["tankbuster"] >= 1
    assert summary["label_counts"]["cosmetic"] >= 1


def test_classifier_handles_missing_fight_model():
    """No fight_model rows → labeled=0 with a note."""
    with SessionLocal() as s:
        out = classify_canonical_abilities(s, 999_999_999)
    assert out["labeled"] == 0
    assert "note" in out


# ---- Live AC against FRU + M5S fight_model rows ----

def test_live_fru_classification_finds_raidwides_and_cosmetics():
    """11 FRU kills with persisted fight_model → classifier should label some
    raidwides, some cosmetics. Looser assertion since real boss-side coverage
    is noisy (lots of zero-damage casts)."""
    with SessionLocal() as s:
        existing = s.execute(
            select(FightModel.encounter_id).where(FightModel.encounter_id == 1079)
            .limit(1)
        ).scalar()
        if existing is None:
            pytest.skip("FRU fight_model not persisted (run /persist first)")
        # Reclassify (idempotent)
        summary = classify_canonical_abilities(s, 1079)
    assert summary["labeled"] > 0
    counts = summary["label_counts"]
    # We expect at least some raidwides and at least some cosmetics for FRU.
    assert counts.get("raidwide", 0) >= 3
    assert counts.get("cosmetic", 0) >= 3
