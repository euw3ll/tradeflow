"""add_close_info_to_trades

Revision ID: fb136ee62ca2
Revises: 09d80b6e64de
Create Date: 2025-08-23 11:50:00.123456

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'fb136ee62ca2'
down_revision = '09d80b6e64de'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Comando para adicionar as novas colunas à tabela 'trades'
    op.add_column('trades', sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('trades', sa.Column('closed_pnl', sa.Float(), nullable=True))


def downgrade() -> None:
    # Comando para remover as colunas caso precise reverter
    op.drop_column('trades', 'closed_pnl')
    op.drop_column('trades', 'closed_at')