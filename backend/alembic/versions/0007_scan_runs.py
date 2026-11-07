"""scan_runs: one fact row per scan attempt (admin analytics)

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-07 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0007'
down_revision: Union[str, Sequence[str], None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('scan_runs',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('repo_id', sa.Text(), nullable=False),
    sa.Column('workspace_id', sa.Text(), nullable=True),
    sa.Column('branch', sa.Text(), nullable=False),
    sa.Column('snapshot_ts', sa.String(length=32), nullable=True),
    sa.Column('triggered_by', sa.String(length=16), nullable=False),
    sa.Column('queued_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('outcome', sa.String(length=16), nullable=False),
    sa.Column('stage_seconds', sa.JSON(), nullable=False),
    sa.Column('issue_stages', sa.JSON(), nullable=False),
    sa.Column('first_error', sa.Text(), nullable=True),
    sa.Column('package_count', sa.Integer(), nullable=False),
    sa.Column('package_counts', sa.JSON(), nullable=False),
    sa.Column('flag_counts', sa.JSON(), nullable=False),
    sa.Column('ai_status', sa.String(length=16), nullable=True),
    sa.Column('ai_detail', sa.Text(), nullable=True),
    sa.Column('ai_provider', sa.Text(), nullable=True),
    sa.Column('ai_model', sa.Text(), nullable=True),
    sa.Column('ai_batches', sa.Integer(), nullable=False),
    sa.Column('ai_failed_batches', sa.Integer(), nullable=False),
    sa.Column('ai_tokens_in', sa.Integer(), nullable=False),
    sa.Column('ai_tokens_out', sa.Integer(), nullable=False),
    sa.Column('ai_searches', sa.Integer(), nullable=False),
    sa.Column('ai_cost_usd', sa.Float(), nullable=True),
    sa.Column('upstream_errors', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_scan_runs_repo_branch', 'scan_runs', ['repo_id', 'branch'], unique=False)
    op.create_index('ix_scan_runs_started_at', 'scan_runs', ['started_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_scan_runs_started_at', table_name='scan_runs')
    op.drop_index('ix_scan_runs_repo_branch', table_name='scan_runs')
    op.drop_table('scan_runs')
