"""Merge heads 011_message_energy and 013_user_is_banned.

Revision ID: 014_merge_heads
Revises: 011_message_energy, 013_user_is_banned
Create Date: 2026-06-16
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "014_merge_heads"
branch_labels = None
depends_on = None

# alembic merge — no DDL changes, just unifies the two heads into one
down_revision = ("011_message_energy", "013_user_is_banned")


def upgrade():
    pass


def downgrade():
    pass
