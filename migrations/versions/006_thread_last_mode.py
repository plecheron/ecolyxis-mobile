"""thread last_mode: remember the chat mode per thread

Revision ID: 006_thread_last_mode
Revises: 005_message_reasoning_tokens
Create Date: 2026-06-05
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '006_thread_last_mode'
down_revision = '005_message_reasoning_tokens'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('thread', sa.Column('last_mode', sa.String(20), nullable=True))


def downgrade():
    op.drop_column('thread', 'last_mode')
