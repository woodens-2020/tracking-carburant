"""admin_codes table

Revision ID: h001i0000001
Revises: f001g0000001
Create Date: 2026-07-03
"""
from alembic import op
import sqlalchemy as sa

revision     = 'h001i0000001'
down_revision = 'f001g0000001'
branch_labels = None
depends_on    = None


def upgrade():
    op.create_table(
        "admin_codes",
        sa.Column("id",         sa.Integer(),                            nullable=False, primary_key=True),
        sa.Column("user_id",    sa.Integer(),                            nullable=False),
        sa.Column("code_hash",  sa.String(64),                           nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True),              nullable=False),
        sa.Column("used",       sa.Boolean(),                nullable=False, server_default="false"),
        sa.Column("attempts",   sa.Integer(),                nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["user_id"], ["utilisateurs.id"], ondelete="CASCADE"),
    )
    op.create_index("idx_admin_code_user",    "admin_codes", ["user_id"])
    op.create_index("idx_admin_code_expires", "admin_codes", ["expires_at"])


def downgrade():
    op.drop_index("idx_admin_code_expires", "admin_codes")
    op.drop_index("idx_admin_code_user",    "admin_codes")
    op.drop_table("admin_codes")
