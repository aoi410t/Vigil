"""Cactbot timeline parser + fight_model annotator (Stage 1 of cactbot integration).

Cactbot (https://github.com/OverlayPlugin/cactbot) ships hand-authored timeline
files for every FFXIV encounter. We vendor a small subset under `vendor/cactbot/`
and parse them here to annotate each `fight_model` row with:

  - `cactbot_label` — human-readable mechanic name (e.g. "Burnished Glory")
  - `cactbot_phase_label` — phase name (e.g. "P2", "Adds Phase")
  - `cactbot_expected_t_ms` — expected phase-relative time per the canonical script

Cactbot timelines use absolute time from pull start. We segment them into
phases by walking the file and bumping a counter when we hit a `# Phase X`
header comment. Each entry's `phase_relative_t_s` is computed against the
first entry in that phase.

Stage 2 (later): expected-vs-actual diff per pull.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import FightModel

CACTBOT_DIR = Path(__file__).resolve().parent.parent / "vendor" / "cactbot"

# encounter_id -> timeline filename (relative to CACTBOT_DIR)
# v1.17.0: keyed on the CANONICAL encounter ID (e.g. DSR 1076, not legacy 1065).
# `load_timeline_for_encounter` canonicalizes its input before lookup, so
# callers can still pass either alias.
TIMELINE_FILES: dict[int, str] = {
    101: "r9s.txt",
    102: "r10s.txt",
    103: "r11s.txt",
    104: "r12s.txt",
    105: "r12s.txt",         # M12S-P2 shares the file
    1079: "futures_rewritten.txt",
    1068: "the_omega_protocol.txt",
    1076: "dragonsongs_reprise_ultimate.txt",  # DSR canonical (was 1065)
}

# Match `# <HEXID> <Name>` from cactbot's trailing "ALL ENCOUNTER ABILITIES"
# comment block. These cover sub-cast / VFX abilities cactbot intentionally
# omits from the timeline body but still wants to document — they don't have
# an expected time, but they have a human name we can use as a fallback label.
_COMMENT_ABILITY = re.compile(
    r"^#\s*(?P<hex>[0-9A-Fa-f]{3,4})\s+(?P<name>[^\n#]+?)\s*$",
)


# Match an Ability timeline line:
#   <time> "<label>" Ability { id: "<HEX>" ... }
#   <time> "<label>" Ability { id: ["HEX1", "HEX2", ...] ... }
# Excludes commented-out lines (those start with `#Ability`).
_ABILITY_LINE = re.compile(
    r"""
    ^
    (?P<time>\d+(?:\.\d+)?)
    \s+
    "(?P<label>[^"]*)"
    \s+
    Ability\s*\{
    [^}]*?
    \bid:\s*(?P<ids>"[0-9A-Fa-f]+"|\[[^\]]+\])
    """,
    re.VERBOSE,
)

# Match a commented-out timeline line:
#   <time> "<label>" #Ability { id: "<HEX>" ... }
#   <time> "<label>" duration N #Ability { id: "<HEX>" ... }   <- TOP style
# Cactbot uses these to document sub-cast effects ("Wave Cannon Puddle 1",
# "Cosmo Dive Far") that aren't synced to game-log events but still have a
# human label + game ID. We harvest these as fallback names.
_COMMENTED_ABILITY_LINE = re.compile(
    r"""
    ^
    (?P<time>\d+(?:\.\d+)?)
    \s+
    "(?P<label>[^"]*)"
    [^"#]*?              # optional `duration N` / `window M,N` / `jump "..."`
    \#Ability\s*\{
    [^}]*?
    \bid:\s*(?P<ids>"[0-9A-Fa-f]+"|\[[^\]]+\])
    """,
    re.VERBOSE,
)

# Phase marker comments. Cactbot files use a few different conventions; we
# accept any of "# Phase Two", "# Phase 2", "# P2", "# Adds Phase", "# -- p2 --".
_PHASE_NAMES_ORDINAL = {
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
}


def _is_phase_marker(comment: str) -> tuple[bool, str | None]:
    """Detect `# Phase ...` style headers; return (is_marker, label or None)."""
    s = comment.lstrip("#").strip()
    low = s.lower()
    if low.startswith("phase "):
        rest = low[6:].strip()
        if rest.isdigit():
            return True, f"P{rest}"
        for word, num in _PHASE_NAMES_ORDINAL.items():
            if rest.startswith(word):
                return True, f"P{num}"
    if low.startswith("p") and len(low) >= 2 and low[1].isdigit():
        # "# P2", "# P2: ..."
        return True, f"P{low[1]}"
    if "adds phase" in low:
        return True, "Adds"
    if low.startswith("-- p") and low[4:].split()[0].rstrip("-").isdigit():
        return True, f"P{low[4:].split()[0].rstrip('-')}"
    return False, None


def _parse_hex_ids(raw: str) -> list[int]:
    """Parse `"B375"` or `["B375", "B376"]` -> [decimal ids]."""
    s = raw.strip()
    items = re.findall(r'"([0-9A-Fa-f]+)"', s)
    out: list[int] = []
    for hex_id in items:
        try:
            out.append(int(hex_id, 16))
        except ValueError:
            pass
    return out


@dataclass
class CactbotEntry:
    """One parsed timeline entry from a cactbot .txt file."""

    abs_time_s: float
    label: str
    ability_ids: list[int]
    phase_index: int
    phase_label: str
    phase_relative_t_s: float = 0.0  # set after parsing


@dataclass
class ParsedTimeline:
    encounter_file: str
    entries: list[CactbotEntry]
    phase_labels: dict[int, str] = field(default_factory=dict)
    # Ability ID -> cactbot's documented name from the trailing comment block.
    # Used as a fallback label when the ability isn't in the timeline body
    # (sub-cast VFX, transitional add-phase ticks, etc.).
    fallback_names: dict[int, str] = field(default_factory=dict)


def parse_timeline_file(path: Path) -> ParsedTimeline:
    """Parse one cactbot .txt timeline. Returns a flat list of entries."""
    entries: list[CactbotEntry] = []
    phase_index = 0
    phase_labels: dict[int, str] = {0: "P1"}
    fallback_names: dict[int, str] = {}

    with path.open(encoding="utf-8") as f:
        for raw in f:
            stripped = raw.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                is_phase, label = _is_phase_marker(stripped)
                if is_phase and label is not None and label not in phase_labels.values():
                    phase_index += 1
                    phase_labels[phase_index] = label
                else:
                    # Try `# <HEX> <Name>` ability-doc comment. Only meaningful
                    # for short hex (3-4 chars) + ascii name; skip our own
                    # section comments like `# Adds Phase`.
                    cm = _COMMENT_ABILITY.match(stripped)
                    if cm is not None:
                        name = cm.group("name").strip()
                        # Skip lines where the "name" is a continuation of a
                        # section header (heuristic: starts with a verb-y all-
                        # caps notes like "VFX" alone, or is empty).
                        if name and not name.startswith(("--", "(", "{")):
                            try:
                                aid = int(cm.group("hex"), 16)
                            except ValueError:
                                aid = None
                            # First occurrence wins (cactbot sometimes notes the
                            # same ID twice with extra context — first line is
                            # the canonical name).
                            if aid is not None and aid not in fallback_names:
                                fallback_names[aid] = name
                continue

            m = _ABILITY_LINE.match(stripped)
            if m is not None:
                try:
                    abs_t = float(m.group("time"))
                except ValueError:
                    continue
                label = m.group("label").strip()
                if not label or label.startswith("--"):
                    # marker line ("--sync--", "--middle--"); skip
                    continue
                ids = _parse_hex_ids(m.group("ids"))
                if not ids:
                    continue
                entries.append(CactbotEntry(
                    abs_time_s=abs_t,
                    label=label,
                    ability_ids=ids,
                    phase_index=phase_index,
                    phase_label=phase_labels[phase_index],
                ))
                continue

            # Commented-out `#Ability` line — harvest its (id, label) for the
            # fallback-name map. These cover sub-cast / VFX abilities cactbot
            # documents but doesn't sync against game-log events.
            cm = _COMMENTED_ABILITY_LINE.match(stripped)
            if cm is not None:
                label = cm.group("label").strip()
                if label and not label.startswith("--"):
                    for aid in _parse_hex_ids(cm.group("ids")):
                        if aid not in fallback_names:
                            fallback_names[aid] = label

    # Compute phase-relative time per entry. Phase 0 is anchored at the pull
    # start (abs_time=0) so it lines up with our `fight_model.relative_t_ms`,
    # which T-103 measures from fight start. Subsequent phases anchor at the
    # first entry that lands inside them (cactbot doesn't always emit an
    # explicit phase-start abs_time, but the first event is a good proxy).
    phase_starts: dict[int, float] = {0: 0.0}
    for e in entries:
        if e.phase_index not in phase_starts:
            phase_starts[e.phase_index] = e.abs_time_s
    for e in entries:
        e.phase_relative_t_s = e.abs_time_s - phase_starts[e.phase_index]

    return ParsedTimeline(
        encounter_file=path.name,
        entries=entries,
        phase_labels=phase_labels,
        fallback_names=fallback_names,
    )


def load_timeline_for_encounter(encounter_id: int) -> ParsedTimeline | None:
    """Return the parsed cactbot timeline for an encounter, or None if not vendored.

    v1.17.0: canonicalizes the encounter_id before lookup so legacy aliases
    (e.g. DSR 1065) resolve to the canonical timeline file.
    """
    # Local import avoids a circular dep with analysis.death_inference at load time.
    from analysis._encounter import canonical_encounter_id
    fname = TIMELINE_FILES.get(canonical_encounter_id(encounter_id))
    if fname is None:
        return None
    path = CACTBOT_DIR / fname
    if not path.is_file():
        return None
    return parse_timeline_file(path)


def _best_match(
    candidates: Iterable[CactbotEntry],
    fight_model_phase: int,
    fight_model_rel_t_ms: int,
) -> CactbotEntry | None:
    """Pick the candidate closest to `(phase, relative_t)`.

    Prefer entries whose cactbot `phase_index` matches our `fight_model_phase`
    exactly. If none match, fall back to the closest entry across any phase.
    Within the chosen pool, pick the entry with the smallest absolute
    difference in phase-relative time.
    """
    candidates = list(candidates)
    if not candidates:
        return None
    target_s = fight_model_rel_t_ms / 1000.0
    same_phase = [c for c in candidates if c.phase_index == fight_model_phase]
    pool = same_phase or candidates
    return min(pool, key=lambda c: abs(c.phase_relative_t_s - target_s))


def annotate_fight_model_for_encounter(
    session: Session,
    encounter_id: int,
    version: int = 1,
) -> dict[str, int]:
    """Match cactbot timeline entries to fight_model rows, persist annotations.

    Walks every fight_model row for `(canonical_encounter_id, version)`, looks
    up cactbot entries whose ability ID set contains the row's ability, and
    picks the best match by phase + time proximity. Sets `cactbot_label`,
    `cactbot_phase_label`, `cactbot_expected_t_ms` on the row.

    v1.17.0: reads fight_model at the canonical encounter ID.

    Returns counters: `{annotated, missed_no_timeline, missed_no_match}`.
    """
    from analysis._encounter import canonical_encounter_id
    canonical = canonical_encounter_id(encounter_id)
    timeline = load_timeline_for_encounter(canonical)
    if timeline is None:
        return {"annotated": 0, "missed_no_timeline": 0, "missed_no_match": 0}

    # Build ability_id -> [entry] index for fast lookup
    by_ability: dict[int, list[CactbotEntry]] = {}
    for entry in timeline.entries:
        for aid in entry.ability_ids:
            by_ability.setdefault(aid, []).append(entry)

    rows = session.execute(
        select(FightModel)
        .where(FightModel.encounter_id == canonical,
               FightModel.version == version)
    ).scalars().all()

    annotated = 0
    annotated_fallback = 0
    no_match = 0
    for row in rows:
        candidates = by_ability.get(row.ability_game_id, [])
        if not candidates:
            # No timeline-body entry. Try the trailing-comment-block fallback —
            # cactbot intentionally omits sub-cast VFX and short-transition adds
            # from the timeline but documents their names in `# <HEX> <Name>`
            # comments at the bottom of the file.
            fallback = timeline.fallback_names.get(row.ability_game_id)
            if fallback is not None:
                row.cactbot_label = fallback
                row.cactbot_phase_label = None
                row.cactbot_expected_t_ms = None
                annotated_fallback += 1
            else:
                row.cactbot_label = None
                row.cactbot_phase_label = None
                row.cactbot_expected_t_ms = None
                no_match += 1
            continue
        best = _best_match(candidates, row.phase, row.relative_t_ms or 0)
        if best is None:
            no_match += 1
            continue
        row.cactbot_label = best.label
        row.cactbot_phase_label = best.phase_label
        row.cactbot_expected_t_ms = int(best.phase_relative_t_s * 1000)
        annotated += 1

    session.flush()
    return {
        "annotated": annotated,
        "annotated_fallback": annotated_fallback,
        "missed_no_timeline": 0,
        "missed_no_match": no_match,
    }
