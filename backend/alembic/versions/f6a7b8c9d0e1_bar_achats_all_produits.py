"""bar_achats_all_produits

Étend bar_achats pour accepter tous les produits (bar + station service).

Changements :
- produit_id devient nullable (était NOT NULL)
- ajout de station_produit_id FK vers produits.id
- CHECK : exactement un des deux FK doit être non-null
- index sur station_produit_id

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-07-01

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'f6a7b8c9d0e1'
down_revision: Union[str, Sequence[str], None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Rendre produit_id nullable
    op.alter_column('bar_achats', 'produit_id', nullable=True)

    # 2. Ajouter station_produit_id FK vers produits
    op.add_column('bar_achats',
        sa.Column('station_produit_id', sa.Integer(), nullable=True)
    )
    op.create_foreign_key(
        'fk_bar_achats_station_produit',
        'bar_achats', 'produits',
        ['station_produit_id'], ['id'],
        ondelete='RESTRICT',
    )

    # 3. CHECK : exactement un des deux FK non-null (XOR)
    op.create_check_constraint(
        'chk_bar_achat_produit_xor',
        'bar_achats',
        '(produit_id IS NOT NULL)::int + (station_produit_id IS NOT NULL)::int = 1',
    )

    # 4. Index sur le nouveau FK
    op.create_index('idx_bar_achats_station_produit', 'bar_achats', ['station_produit_id'])


def downgrade() -> None:
    op.drop_index('idx_bar_achats_station_produit', table_name='bar_achats')
    op.drop_constraint('chk_bar_achat_produit_xor', 'bar_achats', type_='check')
    op.drop_constraint('fk_bar_achats_station_produit', 'bar_achats', type_='foreignkey')
    op.drop_column('bar_achats', 'station_produit_id')
    op.alter_column('bar_achats', 'produit_id', nullable=False)
