"""hotel_init

Création des tables de la section Hôtel :
- hotel_chambres   : configuration des chambres
- hotel_employes   : personnel hôtel
- hotel_reservations : enregistrements clients (nuit / moment)

Revision ID: i9b0c1d2e3f4
Revises: h8a9b0c1d2e3
Create Date: 2026-07-01
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'i9b0c1d2e3f4'
down_revision: Union[str, Sequence[str], None] = 'h8a9b0c1d2e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── hotel_chambres ───────────────────────────────────────────
    op.create_table(
        'hotel_chambres',
        sa.Column('id',           sa.Integer(),     primary_key=True),
        sa.Column('numero',       sa.String(20),    nullable=False),
        sa.Column('type_chambre', sa.String(20),    nullable=False, server_default='SIMPLE'),
        sa.Column('etage',        sa.Integer(),     nullable=True),
        sa.Column('capacite',     sa.Integer(),     nullable=False, server_default='1'),
        sa.Column('prix_nuit',    sa.Numeric(12,2), nullable=False),
        sa.Column('prix_moment',  sa.Numeric(12,2), nullable=True),
        sa.Column('statut',       sa.String(20),    nullable=False, server_default='DISPONIBLE'),
        sa.Column('description',  sa.String(300),   nullable=True),
        sa.Column('actif',        sa.Boolean(),     nullable=False, server_default='true'),
        sa.Column('date_creation', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint('numero', name='uq_hotel_chambre_numero'),
        sa.CheckConstraint("type_chambre IN ('SIMPLE','DOUBLE','SUITE','VIP')",
                           name='chk_hotel_chambre_type'),
        sa.CheckConstraint("statut IN ('DISPONIBLE','OCCUPEE','MAINTENANCE','FERMEE')",
                           name='chk_hotel_chambre_statut'),
        sa.CheckConstraint('prix_nuit > 0',  name='chk_hotel_chambre_prix_nuit'),
        sa.CheckConstraint('capacite >= 1',  name='chk_hotel_chambre_capacite'),
    )
    op.create_index('idx_hotel_chambres_statut', 'hotel_chambres', ['statut'])
    op.create_index('idx_hotel_chambres_actif',  'hotel_chambres', ['actif'])

    # ── hotel_employes ───────────────────────────────────────────
    op.create_table(
        'hotel_employes',
        sa.Column('id',            sa.Integer(),     primary_key=True),
        sa.Column('nom',           sa.String(100),   nullable=False),
        sa.Column('prenom',        sa.String(100),   nullable=False),
        sa.Column('poste',         sa.String(40),    nullable=False, server_default='RECEPTIONNISTE'),
        sa.Column('telephone',     sa.String(30),    nullable=True),
        sa.Column('email',         sa.String(100),   nullable=True),
        sa.Column('date_embauche', sa.Date(),        nullable=True),
        sa.Column('salaire_base',  sa.Numeric(12,2), nullable=True),
        sa.Column('actif',         sa.Boolean(),     nullable=False, server_default='true'),
        sa.Column('notes',         sa.String(300),   nullable=True),
        sa.Column('created_at',    sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint(
            "poste IN ('RECEPTIONNISTE','FEMME_DE_CHAMBRE','GERANT','SECURITE','AUTRE')",
            name='chk_hotel_emp_poste',
        ),
    )
    op.create_index('idx_hotel_employes_actif', 'hotel_employes', ['actif'])

    # ── hotel_reservations ───────────────────────────────────────
    op.create_table(
        'hotel_reservations',
        sa.Column('id',                 sa.Integer(),     primary_key=True),
        sa.Column('chambre_id',         sa.Integer(),     nullable=False),
        sa.Column('client_nom',         sa.String(150),   nullable=False),
        sa.Column('client_contact',     sa.String(100),   nullable=True),
        sa.Column('client_id_piece',    sa.String(80),    nullable=True),
        sa.Column('type_sejour',        sa.String(10),    nullable=False),
        sa.Column('date_arrivee',       sa.DateTime(timezone=True), nullable=False),
        sa.Column('date_depart_prevue', sa.DateTime(timezone=True), nullable=False),
        sa.Column('date_depart_reel',   sa.DateTime(timezone=True), nullable=True),
        sa.Column('nb_nuits',           sa.Integer(),     nullable=True),
        sa.Column('nb_heures',          sa.Numeric(5,2),  nullable=True),
        sa.Column('prix_unitaire',      sa.Numeric(12,2), nullable=False),
        sa.Column('montant_total',      sa.Numeric(12,2), nullable=False),
        sa.Column('montant_paye',       sa.Numeric(12,2), nullable=False, server_default='0'),
        sa.Column('solde',              sa.Numeric(12,2), nullable=False, server_default='0'),
        sa.Column('statut',             sa.String(20),    nullable=False, server_default='EN_COURS'),
        sa.Column('mode_paiement',      sa.String(20),    nullable=True),
        sa.Column('notes',              sa.String(300),   nullable=True),
        sa.Column('employe_id',         sa.Integer(),     nullable=True),
        sa.Column('created_at',         sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['chambre_id'], ['hotel_chambres.id'], ondelete='RESTRICT',
                                name='fk_hotel_res_chambre'),
        sa.ForeignKeyConstraint(['employe_id'], ['hotel_employes.id'], ondelete='SET NULL',
                                name='fk_hotel_res_employe'),
        sa.CheckConstraint("type_sejour IN ('NUIT','MOMENT')",
                           name='chk_hotel_res_type'),
        sa.CheckConstraint("statut IN ('EN_COURS','TERMINEE','ANNULEE')",
                           name='chk_hotel_res_statut'),
        sa.CheckConstraint('montant_total >= 0', name='chk_hotel_res_total_pos'),
        sa.CheckConstraint('montant_paye  >= 0', name='chk_hotel_res_paye_pos'),
        sa.CheckConstraint('solde         >= 0', name='chk_hotel_res_solde_pos'),
    )
    op.create_index('idx_hotel_res_chambre', 'hotel_reservations', ['chambre_id'])
    op.create_index('idx_hotel_res_statut',  'hotel_reservations', ['statut'])
    op.create_index('idx_hotel_res_date',    'hotel_reservations', ['date_arrivee'])


def downgrade() -> None:
    op.drop_table('hotel_reservations')
    op.drop_table('hotel_employes')
    op.drop_table('hotel_chambres')
