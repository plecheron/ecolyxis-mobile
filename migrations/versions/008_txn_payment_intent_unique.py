"""Unique index on transaction.stripe_payment_intent_id (webhook idempotency, #87)

NULLs are distinct in both Postgres and SQLite, so the many usage/refund rows
without a payment intent are unaffected; only a duplicate Stripe credit can
collide.

Revision ID: 008_txn_pi_unique
Revises: cdcaab4c7b22
Create Date: 2026-06-12
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "008_txn_pi_unique"
down_revision = "cdcaab4c7b22"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("transaction", schema=None) as batch_op:
        batch_op.create_unique_constraint(
            "uq_transaction_stripe_payment_intent_id", ["stripe_payment_intent_id"]
        )


def downgrade():
    with op.batch_alter_table("transaction", schema=None) as batch_op:
        batch_op.drop_constraint(
            "uq_transaction_stripe_payment_intent_id", type_="unique"
        )
