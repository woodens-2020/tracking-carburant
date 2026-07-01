"""categorie_fk

Interconnexion bar_produits.categorie <-> bar_categories.nom :
- Sync des valeurs manquantes dans bar_categories
- Ajout FK bar_produits.categorie → bar_categories.nom
  ON UPDATE CASCADE (renommer une catégorie met à jour tous ses produits)
  ON DELETE RESTRICT  (impossible de supprimer une catégorie utilisée)

Revision ID: h8a9b0c1d2e3
Revises: g7a8b9c0d1e2
Create Date: 2026-07-01
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'h8a9b0c1d2e3'
down_revision: Union[str, Sequence[str], None] = 'g7a8b9c0d1e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Insérer dans bar_categories toutes les valeurs de bar_produits.categorie
    #    qui n'y sont pas encore (ON CONFLICT DO NOTHING grâce à uq_bar_categories_nom)
    conn.execute(sa.text("""
        INSERT INTO bar_categories (nom, couleur)
        SELECT DISTINCT categorie, '#64748b'
        FROM bar_produits
        WHERE categorie IS NOT NULL
        ON CONFLICT (nom) DO NOTHING
    """))

    # 2. Ajouter la FK avec ON UPDATE CASCADE + ON DELETE RESTRICT
    op.create_foreign_key(
        'fk_bar_produits_categorie',
        'bar_produits', 'bar_categories',
        ['categorie'], ['nom'],
        onupdate='CASCADE',
        ondelete='RESTRICT',
    )


def downgrade() -> None:
    op.drop_constraint('fk_bar_produits_categorie', 'bar_produits', type_='foreignkey')
