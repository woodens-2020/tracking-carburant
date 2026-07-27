"""livraison_rapport_cloture — rapport de vente fige par cargaison (historique de vente)

Revision ID: o001p0000001
Revises: n001o0000001
Create Date: 2026-07-26
"""
from alembic import op
import sqlalchemy as sa

revision      = 'o001p0000001'
down_revision = 'n001o0000001'
branch_labels = None
depends_on    = None


def upgrade() -> None:
    op.add_column('livraisons', sa.Column('rapport_gallons_vendus', sa.Numeric(14, 3), nullable=True))
    op.add_column('livraisons', sa.Column('rapport_revenu', sa.Numeric(14, 2), nullable=True))


def downgrade() -> None:
    op.drop_column('livraisons', 'rapport_revenu')
    op.drop_column('livraisons', 'rapport_gallons_vendus')
