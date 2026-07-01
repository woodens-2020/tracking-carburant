"""bar_achats_statut

Colonne statut sur bar_achats pour le workflow en deux étapes :
EN_ATTENTE (achat créé, stock pas encore mis à jour)
CONFIRME   (stock mis à jour, ou non applicable pour carburants)

Revision ID: g7a8b9c0d1e2
Revises: f6a7b8c9d0e1
Create Date: 2026-07-01

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'g7a8b9c0d1e2'
down_revision: Union[str, Sequence[str], None] = 'f6a7b8c9d0e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Ajouter colonne statut (nullable d'abord pour pouvoir setter les existants)
    op.add_column('bar_achats',
        sa.Column('statut', sa.String(20), nullable=True)
    )

    conn = op.get_bind()

    # Existants ayant déjà un mouvement de stock → CONFIRME
    conn.execute(sa.text("""
        UPDATE bar_achats SET statut = 'CONFIRME'
        WHERE id IN (
            SELECT DISTINCT achat_id FROM bar_mouvements_stock
            WHERE achat_id IS NOT NULL
        )
    """))

    # Carburants (station_produit_id non null) → pas de stock bar → CONFIRME
    conn.execute(sa.text("""
        UPDATE bar_achats SET statut = 'CONFIRME'
        WHERE station_produit_id IS NOT NULL AND statut IS NULL
    """))

    # Tout le reste → EN_ATTENTE
    conn.execute(sa.text("""
        UPDATE bar_achats SET statut = 'EN_ATTENTE'
        WHERE statut IS NULL
    """))

    # Passer NOT NULL avec la valeur par défaut
    op.alter_column('bar_achats', 'statut', nullable=False,
                    server_default='EN_ATTENTE')

    op.create_check_constraint(
        'chk_bar_achat_statut',
        'bar_achats',
        "statut IN ('EN_ATTENTE','CONFIRME')",
    )
    op.create_index('idx_bar_achats_statut', 'bar_achats', ['statut'])


def downgrade() -> None:
    op.drop_index('idx_bar_achats_statut', table_name='bar_achats')
    op.drop_constraint('chk_bar_achat_statut', 'bar_achats', type_='check')
    op.drop_column('bar_achats', 'statut')
