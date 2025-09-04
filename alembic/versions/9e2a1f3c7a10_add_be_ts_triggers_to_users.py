"""add be/ts triggers to users table

Revision ID: 9e2a1f3c7a10
Revises: f3d6de56aebe
Create Date: 2025-09-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9e2a1f3c7a10'
down_revision: Union[str, Sequence[str], None] = 'f3d6de56aebe'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: add be_trigger_pct and ts_trigger_pct to users."""
    op.add_column('users', sa.Column('be_trigger_pct', sa.Float(), nullable=False, server_default='0'))
    op.add_column('users', sa.Column('ts_trigger_pct', sa.Float(), nullable=False, server_default='0'))
    # remove server_default after filling defaults (optional)
    op.alter_column('users', 'be_trigger_pct', server_default=None)
    op.alter_column('users', 'ts_trigger_pct', server_default=None)


def downgrade() -> None:
    """Downgrade schema: drop added columns."""
    op.drop_column('users', 'ts_trigger_pct')
    op.drop_column('users', 'be_trigger_pct')

