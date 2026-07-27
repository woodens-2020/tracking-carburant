"""livraison_cloture_fifo — clôture de cargaison (suivi FIFO par livraison)

Revision ID: n001o0000001
Revises: m001n0000001
Create Date: 2026-07-26
"""
from alembic import op
import sqlalchemy as sa

revision      = 'n001o0000001'
down_revision = 'm001n0000001'
branch_labels = None
depends_on    = None


def upgrade() -> None:
    op.add_column('livraisons', sa.Column('terminee', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column('livraisons', sa.Column('gallons_report_recu', sa.Numeric(14, 3), nullable=False, server_default='0'))
    op.add_column('livraisons', sa.Column('gallons_restants_cloture', sa.Numeric(14, 3), nullable=True))
    op.add_column('livraisons', sa.Column('date_cloture', sa.DateTime(timezone=True), nullable=True))
    op.add_column('livraisons', sa.Column('utilisateur_cloture_id', sa.Integer(), sa.ForeignKey('utilisateurs.id', ondelete='SET NULL'), nullable=True))
    op.add_column('livraisons', sa.Column('report_vers_livraison_id', sa.Integer(), sa.ForeignKey('livraisons.id', ondelete='SET NULL'), nullable=True))


def downgrade() -> None:
    op.drop_column('livraisons', 'report_vers_livraison_id')
    op.drop_column('livraisons', 'utilisateur_cloture_id')
    op.drop_column('livraisons', 'date_cloture')
    op.drop_column('livraisons', 'gallons_restants_cloture')
    op.drop_column('livraisons', 'gallons_report_recu')
    op.drop_column('livraisons', 'terminee')
