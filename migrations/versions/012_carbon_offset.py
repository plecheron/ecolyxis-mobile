"""Create carbon_offset table for carbon capture and tree planting records.

Revision ID: 012_carbon_offset
"""
from alembic import op
import sqlalchemy as sa

revision = "012_carbon_offset"
down_revision = "935297907504"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "carbon_offset",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("offset_type", sa.String(20), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("amount_kg", sa.Float()),
        sa.Column("tree_count", sa.Integer()),
        sa.Column("reference_number", sa.String(100)),
        sa.Column("cost_gbp", sa.Float()),
        sa.Column("certificate_image", sa.LargeBinary()),
        sa.Column("certificate_image_filename", sa.String(200)),
        sa.Column("certificate_image_mime", sa.String(100)),
        sa.Column("purchase_date", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("created_by", sa.String(80)),
    )
    op.create_index("ix_carbon_offset_type", "carbon_offset", ["offset_type"])


def downgrade():
    op.drop_index("ix_carbon_offset_type", table_name="carbon_offset")
    op.drop_table("carbon_offset")
