"""add adaptive stop loss controls

Revision ID: c12345d6789a
Revises: 9e2a1f3c7a10
Create Date: 2025-09-20 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c12345d6789a'
down_revision: Union[str, Sequence[str], None] = 'cb01_add_circuit_breaker_fields'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add manual/dynamic adaptive SL controls to users."""
    op.add_column('users', sa.Column('adaptive_sl_max_pct', sa.Float(), nullable=False, server_default='0'))
    op.add_column('users', sa.Column('adaptive_sl_tighten_pct', sa.Float(), nullable=False, server_default='0'))
    op.add_column('users', sa.Column('adaptive_sl_timeout_minutes', sa.Integer(), nullable=False, server_default='0'))

    op.alter_column('users', 'adaptive_sl_max_pct', server_default=None)
    op.alter_column('users', 'adaptive_sl_tighten_pct', server_default=None)
    op.alter_column('users', 'adaptive_sl_timeout_minutes', server_default=None)


def downgrade() -> None:
    """Remove adaptive SL control columns."""
    op.drop_column('users', 'adaptive_sl_timeout_minutes')
    op.drop_column('users', 'adaptive_sl_tighten_pct')
    op.drop_column('users', 'adaptive_sl_max_pct')
