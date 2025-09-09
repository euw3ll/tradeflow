"""add alert cleanup settings and table

Revision ID: 2a4d_alert_cleanup_settings
Revises: 0f2c1a9d7f8a
Create Date: 2025-09-09
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '2a4d_alert_cleanup_settings'
down_revision = '0f2c1a9d7f8a'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('users') as batch_op:
        batch_op.add_column(sa.Column('alert_cleanup_mode', sa.String(length=20), nullable=False, server_default='OFF'))
        batch_op.add_column(sa.Column('alert_cleanup_delay_minutes', sa.Integer(), nullable=False, server_default='30'))

    op.create_table(
        'alert_messages',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_telegram_id', sa.BigInteger(), nullable=False),
        sa.Column('message_id', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now())
    )
    op.create_index('ix_alert_messages_user', 'alert_messages', ['user_telegram_id'])

    # remove defaults
    with op.batch_alter_table('users') as batch_op:
        batch_op.alter_column('alert_cleanup_mode', server_default=None)
        batch_op.alter_column('alert_cleanup_delay_minutes', server_default=None)


def downgrade():
    op.drop_index('ix_alert_messages_user', table_name='alert_messages')
    op.drop_table('alert_messages')
    with op.batch_alter_table('users') as batch_op:
        batch_op.drop_column('alert_cleanup_delay_minutes')
        batch_op.drop_column('alert_cleanup_mode')
