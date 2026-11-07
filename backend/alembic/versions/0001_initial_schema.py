"""initial schema

Revision ID: 0001
Revises: 
Create Date: 2026-10-04

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('app_settings',
    sa.Column('key', sa.Text(), nullable=False),
    sa.Column('value', sa.JSON(), nullable=True),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('repos',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('url', sa.Text(), nullable=False),
    sa.Column('release_date', sa.Date(), nullable=True),
    sa.Column('interval_days', sa.Integer(), nullable=False),
    sa.Column('inspect_image', sa.Text(), nullable=True),
    sa.Column('image_tags', sa.JSON(), nullable=False),
    sa.Column('detected_tag_vars', sa.JSON(), nullable=False),
    sa.Column('token_override', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_scan_by_branch', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('snapshots',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('repo_id', sa.Text(), nullable=False),
    sa.Column('branch', sa.Text(), nullable=False),
    sa.Column('ts', sa.String(length=32), nullable=False),
    sa.Column('indexed', sa.Boolean(), nullable=False),
    sa.Column('package_count', sa.Integer(), nullable=False),
    sa.Column('flag_counts', sa.JSON(), nullable=False),
    sa.Column('degraded', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('repo_id', 'branch', 'ts', name='uq_snapshots_repo_branch_ts')
    )
    op.create_table('enrichments',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('snapshot_id', sa.Integer(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('key', sa.Text(), nullable=False),
    sa.Column('ecosystem', sa.String(length=32), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('version', sa.Text(), nullable=True),
    sa.Column('verdict', sa.String(length=32), nullable=False),
    sa.Column('eol_date', sa.Date(), nullable=True),
    sa.Column('data', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.ForeignKeyConstraint(['snapshot_id'], ['snapshots.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_enrichments_eco_name_version', 'enrichments', ['ecosystem', sa.literal_column('lower(name)'), 'version'], unique=False)
    op.create_index('ix_enrichments_snapshot', 'enrichments', ['snapshot_id', 'position'], unique=False)
    op.create_index('ix_enrichments_verdict', 'enrichments', ['verdict'], unique=False)
    op.create_table('flags',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('snapshot_id', sa.Integer(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('key', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=32), nullable=False),
    sa.Column('reasons', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['snapshot_id'], ['snapshots.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_flags_snapshot', 'flags', ['snapshot_id', 'position'], unique=False)
    op.create_index('ix_flags_status', 'flags', ['status'], unique=False)
    op.create_table('packages',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('snapshot_id', sa.Integer(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('key', sa.Text(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('version', sa.Text(), nullable=True),
    sa.Column('ecosystem', sa.String(length=32), nullable=False),
    sa.Column('source_file', sa.Text(), nullable=False),
    sa.Column('is_direct', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['snapshot_id'], ['snapshots.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_packages_eco_name_version', 'packages', ['ecosystem', sa.literal_column('lower(name)'), 'version'], unique=False)
    op.create_index('ix_packages_snapshot', 'packages', ['snapshot_id', 'position'], unique=False)
    op.create_table('repo_branches',
    sa.Column('repo_id', sa.Text(), nullable=False),
    sa.Column('branch', sa.Text(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['repo_id'], ['repos.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('repo_id', 'branch')
    )
    op.create_table('scan_logs',
    sa.Column('snapshot_id', sa.Integer(), nullable=False),
    sa.Column('log', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['snapshot_id'], ['snapshots.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('snapshot_id')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('scan_logs')
    op.drop_table('repo_branches')
    op.drop_index('ix_packages_snapshot', table_name='packages')
    op.drop_index('ix_packages_eco_name_version', table_name='packages')
    op.drop_table('packages')
    op.drop_index('ix_flags_status', table_name='flags')
    op.drop_index('ix_flags_snapshot', table_name='flags')
    op.drop_table('flags')
    op.drop_index('ix_enrichments_verdict', table_name='enrichments')
    op.drop_index('ix_enrichments_snapshot', table_name='enrichments')
    op.drop_index('ix_enrichments_eco_name_version', table_name='enrichments')
    op.drop_table('enrichments')
    op.drop_table('snapshots')
    op.drop_table('repos')
    op.drop_table('app_settings')
