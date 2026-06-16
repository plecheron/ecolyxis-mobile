"""Add energy_ledger table for archived deleted message energy.

Revision ID: 015_energy_ledger
Revises: 014_merge_heads
"""
from alembic import op
import sqlalchemy as sa

revision = "015_energy_ledger"
down_revision = "014_merge_heads"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "energy_ledger",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("energy_wh", sa.Float(), nullable=False, server_default="0"),
        sa.Column("co2e_g", sa.Float(), nullable=False, server_default="0"),
        sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(30), nullable=False, server_default="delete"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_energy_ledger_user_id", "energy_ledger", ["user_id"])


def downgrade():
    op.drop_index("ix_energy_ledger_user_id", table_name="energy_ledger")
    op.drop_table("energy_ledger")
