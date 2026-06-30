"""bar_produit_caisse_fields

Ajoute vendu_par_caisse et unites_par_caisse sur bar_produits.

Revision ID: b2c3d4e5f6a7
Revises: a601f714d2f2
Create Date: 2026-06-29

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a601f714d2f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('bar_produits',
        sa.Column('vendu_par_caisse',  sa.Boolean(), nullable=False, server_default=sa.text('false')))
    op.add_column('bar_produits',
        sa.Column('unites_par_caisse', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('bar_produits', 'unites_par_caisse')
    op.drop_column('bar_produits', 'vendu_par_caisse')
