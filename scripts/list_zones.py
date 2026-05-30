"""List current (non-frozen) FFLogs zones + encounters; print a sample report code."""
from __future__ import annotations

from ingest import FFLogsClient


def main() -> int:
    with FFLogsClient() as c:
        zones = c.graphql(
            "query { worldData { zones { id name frozen encounters { id name } } } }"
        )["worldData"]["zones"]
        current = [z for z in zones if not z.get("frozen")]
        print(f"non-frozen zones: {len(current)} / {len(zones)} total")
        for z in current:
            print(f"  zone {z['id']}: {z['name']}")
            for enc in z.get("encounters", []):
                print(f"    encounter {enc['id']}: {enc['name']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
