"""station CRM — clients, credits, remboursements, factures

Revision ID: m001n0000001
Revises: l001m0000001
Create Date: 2026-07-04
"""
from alembic import op
import sqlalchemy as sa

revision = 'm001n0000001'
down_revision = 'l001m0000001'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()

    # ── station_clients ──────────────────────────────────────────────
    exists = conn.execute(sa.text(
        "SELECT 1 FROM information_schema.tables WHERE table_name='station_clients'"
    )).fetchone()
    if not exists:
        op.create_table(
            'station_clients',
            sa.Column('id',          sa.Integer(),    primary_key=True),
            sa.Column('nom',         sa.String(150),  nullable=False),
            sa.Column('telephone',   sa.String(30),   nullable=True),
            sa.Column('email',       sa.String(254),  nullable=True),
            sa.Column('nif',         sa.String(50),   nullable=True),
            sa.Column('adresse',     sa.String(300),  nullable=True),
            sa.Column('type_client', sa.String(20),   nullable=False, server_default='PARTICULIER'),
            sa.Column('notes',       sa.String(500),  nullable=True),
            sa.Column('actif',       sa.Boolean(),    nullable=False, server_default=sa.text('true')),
            sa.Column('created_at',  sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("type_client IN ('PARTICULIER','ENTREPRISE')", name='chk_sc_type'),
        )
        op.create_index('idx_sc_nom',   'station_clients', ['nom'])
        op.create_index('idx_sc_actif', 'station_clients', ['actif'])

    # ── station_credits ──────────────────────────────────────────────
    exists = conn.execute(sa.text(
        "SELECT 1 FROM information_schema.tables WHERE table_name='station_credits'"
    )).fetchone()
    if not exists:
        op.create_table(
            'station_credits',
            sa.Column('id',            sa.Integer(),        primary_key=True),
            sa.Column('client_id',     sa.Integer(),        sa.ForeignKey('station_clients.id', ondelete='RESTRICT'), nullable=False),
            sa.Column('produit_id',    sa.Integer(),        sa.ForeignKey('produits.id',         ondelete='SET NULL'),  nullable=True),
            sa.Column('numero',        sa.String(30),       nullable=False, unique=True),
            sa.Column('montant_total', sa.Numeric(14, 2),   nullable=False),
            sa.Column('montant_paye',  sa.Numeric(14, 2),   nullable=False, server_default=sa.text('0')),
            sa.Column('quantite',      sa.Numeric(12, 3),   nullable=True),
            sa.Column('prix_unitaire', sa.Numeric(12, 2),   nullable=True),
            sa.Column('statut',        sa.String(20),       nullable=False, server_default='EN_COURS'),
            sa.Column('date_credit',   sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column('date_echeance', sa.Date(),           nullable=True),
            sa.Column('notes',         sa.String(500),      nullable=True),
            sa.CheckConstraint('montant_total > 0',  name='chk_scredit_total_pos'),
            sa.CheckConstraint('montant_paye >= 0',  name='chk_scredit_paye_pos'),
            sa.CheckConstraint("statut IN ('EN_COURS','SOLDE','ANNULE')", name='chk_scredit_statut'),
        )
        op.create_index('idx_scredit_client', 'station_credits', ['client_id'])
        op.create_index('idx_scredit_statut', 'station_credits', ['statut'])
        op.create_index('idx_scredit_date',   'station_credits', ['date_credit'])

    # ── station_credit_remboursements ────────────────────────────────
    exists = conn.execute(sa.text(
        "SELECT 1 FROM information_schema.tables WHERE table_name='station_credit_remboursements'"
    )).fetchone()
    if not exists:
        op.create_table(
            'station_credit_remboursements',
            sa.Column('id',                 sa.Integer(),       primary_key=True),
            sa.Column('credit_id',          sa.Integer(),       sa.ForeignKey('station_credits.id', ondelete='CASCADE'), nullable=False),
            sa.Column('montant',            sa.Numeric(14, 2),  nullable=False),
            sa.Column('date_remboursement', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column('notes',              sa.String(300),     nullable=True),
            sa.CheckConstraint('montant > 0', name='chk_scrembours_montant_pos'),
        )
        op.create_index('idx_scrembours_credit', 'station_credit_remboursements', ['credit_id'])

    # ── station_factures ─────────────────────────────────────────────
    exists = conn.execute(sa.text(
        "SELECT 1 FROM information_schema.tables WHERE table_name='station_factures'"
    )).fetchone()
    if not exists:
        op.create_table(
            'station_factures',
            sa.Column('id',              sa.Integer(),       primary_key=True),
            sa.Column('client_id',       sa.Integer(),       sa.ForeignKey('station_clients.id', ondelete='RESTRICT'), nullable=False),
            sa.Column('numero_facture',  sa.String(30),      nullable=False, unique=True),
            sa.Column('date_facture',    sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column('date_echeance',   sa.Date(),          nullable=True),
            sa.Column('lignes',          sa.JSON(),          nullable=False, server_default=sa.text("'[]'::json")),
            sa.Column('montant_ht',      sa.Numeric(14, 2),  nullable=False, server_default=sa.text('0')),
            sa.Column('taux_tva',        sa.Numeric(5, 2),   nullable=False, server_default=sa.text('0')),
            sa.Column('montant_tva',     sa.Numeric(14, 2),  nullable=False, server_default=sa.text('0')),
            sa.Column('montant_ttc',     sa.Numeric(14, 2),  nullable=False, server_default=sa.text('0')),
            sa.Column('statut',          sa.String(20),      nullable=False, server_default='BROUILLON'),
            sa.Column('notes',           sa.String(500),     nullable=True),
            sa.Column('email_envoye_at', sa.DateTime(timezone=True), nullable=True),
            sa.CheckConstraint('montant_ttc >= 0',  name='chk_sfact_ttc_pos'),
            sa.CheckConstraint('taux_tva >= 0',     name='chk_sfact_tva_pos'),
            sa.CheckConstraint("statut IN ('BROUILLON','ENVOYEE','PAYEE','ANNULEE')", name='chk_sfact_statut'),
        )
        op.create_index('idx_sfact_client', 'station_factures', ['client_id'])
        op.create_index('idx_sfact_statut', 'station_factures', ['statut'])
        op.create_index('idx_sfact_date',   'station_factures', ['date_facture'])


def downgrade():
    op.drop_table('station_factures')
    op.drop_table('station_credit_remboursements')
    op.drop_table('station_credits')
    op.drop_table('station_clients')
