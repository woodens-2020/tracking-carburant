"""cuisine_tables_init

Revision ID: b001c0000001
Revises: a601f714d2f2
Create Date: 2026-07-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b001c0000001'
down_revision: Union[str, Sequence[str], None] = 'i9b0c1d2e3f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'cuisine_plats',
        sa.Column('id',            sa.Integer(),       nullable=False),
        sa.Column('nom',           sa.String(150),     nullable=False),
        sa.Column('categorie',     sa.String(80),      nullable=True),
        sa.Column('description',   sa.String(300),     nullable=True),
        sa.Column('prix_vente',    sa.Numeric(12, 2),  nullable=False),
        sa.Column('cout_estime',   sa.Numeric(12, 2),  nullable=True),
        sa.Column('actif',         sa.Boolean(),       nullable=False, server_default=sa.text('true')),
        sa.Column('date_creation', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.CheckConstraint('prix_vente > 0', name='chk_cuisine_plat_prix_pos'),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'cuisine_depenses',
        sa.Column('id',           sa.Integer(),      nullable=False),
        sa.Column('description',  sa.String(200),    nullable=False),
        sa.Column('categorie',    sa.String(80),     nullable=True),
        sa.Column('montant',      sa.Numeric(12, 2), nullable=False),
        sa.Column('date_depense', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('fournisseur',  sa.String(150),    nullable=True),
        sa.Column('notes',        sa.String(300),    nullable=True),
        sa.CheckConstraint('montant > 0', name='chk_cuisine_dep_montant_pos'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_cuisine_depenses_date', 'cuisine_depenses', ['date_depense'])

    op.create_table(
        'cuisine_ventes',
        sa.Column('id',            sa.Integer(),    nullable=False),
        sa.Column('numero_ticket', sa.String(30),   nullable=False),
        sa.Column('date_heure',    sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('total',         sa.Numeric(12, 2), nullable=False),
        sa.Column('mode_paiement', sa.String(20),   nullable=False, server_default='CASH'),
        sa.Column('client_nom',    sa.String(100),  nullable=True),
        sa.Column('notes',         sa.String(200),  nullable=True),
        sa.Column('statut',        sa.String(20),   nullable=False, server_default='VALIDEE'),
        sa.CheckConstraint('total >= 0',                         name='chk_cuisine_vente_total_pos'),
        sa.CheckConstraint("statut IN ('VALIDEE','ANNULEE')",    name='chk_cuisine_vente_statut'),
        sa.CheckConstraint("mode_paiement IN ('CASH','CREDIT')", name='chk_cuisine_vente_mode'),
        sa.UniqueConstraint('numero_ticket'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_cuisine_ventes_date',   'cuisine_ventes', ['date_heure'])
    op.create_index('idx_cuisine_ventes_statut', 'cuisine_ventes', ['statut'])

    op.create_table(
        'cuisine_lignes_vente',
        sa.Column('id',            sa.Integer(),      nullable=False),
        sa.Column('vente_id',      sa.Integer(),      nullable=False),
        sa.Column('plat_id',       sa.Integer(),      nullable=True),
        sa.Column('nom_plat',      sa.String(150),    nullable=False),
        sa.Column('quantite',      sa.Integer(),      nullable=False),
        sa.Column('prix_unitaire', sa.Numeric(12, 2), nullable=False),
        sa.Column('sous_total',    sa.Numeric(12, 2), nullable=False),
        sa.CheckConstraint('quantite > 0',      name='chk_cuisine_lv_qte_pos'),
        sa.CheckConstraint('prix_unitaire >= 0', name='chk_cuisine_lv_prix_pos'),
        sa.CheckConstraint('sous_total >= 0',    name='chk_cuisine_lv_sous_pos'),
        sa.ForeignKeyConstraint(['vente_id'], ['cuisine_ventes.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['plat_id'],  ['cuisine_plats.id'],  ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('idx_cuisine_lv_vente', 'cuisine_lignes_vente', ['vente_id'])
    op.create_index('idx_cuisine_lv_plat',  'cuisine_lignes_vente', ['plat_id'])


def downgrade() -> None:
    op.drop_index('idx_cuisine_lv_plat',  table_name='cuisine_lignes_vente')
    op.drop_index('idx_cuisine_lv_vente', table_name='cuisine_lignes_vente')
    op.drop_table('cuisine_lignes_vente')
    op.drop_index('idx_cuisine_ventes_statut', table_name='cuisine_ventes')
    op.drop_index('idx_cuisine_ventes_date',   table_name='cuisine_ventes')
    op.drop_table('cuisine_ventes')
    op.drop_index('idx_cuisine_depenses_date', table_name='cuisine_depenses')
    op.drop_table('cuisine_depenses')
    op.drop_table('cuisine_plats')
