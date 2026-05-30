"""Unit tests for T-109 combatant pruning."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select

from db.models import Combatant, Event, Fight, IngestionLedger, Report
from ingest.events import prune_inactive_combatants


def _seed_fight(session, *, code: str = "PRUNE_T109", fight_local_id: int = 1) -> int:
    """Insert a minimal Report + IngestionLedger + Fight and return the fight PK."""
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    session.add(Report(code=code, is_public=True, ingested_at=datetime.now(timezone.utc)))
    session.flush()
    session.add(IngestionLedger(
        report_code=code,
        fights_ingested=[fight_local_id],
        last_event_ts=0,
        status="open",
        last_polled_at=datetime.now(timezone.utc),
    ))
    fight = Fight(
        report_code=code,
        fight_id_in_report=fight_local_id,
        encounter_id=101,
        is_kill=False,
        start_time=end_ms - 60_000,
        end_time=end_ms,
        duration_ms=60_000,
    )
    session.add(fight)
    session.flush()
    return fight.id


def _add_combatants(session, fight_id: int, player_ids: list[int]) -> None:
    for pid in player_ids:
        session.add(Combatant(
            fight_id=fight_id, player_id=pid, name=f"P{pid}", server="S", job="PLD"
        ))
    session.flush()


def _add_event(session, fight_id: int, *, ts: int, src: int | None, etype: str = "cast") -> None:
    session.add(Event(
        fight_id=fight_id, ts=ts, type=etype, source_id=src,
        target_id=None, ability_game_id=1, amount=None, raw={},
    ))


def test_prune_keeps_only_active_source_ids(db_session):
    """8 seeded combatants but only 3 produce cast/damage events -> 3 remain."""
    fid = _seed_fight(db_session)
    _add_combatants(db_session, fid, list(range(1, 9)))  # 8 candidates
    # Only IDs 1, 3, 5 actually act.
    _add_event(db_session, fid, ts=100, src=1, etype="cast")
    _add_event(db_session, fid, ts=200, src=3, etype="damage")
    _add_event(db_session, fid, ts=300, src=5, etype="calculateddamage")
    db_session.flush()

    deleted = prune_inactive_combatants(db_session, fid)

    assert deleted == 5
    remaining = set(db_session.execute(
        select(Combatant.player_id).where(Combatant.fight_id == fid)
    ).scalars().all())
    assert remaining == {1, 3, 5}


def test_prune_is_idempotent(db_session):
    fid = _seed_fight(db_session, code="PRUNE_T109_B")
    _add_combatants(db_session, fid, [1, 2, 3])
    _add_event(db_session, fid, ts=100, src=1, etype="cast")
    _add_event(db_session, fid, ts=200, src=2, etype="damage")
    db_session.flush()

    first = prune_inactive_combatants(db_session, fid)
    second = prune_inactive_combatants(db_session, fid)

    assert first == 1
    assert second == 0  # nothing left to prune
    remaining = set(db_session.execute(
        select(Combatant.player_id).where(Combatant.fight_id == fid)
    ).scalars().all())
    assert remaining == {1, 2}


def test_prune_no_events_is_safe_noop(db_session):
    """If T-005 hasn't run yet, the prune must not wipe the speculative roster."""
    fid = _seed_fight(db_session, code="PRUNE_T109_C")
    _add_combatants(db_session, fid, [1, 2, 3])
    # No events for this fight yet.
    db_session.flush()

    deleted = prune_inactive_combatants(db_session, fid)

    assert deleted == 0
    count = db_session.execute(
        select(func.count(Combatant.fight_id)).where(Combatant.fight_id == fid)
    ).scalar_one()
    assert count == 3


def test_prune_ignores_non_active_event_types(db_session):
    """`applybuff`/`death` events don't count as evidence of having played —
    only cast/damage/calculateddamage do."""
    fid = _seed_fight(db_session, code="PRUNE_T109_D")
    _add_combatants(db_session, fid, [1, 2, 3])
    # Player 1 actually plays (cast). Players 2 and 3 only appear in buff/death.
    _add_event(db_session, fid, ts=100, src=1, etype="cast")
    _add_event(db_session, fid, ts=150, src=2, etype="applybuff")
    _add_event(db_session, fid, ts=200, src=3, etype="death")
    db_session.flush()

    deleted = prune_inactive_combatants(db_session, fid)

    assert deleted == 2
    remaining = set(db_session.execute(
        select(Combatant.player_id).where(Combatant.fight_id == fid)
    ).scalars().all())
    assert remaining == {1}


def test_prune_ignores_null_source_id(db_session):
    """Events with sourceID=null (non-attributable) should not preserve anything."""
    fid = _seed_fight(db_session, code="PRUNE_T109_E")
    _add_combatants(db_session, fid, [1, 2])
    _add_event(db_session, fid, ts=100, src=1, etype="cast")
    _add_event(db_session, fid, ts=200, src=None, etype="damage")  # cascade non-attributable
    db_session.flush()

    deleted = prune_inactive_combatants(db_session, fid)

    assert deleted == 1
    remaining = set(db_session.execute(
        select(Combatant.player_id).where(Combatant.fight_id == fid)
    ).scalars().all())
    assert remaining == {1}


def test_prune_scoped_to_one_fight(db_session):
    """Pruning fight A doesn't touch fight B's combatants."""
    fid_a = _seed_fight(db_session, code="PRUNE_T109_F1", fight_local_id=1)
    fid_b = _seed_fight(db_session, code="PRUNE_T109_F2", fight_local_id=1)
    _add_combatants(db_session, fid_a, [1, 2, 3])
    _add_combatants(db_session, fid_b, [4, 5, 6])
    _add_event(db_session, fid_a, ts=100, src=1, etype="cast")  # only 1 active in A
    _add_event(db_session, fid_b, ts=100, src=4, etype="cast")
    _add_event(db_session, fid_b, ts=150, src=5, etype="cast")
    _add_event(db_session, fid_b, ts=200, src=6, etype="cast")
    db_session.flush()

    deleted_a = prune_inactive_combatants(db_session, fid_a)

    assert deleted_a == 2
    b_remaining = db_session.execute(
        select(func.count(Combatant.fight_id)).where(Combatant.fight_id == fid_b)
    ).scalar_one()
    assert b_remaining == 3  # untouched
