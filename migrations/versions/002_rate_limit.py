"""add rate_limit_bucket table

Revision ID: 002_rate_limit
Revises: 001_initial
Create Date: 2026-05-25
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '002_rate_limit'
down_revision = '001_initial'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'rate_limit_bucket',
        sa.Column('key_hash', sa.String(length=64), nullable=False),
        sa.Column('tokens', sa.Float(), nullable=False),
        sa.Column('last_refill', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('key_hash'),
    )


def downgrade():
    op.drop_table('rate_limit_bucket')
