"""Rule-based classifier for ability labels (T-108, PLAN §11).

Reads XIVAPI Action/Status descriptions and emits a `(label, confidence)` pair.
Labels are intentionally a small fixed vocabulary (see `LABELS`); rules use
description text patterns and fall back to `('unknown', 0.0)` so the review
queue (T-108 UI) can catch them. M-BURST and M-MIT will read `confidence >=
AUTO_HIGH_THRESHOLD` plus user-confirmed rows.

Confidence guidance:
  ≥ 0.85 → strong match, auto-apply
  0.50–0.85 → likely but worth a review
  < 0.50 → low signal, default to review queue
"""
from __future__ import annotations

import re
from typing import Iterable

LABELS = frozenset({
    "raid_buff",          # party damage buff (e.g. Divination, Searing Light)
    "personal_buff",      # damage buff on self only (e.g. Inner Release)
    "mit_party",          # party-wide damage taken reduction (e.g. Shake It Off)
    "mit_boss_debuff",    # debuff on enemies cutting their damage dealt (Reprisal, Feint, Addle)
    "mit_self",           # self-only damage taken reduction (Rampart, Sentinel)
    "damage_down",        # mistake debuff lowering player damage dealt
    "ignore",             # explicitly uninteresting for M-BURST/M-MIT
    "unknown",            # classifier couldn't decide — review queue
})

AUTO_HIGH_THRESHOLD = 0.85

# Strip XIVAPI's HTML markup, <If(...)>...</If> conditional templates,
# and color spans so the patterns below see clean prose.
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_TEMPLATE_RE = re.compile(r"<If\([^)]*\)>.*?(?:</If>|<Else/>)", re.DOTALL)
_WS_RE = re.compile(r"\s+")


def clean_description(desc: str | None) -> str:
    if not desc:
        return ""
    s = _TEMPLATE_RE.sub(" ", desc)
    s = _HTML_TAG_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip().lower()


def _contains_any(text: str, needles: Iterable[str]) -> bool:
    return any(n in text for n in needles)


# Strong textual signals.
PARTY_TERMS = ("party members", "nearby party", "party member", "you and nearby allies",
               "you and nearby party", "party's", "all party members")
ENEMY_TERMS = ("nearby enemies", "target's", "the target", "enemies'",
               "enemy's damage")
SELF_ONLY_HINTS = ("increases your damage", "increasing your damage",
                   "your damage dealt is increased")
MIT_VERBS = ("reduces damage taken", "damage taken is reduced", "reducing damage taken")
DEBUFF_DAMAGE_VERBS = ("reduces damage dealt", "damage dealt is reduced",
                       "reducing damage dealt by", "reduces target's physical damage",
                       "reduces target's magic damage")
BUFF_DAMAGE_VERBS = ("increases damage dealt", "increasing damage dealt",
                     "damage dealt is increased", "increasing your damage",
                     "increases your damage")


def classify(name: str | None, description: str | None, kind: str | None = None) -> tuple[str, float]:
    """Return `(label, confidence)` for one ability.

    `kind` is the XIVAPI namespace ('action' | 'status' | 'unknown') from the
    abilities row. Status descriptions tend to describe the *effect* of the
    buff/debuff; action descriptions describe what the cast *does*. Both
    overlap enough that the same rules cover them.
    """
    n = (name or "").lower()
    c = clean_description(description)

    # 1. Damage Down — distinctive name across the whole game.
    if "damage down" in n:
        return ("damage_down", 1.0)

    # No description = no signal.
    if not c:
        return ("unknown", 0.0)

    # 2. Enemy-targeted "reduces damage dealt" (Reprisal, Feint, Addle).
    if _contains_any(c, DEBUFF_DAMAGE_VERBS):
        if _contains_any(c, ENEMY_TERMS):
            return ("mit_boss_debuff", 0.92)
        # "Damage dealt is reduced" applied to the *player* is Damage Down.
        if _contains_any(c, ("your damage dealt is reduced",)):
            return ("damage_down", 0.9)
        return ("mit_boss_debuff", 0.6)

    # 3. "Reduces damage taken" / "Damage taken is reduced" → mit.
    if _contains_any(c, MIT_VERBS):
        if _contains_any(c, PARTY_TERMS):
            return ("mit_party", 0.9)
        return ("mit_self", 0.85)

    # 4. Damage-buff verbs → raid vs personal.
    if _contains_any(c, BUFF_DAMAGE_VERBS):
        if _contains_any(c, PARTY_TERMS):
            return ("raid_buff", 0.92)
        if _contains_any(c, SELF_ONLY_HINTS) or "your damage" in c:
            return ("personal_buff", 0.9)
        # Status descriptions for personal CDs are typically terse ("Damage dealt
        # is increased.") with no qualifier — common enough to trust at auto-high.
        # Action descriptions without an explicit self-qualifier are murkier; keep
        # them at low confidence for the review queue.
        if kind == "status":
            return ("personal_buff", 0.85)
        return ("personal_buff", 0.5)

    # 5. Strong "just an attack" signal — XIVAPI describes damaging abilities
    # using the word "potency". M-BURST/M-MIT never need these, so promote to
    # auto-high so they don't clog the review queue.
    if "potency" in c:
        return ("ignore", 0.95)

    # 6. Catch-all: nothing matched the mit/buff vocabulary → not interesting.
    # Weak confidence keeps it in the review queue.
    return ("ignore", 0.3)


