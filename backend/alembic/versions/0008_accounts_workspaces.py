"""accounts and workspaces: users, sessions, workspaces, memberships,
workspace settings and secrets, activity, usage events; repos.workspace_id

Every existing repo moves into a built-in "local" workspace owned by a
built-in "local" user, which is what the local mode (AUTH_MODE=none) runs
as. The old app-wide runtime settings (app_settings) become that
workspace's settings.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-08 10:00:00.000000

"""
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0008'
down_revision: Union[str, Sequence[str], None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LOCAL = "local"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('users',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('github_id', sa.BigInteger(), nullable=True),
    sa.Column('login', sa.Text(), nullable=False),
    sa.Column('name', sa.Text(), nullable=True),
    sa.Column('avatar_url', sa.Text(), nullable=True),
    sa.Column('platform_role', sa.String(length=16), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('github_token', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ux_users_github_id', 'users', ['github_id'], unique=True)
    op.create_table('workspaces',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('personal', sa.Boolean(), nullable=False),
    sa.Column('created_by', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('sessions',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('user_agent', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_sessions_user', 'sessions', ['user_id'], unique=False)
    op.create_table('memberships',
    sa.Column('workspace_id', sa.Text(), nullable=False),
    sa.Column('user_id', sa.Text(), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('workspace_id', 'user_id')
    )
    op.create_index('ix_memberships_user', 'memberships', ['user_id'], unique=False)
    op.create_table('workspace_settings',
    sa.Column('workspace_id', sa.Text(), nullable=False),
    sa.Column('key', sa.Text(), nullable=False),
    sa.Column('value', sa.JSON(), nullable=True),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('workspace_id', 'key')
    )
    op.create_table('secrets',
    sa.Column('workspace_id', sa.Text(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('ciphertext', sa.Text(), nullable=False),
    sa.Column('hint', sa.String(length=16), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_by', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('workspace_id', 'name')
    )
    op.create_table('user_activity_days',
    sa.Column('user_id', sa.Text(), nullable=False),
    sa.Column('day', sa.Date(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', 'day')
    )
    op.create_table('usage_events',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('event', sa.String(length=48), nullable=False),
    sa.Column('workspace_id', sa.Text(), nullable=True),
    sa.Column('user_id', sa.Text(), nullable=True),
    sa.Column('meta', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_usage_events_at', 'usage_events', ['at'], unique=False)
    op.create_index('ix_usage_events_event_at', 'usage_events', ['event', 'at'], unique=False)
    op.add_column('repos', sa.Column('workspace_id', sa.Text(), nullable=True))
    op.add_column('repos', sa.Column('name', sa.Text(), nullable=True))
    op.add_column('repos', sa.Column('created_by', sa.Text(), nullable=True))
    op.create_index('ix_repos_workspace', 'repos', ['workspace_id'], unique=False)

    # --- data: the built-in local user and workspace ---------------------------
    now = datetime.now(timezone.utc)
    users = sa.table('users', sa.column('id'), sa.column('login'), sa.column('name'),
                     sa.column('platform_role'), sa.column('status'), sa.column('created_at'))
    workspaces = sa.table('workspaces', sa.column('id'), sa.column('name'), sa.column('personal'),
                          sa.column('created_by'), sa.column('created_at'))
    memberships = sa.table('memberships', sa.column('workspace_id'), sa.column('user_id'),
                           sa.column('role'), sa.column('created_at'))
    op.bulk_insert(users, [{"id": LOCAL, "login": LOCAL, "name": "Local user",
                            "platform_role": "admin", "status": "active", "created_at": now}])
    op.bulk_insert(workspaces, [{"id": LOCAL, "name": "Local workspace", "personal": True,
                                 "created_by": LOCAL, "created_at": now}])
    op.bulk_insert(memberships, [{"workspace_id": LOCAL, "user_id": LOCAL, "role": "owner",
                                  "created_at": now}])
    op.execute(sa.text("UPDATE repos SET workspace_id = :ws WHERE workspace_id IS NULL")
               .bindparams(ws=LOCAL))
    op.execute(sa.text(
        "INSERT INTO workspace_settings (workspace_id, key, value) "
        "SELECT :ws, key, value FROM app_settings").bindparams(ws=LOCAL))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_repos_workspace', table_name='repos')
    op.drop_column('repos', 'created_by')
    op.drop_column('repos', 'name')
    op.drop_column('repos', 'workspace_id')
    op.drop_index('ix_usage_events_event_at', table_name='usage_events')
    op.drop_index('ix_usage_events_at', table_name='usage_events')
    op.drop_table('usage_events')
    op.drop_table('user_activity_days')
    op.drop_table('secrets')
    op.drop_table('workspace_settings')
    op.drop_index('ix_memberships_user', table_name='memberships')
    op.drop_table('memberships')
    op.drop_index('ix_sessions_user', table_name='sessions')
    op.drop_table('sessions')
    op.drop_table('workspaces')
    op.drop_index('ux_users_github_id', table_name='users')
    op.drop_table('users')
