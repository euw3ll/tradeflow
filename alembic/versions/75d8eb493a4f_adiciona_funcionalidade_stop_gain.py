"""Adiciona funcionalidade stop gain

Revision ID: 75d8eb493a4f
Revises: 8556fc30af64
Create Date: 2025-08-25 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '75d8eb493a4f'
down_revision: Union[str, Sequence[str], None] = '8556fc30af64'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('stop_gain_trigger_pct', sa.Float(), nullable=False, server_default='0.0'))
        batch_op.add_column(sa.Column('stop_gain_lock_pct', sa.Float(), nullable=False, server_default='0.0'))

    with op.batch_alter_table('trades', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_stop_gain_active', sa.Boolean(), nullable=False, server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('trades', schema=None) as batch_op:
        batch_op.drop_column('is_stop_gain_active')

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('stop_gain_lock_pct')
        batch_op.drop_column('stop_gain_trigger_pct')