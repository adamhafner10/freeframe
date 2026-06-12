"""add s3_key_watermarked to media_files

Stores the burned-watermark output key produced by apply_watermark(). Served in
place of the clean raw/HLS only for share links with show_watermark=True. Nullable
because most media never gets a watermark pass.

Revision ID: f2a3b4c5d6e7
Revises: e0f1a2b3c4d5
Create Date: 2026-06-12 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2a3b4c5d6e7'
down_revision: Union[str, Sequence[str], None] = 'e0f1a2b3c4d5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'media_files',
        sa.Column('s3_key_watermarked', sa.String(length=1000), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('media_files', 's3_key_watermarked')
