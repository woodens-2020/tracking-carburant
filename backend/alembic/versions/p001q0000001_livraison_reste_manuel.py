"""livraison_reste_manuel — reste avant saisi manuellement, compte dans le stock agrege

Revision ID: p001q0000001
Revises: o001p0000001
Create Date: 2026-07-27
"""
from alembic import op
import sqlalchemy as sa

revision      = 'p001q0000001'
down_revision = 'o001p0000001'
branch_labels = None
depends_on    = None


def upgrade() -> None:
    op.add_column('livraisons', sa.Column('gallons_reste_manuel', sa.Numeric(14, 3), nullable=False, server_default='0'))


def downgrade() -> None:
    op.drop_column('livraisons', 'gallons_reste_manuel')
