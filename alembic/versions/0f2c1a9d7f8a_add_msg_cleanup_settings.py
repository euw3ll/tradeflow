"""
add msg cleanup settings to users

Revision ID: 0f2c1a9d7f8a
Revises: 9e2a1f3c7a10
Create Date: 2025-09-06 20:10:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0f2c1a9d7f8a'
down_revision = '9e2a1f3c7a10'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('msg_cleanup_mode', sa.String(length=20), nullable=False, server_default='OFF'))
    op.add_column('users', sa.Column('msg_cleanup_delay_minutes', sa.Integer(), nullable=False, server_default='30'))

    # Remove server_default depois para deixar apenas os defaults da app
    with op.batch_alter_table('users') as batch_op:
        batch_op.alter_column('msg_cleanup_mode', server_default=None)
        batch_op.alter_column('msg_cleanup_delay_minutes', server_default=None)


def downgrade() -> None:
    with op.batch_alter_table('users') as batch_op:
        batch_op.drop_column('msg_cleanup_delay_minutes')
        batch_op.drop_column('msg_cleanup_mode')

