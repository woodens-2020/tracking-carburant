"""geoloc_visibility — table geolocalisation_config + colonnes distance_km/statut_geoloc

Revision ID: m001n0000001
Revises: l001m0000001
Create Date: 2026-07-16
"""
from alembic import op
import sqlalchemy as sa

revision      = 'm001n0000001'
down_revision = 'l001m0000001'
branch_labels = None
depends_on    = None


def upgrade() -> None:
    op.create_table(
        'geolocalisation_config',
        sa.Column('id',                    sa.Integer(),               primary_key=True),
        sa.Column('institution_latitude',  sa.Float(),                 nullable=True),
        sa.Column('institution_longitude', sa.Float(),                 nullable=True),
        sa.Column('rayon_alerte_km',       sa.Float(),                 nullable=False, server_default='1.0'),
        sa.Column('updated_at',            sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column('login_security_events', sa.Column('distance_km',   sa.Float(),    nullable=True))
    op.add_column('login_security_events', sa.Column('statut_geoloc', sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column('login_security_events', 'statut_geoloc')
    op.drop_column('login_security_events', 'distance_km')
    op.drop_table('geolocalisation_config')
