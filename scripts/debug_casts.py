"""One-shot: who are the cast sources in the M5S report?"""
from __future__ import annotations

import sys

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import Combatant, Event, Fight
from db.session import engine


def main(argv):
    code = argv[1] if len(argv) > 1 else "mVCt9aDdzq2Q8BLJ"
    with Session(engine) as session:
        fights = session.query(Fight).filter_by(report_code=code).all()
        fight_ids = [f.id for f in fights]
        player_ids = {
            (c.fight_id, c.player_id)
            for c in session.query(Combatant).filter(Combatant.fight_id.in_(fight_ids)).all()
        }
        print(f"fights: {len(fights)}  player rows: {len(player_ids)}")

        casts = session.execute(
            select(Event.source_id, func.count(Event.id))
            .where(Event.fight_id.in_(fight_ids), Event.type == "cast")
            .group_by(Event.source_id)
            .order_by(func.count(Event.id).desc())
            .limit(20)
        ).all()
        print("top cast sources (id, count):")
        for sid, n in casts:
            print(f"  {sid}: {n}")

        # Sample one fight: list non-player cast source_ids
        if fights:
            f = fights[0]
            f_players = {pid for (fid, pid) in player_ids if fid == f.id}
            sources = session.execute(
                select(Event.source_id, func.count(Event.id))
                .where(Event.fight_id == f.id, Event.type == "cast")
                .group_by(Event.source_id)
            ).all()
            print(f"\nfight {f.id} (encounter {f.encounter_id}, end={f.end_time}):")
            print(f"  players: {sorted(f_players)}")
            for sid, n in sources:
                tag = "PLAYER" if sid in f_players else "NPC?"
                print(f"  src {sid}: {n} casts  [{tag}]")


if __name__ == "__main__":
    main(sys.argv)
