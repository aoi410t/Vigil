"""M-REPORT (T-307) pasteable Discord session summary.

For one report (= one prog session), assemble the headline numbers from the
modules already shipped:
  - encounter + report code + duration + pulls + kills/wipes
  - wipe-type breakdown (T-302 classify_wipe_type)
  - best prog (lowest fight_percentage achieved)
  - top mechanics we died to (T-206 cartography filtered to this report's fights)
  - mit hit-rate across the session (T-303 mit_audit_summary aggregated)
  - per-player fault totals (T-302 fault_aggregate restricted to this report)
  - worst per-mechanic consistency (T-306)

Output is a single Markdown string ready to paste into Discord. Falls back
gracefully when a section's prerequisites aren't ingested yet.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analysis.cartography import _active_players_by_fight
from analysis.consistency import consistency_for_encounter
from analysis.fault_attribution import classify_wipe_type
from analysis.mit_audit import mit_audit_summary
from db.models import Ability, Event, FaultScore, Fight, FightModel, Report


def _resolve_ability_name(session: Session, ability_id: int | None) -> str:
    if ability_id is None:
        return "(non-attributable)"
    row = session.get(Ability, ability_id)
    if row is None or not row.name:
        return f"ability {ability_id}"
    return f"{row.name} ({ability_id})"


def generate_session_report(session: Session, report_code: str,
                            static_id: int) -> dict[str, Any]:
    """Build the Discord-pasteable summary + the structured data behind it."""
    rep = session.get(Report, report_code)
    if rep is None:
        return {"report_code": report_code, "markdown": "",
                "note": "report not ingested"}

    fights = session.execute(
        select(Fight).where(Fight.report_code == report_code)
        .order_by(Fight.start_time)
    ).scalars().all()
    if not fights:
        return {"report_code": report_code, "markdown": "",
                "note": "no fights in report"}

    encounter_ids = sorted({f.encounter_id for f in fights if f.encounter_id})
    primary_enc = max(encounter_ids,
                      key=lambda e: sum(1 for f in fights if f.encounter_id == e),
                      default=None)
    enc_fights = [f for f in fights if f.encounter_id == primary_enc]
    kills = [f for f in enc_fights if f.is_kill]
    wipes = [f for f in enc_fights if f.is_kill is False]

    # Best progression: lowest fight_percentage on any wipe; kills count as 0%.
    fp_vals = [float(f.fight_percentage) for f in enc_fights
               if f.fight_percentage is not None]
    best_fp = (0.0 if kills else (min(fp_vals) if fp_vals else None))
    best_phase = max((f.last_phase for f in enc_fights
                      if f.last_phase is not None), default=None)

    duration_ms = sum(f.duration_ms or 0 for f in enc_fights)

    # Wipe-type breakdown
    wipe_type_counts: dict[str, int] = defaultdict(int)
    for f in enc_fights:
        wt = classify_wipe_type(session, f.id).get("wipe_type", "unknown")
        wipe_type_counts[wt] += 1

    # Death cartography filtered to this report's fights
    fight_ids = [f.id for f in enc_fights]
    active_by_fight = _active_players_by_fight(session, fight_ids)
    death_rows = session.execute(
        select(Event.fight_id, Event.target_id, Event.ability_game_id)
        .where(Event.fight_id.in_(fight_ids), Event.type == "death")
    ).all()
    deaths_by_ability: dict[int | None, int] = defaultdict(int)
    for fid, tid, aid in death_rows:
        if tid in active_by_fight.get(fid, set()):
            deaths_by_ability[aid] += 1
    top_killers = sorted(
        deaths_by_ability.items(), key=lambda kv: -kv[1]
    )[:5]

    # Mit-audit summary aggregated across fights (scoped to caller's static)
    mit_totals = {"raidwides": 0, "with_plan": 0, "slots_total": 0,
                  "missed_total": 0}
    for f in enc_fights:
        s = mit_audit_summary(session, f.id, static_id)
        mit_totals["raidwides"] += s.get("raidwide_count", 0)
        mit_totals["with_plan"] += s.get("with_plan", 0)
        mit_totals["slots_total"] += s.get("planned_slots_total", 0)
        mit_totals["missed_total"] += s.get("missed_mits_total", 0)
    mit_hit_rate = (
        (mit_totals["slots_total"] - mit_totals["missed_total"])
        / mit_totals["slots_total"]
        if mit_totals["slots_total"] > 0 else None
    )

    # Per-player fault aggregate restricted to this report's fights, scoped to static
    fault_rows = session.execute(
        select(FaultScore).where(FaultScore.fight_id.in_(fight_ids),
                                 FaultScore.static_id == static_id)
    ).scalars().all()
    by_player: dict[int, dict[str, Any]] = defaultdict(
        lambda: {"name": None, "job": None, "root": 0, "cascade": 0,
                 "mit_failure": 0, "unknown": 0, "score": 0.0}
    )
    for fs in fault_rows:
        r = fs.reasons or {}
        b = by_player[fs.player_id]
        b["name"] = r.get("name") or b["name"]
        b["job"] = r.get("job") or b["job"]
        b["root"] += int(r.get("root", 0))
        b["cascade"] += int(r.get("cascade", 0))
        b["mit_failure"] += int(r.get("mit_failure", 0))
        b["unknown"] += int(r.get("unknown", 0))
        b["score"] += float(fs.score or 0)
    fault_top = sorted(
        by_player.values(), key=lambda v: -v["score"],
    )[:5]

    # Worst per-mechanic consistency (only if fight_model + watchlist exist)
    cons = consistency_for_encounter(session, primary_enc, static_id) if primary_enc else {
        "mechanics": []
    }
    cons_worst = cons.get("mechanics", [])[:3]

    md = _render_markdown(
        report_code=report_code,
        primary_enc=primary_enc,
        rep=rep,
        kills=len(kills), wipes=len(wipes), pulls=len(enc_fights),
        duration_ms=duration_ms,
        best_fp=best_fp, best_phase=best_phase,
        wipe_type_counts=dict(wipe_type_counts),
        top_killers=[(_resolve_ability_name(session, aid), n)
                     for aid, n in top_killers],
        mit_totals=mit_totals,
        mit_hit_rate=mit_hit_rate,
        fault_top=fault_top,
        cons_worst=cons_worst,
        session=session,
    )

    return {
        "report_code": report_code,
        "primary_encounter": primary_enc,
        "markdown": md,
        "pulls": len(enc_fights),
        "kills": len(kills),
        "wipes": len(wipes),
        "best_fight_percentage": best_fp,
    }


def _render_markdown(*, report_code, primary_enc, rep, kills, wipes, pulls,
                     duration_ms, best_fp, best_phase, wipe_type_counts,
                     top_killers, mit_totals, mit_hit_rate, fault_top,
                     cons_worst, session) -> str:
    dur_min = duration_ms // 60_000
    dur_sec = (duration_ms % 60_000) // 1000

    parts: list[str] = []
    parts.append(f"**Session: `{report_code}` — encounter {primary_enc}**")
    if rep.start_time:
        parts.append(f"_{rep.start_time.astimezone(timezone.utc).date()} · "
                     f"{pulls} pulls · {kills}K / {wipes}W · "
                     f"{dur_min}m{dur_sec:02d}s combat_")
    headline = []
    if best_phase is not None:
        headline.append(f"best phase **P{best_phase}**")
    if best_fp is not None:
        headline.append(f"best fp **{best_fp:.1f}%** remaining" if best_fp > 0
                        else "**KILL** secured")
    if headline:
        parts.append(" · ".join(headline))

    if wipe_type_counts:
        wt_str = ", ".join(f"{k}: {v}" for k, v in
                            sorted(wipe_type_counts.items(),
                                   key=lambda kv: -kv[1]))
        parts.append(f"**Wipe types**: {wt_str}")

    if top_killers:
        parts.append("**Top killing abilities**")
        for name, n in top_killers:
            parts.append(f"  • {n}× — {name}")

    if mit_totals["slots_total"] > 0:
        rate = f"{mit_hit_rate * 100:.0f}%" if mit_hit_rate is not None else "—"
        parts.append(
            f"**Mit audit**: {mit_totals['raidwides']} raidwides · "
            f"{mit_totals['with_plan']} with plan · "
            f"{mit_totals['slots_total'] - mit_totals['missed_total']}/"
            f"{mit_totals['slots_total']} mits fired ({rate})"
        )
    elif mit_totals["raidwides"] > 0:
        parts.append(
            f"**Mit audit**: {mit_totals['raidwides']} raidwides, no strat plan "
            f"configured yet (set one in Encounters → Strat)"
        )

    if fault_top:
        parts.append("**Fault scores**")
        for p in fault_top:
            display = p["name"] or "?"
            if p["job"]:
                display = f"{display} ({p['job']})"
            sub = []
            if p["root"]: sub.append(f"{p['root']} root")
            if p["mit_failure"]: sub.append(f"{p['mit_failure']} mit-fail")
            if p["cascade"]: sub.append(f"{p['cascade']} cascade")
            sub_str = " · ".join(sub) if sub else "no faults"
            parts.append(f"  • {display}: **{p['score']:.1f}** ({sub_str})")

    if cons_worst:
        parts.append("**Worst consistency**")
        for m in cons_worst:
            name = _resolve_ability_name(session, m["ability_game_id"])
            pct = int(m["clean_rate"] * 100)
            parts.append(
                f"  • P{m['phase']} {name} — clean **{m['occurrences_clean']}/"
                f"{m['occurrences_total']}** ({pct}%)"
            )

    return "\n".join(parts)
