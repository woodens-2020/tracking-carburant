"""bar_achat_depenses

Table bar_achat_depenses — dépenses supplémentaires par achat bar
(transport, manutention, etc.) pour le calcul du coût total.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-06-30

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, Sequence[str], None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'bar_achat_depenses',
        sa.Column('id',          sa.Integer(),       primary_key=True),
        sa.Column('achat_id',    sa.Integer(),       sa.ForeignKey('bar_achats.id', ondelete='CASCADE'), nullable=False),
        sa.Column('description', sa.String(150),     nullable=False),
        sa.Column('montant',     sa.Numeric(12, 2),  nullable=False),
        sa.CheckConstraint('montant >= 0', name='chk_bar_achat_dep_montant_pos'),
    )
    op.create_index('idx_bar_achat_dep_achat', 'bar_achat_depenses', ['achat_id'])


def downgrade() -> None:
    op.drop_index('idx_bar_achat_dep_achat', table_name='bar_achat_depenses')
    op.drop_table('bar_achat_depenses')
