"""Drop legacy llm_queue table and trigger

Revision ID: 010_drop_llm_queue
"""
from alembic import op
import sqlalchemy as sa

revision = "010_drop_llm_queue"
down_revision = "009_user_is_admin"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("DROP FUNCTION IF EXISTS notify_llm_queue() CASCADE")
    op.execute("DROP TABLE IF EXISTS llm_queue CASCADE")


def downgrade():
    pass  # Not restoring legacy queue
