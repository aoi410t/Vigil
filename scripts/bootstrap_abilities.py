"""T-108 live verification — pull XIVAPI for every distinct ability_game_id in
events, then run the classifier to populate `ability_labels`.

Polite to XIVAPI: defaults to 0.06s between requests (≈16 req/s). Skips IDs
already in the `abilities` table. Pass `--force` to refresh.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter

from sqlalchemy.orm import Session

from analysis.ability_classifier import AUTO_HIGH_THRESHOLD, relabel_all
from db.session import engine
from ingest.xivapi import XIVAPIClient, bootstrap_abilities_from_events


def main(argv: list[str]) -> int:
    if engine is None:
        raise SystemExit("DATABASE_URL not configured")

    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="re-fetch IDs already present in `abilities`")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap ID count for a quick smoke run")
    ap.add_argument("--skip-fetch", action="store_true",
                    help="only re-run the classifier; no XIVAPI calls")
    args = ap.parse_args(argv[1:])

    with Session(engine) as session:
        if not args.skip_fetch:
            with XIVAPIClient() as client:
                only_ids = None
                if args.limit is not None:
                    from ingest.xivapi import distinct_ability_ids
                    ids = list(distinct_ability_ids(session).keys())
                    only_ids = ids[: args.limit]
                summary = bootstrap_abilities_from_events(
                    session, client, force=args.force, only_ids=only_ids,
                )
            print("\n== XIVAPI fetch summary ==")
            for k, v in summary.items():
                print(f"  {k:>16}: {v}")

        print("\n== Running classifier ==")
        label_summary = relabel_all(session)
        for k, v in sorted(label_summary.items(), key=lambda kv: -kv[1]):
            print(f"  {k:>18}: {v}")

        # Confidence distribution
        from sqlalchemy import select
        from db.models import AbilityLabel
        confs = session.execute(
            select(AbilityLabel.confidence).where(AbilityLabel.source == "auto")
        ).scalars().all()
        if confs:
            buckets = Counter()
            for c in confs:
                cf = float(c) if c is not None else 0.0
                bucket = ">=0.85" if cf >= AUTO_HIGH_THRESHOLD else ("0.50-0.85" if cf >= 0.5 else "<0.50")
                buckets[bucket] += 1
            print("\n== Auto-label confidence distribution ==")
            for b in (">=0.85", "0.50-0.85", "<0.50"):
                print(f"  {b:>10}: {buckets.get(b, 0)}")

    print("\nOK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
