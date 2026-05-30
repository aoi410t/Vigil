"""user_is_developer

Revision ID: da8534b86685
Revises: 11cd54903d42
Create Date: 2026-05-25 05:35:39.100709

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'da8534b86685'
down_revision: Union[str, None] = '11cd54903d42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add users.is_developer (v1.7.1): UI surfaces dev-only views when set.

    Default False. Auth middleware promotes to True when the user logs in
    with the DEV_PASSWORD env var (or, for backwards compat, when the
    username matches AUTH_USERNAME and only AUTH_PASSWORD is configured).

    Pre-existing users created before this migration default to False; the
    legacy dev (AUTH_USERNAME) is auto-promoted to True on their next login.
    """
    op.add_column(
        "users",
        sa.Column("is_developer", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("users", "is_developer")
