"""create base tables

Revision ID: 0001
Revises: 
Create Date: 2024-06-01
"""

from alembic import op
import sqlalchemy as sa
from database.models import Base

# revision identifiers, used by Alembic.
revision = "0001"
down_revision = None
branch_labels = ("expand",)
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind, checkfirst=True)
