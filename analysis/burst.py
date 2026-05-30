"""M-BURST: 2-minute burst alignment (T-105, PLAN §9).

For each fight, derive shared **burst windows** from raid-buff cast events
(`raid_buff`-labeled abilities), then measure per player how many of their own
**personal cooldowns** (`personal_buff`-labeled) fired inside one of those
windows vs. drifted out.

Reads only labels with `source = 'user'` or `confidence >= AUTO_HIGH_THRESHOLD`
(per the T-108 contract in IDEAS.md) — auto-low rows stay in the review queue
until the user confirms them.

Per-raid-buff window length: if the ability's `abilities.duration_ms` is
populated (by `scripts/scrape_ability_durations.py` from the FFXIV wiki — T-108
follow-up shipped in v1.5.7), each cast opens a window of that exact duration.
Otherwise the cast falls back to a 20s default. Most raid buffs are 20s
post-Endwalker trait updates; the few that aren't (Reprisal/Feint/Addle 15s,
Searing Light 20s after the 30s nerf, etc.) get accurate sizing once the wiki
scrape has populated their rows.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analysis.ability_classifier import AUTO_HIGH_THRESHOLD
from db.models import Ability, AbilityLabel, Combatant, Event, Fight

DEFAULT_RAID_BUFF_WINDOW_MS = 20_000


def labelled_ability_ids(session: Session, label: str) -> set[int]:
    """Ability IDs we trust to wear `label` — user-confirmed or auto-high."""
    rows = session.execute(
        select(AbilityLabel.ability_game_id)
        .where(
            AbilityLabel.label == label,
            ((AbilityLabel.source == "user")
             | (AbilityLabel.confidence >= AUTO_HIGH_THRESHOLD)),
        )
    ).scalars().all()
    return set(rows)


def partition_by_kind(session: Session, ability_ids: set[int]) -> tuple[set[int], set[int]]:
    """Split a label's ability IDs into `(action_ids, status_ids)`.

    Window construction reads `cast` events on actions; personal-CD timing
    reads `applybuff` events on statuses. Mixing the two would either inflate
    windows (1 raid buff cast → 8 applybuff events) or miss timing (action
    casts don't fire for status-only personal CDs).
    """
    if not ability_ids:
        return set(), set()
    rows = session.execute(
        select(Ability.ability_game_id, Ability.kind)
        .where(Ability.ability_game_id.in_(ability_ids))
    ).all()
    actions = {aid for aid, kind in rows if kind == "action"}
    statuses = {aid for aid, kind in rows if kind == "status"}
    return actions, statuses


def duration_ms_for_abilities(
    session: Session, ability_ids: set[int],
) -> dict[int, int]:
    """Return ability_id -> wiki-scraped duration_ms for ids that have one.

    Caller falls back to a default window length for ids not in the result.
    """
    if not ability_ids:
        return {}
    rows = session.execute(
        select(Ability.ability_game_id, Ability.duration_ms)
        .where(
            Ability.ability_game_id.in_(ability_ids),
            Ability.duration_ms.is_not(None),
        )
    ).all()
    return {int(aid): int(dur) for aid, dur in rows}


def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping or touching [start, end] intervals."""
    if not intervals:
        return []
    sorted_ivs = sorted(intervals)
    out: list[tuple[int, int]] = [sorted_ivs[0]]
    for start, end in sorted_ivs[1:]:
        last_start, last_end = out[-1]
        if start <= last_end:
            out[-1] = (last_start, max(last_end, end))
        else:
            out.append((start, end))
    return out


def in_any_interval(ts: int, intervals: list[tuple[int, int]]) -> bool:
    """O(n) linear scan — n is window count per fight, typically ≤ 6."""
    return any(start <= ts <= end for start, end in intervals)


def burst_alignment_for_report(
    session: Session,
    code: str,
    *,
    window_ms: int = DEFAULT_RAID_BUFF_WINDOW_MS,
) -> dict[str, Any]:
    """Per-fight, per-player burst alignment.

    Returns `{report_code, raid_buff_ids, personal_buff_ids, fights: […]}`.
    Each fight: `{fight_id, fight_id_in_report, is_kill, duration_ms,
    burst_windows: [(start, end), …], players: [{player_id, name, job,
    personal_casts_total, in_window, drift, in_window_pct}]}`.
    """
    raid_buff_ids = labelled_ability_ids(session, "raid_buff")
    personal_buff_ids = labelled_ability_ids(session, "personal_buff")
    raid_action_ids, _raid_status_ids = partition_by_kind(session, raid_buff_ids)
    personal_action_ids, personal_status_ids = partition_by_kind(
        session, personal_buff_ids,
    )
    # Per-ability window override (wiki-scraped). Falls back to window_ms when
    # the ability has no row in the result.
    raid_durations = duration_ms_for_abilities(session, raid_action_ids)

    fights = session.execute(
        select(Fight)
        .where(Fight.report_code == code)
        .order_by(Fight.start_time, Fight.id)
    ).scalars().all()

    if not fights:
        return {
            "report_code": code,
            "raid_buff_ids": sorted(raid_buff_ids),
            "personal_buff_ids": sorted(personal_buff_ids),
            "window_ms": window_ms,
            "fights": [],
        }

    fight_ids = [f.id for f in fights]
    combatants = session.execute(
        select(Combatant).where(Combatant.fight_id.in_(fight_ids))
    ).scalars().all()
    combatant_lookup = {(c.fight_id, c.player_id): c for c in combatants}
    players_by_fight: dict[int, list[int]] = {}
    for c in combatants:
        players_by_fight.setdefault(c.fight_id, []).append(c.player_id)

    # Per-fight raid casts as (ts, ability_id) so we can apply per-ability
    # window lengths from raid_durations later.
    raid_by_fight: dict[int, list[tuple[int, int]]] = {}
    personal_by_fp: dict[tuple[int, int], list[int]] = {}

    # Window-defining events: raid-buff actions fire `cast` once per cast.
    if raid_action_ids:
        for fid, aid, ts in session.execute(
            select(Event.fight_id, Event.ability_game_id, Event.ts)
            .where(
                Event.fight_id.in_(fight_ids),
                Event.type == "cast",
                Event.ability_game_id.in_(raid_action_ids),
                Event.ts.is_not(None),
            )
        ).all():
            raid_by_fight.setdefault(fid, []).append((int(ts), int(aid)))

    # Personal-CD timing: prefer action `cast` (when label covers the action),
    # otherwise fall back to status `applybuff` (which is the moment the buff
    # actually activates). For status-only personal labels the applybuff target
    # *is* the player whose CD it is — most personal buffs are self-applied.
    if personal_action_ids:
        for fid, sid, ts in session.execute(
            select(Event.fight_id, Event.source_id, Event.ts)
            .where(
                Event.fight_id.in_(fight_ids),
                Event.type == "cast",
                Event.source_id.is_not(None),
                Event.ability_game_id.in_(personal_action_ids),
                Event.ts.is_not(None),
            )
        ).all():
            personal_by_fp.setdefault((fid, sid), []).append(int(ts))

    if personal_status_ids:
        # Status applybuff: the *target* is the player who gained the buff,
        # which is the CD-owner for self-applied personal CDs.
        for fid, tid, ts in session.execute(
            select(Event.fight_id, Event.target_id, Event.ts)
            .where(
                Event.fight_id.in_(fight_ids),
                Event.type == "applybuff",
                Event.target_id.is_not(None),
                Event.ability_game_id.in_(personal_status_ids),
                Event.ts.is_not(None),
            )
        ).all():
            personal_by_fp.setdefault((fid, tid), []).append(int(ts))

    out_fights = []
    for f in fights:
        raid_casts = raid_by_fight.get(f.id, [])
        # Per-cast window length: wiki-scraped if available, else default.
        windows = merge_intervals([
            (t, t + raid_durations.get(aid, window_ms))
            for t, aid in raid_casts
        ])

        per_player = []
        for pid in sorted(set(players_by_fight.get(f.id, []))):
            personal_ts = personal_by_fp.get((f.id, pid), [])
            total = len(personal_ts)
            if total == 0:
                continue
            in_w = sum(1 for t in personal_ts if in_any_interval(t, windows))
            c = combatant_lookup.get((f.id, pid))
            per_player.append({
                "player_id": pid,
                "name": c.name if c else None,
                "job": c.job if c else None,
                "personal_casts_total": total,
                "in_window": in_w,
                "drift": total - in_w,
                "in_window_pct": round(in_w / total, 3),
            })
        per_player.sort(key=lambda p: (p["in_window_pct"], -p["personal_casts_total"]))

        out_fights.append({
            "fight_id": f.id,
            "fight_id_in_report": f.fight_id_in_report,
            "is_kill": f.is_kill,
            "duration_ms": f.duration_ms,
            "burst_windows": [list(w) for w in windows],
            "players": per_player,
        })

    return {
        "report_code": code,
        "raid_buff_ids": sorted(raid_buff_ids),
        "personal_buff_ids": sorted(personal_buff_ids),
        "window_ms": window_ms,
        "fights": out_fights,
    }
