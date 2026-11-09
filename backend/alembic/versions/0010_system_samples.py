"""system_samples: an hourly reading of the server's health (admin analytics)

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-09 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0010'
down_revision: Union[str, Sequence[str], None] = '0009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('system_samples',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('disk_used_bytes', sa.BigInteger(), nullable=True),
    sa.Column('disk_total_bytes', sa.BigInteger(), nullable=True),
    sa.Column('db_bytes', sa.BigInteger(), nullable=True),
    sa.Column('uploads_bytes', sa.BigInteger(), nullable=True),
    sa.Column('rss_bytes', sa.BigInteger(), nullable=True),
    sa.Column('mem_total_bytes', sa.BigInteger(), nullable=True),
    sa.Column('queue_waiting', sa.Integer(), nullable=False),
    sa.Column('scans_running', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_system_samples_at', 'system_samples', ['at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_system_samples_at', table_name='system_samples')
    op.drop_table('system_samples')
