"""stage 5: persist fetch normalization notices on campaigns

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-26 13:00:00.000000

The editor loads notices with the campaign detail (plan stage 5), so the
normalization/deletion notices produced by create, import, and fetch must
survive backend restarts instead of living only in the mutation response.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column("notices", sa.JSON(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("campaigns", "notices")
