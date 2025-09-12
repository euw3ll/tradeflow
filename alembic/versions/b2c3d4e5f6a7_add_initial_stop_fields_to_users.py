"""add initial stop fields to users

Revision ID: b2c3d4e5f6a7
Revises: a7b1c2d3e4f5
Create Date: 2025-09-12 00:10:00
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b2c3d4e5f6a7'
down_revision = 'a7b1c2d3e4f5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('initial_sl_mode', sa.String(length=20), nullable=False, server_default='ADAPTIVE'))
    op.add_column('users', sa.Column('initial_sl_fixed_pct', sa.Float(), nullable=False, server_default='1.0'))
    op.add_column('users', sa.Column('risk_per_trade_pct', sa.Float(), nullable=False, server_default='1.0'))
    with op.batch_alter_table('users') as batch_op:
        batch_op.alter_column('initial_sl_mode', server_default=None)
        batch_op.alter_column('initial_sl_fixed_pct', server_default=None)
        batch_op.alter_column('risk_per_trade_pct', server_default=None)


def downgrade() -> None:
    with op.batch_alter_table('users') as batch_op:
        batch_op.drop_column('risk_per_trade_pct')
        batch_op.drop_column('initial_sl_fixed_pct')
        batch_op.drop_column('initial_sl_mode')

