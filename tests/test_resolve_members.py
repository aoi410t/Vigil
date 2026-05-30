"""T-107 combatant→member resolution tests."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete

from analysis.resolve_members import (
    coverage_summary,
    resolve_combatants_for_report,
)
from db.models import (
    CharacterAlias,
    Combatant,
    Fight,
    IngestionLedger,
    Member,
    Report,
)
from db.session import SessionLocal, engine

pytestmark = pytest.mark.skipif(
    engine is None, reason="DATABASE_URL not configured"
)

REPORT_CODE = "T107_TEST"


@pytest.fixture
def session():
    with SessionLocal() as s:
        # Seed report + 2 fights + combatants + members + aliases.
        s.add(Report(code=REPORT_CODE,
                     ingested_at=datetime.now(timezone.utc)))
        s.flush()
        f1 = Fight(report_code=REPORT_CODE, fight_id_in_report=1,
                   encounter_id=1, start_time=0, end_time=100)
        f2 = Fight(report_code=REPORT_CODE, fight_id_in_report=2,
                   encounter_id=1, start_time=200, end_time=300)
        s.add_all([f1, f2])
        s.flush()
        # Fight 1: Alice (Aether), Bob (no server), unknown player.
        s.add_all([
            Combatant(fight_id=f1.id, player_id=1, name="Alice Tankerton",
                      server="Aether", job="Paladin"),
            Combatant(fight_id=f1.id, player_id=2, name="Bob Healington",
                      server=None, job="Astrologian"),
            Combatant(fight_id=f1.id, player_id=3, name="Stranger Danger",
                      server=None, job="Monk"),
        ])
        # Fight 2: same Alice but re-rolled to Warrior.
        s.add_all([
            Combatant(fight_id=f2.id, player_id=1, name="Alice Tankerton",
                      server="Aether", job="Warrior"),
        ])
        # Roster
        alice = Member(static_id=1, name="Alice", created_at=datetime.now(timezone.utc))
        bob = Member(static_id=1, name="Bob", created_at=datetime.now(timezone.utc))
        s.add_all([alice, bob])
        s.flush()
        s.add_all([
            CharacterAlias(member_id=alice.id, character_name="Alice Tankerton",
                           server="Aether",
                           created_at=datetime.now(timezone.utc)),
            CharacterAlias(member_id=bob.id, character_name="Bob Healington",
                           server=None,
                           created_at=datetime.now(timezone.utc)),
        ])
        s.commit()
        try:
            yield s
        finally:
            # Cleanup
            s.execute(delete(Combatant).where(Combatant.fight_id.in_([f1.id, f2.id])))
            s.execute(delete(Fight).where(Fight.report_code == REPORT_CODE))
            s.execute(delete(IngestionLedger).where(IngestionLedger.report_code == REPORT_CODE))
            s.execute(delete(Report).where(Report.code == REPORT_CODE))
            s.execute(delete(CharacterAlias).where(CharacterAlias.member_id.in_([alice.id, bob.id])))
            s.execute(delete(Member).where(Member.id.in_([alice.id, bob.id])))
            s.commit()


def test_resolves_known_alias_with_server(session):
    result = resolve_combatants_for_report(session, REPORT_CODE)
    f1 = result["fights"][0]["combatants"]
    alice = next(c for c in f1 if c["name"] == "Alice Tankerton")
    assert alice["member_name"] == "Alice"
    assert alice["job"] == "Paladin"


def test_resolves_known_alias_name_only_when_server_null(session):
    result = resolve_combatants_for_report(session, REPORT_CODE)
    bob = next(c for c in result["fights"][0]["combatants"] if c["name"] == "Bob Healington")
    assert bob["member_name"] == "Bob"


def test_unknown_combatant_is_null_member(session):
    result = resolve_combatants_for_report(session, REPORT_CODE)
    stranger = next(c for c in result["fights"][0]["combatants"] if c["name"] == "Stranger Danger")
    assert stranger["member_id"] is None
    assert stranger["member_name"] is None


def test_per_fight_job_can_differ(session):
    """Same member can play different jobs across fights — must come from
    combatant row, not the member record."""
    result = resolve_combatants_for_report(session, REPORT_CODE)
    f1_alice = next(c for c in result["fights"][0]["combatants"] if c["name"] == "Alice Tankerton")
    f2_alice = next(c for c in result["fights"][1]["combatants"] if c["name"] == "Alice Tankerton")
    assert f1_alice["job"] == "Paladin"
    assert f2_alice["job"] == "Warrior"
    assert f1_alice["member_name"] == f2_alice["member_name"] == "Alice"


def test_coverage_summary_counts_distinct_characters(session):
    cov = coverage_summary(session, REPORT_CODE)
    # Alice + Bob + Stranger = 3 distinct names. 2 resolved (Alice, Bob), 1 unresolved.
    assert cov["total_characters"] == 3
    assert cov["resolved"] == 2
    assert {u["name"] for u in cov["unresolved"]} == {"Stranger Danger"}


def test_unknown_report_returns_empty(session):
    result = resolve_combatants_for_report(session, "NOPE")
    assert result == {"report_code": "NOPE", "fights": []}


def test_ambiguous_name_unresolved_when_multiple_members_share_a_name(session):
    """Two aliases under different members both named 'Bob Healington' must
    NOT silently resolve — they should land in unresolved."""
    # Add a second member with the same alias name but a server, plus a third
    # combatant in fight 1 with that name and no server (we already have Bob;
    # add a Carol).
    carol = Member(static_id=1, name="Carol", created_at=datetime.now(timezone.utc))
    session.add(carol)
    session.flush()
    session.add(CharacterAlias(
        member_id=carol.id, character_name="Bob Healington", server="Primal",
        created_at=datetime.now(timezone.utc),
    ))
    session.commit()

    try:
        result = resolve_combatants_for_report(session, REPORT_CODE)
        bob = next(c for c in result["fights"][0]["combatants"]
                   if c["name"] == "Bob Healington")
        # Bob's combatant row has server=None and there are now 2 aliases under
        # this name — must remain unresolved.
        assert bob["member_id"] is None
    finally:
        session.execute(delete(CharacterAlias).where(CharacterAlias.member_id == carol.id))
        session.execute(delete(Member).where(Member.id == carol.id))
        session.commit()
