"""Quick look at a death event's raw payload to find the killing-ability field."""
from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Event, Fight
from db.session import engine


def main():
    with Session(engine) as session:
        # find a fight in the M5S report
        f = session.query(Fight).filter_by(report_code="mVCt9aDdzq2Q8BLJ").first()
        # Find a death event where our ability_game_id ended up null
        ev = session.execute(
            select(Event)
            .where(Event.type == "death", Event.fight_id == f.id, Event.ability_game_id.is_(None))
            .limit(1)
        ).scalar_one_or_none()
        if ev is None:
            # else any death with a value
            ev = session.execute(
                select(Event).where(Event.type == "death", Event.fight_id == f.id).limit(1)
            ).scalar_one_or_none()
        print(f"event id={ev.id} ts={ev.ts} ability_game_id={ev.ability_game_id}")
        print("raw:")
        print(json.dumps(ev.raw, indent=2))


if __name__ == "__main__":
    main()
