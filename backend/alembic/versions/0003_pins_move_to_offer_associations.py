"""move pins from streams to offer associations

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-27 10:00:00.000000

Pins are an offer-level feature (spec section 6.8): a pinned active offer
keeps its share while the remaining active offers split the rest. Stream
shares are always redistributed equally, so ``streams.pinned`` is dropped
and ``stream_offers.pinned`` added. Existing stream pins are not migrated:
pin state is local-only metadata that a fetch legitimately clears.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "stream_offers",
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    with op.batch_alter_table("streams") as batch_op:
        batch_op.drop_column("pinned")


def downgrade() -> None:
    op.add_column(
        "streams",
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    with op.batch_alter_table("stream_offers") as batch_op:
        batch_op.drop_column("pinned")
