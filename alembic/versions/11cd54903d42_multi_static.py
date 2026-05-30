"""multi_static

Revision ID: 11cd54903d42
Revises: cead7d264bf9
Create Date: 2026-05-25 03:04:35.745005

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '11cd54903d42'
down_revision: Union[str, None] = 'cead7d264bf9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Multi-static (v1.6.0): users + statics + memberships, plus static_id
    scoping on user-curated tables.

    Tenancy model (per user pick 2026-05-25): N:M between users and statics
    via `static_memberships`. Each user has a `current_static_id` preference
    so the API knows which static to scope to. Raw FFLogs data (reports,
    fights, events, fight_model, abilities, ability_labels) stays shared
    across statics — no static_id added there.

    Existing data is migrated into a single 'Default Static' (id=1); the
    auth middleware auto-creates user records on first login and auto-joins
    the Default Static so a fresh install with one AUTH_USERNAME still works
    out of the box.
    """
    # ---- 1. New top-level tables -------------------------------------------
    op.create_table(
        "statics",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("username", sa.Text(), nullable=False, unique=True),
        sa.Column("current_static_id", sa.Integer(),
                  sa.ForeignKey("statics.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "static_memberships",
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("static_id", sa.Integer(),
                  sa.ForeignKey("statics.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
    )

    # ---- 2. Seed the Default Static (id=1) ---------------------------------
    op.execute(
        "INSERT INTO statics (id, name, created_at) "
        "VALUES (1, 'Default Static', NOW())"
    )
    # Ensure subsequent INSERTs allocate ids past 1
    op.execute("SELECT setval('statics_id_seq', 1, true)")

    # ---- 3. Add static_id to user-curated tables, default to 1 -------------
    # Order matters: add nullable, backfill to 1, then NOT NULL + FK.
    for table in ("watched_reports", "members", "strat_config",
                  "prog_points", "fault_scores"):
        op.add_column(table, sa.Column("static_id", sa.Integer(), nullable=True))
        op.execute(f"UPDATE {table} SET static_id = 1 WHERE static_id IS NULL")
        op.alter_column(table, "static_id", nullable=False)
        op.create_foreign_key(
            f"fk_{table}_static_id", table, "statics",
            ["static_id"], ["id"], ondelete="CASCADE",
        )

    # ---- 4. Repackage primary/unique keys to include static_id -------------
    # watched_reports: PK was (code); now (static_id, code) — two statics may
    # watch the same code. Ingestion ledger short-circuits redundant work.
    op.execute("ALTER TABLE watched_reports DROP CONSTRAINT watched_reports_pkey")
    op.create_primary_key("watched_reports_pkey", "watched_reports",
                          ["static_id", "code"])

    # members: was unique(name); now unique(static_id, name).
    op.drop_constraint("members_name_key", "members", type_="unique")
    op.create_unique_constraint("uq_members_static_name", "members",
                                ["static_id", "name"])

    # character_aliases: was unique(character_name, server) globally. Now
    # unscoped — two statics may both have an "Alice" alias. The resolver
    # already filters by member relation (T-107), which now implicitly
    # filters by static via members.static_id.
    op.drop_constraint("uq_char_alias_name_server", "character_aliases",
                       type_="unique")

    # strat_config: PK was (encounter_id, mechanic_ref). Now
    # (static_id, encounter_id, mechanic_ref) so two statics can have
    # different strats for the same mechanic.
    op.execute("ALTER TABLE strat_config DROP CONSTRAINT strat_config_pkey")
    op.create_primary_key("strat_config_pkey", "strat_config",
                          ["static_id", "encounter_id", "mechanic_ref"])

    # fault_scores: PK was (fight_id, player_id). Now scoped — different
    # statics may compute different fault_scores for shared fights based on
    # their own strat plan.
    op.execute("ALTER TABLE fault_scores DROP CONSTRAINT fault_scores_pkey")
    op.create_primary_key("fault_scores_pkey", "fault_scores",
                          ["static_id", "fight_id", "player_id"])


def downgrade() -> None:
    # Restore single-tenant PKs + drop static_id columns + drop new tables.
    op.execute("ALTER TABLE fault_scores DROP CONSTRAINT fault_scores_pkey")
    op.create_primary_key("fault_scores_pkey", "fault_scores",
                          ["fight_id", "player_id"])
    op.execute("ALTER TABLE strat_config DROP CONSTRAINT strat_config_pkey")
    op.create_primary_key("strat_config_pkey", "strat_config",
                          ["encounter_id", "mechanic_ref"])
    op.create_unique_constraint("uq_char_alias_name_server", "character_aliases",
                                ["character_name", "server"])
    op.drop_constraint("uq_members_static_name", "members", type_="unique")
    op.create_unique_constraint("members_name_key", "members", ["name"])
    op.execute("ALTER TABLE watched_reports DROP CONSTRAINT watched_reports_pkey")
    op.create_primary_key("watched_reports_pkey", "watched_reports", ["code"])

    for table in ("fault_scores", "prog_points", "strat_config",
                  "members", "watched_reports"):
        op.drop_constraint(f"fk_{table}_static_id", table, type_="foreignkey")
        op.drop_column(table, "static_id")

    op.drop_table("static_memberships")
    op.drop_table("users")
    op.drop_table("statics")
