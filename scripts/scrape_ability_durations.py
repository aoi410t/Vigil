"""Populate abilities.duration_ms + abilities.mit_pct from the FFXIV wiki.

For each ability currently labeled raid_buff / personal_buff / mit_party /
mit_self / mit_boss_debuff (the labels M-BURST and M-MIT care about), fetch
its consolegameswiki page once and write back the parsed buff duration (ms)
plus damage-reduction percentage.

Already-populated rows are skipped unless `--force` is passed (skipping is
per-ability — a row that has duration_ms but not mit_pct will still get
re-scraped to fill in the missing column). Polite pacing (0.5s between
requests by default) — overridable via `--pacing-s`.

Examples:
    python -m scripts.scrape_ability_durations               # populate missing
    python -m scripts.scrape_ability_durations --force       # refresh all
    python -m scripts.scrape_ability_durations --label raid_buff
    python -m scripts.scrape_ability_durations --limit 10    # quick smoke

Once populated:
- M-BURST in analysis/burst.py uses per-ability windows instead of the fixed
  20s default — Reprisal/Feint/Addle 15s, Mage's Ballad 45s, etc. all
  get accurate window sizing.
- StratEditor palette tooltips show both duration and mit_pct so users can
  quickly see "Rampart 30s / 20%" without leaving the UI.
- M-MIT (T-303) reads mit_pct once it grows damage-reduction quantification.
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import Counter

import httpx
from sqlalchemy import or_, select

from db.models import Ability, AbilityLabel
from db.session import SessionLocal, engine
from ingest.wiki import DEFAULT_PACING_S, fetch_metadata_for_ability

DEFAULT_LABELS = (
    "raid_buff", "personal_buff",
    "mit_party", "mit_self", "mit_boss_debuff",
)


def main() -> int:
    if engine is None:
        print("DATABASE_URL not set", file=sys.stderr)
        return 1

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--label", action="append", default=None,
                    help="Only scrape abilities with this label "
                         "(repeatable). Default: all of "
                         f"{list(DEFAULT_LABELS)}")
    ap.add_argument("--force", action="store_true",
                    help="Re-scrape rows even when fully populated.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Stop after N abilities (useful for smoke tests).")
    ap.add_argument("--pacing-s", type=float, default=DEFAULT_PACING_S,
                    help="Sleep between HTTP requests "
                         f"(default {DEFAULT_PACING_S}).")
    args = ap.parse_args()

    labels = args.label if args.label else list(DEFAULT_LABELS)

    with SessionLocal() as session:
        q = (
            select(Ability.ability_game_id, Ability.name,
                   Ability.duration_ms, Ability.mit_pct)
            .join(AbilityLabel,
                  AbilityLabel.ability_game_id == Ability.ability_game_id)
            .where(AbilityLabel.label.in_(labels))
            .where(Ability.name.is_not(None))
        )
        if not args.force:
            # A row needs scraping if either field is missing.
            q = q.where(or_(Ability.duration_ms.is_(None),
                            Ability.mit_pct.is_(None)))
        q = q.order_by(Ability.ability_game_id)
        if args.limit is not None:
            q = q.limit(args.limit)
        targets = session.execute(q).all()

    if not targets:
        print("Nothing to scrape (filter set empty or all already populated).")
        return 0

    print(f"Scraping {len(targets)} abilities…")

    results = Counter()
    with httpx.Client(timeout=15.0) as client:
        for i, (ability_id, name, existing_dur, existing_mit) in enumerate(targets):
            if i > 0 and args.pacing_s > 0:
                time.sleep(args.pacing_s)
            meta = fetch_metadata_for_ability(client, name)
            dur_ms = meta["duration_ms"]
            mit_pct = meta["mit_pct"]
            changed_fields: list[str] = []
            with SessionLocal() as s2:
                a = s2.get(Ability, ability_id)
                if a is not None:
                    if dur_ms is not None and dur_ms != existing_dur:
                        a.duration_ms = dur_ms
                        changed_fields.append(f"dur={dur_ms}ms")
                    if mit_pct is not None and mit_pct != existing_mit:
                        a.mit_pct = mit_pct
                        changed_fields.append(f"mit={mit_pct}%")
                    s2.commit()
            if changed_fields:
                results["updated"] += 1
                marker = "+"
            elif dur_ms is None and mit_pct is None:
                results["no_data_parsed"] += 1
                marker = "--"
            else:
                results["unchanged"] += 1
                marker = "="
            old_dur = f"{existing_dur}ms" if existing_dur is not None else "—"
            new_dur = f"{dur_ms}ms" if dur_ms is not None else "—"
            old_mit = f"{existing_mit}%" if existing_mit is not None else "—"
            new_mit = f"{mit_pct}%" if mit_pct is not None else "—"
            print(f"  {marker} [{ability_id:>7}] {name:<35}  "
                  f"dur {old_dur:>7}->{new_dur:>7}  "
                  f"mit {old_mit:>5}->{new_mit:>5}")

    print()
    print("Summary:")
    for k, v in sorted(results.items()):
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
