"""invites and audit_events (roles, invites, audit log)

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-08 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0009'
down_revision: Union[str, Sequence[str], None] = '0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('invites',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('workspace_id', sa.Text(), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('github_login', sa.Text(), nullable=True),
    sa.Column('token_hash', sa.String(length=64), nullable=True),
    sa.Column('created_by', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('responded_by', sa.Text(), nullable=True),
    sa.Column('responded_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_invites_login', 'invites', ['github_login'], unique=False)
    op.create_index('ix_invites_workspace', 'invites', ['workspace_id'], unique=False)
    op.create_index('ux_invites_token', 'invites', ['token_hash'], unique=True)
    op.create_table('audit_events',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('workspace_id', sa.Text(), nullable=True),
    sa.Column('actor_user_id', sa.Text(), nullable=True),
    sa.Column('actor_kind', sa.String(length=16), nullable=False),
    sa.Column('action', sa.String(length=48), nullable=False),
    sa.Column('target_type', sa.String(length=24), nullable=True),
    sa.Column('target_id', sa.Text(), nullable=True),
    sa.Column('details', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_audit_workspace_id', 'audit_events', ['workspace_id', 'id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_audit_workspace_id', table_name='audit_events')
    op.drop_table('audit_events')
    op.drop_index('ux_invites_token', table_name='invites')
    op.drop_index('ix_invites_workspace', table_name='invites')
    op.drop_index('ix_invites_login', table_name='invites')
    op.drop_table('invites')
