"""CRM — table station_interactions (historique interactions partenaires)

Revision ID: n001o0000001
Revises: m001n0000001
Create Date: 2026-07-04
"""
from alembic import op
import sqlalchemy as sa

revision = 'n001o0000001'
down_revision = 'm001n0000001'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    exists = conn.execute(sa.text(
        "SELECT 1 FROM information_schema.tables WHERE table_name='station_interactions'"
    )).fetchone()
    if not exists:
        op.create_table(
            'station_interactions',
            sa.Column('id',               sa.Integer(),    primary_key=True),
            sa.Column('client_id',        sa.Integer(),    sa.ForeignKey('station_clients.id', ondelete='CASCADE'), nullable=False),
            sa.Column('utilisateur_id',   sa.Integer(),    sa.ForeignKey('utilisateurs.id',    ondelete='SET NULL'), nullable=True),
            sa.Column('type_interaction', sa.String(20),   nullable=False),
            sa.Column('titre',            sa.String(150),  nullable=False),
            sa.Column('description',      sa.Text(),       nullable=True),
            sa.Column('date_interaction', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column('created_at',       sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint(
                "type_interaction IN ('APPEL','EMAIL','REUNION','NOTE','VISITE')",
                name='chk_si_type',
            ),
        )
        op.create_index('idx_si_client', 'station_interactions', ['client_id'])
        op.create_index('idx_si_date',   'station_interactions', ['date_interaction'])


def downgrade():
    op.drop_table('station_interactions')
