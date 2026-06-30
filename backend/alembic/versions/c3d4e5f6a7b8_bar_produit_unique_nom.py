"""bar_produit_unique_nom

Contrainte unique sur bar_produits.nom (insensible à la casse via index fonctionnel).

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-29

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Index unique fonctionnel sur lower(nom) — empêche les doublons insensibles à la casse
    op.execute(
        "CREATE UNIQUE INDEX uq_bar_produits_nom_lower ON bar_produits (lower(nom))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_bar_produits_nom_lower")
