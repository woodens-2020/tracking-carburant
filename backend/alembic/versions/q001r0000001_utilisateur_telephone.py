"""utilisateur_telephone — second canal OTP par SMS (Twilio)

Revision ID: q001r0000001
Revises: p001q0000001
Create Date: 2026-07-27
"""
from alembic import op
import sqlalchemy as sa

revision      = 'q001r0000001'
down_revision = 'p001q0000001'
branch_labels = None
depends_on    = None


def upgrade() -> None:
    op.add_column('utilisateurs', sa.Column('telephone', sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column('utilisateurs', 'telephone')
