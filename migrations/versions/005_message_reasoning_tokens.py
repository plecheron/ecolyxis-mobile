"""message reasoning_tokens: live thinking-token count

Revision ID: 005_message_reasoning_tokens
Revises: 004_generation_jobs
Create Date: 2026-06-05
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '005_message_reasoning_tokens'
down_revision = '004_generation_jobs'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('message', sa.Column('reasoning_tokens', sa.Integer(), nullable=True))


def downgrade():
    op.drop_column('message', 'reasoning_tokens')
