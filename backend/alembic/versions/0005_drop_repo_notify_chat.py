"""drop repo notify_chat (Google Chat notifications removed)

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-07 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: Union[str, Sequence[str], None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column('repos', 'notify_chat')


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column('repos', sa.Column('notify_chat', sa.Boolean(), server_default=sa.text('false'), nullable=False))
