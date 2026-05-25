"""initial schema — all tables

Revision ID: 001_initial
Revises: 
Create Date: 2026-05-25

Captures the full production schema. Replaces the previous migration
(9438c89cc322) which only had ALTER TABLE commands and no CREATE TABLE.

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '001_initial'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # ── user ──────────────────────────────────────────────────────────
    op.create_table(
        'user',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('username', sa.String(length=80), nullable=False),
        sa.Column('email', sa.String(length=120), nullable=False),
        sa.Column('password_hash', sa.String(length=256), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_login', sa.DateTime(timezone=True), nullable=True),
        sa.Column('tier', sa.String(length=20), nullable=False, server_default='free'),
        sa.Column('stripe_customer_id', sa.String(length=120), nullable=True),
        sa.Column('stripe_subscription_id', sa.String(length=120), nullable=True),
        sa.Column('subscription_status', sa.String(length=30), nullable=True),
        sa.Column('cancel_at_period_end', sa.Boolean(), nullable=False, server_default='false'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('user_email_key', 'user', ['email'], unique=True)
    op.create_index('user_username_key', 'user', ['username'], unique=True)

    # ── thread ────────────────────────────────────────────────────────
    op.create_table(
        'thread',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=True),
        sa.Column('system_prompt', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )

    # ── message ───────────────────────────────────────────────────────
    op.create_table(
        'message',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('thread_id', sa.String(length=36), nullable=False),
        sa.Column('role', sa.String(length=20), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('tokens_used', sa.Integer(), nullable=True),
        sa.Column('message_type', sa.String(length=10), nullable=False, server_default='text'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['thread_id'], ['thread.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )

    # ── web_authn_credential ──────────────────────────────────────────
    op.create_table(
        'web_authn_credential',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('credential_id', sa.LargeBinary(), nullable=False),
        sa.Column('public_key', sa.LargeBinary(), nullable=False),
        sa.Column('sign_count', sa.Integer(), nullable=True),
        sa.Column('name', sa.String(length=80), nullable=True),
        sa.Column('transports', sa.String(length=200), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'web_authn_credential_credential_id_key',
        'web_authn_credential', ['credential_id'], unique=True,
    )

    # ── api_key ───────────────────────────────────────────────────────
    op.create_table(
        'api_key',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=80), nullable=False),
        sa.Column('key_hash', sa.String(length=64), nullable=False),
        sa.Column('key_prefix', sa.String(length=8), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('api_key_key_hash_key', 'api_key', ['key_hash'], unique=True)

    # ── api_usage ─────────────────────────────────────────────────────
    op.create_table(
        'api_usage',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('api_key_id', sa.Integer(), nullable=False),
        sa.Column('endpoint', sa.String(length=100), nullable=False),
        sa.Column('model', sa.String(length=100), nullable=True),
        sa.Column('tokens_prompt', sa.Integer(), nullable=True),
        sa.Column('tokens_completion', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['api_key_id'], ['api_key.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_api_usage_api_key_id', 'api_usage', ['api_key_id'])
    op.create_index('idx_api_usage_created_at', 'api_usage', ['created_at'])

    # ── wallet ────────────────────────────────────────────────────────
    op.create_table(
        'wallet',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('balance_pence', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('wallet_user_id_key', 'wallet', ['user_id'], unique=True)

    # ── transaction ───────────────────────────────────────────────────
    op.create_table(
        'transaction',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('wallet_id', sa.Integer(), nullable=False),
        sa.Column('type', sa.String(length=20), nullable=False),
        sa.Column('amount_pence', sa.Integer(), nullable=False),
        sa.Column('description', sa.String(length=255), nullable=False),
        sa.Column('api_key_id', sa.Integer(), nullable=True),
        sa.Column('stripe_payment_intent_id', sa.String(length=120), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['api_key_id'], ['api_key.id'], ),
        sa.ForeignKeyConstraint(['wallet_id'], ['wallet.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_transaction_wallet_id', 'transaction', ['wallet_id'])

    # ── generated_image ───────────────────────────────────────────────
    op.create_table(
        'generated_image',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('message_id', sa.Integer(), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('thread_id', sa.String(length=36), nullable=False),
        sa.Column('prompt', sa.Text(), nullable=False),
        sa.Column('seed', sa.BigInteger(), nullable=False),
        sa.Column('width', sa.Integer(), nullable=False),
        sa.Column('height', sa.Integer(), nullable=False),
        sa.Column('filename', sa.String(length=120), nullable=False),
        sa.Column('parent_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['message_id'], ['message.id'], ),
        sa.ForeignKeyConstraint(['parent_id'], ['generated_image.id'], ),
        sa.ForeignKeyConstraint(['thread_id'], ['thread.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )

    # ── generated_video ───────────────────────────────────────────────
    op.create_table(
        'generated_video',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('message_id', sa.Integer(), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('thread_id', sa.String(length=36), nullable=False),
        sa.Column('prompt', sa.Text(), nullable=False),
        sa.Column('seed', sa.Integer(), nullable=False),
        sa.Column('width', sa.Integer(), nullable=False),
        sa.Column('height', sa.Integer(), nullable=False),
        sa.Column('frames', sa.Integer(), nullable=False),
        sa.Column('fps', sa.Integer(), nullable=False),
        sa.Column('filename', sa.String(length=120), nullable=False),
        sa.Column('duration_s', sa.Float(), nullable=True),
        sa.Column('model', sa.String(length=60), nullable=False),
        sa.Column('parent_image_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['message_id'], ['message.id'], ),
        sa.ForeignKeyConstraint(['parent_image_id'], ['generated_image.id'], ),
        sa.ForeignKeyConstraint(['thread_id'], ['thread.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )

    # ── llm_queue ─────────────────────────────────────────────────────
    op.create_table(
        'llm_queue',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('is_premium', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'idx_queue_status', 'llm_queue',
        ['status', 'is_premium', 'created_at'],
    )


def downgrade():
    op.drop_table('llm_queue')
    op.drop_table('generated_video')
    op.drop_table('generated_image')
    op.drop_table('transaction')
    op.drop_table('wallet')
    op.drop_table('api_usage')
    op.drop_table('api_key')
    op.drop_table('web_authn_credential')
    op.drop_table('message')
    op.drop_table('thread')
    op.drop_table('user')
