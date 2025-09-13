"""add circuit breaker scope and override fields

Revision ID: cb01_add_circuit_breaker_fields
Revises: b2c3d4e5f6a7
Create Date: 2025-09-13 00:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = 'cb01_add_circuit_breaker_fields'
down_revision = 'b2c3d4e5f6a7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('users') as batch_op:
        batch_op.add_column(sa.Column('circuit_breaker_scope', sa.String(length=10), nullable=False, server_default='SIDE'))
        batch_op.add_column(sa.Column('reversal_override_enabled', sa.Boolean(), nullable=False, server_default=sa.text('0')))
        batch_op.add_column(sa.Column('probe_size_factor', sa.Float(), nullable=False, server_default='0.5'))
        batch_op.add_column(sa.Column('backoff_escalation', sa.Boolean(), nullable=False, server_default=sa.text('0')))
        batch_op.alter_column('circuit_breaker_scope', server_default=None)
        batch_op.alter_column('reversal_override_enabled', server_default=None)
        batch_op.alter_column('probe_size_factor', server_default=None)
        batch_op.alter_column('backoff_escalation', server_default=None)


def downgrade() -> None:
    with op.batch_alter_table('users') as batch_op:
        batch_op.drop_column('backoff_escalation')
        batch_op.drop_column('probe_size_factor')
        batch_op.drop_column('reversal_override_enabled')
        batch_op.drop_column('circuit_breaker_scope')

