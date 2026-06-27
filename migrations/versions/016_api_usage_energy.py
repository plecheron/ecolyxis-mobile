"""Add energy_wh and co2e_g to api_usage for sustainability tracking.

Revision ID: 016_api_usage_energy
Revises: 015_energy_ledger
"""
from alembic import op
import sqlalchemy as sa

revision = "016_api_usage_energy"
down_revision = "015_energy_ledger"
branch_labels = None
depends_on = None

# Match app.sustainability constants
WH_PER_TOKEN = 0.001
UK_GRID_CO2_PER_KWH = 0.18
ECOLYXIS_PUE = 1.0


def upgrade():
    op.add_column("api_usage", sa.Column("energy_wh", sa.Float(), nullable=True))
    op.add_column("api_usage", sa.Column("co2e_g", sa.Float(), nullable=True))

    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            UPDATE api_usage
            SET energy_wh = (tokens_prompt + tokens_completion) * :wh,
                co2e_g = (tokens_prompt + tokens_completion) * :wh * :pue * :grid
            WHERE tokens_prompt + tokens_completion > 0
            """
        ),
        {"wh": WH_PER_TOKEN, "pue": ECOLYXIS_PUE, "grid": UK_GRID_CO2_PER_KWH},
    )


def downgrade():
    op.drop_column("api_usage", "co2e_g")
    op.drop_column("api_usage", "energy_wh")
