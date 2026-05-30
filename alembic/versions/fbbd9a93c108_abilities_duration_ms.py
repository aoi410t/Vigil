"""abilities_duration_ms

Revision ID: fbbd9a93c108
Revises: 8ac127cddffe
Create Date: 2026-05-25 00:18:30.669629

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fbbd9a93c108'
down_revision: Union[str, None] = '8ac127cddffe'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add abilities.duration_ms (nullable BigInteger).

    Populated by scripts/scrape_ability_durations.py from the FFXIV wiki
    (consolegameswiki.com). M-BURST in analysis/burst.py reads it to use per-
    raid-buff window lengths instead of the fixed 20s default — captures the
    trait-modified values (Brotherhood 20s post-trait, Reprisal/Feint/Addle
    15s, etc.) that XIVAPI doesn't expose in a parseable form.
    """
    op.add_column(
        "abilities",
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("abilities", "duration_ms")
