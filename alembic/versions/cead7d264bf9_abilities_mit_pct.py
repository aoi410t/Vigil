"""abilities_mit_pct

Revision ID: cead7d264bf9
Revises: fbbd9a93c108
Create Date: 2026-05-25 02:33:04.594666

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'cead7d264bf9'
down_revision: Union[str, None] = 'fbbd9a93c108'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add abilities.mit_pct (nullable Integer, 0-100).

    Populated by scripts/scrape_ability_durations.py (extended in v1.5.9)
    from the FFXIV wiki, alongside duration_ms. Stored as integer percent
    (e.g. 10 for "10%", 30 for "30%"). NULL when no mit value parsed.

    Today: read-only metadata for the strat editor's palette tooltips.
    Once M-MIT (T-303) grows damage-reduction quantification — currently
    only checks 'did the mit fire' — this column feeds that calculation.
    """
    op.add_column(
        "abilities",
        sa.Column("mit_pct", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("abilities", "mit_pct")
