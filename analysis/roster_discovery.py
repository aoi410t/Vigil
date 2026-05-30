"""v1.15.0 — discover every character seen across the static's watched
reports and report their current roster classification.

A classification is one of:
- "core"        — character is an alias of a Member with kind='core'
- "substitute"  — character is an alias of a Member with kind='substitute'
- "sub"         — character is an alias of any Member, AND that member already
                  owns at least one other alias (so this one is plausibly a
                  sub-account / alt). Used purely to render a UI hint; under
                  the hood it's the same as core/substitute.
- "ignored"     — explicitly listed in `ignored_characters` for this static.
- "unclassified"— combatant exists in our watched reports but no alias /
                  ignore row claims it.

The discovery surface is scoped to ONE static: it joins combatants → fights →
reports → watched_reports filtered on `watched_reports.static_id`. A combatant
that appears in some other static's report doesn't leak in.

For the "sub" rendering, `linked_member_id` and `linked_member_name` point to
the owning member so the UI can show "Alice's sub: Alice Heavyhand".
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import distinct, func, select
from sqlalchemy.orm import Session

from db.models import (
    CharacterAlias,
    Combatant,
    Fight,
    IgnoredCharacter,
    Member,
    WatchedReport,
)


def _coalesce_server(server: str | None) -> str:
    return server or ""


def discovered_characters_for_static(
    session: Session, static_id: int
) -> list[dict[str, Any]]:
    """Return one row per distinct (character_name, server) seen across all
    fights of this static's watched reports.

    Each row: `{character_name, server, latest_job, fights_seen,
    classification, linked_member_id, linked_member_name}`.

    Sorted by `fights_seen` desc (most-recurrent characters surface first),
    tiebroken by character_name.

    Designed to be cheap on the read side: one query for combatant aggregates,
    one for the static's aliases, one for the ignore list. The aggregation is
    server-side via a single SQL grouping; the alias/ignore joins are done in
    Python because we want to surface ALL combatants regardless of
    classification (downstream UI filters).
    """
    # Aggregate combatants for this static's watched reports.
    rows = session.execute(
        select(
            Combatant.name.label("character_name"),
            Combatant.server.label("server"),
            func.count(distinct(Combatant.fight_id)).label("fights_seen"),
            # max(fight.start_time) so we can pick the freshest job seen.
            func.max(Fight.start_time).label("latest_fight_ts"),
        )
        .join(Fight, Fight.id == Combatant.fight_id)
        .join(WatchedReport, WatchedReport.code == Fight.report_code)
        .where(
            WatchedReport.static_id == static_id,
            Combatant.name.is_not(None),
            Combatant.name != "",
        )
        .group_by(Combatant.name, Combatant.server)
    ).all()

    if not rows:
        return []

    # For each (name, server), find the latest job. Done as a follow-up so the
    # group-by above stays simple; small N (≤ a few hundred distinct
    # characters per static).
    latest_jobs: dict[tuple[str, str | None], str | None] = {}
    for r in rows:
        latest = session.execute(
            select(Combatant.job)
            .join(Fight, Fight.id == Combatant.fight_id)
            .join(WatchedReport, WatchedReport.code == Fight.report_code)
            .where(
                WatchedReport.static_id == static_id,
                Combatant.name == r.character_name,
                Combatant.server.is_(r.server) if r.server is None
                else Combatant.server == r.server,
                Combatant.job.is_not(None),
            )
            .order_by(Fight.start_time.desc())
            .limit(1)
        ).scalar_one_or_none()
        latest_jobs[(r.character_name, r.server)] = latest

    # Alias map for this static. Includes the owning member (id, name, kind)
    # and the alias.id so we can pick the PRIMARY alias per member (lowest
    # id, i.e. the first one created — which by convention is the member's
    # main character). Non-primary aliases on a multi-alias member are the
    # sub-accounts.
    alias_rows = session.execute(
        select(
            CharacterAlias.id,
            CharacterAlias.character_name,
            CharacterAlias.server,
            CharacterAlias.member_id,
            Member.name,
            Member.kind,
        )
        .join(Member, Member.id == CharacterAlias.member_id)
        .where(Member.static_id == static_id)
        .order_by(CharacterAlias.id)
    ).all()
    by_keyed: dict[tuple[str, str], tuple[int, int, str, str]] = {}
    by_name: dict[str, list[tuple[int, int, str, str]]] = {}
    member_alias_counts: dict[int, int] = {}
    primary_alias_id: dict[int, int] = {}  # member_id -> alias.id of primary
    for ar in alias_rows:
        member_alias_counts[ar.member_id] = member_alias_counts.get(ar.member_id, 0) + 1
        # Because we ordered by alias.id ASC, the FIRST time we see a member_id
        # is its primary alias.
        if ar.member_id not in primary_alias_id:
            primary_alias_id[ar.member_id] = ar.id
        entry = (ar.id, ar.member_id, ar.name, ar.kind)
        if ar.server:
            by_keyed[(ar.character_name, ar.server)] = entry
        by_name.setdefault(ar.character_name, []).append(entry)

    # Ignore set for this static.
    ignored_rows = session.execute(
        select(IgnoredCharacter.character_name, IgnoredCharacter.server)
        .where(IgnoredCharacter.static_id == static_id)
    ).all()
    ignored: set[tuple[str, str]] = {
        (ir.character_name, _coalesce_server(ir.server)) for ir in ignored_rows
    }

    out: list[dict[str, Any]] = []
    for r in rows:
        name = r.character_name
        server = r.server
        key_ignore = (name, _coalesce_server(server))
        classification: str
        linked_member_id: int | None = None
        linked_member_name: str | None = None

        # Alias lookup mirrors resolve_members.py: prefer (name, server) exact
        # match; fall back to name-only iff exactly one member claims it.
        owner: tuple[int, int, str, str] | None = None
        if server and (name, server) in by_keyed:
            owner = by_keyed[(name, server)]
        elif name in by_name and len(by_name[name]) == 1:
            owner = by_name[name][0]

        if owner is not None:
            alias_id, mid, mname, mkind = owner
            linked_member_id = mid
            linked_member_name = mname
            # The PRIMARY alias (lowest alias.id) is the member's main
            # character — classification stays the member's kind ('core' or
            # 'substitute'). Any NON-primary alias on a multi-alias member is
            # a sub-account. Bugfix v1.15.1: previously every alias of a
            # multi-alias member was marked 'sub', so both halves of a pair
            # showed as each other's sub.
            if (member_alias_counts.get(mid, 1) > 1
                    and primary_alias_id.get(mid) != alias_id):
                classification = "sub"
            else:
                classification = mkind  # 'core' or 'substitute'
        elif key_ignore in ignored:
            classification = "ignored"
        else:
            classification = "unclassified"

        out.append({
            "character_name": name,
            "server": server,
            "latest_job": latest_jobs.get((name, server)),
            "fights_seen": int(r.fights_seen),
            "classification": classification,
            "linked_member_id": linked_member_id,
            "linked_member_name": linked_member_name,
        })

    # fights_seen desc, then character_name for stable order.
    out.sort(key=lambda d: (-d["fights_seen"], d["character_name"].lower()))
    return out
