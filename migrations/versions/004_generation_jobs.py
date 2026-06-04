"""generation jobs: durable async job records + job_id links

Revision ID: 004_generation_jobs
Revises: 003_post_table
Create Date: 2026-06-04
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '004_generation_jobs'
down_revision = '003_post_table'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'generation_job',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id'), nullable=False),
        sa.Column('thread_id', sa.String(36), sa.ForeignKey('thread.id'), nullable=True),
        sa.Column('kind', sa.String(20), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='queued'),
        sa.Column('is_premium', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('params', sa.JSON(), nullable=True),
        sa.Column('result', sa.JSON(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('worker_id', sa.String(80), nullable=True),
        sa.Column('heartbeat_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
    )
    op.create_index('idx_job_status', 'generation_job', ['status', 'is_premium', 'created_at'])
    op.create_index('idx_job_user', 'generation_job', ['user_id', 'created_at'])

    # Link final artifacts back to their producing job (exactly-once via UNIQUE).
    for table in ('message', 'generated_image', 'generated_video'):
        op.add_column(table, sa.Column('job_id', sa.String(36), nullable=True))
        op.create_foreign_key(
            f'fk_{table}_job_id', table, 'generation_job',
            ['job_id'], ['id'],
        )
        op.create_unique_constraint(f'uq_{table}_job_id', table, ['job_id'])


def downgrade():
    for table in ('message', 'generated_image', 'generated_video'):
        op.drop_constraint(f'uq_{table}_job_id', table, type_='unique')
        op.drop_constraint(f'fk_{table}_job_id', table, type_='foreignkey')
        op.drop_column(table, 'job_id')

    op.drop_index('idx_job_user', table_name='generation_job')
    op.drop_index('idx_job_status', table_name='generation_job')
    op.drop_table('generation_job')
