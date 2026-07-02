"""Gestion des connexions : ip/user_agent sur sessions + table audit_logs

Revision ID: f001g0000001
Revises: e001f0000001
Create Date: 2026-07-02 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str  = 'f001g0000001'
down_revision: Union[str, Sequence[str], None] = 'e001f0000001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None]    = None


def upgrade() -> None:
    # Enrichir la table sessions avec IP et navigateur
    op.add_column('sessions', sa.Column('ip_address', sa.String(45),  nullable=True))
    op.add_column('sessions', sa.Column('user_agent', sa.String(255), nullable=True))

    # Créer la table journal d'activité
    op.create_table(
        'audit_logs',
        sa.Column('id',             sa.Integer(),    primary_key=True, autoincrement=True),
        sa.Column('user_id',        sa.Integer(),    nullable=True),
        sa.Column('action',         sa.String(50),   nullable=False),
        sa.Column('target_user_id', sa.Integer(),    nullable=True),
        sa.Column('ip_address',     sa.String(45),   nullable=True),
        sa.Column('details',        sa.Text(),       nullable=True),
        sa.Column('created_at',     sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['user_id'],        ['utilisateurs.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['target_user_id'], ['utilisateurs.id'], ondelete='SET NULL'),
    )
    op.create_index('idx_audit_user',    'audit_logs', ['user_id'])
    op.create_index('idx_audit_action',  'audit_logs', ['action'])
    op.create_index('idx_audit_created', 'audit_logs', ['created_at'])


def downgrade() -> None:
    op.drop_index('idx_audit_created', 'audit_logs')
    op.drop_index('idx_audit_action',  'audit_logs')
    op.drop_index('idx_audit_user',    'audit_logs')
    op.drop_table('audit_logs')
    op.drop_column('sessions', 'user_agent')
    op.drop_column('sessions', 'ip_address')
