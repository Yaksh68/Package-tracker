"""snapshot ai_status / ai_detail

How the optional AI research pass went for each snapshot, recorded apart from
`degraded`. An AI problem leaves the structured results complete.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-07 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0006'
down_revision: Union[str, Sequence[str], None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('snapshots', sa.Column('ai_status', sa.String(length=16), nullable=True))
    op.add_column('snapshots', sa.Column('ai_detail', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('snapshots', 'ai_detail')
    op.drop_column('snapshots', 'ai_status')
