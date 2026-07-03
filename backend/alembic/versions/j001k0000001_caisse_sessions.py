"""caisse_sessions — sessions de caisse par caissière

Revision ID: j001k0000001
Revises: h001i0000001
Create Date: 2026-07-03
"""
from alembic import op
import sqlalchemy as sa

revision     = 'j001k0000001'
down_revision = 'h001i0000001'
branch_labels = None
depends_on    = None


def upgrade() -> None:
    # Lien employe ↔ compte système
    op.add_column('employes', sa.Column('utilisateur_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_employe_utilisateur', 'employes', 'utilisateurs',
        ['utilisateur_id'], ['id'], ondelete='SET NULL',
    )
    op.create_index('idx_employe_utilisateur', 'employes', ['utilisateur_id'])

    # Table des sessions de caisse
    op.create_table(
        'bar_sessions_caisse',
        sa.Column('id',            sa.Integer(),                  primary_key=True),
        sa.Column('caissier_id',   sa.Integer(),                  nullable=False),
        sa.Column('date_session',  sa.Date(),                     nullable=False),
        sa.Column('statut',        sa.String(20),                 nullable=False, server_default='EN_COURS'),
        sa.Column('soumis_at',     sa.DateTime(timezone=True),    nullable=True),
        sa.Column('valide_at',     sa.DateTime(timezone=True),    nullable=True),
        sa.Column('valide_par_id', sa.Integer(),                  nullable=True),
        sa.Column('notes_admin',   sa.String(500),                nullable=True),
        sa.Column('created_at',    sa.DateTime(timezone=True),    nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['caissier_id'],   ['employes.id'],    ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['valide_par_id'], ['utilisateurs.id'], ondelete='SET NULL'),
        sa.UniqueConstraint('caissier_id', 'date_session', name='uq_session_caissier_date'),
        sa.CheckConstraint("statut IN ('EN_COURS','SOUMIS','VALIDE')", name='chk_session_statut'),
    )
    op.create_index('idx_session_caissier', 'bar_sessions_caisse', ['caissier_id'])
    op.create_index('idx_session_date',     'bar_sessions_caisse', ['date_session'])
    op.create_index('idx_session_statut',   'bar_sessions_caisse', ['statut'])


def downgrade() -> None:
    op.drop_index('idx_session_statut',   'bar_sessions_caisse')
    op.drop_index('idx_session_date',     'bar_sessions_caisse')
    op.drop_index('idx_session_caissier', 'bar_sessions_caisse')
    op.drop_table('bar_sessions_caisse')

    op.drop_index('idx_employe_utilisateur', 'employes')
    op.drop_constraint('fk_employe_utilisateur', 'employes', type_='foreignkey')
    op.drop_column('employes', 'utilisateur_id')
