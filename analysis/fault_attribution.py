"""M-FAULT (T-302) strat-aware fault attribution.

For each death in a fight, classify as **root** (the player's own
failure — single-target avoidable they ate, or a body-check they missed)
or **cascade** (collateral damage from someone else's earlier failure or
mit-down state — typically a raidwide they couldn't survive because the
raid was already wounded).

The signal is the killing ability's `type_label` from `fight_model`
(populated by T-203):
  - `tankbuster` / `aoe_party` → root (single- or few-target mechanic; the
    targeted player should have mitigated, swapped, or moved)
  - `raidwide` → cascade-candidate. If mits-up + no preceding death in the
    window → flag for T-303 mit audit; otherwise it's collateral.
  - `enrage` → boss-DPS-check failure, not a player fault
  - non-attributable (`ability_id` null) → cascade (FFLogs' sourceID=-1
    pattern almost always means a follow-up that took someone already-dead-by-someone-else)
  - unknown labels → leave as `unknown`; user reviews via Abilities queue

The wipe-type classifier (`classify_wipe_type`) categorizes whole fights
as enrage-gated / mechanics-gated / mixed — useful headline for T-307
Discord reports.

Writes per-(fight, player) rows to `fault_scores` (already in schema).
`score` is roots × 1.0 + cascades × 0.1 (roots count 10× — a root fault
*caused* the cascade chain). `reasons` carries the structured breakdown.

T-303 (mit audit) and T-304 (disambiguation) will refine the cascade
classification — this T-302 ship is the per-death structural pass that
those modules build on.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from analysis._encounter import canonical_encounter_id, encounter_id_group
from analysis.cartography import _active_players_by_fight
from analysis._jobs import HEALER_JOBS, TANK_JOBS, role_of
from analysis.strat_config import encode_mechanic_ref
from db.models import (
    AbilityLabel, CharacterAlias, Combatant, Event, FaultScore, Fight,
    FightModel, Member, StratConfig, WatchedReport,
)

ROOT_SCORE = 1.0
CASCADE_SCORE = 0.1
MIT_FAILURE_SCORE = 1.0  # same weight as root — it's the originating fault
# v1.16.0: heal failure. Triggers when a raidwide kills a player but the
# planned mits successfully fired — under proper mit math the raid should
# be heal-survivable, so the kill implies HP wasn't topped, so the healers
# missed the recover-window. The dying player gets ZERO score weight (they
# can't materially help being below 80% if mits were up); the active
# healers split HEAL_FAILURE_TOTAL_WEIGHT equally between them.
HEAL_FAILURE_TOTAL_WEIGHT = 1.0
HEAL_FAILURE_VICTIM_SCORE = 0.0  # victim gets no fault weight for this death
PRECEDING_DEATH_WINDOW_MS = 5_000

# v1.14.5: phase-weighted scoring for the encounter aggregate (Home fault
# table). Applied AT READ TIME in `fault_aggregate_for_encounter` so the
# raw per-fight FaultScore.score rows don't need recomputing when the
# formula changes. Three multiplicative factors:
#   1. Phase severity (gentle quadratic) — late phases penalize more.
#   2. Within-phase severity — wipes nearer the boss kill weigh more.
#   3. Prog relevance — wipes at the current prog wall hurt more than
#      wipes in already-cleared phases.

def _phase_severity(phase: int | None) -> float:
    """Non-linear absolute phase penalty. Quadratic-ish, mild — P5 is
    ~1.7× P3, P7 is ~5× P0. Returns 1.0 for unknown / negative phases."""
    if phase is None or phase < 0:
        return 1.0
    return 1.0 + phase * (phase + 1) / 14.0


def _within_phase_severity(fight_pct: float | None) -> float:
    """Boss HP at wipe time as a within-phase modifier. fp=100% (just
    entered phase) → 1.0; fp=0% (boss almost dead) → 1.5. Same phase, but
    a wipe with the boss at 5% HP carries more weight than one at 80%."""
    if fight_pct is None:
        return 1.0
    fp = max(0.0, min(100.0, float(fight_pct)))
    return 1.0 + (1.0 - fp / 100.0) * 0.5


def _prog_distance(phase: int | None, fight_pct: float | None) -> float | None:
    """Continuous prog measure combining phase + within-phase progress
    (v1.16.0). `prog = phase + (1 - fp/100)`. P3 fresh entry = 3.0,
    P3 boss-near-dead = 3.99, P4 fresh entry = 4.0. Smooth across phase
    boundaries — no cliffs.

    Returns None when phase is None (we can't reason about prog distance
    without a phase number). When fight_pct is None we fall back to
    phase alone (i.e. treat it as fp=100, freshly entered)."""
    if phase is None or phase < 0:
        return None
    if fight_pct is None:
        return float(phase)
    fp = max(0.0, min(100.0, float(fight_pct)))
    return phase + (1.0 - fp / 100.0)


NEAR_WALL_TOLERANCE = 0.5  # within half a phase of the wall = no de-weighting
PROG_DECAY_K = 0.3  # exp(-K * (delta - tolerance)) beyond the tolerance zone


def _prog_relevance(wipe_phase: int | None,
                    best_prog_at_time: float | None,
                    fight_pct: float | None = None) -> float:
    """Wipes near the current prog wall = full weight. Wipes well past
    the wall get exp-decayed down to a 0.3× floor.

    v1.16.1: now uses a CONTINUOUS `best_prog_at_time` (a prog_distance,
    not a phase integer) AND a near-wall plateau. The plateau means a
    backslide that's <0.5 prog units behind the wall stays at full weight,
    matching the intuition that a near-wall wipe shouldn't be penalized
    just because it landed a few mechanics before the high-water mark.
    Beyond the plateau the decay is exp(-K * (delta - tolerance)).

    Example math with default constants (tolerance=0.5, K=0.3):
      - same prog as wall → 1.00
      - 0.3 behind (mid-phase backslide) → 1.00 (within plateau)
      - 0.7 behind → exp(-0.06) ≈ 0.94
      - 1.5 behind (one phase) → exp(-0.30) ≈ 0.74
      - 3.0 behind (two phases) → exp(-0.75) ≈ 0.47
      - 5.0 behind → exp(-1.35) ≈ 0.26 → clamped to 0.3
    """
    if wipe_phase is None or best_prog_at_time is None:
        return 1.0
    import math
    our_prog = _prog_distance(wipe_phase, fight_pct)
    if our_prog is None:
        return 1.0
    delta = best_prog_at_time - our_prog
    if delta <= NEAR_WALL_TOLERANCE:
        return 1.0
    return max(0.3, math.exp(-PROG_DECAY_K * (delta - NEAR_WALL_TOLERANCE)))


# v1.16.0: combined per-wipe multiplier is capped so one extreme wipe
# can't dominate a player's whole-encounter score. The four factors
# (phase × within × prog × repeat) can otherwise compound to >20× in
# pathological cases — phase=5 + near-kill + at-wall + serial offender.
COMBINED_MULTIPLIER_CAP = 8.0


def fight_score_multiplier(last_phase: int | None,
                           fight_percentage: float | None,
                           best_prog_at_time: float | int | None) -> float:
    """Combined per-wipe score multiplier (v1.14.5, refined v1.16.x).
    Phase severity × within-phase × prog relevance, then capped at
    `COMBINED_MULTIPLIER_CAP` to bound one-wipe dominance.

    v1.16.1: `best_prog_at_time` is now a continuous prog distance, not a
    phase integer. Callers tracking only a high-water phase can pass it
    as an int — we treat int N as `_prog_distance(N, None) = N`."""
    if isinstance(best_prog_at_time, int):
        best_prog: float | None = float(best_prog_at_time)
    elif isinstance(best_prog_at_time, float):
        best_prog = best_prog_at_time
    else:
        best_prog = None
    raw = (_phase_severity(last_phase)
           * _within_phase_severity(fight_percentage)
           * _prog_relevance(last_phase, best_prog, fight_percentage))
    return min(raw, COMBINED_MULTIPLIER_CAP)


# v1.14.6: repeat-offender amplifier for past-wall root deaths. Each time
# a player is the root of a wipe in a phase the group has already moved
# past, this multiplier grows — and the curve self-scales by the player's
# total wipe attendance so the same 5 offenses hit a 100-wipe static way
# harder than a 1000-wipe static.
REPEAT_PENALTY_K = 4.0  # exp(K * rate). K=4: 5%→1.22, 20%→2.23, 40%→cap
REPEAT_PENALTY_CAP = 5.0  # max multiplier — prevents runaway at extreme rates
# v1.16.0: floor the denominator at 20 wipes so the rate doesn't go wild
# during the first few attempts. 1 past-wall root in your first 2 wipes
# is noise; the floor turns 1/2 → 1/max(2,20) = 5% (a 1.22× nudge) instead
# of capping at 5×. Once the player accumulates real attendance the floor
# stops mattering.
REPEAT_RATE_MIN_DENOMINATOR = 20


def repeat_offender_multiplier(past_wall_offense_count: int,
                                total_wipes_attended: int) -> float:
    """exp-scaled penalty on the rate of past-wall *fault offenses*
    (roots + mit_failures combined — both are 1.0-weight originating
    faults, v1.16.0 extended from roots-only to also count mit_failures).

    Cumulative count + cumulative attendance, both inclusive of the
    current wipe. Denominator floored at `REPEAT_RATE_MIN_DENOMINATOR`
    so cold-start noise doesn't spike the multiplier in early prog.
    Caps at `REPEAT_PENALTY_CAP` so a 60% rate doesn't blow up.
    """
    import math
    if past_wall_offense_count <= 0:
        return 1.0
    denom = max(total_wipes_attended, REPEAT_RATE_MIN_DENOMINATOR)
    rate = past_wall_offense_count / denom
    return min(REPEAT_PENALTY_CAP, math.exp(REPEAT_PENALTY_K * rate))

# v1.12.0 classifier overhaul.
# Cascade pressure is a continuous score (sum of decay-weighted preceding
# raid-wounding deaths) — replaces the binary "death in last 5s" cliff.
# 1.0 at t-0, 0.0 at t-5s, linear decay between. Threshold at 0.5 (≥ half
# a full-weight preceding death) flips classification from root to cascade
# for raidwide deaths with no mit plan to consult.
CASCADE_PRESSURE_THRESHOLD = 0.5
# Only these labels mark a preceding death as "raid-wounding" — i.e. the
# kind of failure that pressures healers and mit budget for follow-ups.
# A tank dying to a single-target tankbuster doesn't pressure the next
# raidwide; it's an independent fault.
RAID_WOUNDING_LABELS = ("raidwide", "aoe_party")
# Raidwide death attribution lookback — same heuristic T-304 uses.
RAIDWIDE_DEATH_LOOKBACK_MS = 15_000

# v1.11.0 survive-fault signals (PLAN §3 Invariant 5).
# Damage Down is FFXIV's "survive-your-mistake" debuff — strong evidence of
# botched mechanics that didn't kill the player but did degrade their output.
DAMAGE_DOWN_SCORE = 0.5
# Avoidable damage is summed in raw damage and normalized: every 100k of
# damage a non-tank ate from tankbusters (or unattributed-but-targeted hits)
# is worth ~half a root. Tuneable — too high and noisy heals dominate; too
# low and big eaters disappear.
AVOIDABLE_DAMAGE_PER_POINT = 100_000
AVOIDABLE_DAMAGE_SCORE_CAP = 5.0  # cap per fight so one super-hit can't dominate
# v1.16.0: per-hit floor — skip tankbuster damage events below this threshold.
# Tankbusters splash to other rows for trivial amounts (a few thousand HP);
# without a floor those splash ticks pile up on whichever non-tank caught the
# AoE wash and shouldn't count as "avoidable damage I ate". 50k is roughly
# 1/3 of a base raidwide hit at current iLvl — clearly intentional impact,
# not splash. Tune up if false positives keep appearing.
AVOIDABLE_DAMAGE_MIN_HIT = 50_000


def classify_wipe_type(session: Session, fight_id: int,
                       version: int = 1) -> dict[str, Any]:
    """Classify a whole fight as enrage_dps / mechanics / body_check / mixed.

    Heuristic:
      - is_kill → 'kill'
      - last death's killing ability labeled 'enrage' in fight_model → 'enrage_dps'
      - majority of deaths to ability with label 'aoe_party' or 'tankbuster'
        (the body-check family) → 'body_check'
      - majority to 'raidwide' or non-attributable → 'mechanics' (cascade)
      - otherwise 'mixed'
    """
    fight = session.get(Fight, fight_id)
    if fight is None:
        return {"fight_id": fight_id, "wipe_type": "unknown",
                "note": "fight not found"}
    if fight.is_kill:
        return {"fight_id": fight_id, "wipe_type": "kill"}

    active = _active_players_by_fight(session, [fight_id]).get(fight_id, set())
    deaths = session.execute(
        select(Event.target_id, Event.ts, Event.ability_game_id)
        .where(Event.fight_id == fight_id, Event.type == "death")
        .order_by(Event.ts)
    ).all()
    player_deaths = [(t, ts, aid) for t, ts, aid in deaths if t in active]

    if not player_deaths:
        return {"fight_id": fight_id, "wipe_type": "unknown",
                "note": "no player deaths"}

    # v1.17.0: fight_model lives at the canonical encounter ID.
    fm_rows = session.execute(
        select(FightModel.ability_game_id, FightModel.type_label)
        .where(FightModel.encounter_id == canonical_encounter_id(fight.encounter_id),
               FightModel.version == version)
    ).all()
    label_of = {aid: label for aid, label in fm_rows}

    # Last death's ability
    _, _, last_aid = player_deaths[-1]
    if label_of.get(last_aid) == "enrage":
        return {"fight_id": fight_id, "wipe_type": "enrage_dps",
                "deaths": len(player_deaths)}

    # Tally labels across all deaths
    label_counts: dict[str, int] = defaultdict(int)
    for _t, _ts, aid in player_deaths:
        label = label_of.get(aid) or ("non_attributable" if aid is None else "unknown")
        label_counts[label] += 1

    total = len(player_deaths)
    body_check = label_counts.get("aoe_party", 0) + label_counts.get("tankbuster", 0)
    cascade = label_counts.get("raidwide", 0) + label_counts.get("non_attributable", 0)

    if body_check / total >= 0.5:
        wipe_type = "body_check"
    elif cascade / total >= 0.5:
        wipe_type = "mechanics"
    else:
        wipe_type = "mixed"

    return {
        "fight_id": fight_id,
        "wipe_type": wipe_type,
        "deaths": total,
        "label_breakdown": dict(label_counts),
    }


def _death_kind(killing_ability_id: int | None,
                ability_label: str | None,
                cascade_pressure: float,
                mit_audit_info: dict[str, Any] | None = None) -> str:
    """Map (killing_ability, label, context) → 'root' | 'cascade' |
    'mit_failure' | 'enrage' | 'unknown' (v1.12.0).

    Args:
      killing_ability_id: ability ID that landed the killing blow, or None
        for FFLogs' sourceID=-1 non-attributable pattern.
      ability_label: T-203 type_label for the killing ability.
      cascade_pressure: continuous-decay weight of preceding raid-wounding
        deaths (PLAN-spec "raid was already wounded" signal). See
        `CASCADE_PRESSURE_THRESHOLD`.
      mit_audit_info: if the killing ability is a raidwide AND a strat plan
        exists for the specific occurrence, this is `{no_plan, missed_count,
        planned_slots}` for that cast. None when no plan exists OR no
        matching cast was found.

    v1.12.0 changes:
      - **Mit-aware as primary** (#4): for raidwide deaths, consult the
        mit audit BEFORE the preceding-death heuristic. Mits missed →
        mit_failure (full root weight); mits all fired → cascade (heal/mit
        overwhelm despite plan); no plan → fall through.
      - **Strict causality on cascade** (#3): the preceding-death window
        only counts deaths whose ability label is raid-wounding
        (`raidwide` / `aoe_party`) — a tank dying to a single-target
        tankbuster doesn't pressure subsequent raidwides.
      - **Continuous decay** (#9): preceding-death weight decays linearly
        from 1.0 at t-0 to 0.0 at t-5s. Threshold at 0.5 means the
        boundary between root and cascade is no longer a sharp cliff —
        but a single death 100ms before still flips it cleanly.
    """
    if killing_ability_id is None:
        # FFLogs sourceID=-1 / no killing ability is almost always a follow-up.
        return "cascade"
    if ability_label == "enrage":
        return "enrage"
    if ability_label in ("tankbuster", "aoe_party"):
        return "root"
    if ability_label == "raidwide":
        # Mit-aware primary path (v1.12.0).
        if mit_audit_info is not None and not mit_audit_info.get("no_plan"):
            if mit_audit_info.get("missed_count", 0) > 0:
                return "mit_failure"
            # v1.16.0: plan fully fired + raidwide still killed → "heal_failure"
            # (raidwide should be survivable from full HP with mits up; if it
            # killed, the player wasn't topped). Caller splits the weight
            # across the active healers in compute_fault_scores_for_fight.
            return "heal_failure"
        # No plan to consult → preceding-death heuristic.
        return ("cascade"
                if cascade_pressure >= CASCADE_PRESSURE_THRESHOLD
                else "root")
    # cosmetic / unknown / damage_down → leave as unknown
    return "unknown"


def _cascade_pressure(now_ts: int,
                       preceding_deaths: list[tuple[int, str | None]]) -> float:
    """Sum of decay-weighted preceding raid-wounding deaths within the window.

    Each qualifying preceding death contributes `(1 - dt/W)` where dt is the
    time since it occurred and W = `PRECEDING_DEATH_WINDOW_MS`. Deaths
    outside the window or with non-raid-wounding labels contribute zero.
    """
    pressure = 0.0
    for prev_ts, prev_label in preceding_deaths:
        dt = now_ts - prev_ts
        if dt <= 0 or dt > PRECEDING_DEATH_WINDOW_MS:
            continue
        if prev_label not in RAID_WOUNDING_LABELS:
            continue
        pressure += 1.0 - (dt / PRECEDING_DEATH_WINDOW_MS)
    return pressure


def _avoidable_damage_by_player(
    session: Session, fight_id: int, label_of: dict[int, str],
    name_job: dict[int, tuple[str | None, str | None]],
) -> dict[int, int]:
    """Per-player damage taken that the player shouldn't have eaten (v1.11.0).

    The clear cases keyed on T-203 labels:
      - **tankbuster** hitting a non-tank → avoidable (someone else should've
        eaten it via swap, or it shouldn't have hit at all).
      - **aoe_party** is body-check / spread / stack territory — without
        strat_config we can't tell expected-target from non-target, so we
        skip it here. v1.14.0 strat-aware ship will refine this.
      - **raidwide** hits everyone by design → not avoidable.
      - **enrage** / **cosmetic** / **unknown** → not classified as avoidable
        (don't blame on a guess).

    `damage` events only (excludes `calculateddamage` to avoid double-count
    of mitigated/buffed deltas — same convention T-007 uses).
    """
    rows = session.execute(
        select(Event.target_id, Event.ability_game_id, Event.amount)
        .where(
            Event.fight_id == fight_id,
            Event.type == "damage",
            Event.target_id.is_not(None),
            Event.ability_game_id.is_not(None),
        )
    ).all()

    out: dict[int, int] = defaultdict(int)
    for target_id, aid, amount in rows:
        if amount is None or amount <= 0:
            continue
        # v1.16.0: per-hit floor — only count damage events large enough to
        # be a real eat, not splash ticks bleeding to neighboring rows.
        if amount < AVOIDABLE_DAMAGE_MIN_HIT:
            continue
        label = label_of.get(aid)
        if label != "tankbuster":
            continue
        _, job = name_job.get(target_id, (None, None))
        if job in TANK_JOBS:
            continue
        out[target_id] += int(amount)
    return out


def _expected_job_roles_from_role_map(
    assignments: dict[str, Any] | None,
) -> set[str] | None:
    """Translate a strat_config role_map (`{slot_name: MT|OT|H1|H2|D1..D4}`)
    into the set of FFXIV job roles ({tank, healer, dps}) that strat
    expects to be targeted by this mechanic.

    Used by v1.14.0 body-check fault refinement: a DPS dying to a tower
    soak assigned to {MT, OT, H1, H2} → tank/healer-only mechanic, so the
    DPS shouldn't have been hit → cascade, not root.

    Returns None when no role_map info is configured (caller falls back
    to existing classification). Returns `{"tank","healer","dps"}` when
    `any` is anywhere in the map (wildcard).
    """
    role_map = (assignments or {}).get("role_map") or {}
    if not role_map:
        return None
    out: set[str] = set()
    for _slot, role_slot in role_map.items():
        if role_slot is None:
            continue
        if role_slot == "any":
            return {"tank", "healer", "dps"}
        if role_slot in ("MT", "OT"):
            out.add("tank")
        elif role_slot in ("H1", "H2"):
            out.add("healer")
        elif role_slot in ("D1", "D2", "D3", "D4"):
            out.add("dps")
    return out or None


def _aoe_party_casts(session: Session, fight: Fight,
                      version: int = 1) -> list[dict[str, Any]]:
    """Per aoe_party cast in the fight: `{cast_ts, ability_id, occurrence}`.
    Mirrors the raidwide-cast lookup from mit_audit but scoped to aoe_party.
    """
    aoe_ids = list(session.execute(
        select(FightModel.ability_game_id)
        .where(FightModel.encounter_id == canonical_encounter_id(fight.encounter_id),
               FightModel.version == version,
               FightModel.type_label == "aoe_party")
    ).scalars().all())
    if not aoe_ids:
        return []
    cast_rows = session.execute(
        select(Event.ts, Event.ability_game_id)
        .where(Event.fight_id == fight.id,
               Event.type == "cast",
               Event.ability_game_id.in_(aoe_ids))
        .order_by(Event.ts)
    ).all()
    occurrence_counter: dict[int, int] = defaultdict(int)
    out = []
    for ts, aid in cast_rows:
        occ = occurrence_counter[aid]
        occurrence_counter[aid] += 1
        out.append({"cast_ts": int(ts), "ability_id": int(aid),
                    "occurrence": occ})
    return out


# v1.16.1 / v1.16.3: non-attributable death inference. Logic + helpers
# moved to `analysis.death_inference` (shared with `analysis.cartography`).
# We re-export the public names here for tests + downstream callers that
# already imported them.
from analysis.death_inference import (
    INFER_ACTIONABLE_LABELS,
    INFER_CACTBOT_TOLERANCE_MS,
    INFER_LOOKBACK_MS,
    infer_killer_from_cast_proximity as _infer_killer_from_cast_proximity,
    infer_killer_from_cactbot_drift as _infer_killer_from_cactbot_drift,
    build_phase_drift_map as _build_phase_drift_map,
)


def _damage_down_count_by_player(
    session: Session, fight_id: int,
) -> dict[int, int]:
    """Per-player count of Damage Down debuff applications (v1.11.0).

    Damage Down is FFXIV's "survive-your-mistake" penalty — applied when a
    player botches a body-check but doesn't die. T-108's auto-classifier
    labels these abilities with the `damage_down` label; we count
    `applydebuff` events on those IDs per player. Skips `refreshdebuff`
    because that's a re-stack of an existing application — we only want
    the moment the player botched.
    """
    label_ids = session.execute(
        select(AbilityLabel.ability_game_id)
        .where(AbilityLabel.label == "damage_down")
    ).scalars().all()
    if not label_ids:
        return {}
    rows = session.execute(
        select(Event.target_id)
        .where(
            Event.fight_id == fight_id,
            Event.type == "applydebuff",
            Event.ability_game_id.in_(label_ids),
            Event.target_id.is_not(None),
        )
    ).all()
    out: dict[int, int] = defaultdict(int)
    for (target_id,) in rows:
        out[target_id] += 1
    return out


def compute_fault_scores_for_fight(
    session: Session, fight_id: int, static_id: int,
    *, version: int = 1,
) -> dict[str, Any]:
    """Walk deaths in `fight_id`, classify each as root/cascade/enrage/unknown,
    upsert per-player aggregates into `fault_scores`.

    v1.11.0 also tallies **avoidable damage taken** (tankbusters that hit
    non-tanks) and **Damage Down applications** (survive-fault flag) per
    PLAN §3 Invariant 5 — silent contributors who don't show up in the
    death-only signal."""
    fight = session.get(Fight, fight_id)
    if fight is None:
        return {"fight_id": fight_id, "labeled": 0, "note": "fight not found"}

    active = _active_players_by_fight(session, [fight_id]).get(fight_id, set())

    deaths = session.execute(
        select(Event.target_id, Event.ts, Event.ability_game_id)
        .where(Event.fight_id == fight_id, Event.type == "death")
        .order_by(Event.ts)
    ).all()

    fm_rows = session.execute(
        select(FightModel.ability_game_id, FightModel.type_label)
        .where(FightModel.encounter_id == canonical_encounter_id(fight.encounter_id),
               FightModel.version == version)
    ).all()
    label_of = {aid: label for aid, label in fm_rows}

    combatants = session.execute(
        select(Combatant).where(Combatant.fight_id == fight_id)
    ).scalars().all()
    name_job = {c.player_id: (c.name, c.job) for c in combatants}

    # v1.12.0: load mit audit upfront so raidwide deaths can consult it.
    # We import here (not at module top) to avoid circular import with
    # mit_audit which depends on strat_config which is independent.
    from analysis.mit_audit import mit_audit_for_fight
    audit = mit_audit_for_fight(session, fight_id, static_id, version=version)
    # Index by ability_id → sorted list of casts for fast death→cast lookup.
    audit_casts_by_aid: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for cast in audit.get("raidwide_casts", []):
        audit_casts_by_aid[cast["ability_id"]].append(cast)
    for aid in audit_casts_by_aid:
        audit_casts_by_aid[aid].sort(key=lambda c: c["cast_ts"])

    def _audit_for_raidwide_death(ability_id: int, death_ts: int):
        """Find the cast that most likely killed this player. Returns the
        cast dict (with no_plan/missed_count/planned_slots) or None when no
        matching cast falls within the lookback window."""
        casts = audit_casts_by_aid.get(ability_id, ())
        best = None
        for c in casts:
            if c["cast_ts"] > death_ts:
                break
            if death_ts - c["cast_ts"] <= RAIDWIDE_DEATH_LOOKBACK_MS:
                best = c
        return best

    # v1.16.1 / v1.16.3: shared two-layer inference for non-attributable
    # deaths (cast proximity + cactbot drift). Pre-load context only when
    # there's at least one non-attributable death — most fights don't.
    has_nonattributable = any(
        target_id in active and aid is None
        for target_id, _ts, aid in deaths
    )
    if has_nonattributable:
        from analysis import death_inference as di
        _infer_ctx = di.build_inference_context(
            session, fight_id, fight.encounter_id, version=version,
        )
        def _infer_killer(death_ts: int):
            return di.infer_killer(_infer_ctx, death_ts)
    else:
        def _infer_killer(death_ts: int):
            return (None, None, None)

    # First-pass: assign root/cascade/mit_failure per death using
    # cascade-pressure + mit audit context (v1.12.0). v1.16.1: also tries
    # to infer the killer for non-attributable deaths so they classify as
    # something more specific than 'cascade'.
    death_records: list[dict[str, Any]] = []
    # Tracks (ts, label) for each prior death so cascade pressure stays cheap.
    deaths_seen: list[tuple[int, str | None]] = []
    for target_id, ts, raw_aid in deaths:
        if target_id not in active or ts is None:
            continue
        # v1.16.1: try to fill in non-attributable deaths.
        inferred_aid: int | None = None
        inferred_label: str | None = None
        inferred_from: str | None = None
        if raw_aid is None:
            inferred_aid, inferred_label, inferred_from = _infer_killer(int(ts))
        # Use the inferred values for classification when present, else raw.
        aid = raw_aid if raw_aid is not None else inferred_aid
        label = label_of.get(aid) if aid is not None else inferred_label
        pressure = _cascade_pressure(int(ts), deaths_seen)
        audit_info = (_audit_for_raidwide_death(aid, int(ts))
                      if label == "raidwide" and aid is not None else None)
        kind = _death_kind(aid, label, pressure, audit_info)
        death_records.append({
            "player_id": target_id,
            "ts": int(ts),
            "ability_game_id": raw_aid,  # preserve original (None if not attrib)
            "ability_label": label,
            "kind": kind,
            "cascade_pressure": round(pressure, 3),
            "mit_audit": (
                {"no_plan": audit_info["no_plan"],
                 "missed_count": audit_info["missed_count"]}
                if audit_info is not None else None
            ),
            **({"inferred_ability_id": inferred_aid,
                "inferred_ability_label": inferred_label,
                "inferred_from": inferred_from}
               if inferred_from is not None else {}),
        })
        deaths_seen.append((int(ts), label))

    # v1.14.0 body-check refinement: load strat_config assignments + locate
    # the aoe_party cast that killed each aoe_party victim, then reclassify
    # root→cascade for players whose job role isn't in the assignment's
    # expected target set. Means a DPS dying to a tower-soak labeled aoe_party
    # but assigned only to tank+healer slots gets cascade weight (it wasn't
    # their job to take it) — surfaces the *actually-assigned* player's
    # absence as the issue.
    strat_rows = session.execute(
        select(StratConfig.mechanic_ref, StratConfig.assignments)
        .where(StratConfig.encounter_id == canonical_encounter_id(fight.encounter_id),
               StratConfig.static_id == static_id)
    ).all()
    strat_assignments_by_ref: dict[str, dict[str, Any]] = {
        ref: assigns for ref, assigns in strat_rows
    }
    aoe_casts = _aoe_party_casts(session, fight, version=version)
    aoe_casts_by_aid: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for c in aoe_casts:
        aoe_casts_by_aid[c["ability_id"]].append(c)

    def _aoe_cast_for_death(ability_id: int, death_ts: int):
        best = None
        for c in aoe_casts_by_aid.get(ability_id, ()):
            if c["cast_ts"] > death_ts:
                break
            if death_ts - c["cast_ts"] <= RAIDWIDE_DEATH_LOOKBACK_MS:
                best = c
        return best

    body_check_reclassified = 0
    for d in death_records:
        if d["kind"] != "root" or d["ability_label"] != "aoe_party":
            continue
        cast = _aoe_cast_for_death(d["ability_game_id"], d["ts"])
        if cast is None:
            continue
        ref = encode_mechanic_ref(cast["ability_id"], cast["occurrence"])
        strat = strat_assignments_by_ref.get(ref)
        expected_roles = _expected_job_roles_from_role_map(strat)
        if not expected_roles:
            continue
        _, player_job = name_job.get(d["player_id"], (None, None))
        player_role = role_of(player_job)
        if player_role and player_role not in expected_roles:
            d["kind"] = "cascade"
            d["body_check_reclassified"] = True
            body_check_reclassified += 1

    # v1.16.0 heal-failure attribution. When a player dies to a raidwide
    # whose planned mits all fired (kind=='heal_failure'), the dying player
    # carries no fault weight for this death — the raid should have been
    # heal-survivable. Each healer who was ALIVE at death_ts splits
    # HEAL_FAILURE_TOTAL_WEIGHT equally. If only one healer is alive at
    # the moment, that healer gets the full weight; if zero (both healers
    # dead), the death falls back to cascade (the chain is too broken to
    # blame the healers fairly).
    healer_pids = {c.player_id for c in combatants
                   if c.player_id in active and c.job in HEALER_JOBS}
    healer_death_ts = {}
    for target_id, ts, _aid in deaths:
        if target_id in healer_pids and ts is not None:
            # First death timestamp per healer.
            if target_id not in healer_death_ts:
                healer_death_ts[target_id] = int(ts)

    def _alive_healers_at(ts: int) -> list[int]:
        return [pid for pid in healer_pids
                if pid not in healer_death_ts
                or healer_death_ts[pid] > ts]

    # Track per-healer heal_failure attribution: count + score weight.
    heal_failure_caused: dict[int, dict[str, float]] = defaultdict(
        lambda: {"count": 0, "score": 0.0, "incidents": []}
    )
    for d in death_records:
        if d["kind"] != "heal_failure":
            continue
        alive = _alive_healers_at(d["ts"])
        if not alive:
            # Both healers dead — fall back to cascade. Not the healers'
            # fault when the chain is broken; the dying player keeps a
            # small cascade weight as the v1.12.0 code would have assigned.
            d["kind"] = "cascade"
            d["heal_failure_no_healers"] = True
            continue
        share = HEAL_FAILURE_TOTAL_WEIGHT / len(alive)
        for hpid in alive:
            heal_failure_caused[hpid]["count"] += 1
            heal_failure_caused[hpid]["score"] += share
            heal_failure_caused[hpid]["incidents"].append({
                "victim_player_id": d["player_id"],
                "ts": d["ts"],
                "ability_game_id": d["ability_game_id"],
                "share": round(share, 3),
            })
        d["heal_failure_healers"] = list(alive)
        d["heal_failure_share_each"] = round(share, 3)

    # v1.11.0 survive-fault signals (computed once per fight; merged below).
    avoidable_dmg = _avoidable_damage_by_player(session, fight_id,
                                                label_of, name_job)
    damage_downs = _damage_down_count_by_player(session, fight_id)

    # Aggregate per-player. We also union active-players-with-survive-fault
    # into player_agg even if they didn't die — they still need a row so
    # the consumer Home surfaces them.
    player_agg: dict[int, dict[str, Any]] = defaultdict(
        lambda: {"root": 0, "cascade": 0, "mit_failure": 0,
                 "heal_failure": 0,
                 "heal_failure_caused": 0,
                 "heal_failure_caused_score": 0.0,
                 "heal_failure_incidents": [],
                 "enrage": 0, "unknown": 0,
                 "avoidable_damage": 0, "damage_downs": 0,
                 "deaths": []}
    )
    for d in death_records:
        bucket = player_agg[d["player_id"]]
        bucket[d["kind"]] += 1
        bucket["deaths"].append({
            "ts": d["ts"], "ability_game_id": d["ability_game_id"],
            "ability_label": d["ability_label"], "kind": d["kind"],
            "cascade_pressure": d["cascade_pressure"],
            "mit_audit": d["mit_audit"],
            **({"heal_failure_healers": d["heal_failure_healers"],
                "heal_failure_share_each": d["heal_failure_share_each"]}
               if d.get("heal_failure_healers") else {}),
            **({"inferred_ability_id": d["inferred_ability_id"],
                "inferred_ability_label": d["inferred_ability_label"],
                "inferred_from": d["inferred_from"]}
               if d.get("inferred_from") else {}),
        })
    for hpid, hdata in heal_failure_caused.items():
        bucket = player_agg[hpid]
        bucket["heal_failure_caused"] = hdata["count"]
        bucket["heal_failure_caused_score"] = round(hdata["score"], 3)
        bucket["heal_failure_incidents"] = hdata["incidents"]
    for pid, amount in avoidable_dmg.items():
        if pid in active:
            player_agg[pid]["avoidable_damage"] += amount
    for pid, count in damage_downs.items():
        if pid in active:
            player_agg[pid]["damage_downs"] += count

    # Write to fault_scores (replace existing rows for this fight + static)
    session.execute(delete(FaultScore).where(
        FaultScore.fight_id == fight_id,
        FaultScore.static_id == static_id,
    ))
    session.flush()
    for pid, agg in player_agg.items():
        # v1.16.0 composite score:
        #   death_score = root × 1.0 + mit_failure × 1.0 + cascade × 0.1
        #                 + heal_failure × 0.0  (victim takes no weight)
        #                 + heal_failure_caused_score  (healer's share)
        death_score = (agg["root"] * ROOT_SCORE
                       + agg["mit_failure"] * MIT_FAILURE_SCORE
                       + agg["cascade"] * CASCADE_SCORE
                       + agg["heal_failure"] * HEAL_FAILURE_VICTIM_SCORE
                       + agg["heal_failure_caused_score"])
        avoidable_score = min(
            agg["avoidable_damage"] / AVOIDABLE_DAMAGE_PER_POINT,
            AVOIDABLE_DAMAGE_SCORE_CAP,
        )
        damage_down_score = agg["damage_downs"] * DAMAGE_DOWN_SCORE
        score = death_score + avoidable_score + damage_down_score

        # Confidence: fraction of deaths the classifier had a clear verdict on.
        total_deaths = (agg["root"] + agg["cascade"] + agg["mit_failure"]
                        + agg["heal_failure"]
                        + agg["enrage"] + agg["unknown"])
        classified = (agg["root"] + agg["cascade"] + agg["mit_failure"]
                      + agg["heal_failure"] + agg["enrage"])
        classified_fraction = (
            round(classified / total_deaths, 3) if total_deaths > 0
            else None
        )

        name, job = name_job.get(pid, (None, None))
        session.add(FaultScore(
            static_id=static_id,
            fight_id=fight_id,
            player_id=pid,
            score=score,
            reasons={
                "name": name,
                "job": job,
                "root": agg["root"],
                "cascade": agg["cascade"],
                "mit_failure": agg["mit_failure"],
                "heal_failure": agg["heal_failure"],
                "heal_failure_caused": agg["heal_failure_caused"],
                "heal_failure_caused_score": agg["heal_failure_caused_score"],
                "heal_failure_incidents": agg["heal_failure_incidents"],
                "enrage": agg["enrage"],
                "unknown": agg["unknown"],
                "avoidable_damage": agg["avoidable_damage"],
                "damage_downs": agg["damage_downs"],
                "death_score": round(death_score, 3),
                "avoidable_score": round(avoidable_score, 3),
                "damage_down_score": round(damage_down_score, 3),
                "classified_fraction": classified_fraction,
                "deaths": agg["deaths"],
            },
        ))
    session.commit()

    return {
        "fight_id": fight_id,
        "labeled": len(death_records),
        "players_affected": len(player_agg),
        "body_check_reclassified": body_check_reclassified,
        "label_counts": {
            "root": sum(1 for d in death_records if d["kind"] == "root"),
            "cascade": sum(1 for d in death_records if d["kind"] == "cascade"),
            "mit_failure": sum(1 for d in death_records
                               if d["kind"] == "mit_failure"),
            "heal_failure": sum(1 for d in death_records
                                if d["kind"] == "heal_failure"),
            "enrage": sum(1 for d in death_records if d["kind"] == "enrage"),
            "unknown": sum(1 for d in death_records if d["kind"] == "unknown"),
        },
    }


def fault_scores_for_fight(session: Session, fight_id: int,
                           static_id: int) -> dict[str, Any]:
    """Read persisted fault_scores rows for one fight, scoped to static."""
    rows = session.execute(
        select(FaultScore).where(FaultScore.fight_id == fight_id,
                                 FaultScore.static_id == static_id)
        .order_by(FaultScore.score.desc())
    ).scalars().all()
    return {
        "fight_id": fight_id,
        "players": [
            {
                "player_id": r.player_id,
                "score": float(r.score) if r.score is not None else 0.0,
                "reasons": r.reasons or {},
            }
            for r in rows
        ],
    }


def _new_job_bucket() -> dict[str, Any]:
    return {
        "fights": 0, "root": 0, "cascade": 0, "mit_failure": 0,
        "heal_failure": 0, "heal_failure_caused": 0,
        "enrage": 0, "unknown": 0,
        "avoidable_damage": 0, "damage_downs": 0,
        "score": 0.0,
    }


def fault_aggregate_for_encounter(session: Session,
                                  encounter_id: int,
                                  static_id: int) -> dict[str, Any]:
    """Aggregate fault_scores across all our wipes for an encounter, per
    CHARACTER (v1.16.2: was per-player_id; FFLogs player_ids are
    report-scoped not character-scoped, so pid=2 in report A might be
    "Alice on Sage" and pid=2 in report B "Bob on Reaper" — keying by
    pid alone misattributes everything).

    Identity = (combatant.name, combatant.server) resolved PER-(fid, pid)
    via the Combatant table. Each character row carries a per-job
    breakdown inside `jobs_breakdown: {job_name: {fights, root, ...}}`.

    Member alias resolution still merges sub-accounts at the
    `scoped_top_contributors` level + via the frontend's member_id grouping.
    """
    # v1.17.0: union fault scores + watchlist fights across the cloned-encounter group.
    group = encounter_id_group(encounter_id)
    rows = session.execute(
        select(FaultScore, Fight)
        .join(Fight, Fight.id == FaultScore.fight_id)
        .where(Fight.encounter_id.in_(group), Fight.is_kill.is_(False),
               FaultScore.static_id == static_id)
    ).all()

    # v1.14.5: build the running-best-phase timeline so we can weight each
    # wipe's score by the group's prog state at the time. Include kills
    # too — a kill IS the highest phase ever reached (= 'last phase + 1' for
    # final-phase clear, or simply max(last_phase) overall).
    timeline_rows = session.execute(
        select(Fight.id, Fight.start_time, Fight.last_phase,
               Fight.fight_percentage, Fight.is_kill)
        .join(WatchedReport, WatchedReport.code == Fight.report_code)
        .where(Fight.encounter_id.in_(group),
               WatchedReport.static_id == static_id)
        .order_by(Fight.start_time.nulls_last())
    ).all()
    # v1.16.1: running-best is now a CONTINUOUS prog distance (phase +
    # within-phase progress), not a phase integer. Lets `_prog_relevance`
    # de-weight backslides by how far they actually are from the high-water
    # mark — so a P5 fp=10% wipe doesn't backslide a P5 fp=80% pull just
    # because they're "the same phase".
    fight_context: dict[int, tuple[int | None, float | None, float | None]] = {}
    running_best_prog: float | None = None
    for fid, _start_ts, last_phase, fp, _is_kill in timeline_rows:
        # Convert Decimal to float for prog math.
        fp_f = float(fp) if fp is not None else None
        this_prog = _prog_distance(last_phase, fp_f)
        if this_prog is not None:
            running_best_prog = (this_prog if running_best_prog is None
                                  else max(running_best_prog, this_prog))
        fight_context[fid] = (last_phase, fp_f, running_best_prog)

    fight_start_lookup = {
        fid: start_ts for fid, start_ts, *_ in timeline_rows
    }
    # All wipes for this encounter+static.
    wipe_fids = [fid for fid, _, _, _, is_kill in timeline_rows if not is_kill]

    # v1.14.6 / v1.16.2: attendance per CHARACTER. Active pids per fight
    # come from cast/damage source_ids (the masterData-NPC-safe heuristic);
    # we then look up which character that pid was in that specific fight
    # via Combatant, since FFLogs pids are report-scoped.
    from analysis.cartography import _active_players_by_fight
    active_by_fight = _active_players_by_fight(session, wipe_fids)

    # Per-(fid, pid) → (name, server, job) from combatants
    combatant_rows = session.execute(
        select(Combatant.fight_id, Combatant.player_id,
               Combatant.name, Combatant.job, Combatant.server)
        .where(Combatant.fight_id.in_(wipe_fids))
    ).all()
    char_at_fight: dict[tuple[int, int], tuple[str, str | None, str | None]] = {}
    for fid, pid, name, job, server in combatant_rows:
        if name:
            char_at_fight[(fid, pid)] = (name, server, job)

    # Char attendance: per (name, server), list of (fid, pid) pairs.
    # The pid is needed to look up the FaultScore row for that specific
    # fight (FaultScore is keyed on (static_id, fight_id, player_id)).
    char_attendance: dict[tuple[str, str | None], list[tuple[int, int]]] = defaultdict(list)
    for fid, pids in active_by_fight.items():
        for pid in pids:
            info = char_at_fight.get((fid, pid))
            if info is None:
                continue
            name, server, _ = info
            char_attendance[(name, server)].append((fid, pid))

    # v1.15.1: resolve each FFLogs player_id to a roster member via
    # character_aliases (same lookup as resolve_members.py / roster_discovery
    # — prefer (name, server) exact match; fall back to name-only iff a
    # single member claims it). Lets the UI collapse a member's main + sub
    # accounts into one row in "Who's contributing to wipes".
    alias_rows = session.execute(
        select(CharacterAlias.character_name, CharacterAlias.server,
               CharacterAlias.member_id, Member.name)
        .join(Member, Member.id == CharacterAlias.member_id)
        .where(Member.static_id == static_id)
    ).all()
    by_keyed: dict[tuple[str, str], tuple[int, str]] = {}
    by_name_lookup: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for ar in alias_rows:
        by_name_lookup[ar.character_name].append((ar.member_id, ar.name))
        if ar.server:
            by_keyed[(ar.character_name, ar.server)] = (ar.member_id, ar.name)

    def _resolve_member(name: str | None,
                        server: str | None) -> tuple[int | None, str | None]:
        if not name:
            return (None, None)
        if server and (name, server) in by_keyed:
            return by_keyed[(name, server)]
        cand = by_name_lookup.get(name) or []
        if len(cand) == 1:
            return cand[0]
        return (None, None)

    # FaultScore lookup keyed by (pid, fid) so we can join attendance
    # rows to fault data optionally.
    fault_lookup: dict[tuple[int, int], Any] = {}
    for fs, _f in rows:
        fault_lookup[(fs.player_id, fs.fight_id)] = fs

    def _new_char_bucket():
        return {
            "root": 0, "cascade": 0, "mit_failure": 0,
            "heal_failure": 0, "heal_failure_caused": 0,
            "enrage": 0, "unknown": 0,
            "avoidable_damage": 0, "damage_downs": 0,
            "fights": 0, "score": 0.0, "raw_score": 0.0,
            "classified_total": 0, "deaths_total": 0,
            "past_wall_offenses": 0,
            "repeat_multiplier_sum": 0.0,
            "repeat_multiplier_n": 0,
            "worst_wipes": [],
            "name": None, "server": None,
            "primary_job": None,  # most-played job for this character
            "jobs_breakdown": {},  # job_name -> {fights, root, ...}
            "player_ids": [],  # list of pids that represented this char
            "member_id": None, "member_name": None,
        }

    by_char: dict[tuple[str, str | None], dict[str, Any]] = defaultdict(_new_char_bucket)
    # v1.16.1: per-character per-fight weighted score for scoped contributors
    per_fight_weighted: dict[tuple[str, str | None], dict[int, float]] = defaultdict(dict)
    attended_set_by_char: dict[tuple[str, str | None], set[int]] = defaultdict(set)

    for char_key, fid_pid_pairs in char_attendance.items():
        name, server = char_key
        # Sort by fight start_time for the running-best logic + repeat-amp.
        fid_pid_pairs.sort(key=lambda x: fight_start_lookup.get(x[0]) or 0)
        b = by_char[char_key]
        b["name"] = name
        b["server"] = server
        mid, mname = _resolve_member(name, server)
        b["member_id"] = mid
        b["member_name"] = mname

        total_wipes = 0
        past_wall_offenses = 0
        seen_pids: set[int] = set()
        for fid, pid in fid_pid_pairs:
            total_wipes += 1
            attended_set_by_char[char_key].add(fid)
            seen_pids.add(pid)
            _, _, job = char_at_fight.get((fid, pid), (None, None, None))
            job_key = job or "—"
            jb = b["jobs_breakdown"].setdefault(job_key, _new_job_bucket())
            jb["fights"] += 1

            fs = fault_lookup.get((pid, fid))
            ctx = fight_context.get(fid)
            if ctx is not None:
                lp, fp, best_prog = ctx
                phase_severity_mult = _phase_severity(lp)
                within_phase_mult = _within_phase_severity(fp)
                prog_relevance_mult = _prog_relevance(lp, best_prog, fp)
                phase_mult = fight_score_multiplier(lp, fp, best_prog)
                our_prog = _prog_distance(lp, fp)
                is_past_wall = (
                    our_prog is not None
                    and best_prog is not None
                    and (best_prog - our_prog) > NEAR_WALL_TOLERANCE
                )
            else:
                lp = fp = best_prog = None
                phase_severity_mult = within_phase_mult = prog_relevance_mult = 1.0
                phase_mult = 1.0
                is_past_wall = False

            if fs is None:
                continue

            reasons = fs.reasons or {}
            # Helper to bump both the character total and the per-job
            # sub-bucket so they stay in sync.
            def _bump(key: str, amount: int | float, jb=jb):
                b[key] += amount
                if key in jb:
                    jb[key] += amount

            _bump("root", int(reasons.get("root", 0)))
            _bump("cascade", int(reasons.get("cascade", 0)))
            _bump("mit_failure", int(reasons.get("mit_failure", 0)))
            _bump("heal_failure", int(reasons.get("heal_failure", 0)))
            _bump("heal_failure_caused", int(reasons.get("heal_failure_caused", 0)))
            _bump("enrage", int(reasons.get("enrage", 0)))
            _bump("unknown", int(reasons.get("unknown", 0)))
            _bump("avoidable_damage", int(reasons.get("avoidable_damage", 0)))
            _bump("damage_downs", int(reasons.get("damage_downs", 0)))
            raw = float(fs.score or 0)
            b["raw_score"] += raw

            offenses_in_this_wipe = (int(reasons.get("root", 0))
                                     + int(reasons.get("mit_failure", 0)))
            repeat_mult = 1.0
            if is_past_wall and offenses_in_this_wipe > 0:
                past_wall_offenses += offenses_in_this_wipe
                repeat_mult = repeat_offender_multiplier(
                    past_wall_offenses, total_wipes,
                )

            weighted = raw * phase_mult * repeat_mult
            b["score"] += weighted
            jb["score"] += weighted
            per_fight_weighted[char_key][fid] = weighted
            b["repeat_multiplier_sum"] += repeat_mult
            b["repeat_multiplier_n"] += 1

            if raw > 0:
                best_phase_label = (int(best_prog)
                                    if best_prog is not None else None)
                b["worst_wipes"].append({
                    "fight_id": fid,
                    "last_phase": lp,
                    "fight_percentage": float(fp) if fp is not None else None,
                    "best_phase_at_time": best_phase_label,
                    "best_prog_at_time": (round(best_prog, 2)
                                          if best_prog is not None else None),
                    "raw": round(raw, 3),
                    "phase_severity": round(phase_severity_mult, 3),
                    "within_phase": round(within_phase_mult, 3),
                    "prog_relevance": round(prog_relevance_mult, 3),
                    "repeat_multiplier": round(repeat_mult, 3),
                    "weighted": round(weighted, 3),
                    "job": job,
                })

            deaths_this_fight = (int(reasons.get("root", 0))
                                 + int(reasons.get("cascade", 0))
                                 + int(reasons.get("mit_failure", 0))
                                 + int(reasons.get("heal_failure", 0))
                                 + int(reasons.get("enrage", 0))
                                 + int(reasons.get("unknown", 0)))
            classified_this_fight = (int(reasons.get("root", 0))
                                     + int(reasons.get("cascade", 0))
                                     + int(reasons.get("mit_failure", 0))
                                     + int(reasons.get("heal_failure", 0))
                                     + int(reasons.get("enrage", 0)))
            b["deaths_total"] += deaths_this_fight
            b["classified_total"] += classified_this_fight

        # End of per-fight loop for this character.
        b["fights"] = total_wipes
        b["past_wall_offenses"] = past_wall_offenses
        b["player_ids"] = sorted(seen_pids)
        # Primary job = most-played job for this character.
        if b["jobs_breakdown"]:
            b["primary_job"] = max(b["jobs_breakdown"].items(),
                                    key=lambda kv: kv[1]["fights"])[0]
        # Trim decomposition to the top-5 worst-weighted wipes per character.
        b["worst_wipes"].sort(key=lambda w: -w["weighted"])
        b["worst_wipes"] = b["worst_wipes"][:5]

    # Compute classified_fraction per character + finalize repeat-offender
    # metadata for the UI's transparency tooltip.
    for char_key, b in by_char.items():
        b["classified_fraction"] = (
            round(b["classified_total"] / b["deaths_total"], 3)
            if b["deaths_total"] > 0 else None
        )
        n = b["repeat_multiplier_n"]
        b["repeat_multiplier_avg"] = (
            round(b["repeat_multiplier_sum"] / n, 3) if n > 0 else 1.0
        )
        del b["repeat_multiplier_sum"]
        del b["repeat_multiplier_n"]

    # v1.16.1 / v1.16.2: scoped top-N contributors per character. Member-aware:
    # a character with a roster member_id collapses with their sub-accounts.
    def _identity_of(b: dict[str, Any]) -> tuple[str, Any]:
        if b.get("member_id") is not None:
            return ("m", b["member_id"])
        return ("n", b.get("name") or "?")

    identity_chars: dict[tuple[str, Any], list[tuple[str, str | None]]] = defaultdict(list)
    identity_display: dict[tuple[str, Any], dict[str, Any]] = {}
    for char_key, b in by_char.items():
        ident = _identity_of(b)
        identity_chars[ident].append(char_key)
        if ident not in identity_display:
            identity_display[ident] = {
                "name": b.get("member_name") or b.get("name") or "?",
                "member_id": b.get("member_id"),
            }

    identity_fight_weighted: dict[tuple[str, Any], dict[int, float]] = defaultdict(
        lambda: defaultdict(float)
    )
    identity_attended: dict[tuple[str, Any], set[int]] = defaultdict(set)
    for char_key, fight_scores in per_fight_weighted.items():
        ident = _identity_of(by_char[char_key])
        for fid, w in fight_scores.items():
            identity_fight_weighted[ident][fid] += w
    for char_key, fids in attended_set_by_char.items():
        ident = _identity_of(by_char[char_key])
        identity_attended[ident].update(fids)

    scoped_by_identity: dict[tuple[str, Any], list[dict[str, Any]]] = {}
    for focal_ident, focal_fids in identity_attended.items():
        if not focal_fids:
            scoped_by_identity[focal_ident] = []
            continue
        contribs: list[tuple[tuple[str, Any], float]] = []
        for other_ident, other_fight_scores in identity_fight_weighted.items():
            if other_ident == focal_ident:
                continue
            total = sum(score for fid, score in other_fight_scores.items()
                        if fid in focal_fids)
            if total > 0:
                contribs.append((other_ident, total))
        contribs.sort(key=lambda c: -c[1])
        scoped_by_identity[focal_ident] = [
            {
                "name": identity_display[ident]["name"],
                "member_id": identity_display[ident]["member_id"],
                "score": round(score, 2),
            }
            for ident, score in contribs[:5]
        ]

    for ident, char_keys in identity_chars.items():
        scoped = scoped_by_identity.get(ident, [])
        attended_count = len(identity_attended.get(ident, ()))
        for char_key in char_keys:
            by_char[char_key]["scoped_top_contributors"] = scoped
            by_char[char_key]["scoped_wipes_count"] = attended_count

    # Output: one row per character. `player_id` field kept for backward-compat
    # — set to the first pid in player_ids (used for the fault-breakdown drill
    # down). `job` field set to primary_job for legacy frontend code that
    # reads it directly. `jobs_breakdown` carries the per-job tally.
    out_players = []
    for char_key, agg in by_char.items():
        pids = agg.get("player_ids") or []
        legacy_pid = pids[0] if pids else 0
        # Convert jobs_breakdown counts/scores: round scores for stability
        for jb in agg["jobs_breakdown"].values():
            jb["score"] = round(jb["score"], 3)
        out_players.append({
            "player_id": legacy_pid,
            "job": agg.get("primary_job"),
            **{k: v for k, v in agg.items()},
        })
    out_players.sort(key=lambda p: -p["score"])

    return {
        "encounter_id": canonical_encounter_id(encounter_id),
        "wipes_aggregated": len({fs.fight_id for fs, _ in rows}),
        "players": out_players,
    }
