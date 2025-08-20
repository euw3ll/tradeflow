"""add invite code hashing and user roles

Revision ID: 0002
Revises: 0001
Create Date: 2024-06-01
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0002"
down_revision = "0001"
branch_labels = ("expand",)
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("role", sa.String(), nullable=False, server_default="USER"))
    op.add_column("invite_codes", sa.Column("code_hash", sa.String(), nullable=False, unique=True))
    op.add_column("invite_codes", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
    op.add_column("invite_codes", sa.Column("used_at", sa.DateTime(timezone=True)))
    op.add_column("invite_codes", sa.Column("used_by", sa.BigInteger()))
    op.drop_column("invite_codes", "code")


def downgrade() -> None:
    op.add_column("invite_codes", sa.Column("code", sa.String(), nullable=False, unique=True))
    op.drop_column("invite_codes", "used_by")
    op.drop_column("invite_codes", "used_at")
    op.drop_column("invite_codes", "expires_at")
    op.drop_column("invite_codes", "code_hash")
    op.drop_column("users", "role")
