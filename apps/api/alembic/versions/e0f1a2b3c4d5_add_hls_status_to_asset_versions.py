"""add hls_status to asset_versions

Separates HLS-transcode state from processing_status so a FAILED streaming-ladder
transcode no longer flips a raw-playable version to unplayable.

Revision ID: e0f1a2b3c4d5
Revises: d9e0f1a2b3c4
Create Date: 2026-06-12 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e0f1a2b3c4d5'
down_revision: Union[str, Sequence[str], None] = 'd9e0f1a2b3c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# SQLAlchemy names the PG enum after the Python class, lowercased: HLSStatus -> hlsstatus
hls_status_enum = sa.Enum('pending', 'processing', 'ready', 'failed', name='hlsstatus')


def upgrade() -> None:
    """Upgrade schema."""
    hls_status_enum.create(op.get_bind(), checkfirst=True)
    op.add_column(
        'asset_versions',
        sa.Column(
            'hls_status',
            hls_status_enum,
            nullable=False,
            server_default='pending',
        ),
    )
    # Backfill: any version already marked ready that has a processed HLS output
    # gets hls_status='ready'; everything else stays 'pending' (the column default).
    op.execute(
        """
        UPDATE asset_versions av
        SET hls_status = 'ready'
        FROM media_files mf
        WHERE mf.version_id = av.id
          AND mf.s3_key_processed IS NOT NULL
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('asset_versions', 'hls_status')
    hls_status_enum.drop(op.get_bind(), checkfirst=True)
