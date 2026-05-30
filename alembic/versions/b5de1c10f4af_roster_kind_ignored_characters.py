"""roster_kind_ignored_characters

Revision ID: b5de1c10f4af
Revises: da8534b86685
Create Date: 2026-05-26

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b5de1c10f4af'
down_revision: Union[str, None] = 'da8534b86685'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """v1.15.0 — roster discovery + classification.

    1. `members.kind` ('core' | 'substitute', NOT NULL, server-default 'core').
       Backfills every existing row to 'core' so v1.6.0+ rosters keep their
       semantics. The tag is for the human's mental model in the new Roster
       UX; analytics treat both kinds identically today.

    2. `ignored_characters` (static-scoped) — persists the "this combatant is
       not part of our static" decision so the same pugs / loot trades don't
       re-surface in the unclassified panel every time new fights ingest.
       De-dup via a partial unique index on (static_id, character_name,
       server) treating NULL server as the empty string (one ignore entry per
       (name, server) pair per static).
    """
    op.add_column(
        "members",
        sa.Column("kind", sa.Text(), nullable=False, server_default="core"),
    )

    op.create_table(
        "ignored_characters",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("static_id", sa.Integer(), nullable=False),
        sa.Column("character_name", sa.Text(), nullable=False),
        sa.Column("server", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["static_id"], ["statics.id"], ondelete="CASCADE",
            name="fk_ignored_characters_static",
        ),
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_ignored_characters_static_name_server "
        "ON ignored_characters (static_id, character_name, COALESCE(server, ''))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_ignored_characters_static_name_server")
    op.drop_table("ignored_characters")
    op.drop_column("members", "kind")
