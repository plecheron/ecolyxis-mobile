"""Add user.is_admin role flag; backfill the original id==1 admin (#97)

Replaces the hardcoded ``current_user.id == 1`` admin gate with an explicit
role column.

Revision ID: 009_user_is_admin
Revises: 008_txn_pi_unique
Create Date: 2026-06-12
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "009_user_is_admin"
down_revision = "008_txn_pi_unique"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    # Preserve access for the user the old gate granted it to.
    op.execute('UPDATE "user" SET is_admin = TRUE WHERE id = 1')


def downgrade():
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.drop_column("is_admin")
