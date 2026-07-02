"""Table otp_codes pour la vérification en deux étapes

Revision ID: e001f0000001
Revises: d001e0000001
Create Date: 2026-07-02 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str  = 'e001f0000001'
down_revision: Union[str, Sequence[str], None] = 'd001e0000001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None]    = None


def upgrade() -> None:
    op.create_table(
        'otp_codes',
        sa.Column('id',            sa.Integer(),    primary_key=True, autoincrement=True),
        sa.Column('user_id',       sa.Integer(),    nullable=False),
        sa.Column('code_hash',     sa.String(64),   nullable=False),
        sa.Column('pending_token', sa.String(64),   nullable=True, unique=True),
        sa.Column('created_at',    sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('expires_at',    sa.DateTime(timezone=True), nullable=False),
        sa.Column('attempts',      sa.Integer(),    nullable=False, server_default='0'),
        sa.Column('used',          sa.Boolean(),    nullable=False, server_default='false'),
        sa.ForeignKeyConstraint(['user_id'], ['utilisateurs.id'], ondelete='CASCADE'),
    )
    op.create_index('idx_otp_user_id', 'otp_codes', ['user_id'])
    op.create_index('idx_otp_pending', 'otp_codes', ['pending_token'])
    op.create_index('idx_otp_expires', 'otp_codes', ['expires_at'])


def downgrade() -> None:
    op.drop_index('idx_otp_expires', 'otp_codes')
    op.drop_index('idx_otp_pending', 'otp_codes')
    op.drop_index('idx_otp_user_id', 'otp_codes')
    op.drop_table('otp_codes')
