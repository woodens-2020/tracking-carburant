"""bar_categories_table

Table bar_categories — catégories persistantes pour les articles POS.
Seed automatique avec les catégories distinctes déjà présentes dans bar_produits.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-06-30

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, Sequence[str], None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None

# Couleurs par défaut pour les catégories courantes
_COULEURS = {
    'boisson':  '#3fb6a8',
    'alcool':   '#a78bfa',
    'soft':     '#60a5fa',
    'plat':     '#f7a93b',
    'snack':    '#fb923c',
    'autre':    '#94a3b8',
    'tabac':    '#f87171',
    'dessert':  '#f472b6',
}


def upgrade() -> None:
    op.create_table(
        'bar_categories',
        sa.Column('id',            sa.Integer(),     primary_key=True),
        sa.Column('nom',           sa.String(80),    nullable=False),
        sa.Column('couleur',       sa.String(20),    nullable=True),
        sa.Column('date_creation', sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('nom', name='uq_bar_categories_nom'),
    )

    # Seed : récupère les catégories distinctes déjà utilisées dans bar_produits
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT DISTINCT lower(trim(categorie)) FROM bar_produits WHERE categorie IS NOT NULL")
    ).fetchall()

    for (cat,) in rows:
        if not cat:
            continue
        couleur = _COULEURS.get(cat, '#94a3b8')
        conn.execute(
            sa.text(
                "INSERT INTO bar_categories (nom, couleur) VALUES (:nom, :couleur) "
                "ON CONFLICT (nom) DO NOTHING"
            ),
            {"nom": cat, "couleur": couleur},
        )


def downgrade() -> None:
    op.drop_table('bar_categories')
