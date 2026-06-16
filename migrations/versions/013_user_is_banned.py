"""Add is_banned column to user table.

Revision ID: 013_user_is_banned
"""
from alembic import op
import sqlalchemy as sa

revision = "013_user_is_banned"
down_revision = "012_carbon_offset"
branch_labels = None
depends_on = None


def upgrade():
    # Column already exists in production (added via db.create_all)
    # This migration exists for formal tracking only
    try:
        op.add_column("user", sa.Column("is_banned", sa.Boolean(), nullable=False, server_default="false"))
    except Exception:
        pass  # Column already exists


def downgrade():
    op.drop_column("user", "is_banned")
