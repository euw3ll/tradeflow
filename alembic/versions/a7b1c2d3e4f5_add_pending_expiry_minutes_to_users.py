"""add pending_expiry_minutes to users

Revision ID: a7b1c2d3e4f5
Revises: 2a4d_alert_cleanup_settings
Create Date: 2025-09-12 00:00:00
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a7b1c2d3e4f5'
down_revision = '2a4d_alert_cleanup_settings'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add column with server_default for backfill, then drop default
    op.add_column('users', sa.Column('pending_expiry_minutes', sa.Integer(), nullable=False, server_default='0'))
    with op.batch_alter_table('users') as batch_op:
        batch_op.alter_column('pending_expiry_minutes', server_default=None)


def downgrade() -> None:
    with op.batch_alter_table('users') as batch_op:
        batch_op.drop_column('pending_expiry_minutes')

