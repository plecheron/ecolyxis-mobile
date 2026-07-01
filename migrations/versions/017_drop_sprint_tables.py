"""Drop sprint_session and sprint_task tables.

Sprint mode has been removed from the application entirely.

Revision ID: 017_drop_sprint
Revises: 016_api_usage_energy
Create Date: 2026-07-01
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = '017_drop_sprint'
down_revision = '016_api_usage_energy'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_table('sprint_task')
    op.drop_table('sprint_session')


def downgrade():
    # Recreate tables if we need to roll back
    import sqlalchemy as sa
    op.create_table(
        'sprint_session',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('thread_id', sa.String(36), sa.ForeignKey('thread.id'), nullable=False, index=True),
        sa.Column('user_id', sa.Integer, sa.ForeignKey('user.id'), nullable=False),
        sa.Column('state', sa.String(20), nullable=False, server_default='questioning'),
        sa.Column('original_prompt', sa.Text, nullable=False),
        sa.Column('refined_prompt', sa.Text),
        sa.Column('qa_history', sa.Text, server_default='[]'),
        sa.Column('artifact_markdown', sa.Text),
        sa.Column('error', sa.Text),
        sa.Column('created_at', sa.DateTime),
        sa.Column('updated_at', sa.DateTime),
    )
    op.create_table(
        'sprint_task',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('session_id', sa.String(36), sa.ForeignKey('sprint_session.id'), nullable=False, index=True),
        sa.Column('order', sa.Integer, nullable=False),
        sa.Column('title', sa.String(200), nullable=False),
        sa.Column('description', sa.Text, nullable=False),
        sa.Column('depends_on', sa.Text, server_default='[]'),
        sa.Column('status', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('result', sa.Text),
        sa.Column('error', sa.Text),
        sa.Column('job_id', sa.String(36)),
        sa.Column('created_at', sa.DateTime),
        sa.Column('started_at', sa.DateTime),
        sa.Column('completed_at', sa.DateTime),
    )