def classify_many(rows: Iterable[tuple[int, str | None, str | None, str | None]]
                  ) -> list[tuple[int, str, float]]:
    """Bulk helper: `[(ability_id, name, description, kind), …] →
    [(ability_id, label, confidence), …]`."""
    return [(aid, *classify(name, desc, kind)) for (aid, name, desc, kind) in rows]


def relabel_all(session, *, overwrite_user: bool = False) -> dict[str, int]:
    """Run the classifier over every row in `abilities` and upsert `ability_labels`.

    User-confirmed rows (`source='user'`) are skipped unless `overwrite_user=True`
    so a re-run never wipes the user's review work. Returns a summary keyed by
    label so the verify script can show distribution.

    Post-pass: a *status* whose name matches an *action* labeled `raid_buff` or
    `mit_party` is reassigned to the same label. XIVAPI status descriptions are
    terse ("Damage dealt is increased.") and don't carry the party-vs-self
    distinction, so without this cross-pass the buff side of every party buff
    would land as personal_buff.
    """
    from datetime import datetime, timezone

    from sqlalchemy import select

    from db.models import Ability, AbilityLabel

    rows = session.execute(
        select(Ability.ability_game_id, Ability.name, Ability.description, Ability.kind)
    ).all()

    existing_labels: dict[int, AbilityLabel] = {
        lbl.ability_game_id: lbl
        for lbl in session.execute(select(AbilityLabel)).scalars().all()
    }

    summary: dict[str, int] = {lbl: 0 for lbl in LABELS}
    summary["skipped_user"] = 0

    now = datetime.now(timezone.utc)
    # First pass: classify by description.
    initial_labels: dict[int, tuple[str, float]] = {}
    for ability_id, name, desc, kind in rows:
        existing = existing_labels.get(ability_id)
        if existing is not None and existing.source == "user" and not overwrite_user:
            summary["skipped_user"] += 1
            continue
        initial_labels[ability_id] = classify(name, desc, kind)

    # Build action-name → label index for the cross-reference pass.
    action_name_to_label: dict[str, str] = {}
    PARTY_LABELS = {"raid_buff", "mit_party", "mit_boss_debuff"}
    for ability_id, name, _desc, kind in rows:
        if kind != "action" or name is None:
            continue
        label_pair = initial_labels.get(ability_id)
        if label_pair is None:
            existing = existing_labels.get(ability_id)
            if existing is None or existing.label is None:
                continue
            label = existing.label
        else:
            label = label_pair[0]
        if label in PARTY_LABELS:
            action_name_to_label[name.lower()] = label

    # Second pass: status rows that share a name with a party-labeled action
    # adopt the same label.
    final_labels: dict[int, tuple[str, float]] = dict(initial_labels)
    for ability_id, name, _desc, kind in rows:
        if ability_id not in final_labels or kind != "status" or name is None:
            continue
        match = action_name_to_label.get(name.lower())
        if match is not None:
            final_labels[ability_id] = (match, 0.92)

    # Upsert.
    for ability_id, (label, confidence) in final_labels.items():
        summary[label] = summary.get(label, 0) + 1
        existing = existing_labels.get(ability_id)
        if existing is None:
            session.add(AbilityLabel(
                ability_game_id=ability_id,
                label=label,
                confidence=confidence,
                source="auto",
                updated_at=now,
            ))
        else:
            existing.label = label
            existing.confidence = confidence
            existing.source = "auto"
            existing.updated_at = now

    session.commit()
    return summary
