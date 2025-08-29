"""add missing_cycles and last_seen_at to trades

Revision ID: b2a5a70c97b3
Revises: 657fc8fb3f24
Create Date: 2025-08-29 10:39:42.185181

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2a5a70c97b3'
down_revision: Union[str, Sequence[str], None] = '657fc8fb3f24'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None



def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("trades")}
    dialect = bind.dialect.name  # "sqlite", "postgresql", etc.

    # missing_cycles
    if "missing_cycles" not in cols:
        op.add_column(
            "trades",
            sa.Column("missing_cycles", sa.Integer(), nullable=False, server_default="0"),
        )
        # Remover default apenas em Postgres; SQLite não suporta DROP DEFAULT
        if dialect == "postgresql":
            op.execute("ALTER TABLE trades ALTER COLUMN missing_cycles DROP DEFAULT")

    # last_seen_at
    if "last_seen_at" not in cols:
        op.add_column(
            "trades",
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        )

def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("trades")}

    if "last_seen_at" in cols:
        op.drop_column("trades", "last_seen_at")
    if "missing_cycles" in cols:
        op.drop_column("trades", "missing_cycles")