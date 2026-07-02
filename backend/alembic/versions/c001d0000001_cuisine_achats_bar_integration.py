"""cuisine_achats + bar_lignes_vente cuisine integration

Revision ID: c001d0000001
Revises: b001c0000001
Create Date: 2026-07-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c001d0000001'
down_revision: Union[str, Sequence[str], None] = 'b001c0000001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. Rendre produit_id nullable dans bar_lignes_vente ───────────
    op.alter_column('bar_lignes_vente', 'produit_id', nullable=True)

    # ── 2. Ajouter cuisine_plat_id à bar_lignes_vente ─────────────────
    op.add_column('bar_lignes_vente',
        sa.Column('cuisine_plat_id', sa.Integer(),
                  sa.ForeignKey('cuisine_plats.id', ondelete='SET NULL'),
                  nullable=True)
    )

    # ── 3. Contrainte : au moins produit_id OU cuisine_plat_id ────────
    op.create_check_constraint(
        'chk_bar_ligne_produit_ou_cuisine',
        'bar_lignes_vente',
        'produit_id IS NOT NULL OR cuisine_plat_id IS NOT NULL',
    )

    # ── 4. Index cuisine_plat_id ───────────────────────────────────────
    op.create_index('idx_bar_lignes_cuisine_plat', 'bar_lignes_vente', ['cuisine_plat_id'])

    # ── 5. Créer table cuisine_achats ──────────────────────────────────
    op.create_table(
        'cuisine_achats',
        sa.Column('id',            sa.Integer(),      primary_key=True),
        sa.Column('plat_id',       sa.Integer(),      sa.ForeignKey('cuisine_plats.id', ondelete='SET NULL'), nullable=True),
        sa.Column('description',   sa.String(200),    nullable=False),
        sa.Column('categorie',     sa.String(80),     nullable=True),
        sa.Column('quantite',      sa.Numeric(10, 3), nullable=False),
        sa.Column('unite',         sa.String(20),     nullable=True),
        sa.Column('cout_unitaire', sa.Numeric(12, 2), nullable=False),
        sa.Column('total',         sa.Numeric(14, 2), nullable=False),
        sa.Column('date_achat',    sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('fournisseur',   sa.String(150),    nullable=True),
        sa.Column('notes',         sa.String(300),    nullable=True),
        sa.CheckConstraint('quantite > 0',      name='chk_cuisine_achat_qte_pos'),
        sa.CheckConstraint('cout_unitaire >= 0', name='chk_cuisine_achat_cout_pos'),
        sa.CheckConstraint('total >= 0',         name='chk_cuisine_achat_total_pos'),
    )
    op.create_index('idx_cuisine_achats_plat', 'cuisine_achats', ['plat_id'])
    op.create_index('idx_cuisine_achats_date', 'cuisine_achats', ['date_achat'])


def downgrade() -> None:
    op.drop_index('idx_cuisine_achats_date', 'cuisine_achats')
    op.drop_index('idx_cuisine_achats_plat', 'cuisine_achats')
    op.drop_table('cuisine_achats')

    op.drop_index('idx_bar_lignes_cuisine_plat', 'bar_lignes_vente')
    op.drop_constraint('chk_bar_ligne_produit_ou_cuisine', 'bar_lignes_vente', type_='check')
    op.drop_column('bar_lignes_vente', 'cuisine_plat_id')
    op.alter_column('bar_lignes_vente', 'produit_id', nullable=False)
