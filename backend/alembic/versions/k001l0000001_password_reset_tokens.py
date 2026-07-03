"""password_reset_tokens — réinitialisation sécurisée de mot de passe

Revision ID: k001l0000001
Revises: j001k0000001
Create Date: 2026-07-03
"""
from alembic import op
import sqlalchemy as sa

revision     = 'k001l0000001'
down_revision = 'j001k0000001'
branch_labels = None
depends_on    = None


def upgrade() -> None:
    op.create_table(
        'password_reset_tokens',
        sa.Column('id',         sa.Integer(),                primary_key=True),
        sa.Column('user_id',    sa.Integer(),                nullable=False),
        sa.Column('token_hash', sa.String(64),               nullable=False, unique=True),
        sa.Column('created_at', sa.DateTime(timezone=True),  nullable=False, server_default=sa.text('now()')),
        sa.Column('expires_at', sa.DateTime(timezone=True),  nullable=False),
        sa.Column('used',       sa.Boolean(),                nullable=False, server_default='false'),
        sa.Column('ip_address', sa.String(45),               nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['utilisateurs.id'], ondelete='CASCADE'),
    )
    op.create_index('idx_prt_user',    'password_reset_tokens', ['user_id'])
    op.create_index('idx_prt_expires', 'password_reset_tokens', ['expires_at'])
    op.create_index('idx_prt_hash',    'password_reset_tokens', ['token_hash'])


def downgrade() -> None:
    op.drop_index('idx_prt_hash',    'password_reset_tokens')
    op.drop_index('idx_prt_expires', 'password_reset_tokens')
    op.drop_index('idx_prt_user',    'password_reset_tokens')
    op.drop_table('password_reset_tokens')
