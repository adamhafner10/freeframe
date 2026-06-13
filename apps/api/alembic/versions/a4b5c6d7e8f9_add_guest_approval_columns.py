"""add guest approval columns

Lets guests (no account) record an approve/reject decision on a share link.
Makes approvals.user_id NULLABLE and adds nullable guest_email / guest_name so a
guest decision is attributable without a users row. The existing
uq_approvals_version_user unique constraint is preserved — NULL user_ids are
distinct in Postgres, so guest rows never collide with it. A partial unique index
on (version_id, guest_email) WHERE guest_email IS NOT NULL keeps one decision per
guest per version, mirroring the per-member upsert.

Revision ID: a4b5c6d7e8f9
Revises: f2a3b4c5d6e7
Create Date: 2026-06-12 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a4b5c6d7e8f9'
down_revision: Union[str, Sequence[str], None] = 'f2a3b4c5d6e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'approvals',
        sa.Column('guest_email', sa.String(length=255), nullable=True),
    )
    op.add_column(
        'approvals',
        sa.Column('guest_name', sa.String(length=255), nullable=True),
    )
    op.alter_column(
        'approvals',
        'user_id',
        existing_type=sa.dialects.postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    # One decision per guest email per version (member rows have guest_email NULL,
    # so they're excluded from this index).
    op.create_index(
        'uq_approvals_version_guest_email',
        'approvals',
        ['version_id', 'guest_email'],
        unique=True,
        postgresql_where=sa.text('guest_email IS NOT NULL'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('uq_approvals_version_guest_email', table_name='approvals')
    op.alter_column(
        'approvals',
        'user_id',
        existing_type=sa.dialects.postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.drop_column('approvals', 'guest_name')
    op.drop_column('approvals', 'guest_email')
