"""Add energy_wh and co2e_g columns to message table.

Sustainability tracking for v0.7.0-beta: stores real GPU energy consumption
(Watt-hours) and CO₂e (grams) per assistant message, captured from nvidia-smi
power samples during inference.

Revision ID: 011_message_energy
Revises: 935297907504
Create Date: 2026-06-15
"""
from alembic import op
import sqlalchemy as sa

revision = '011_message_energy'
down_revision = '935297907504'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('message', sa.Column('energy_wh', sa.Float(), nullable=True))
    op.add_column('message', sa.Column('co2e_g', sa.Float(), nullable=True))


def downgrade():
    op.drop_column('message', 'co2e_g')
    op.drop_column('message', 'energy_wh')
