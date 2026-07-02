"""Roles table + utilisateurs.role_id — système QuickBooks de permissions

Revision ID: d001e0000001
Revises: c001d0000001
Create Date: 2026-07-02 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'd001e0000001'
down_revision: Union[str, Sequence[str], None] = 'c001d0000001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ROLES_SYSTEME = [
    {
        "nom":         "Administrateur",
        "description": "Accès complet à toutes les fonctionnalités et à la gestion des utilisateurs",
        "permissions": {"finance":"complet","bar":"complet","cuisine":"complet","hotel":"complet","employes":"complet","carburant":"complet","admin":True},
        "est_admin":   True,
        "est_systeme": True,
    },
    {
        "nom":         "Directeur Général",
        "description": "Accès complet à toutes les opérations sans gestion des utilisateurs",
        "permissions": {"finance":"complet","bar":"complet","cuisine":"complet","hotel":"complet","employes":"complet","carburant":"complet","admin":False},
        "est_admin":   False,
        "est_systeme": True,
    },
    {
        "nom":         "Gérant",
        "description": "Accès opérationnel complet à toutes les sections",
        "permissions": {"finance":"complet","bar":"complet","cuisine":"complet","hotel":"complet","employes":"complet","carburant":"complet","admin":False},
        "est_admin":   False,
        "est_systeme": True,
    },
    {
        "nom":         "Comptable",
        "description": "Accès complet aux finances, lecture seule pour les opérations",
        "permissions": {"finance":"complet","bar":"lecture","cuisine":"lecture","hotel":"lecture","employes":"complet","carburant":"lecture","admin":False},
        "est_admin":   False,
        "est_systeme": True,
    },
    {
        "nom":         "Superviseur",
        "description": "Accès complet aux opérations, lecture seule des finances",
        "permissions": {"finance":"lecture","bar":"complet","cuisine":"complet","hotel":"complet","employes":"lecture","carburant":"complet","admin":False},
        "est_admin":   False,
        "est_systeme": True,
    },
    {
        "nom":         "Barman / Caissier Bar",
        "description": "Accès uniquement à la caisse et aux opérations bar",
        "permissions": {"finance":"aucun","bar":"complet","cuisine":"aucun","hotel":"aucun","employes":"aucun","carburant":"aucun","admin":False},
        "est_admin":   False,
        "est_systeme": True,
    },
    {
        "nom":         "Cuisinier",
        "description": "Accès uniquement aux opérations cuisine",
        "permissions": {"finance":"aucun","bar":"aucun","cuisine":"complet","hotel":"aucun","employes":"aucun","carburant":"aucun","admin":False},
        "est_admin":   False,
        "est_systeme": True,
    },
    {
        "nom":         "Réceptionniste",
        "description": "Accès uniquement aux opérations hôtelières",
        "permissions": {"finance":"aucun","bar":"aucun","cuisine":"aucun","hotel":"complet","employes":"aucun","carburant":"aucun","admin":False},
        "est_admin":   False,
        "est_systeme": True,
    },
    {
        "nom":         "Pompiste",
        "description": "Saisie carburant uniquement",
        "permissions": {"finance":"aucun","bar":"aucun","cuisine":"aucun","hotel":"aucun","employes":"aucun","carburant":"complet","admin":False},
        "est_admin":   False,
        "est_systeme": True,
    },
    {
        "nom":         "Vue uniquement",
        "description": "Lecture seule sur toutes les sections",
        "permissions": {"finance":"lecture","bar":"lecture","cuisine":"lecture","hotel":"lecture","employes":"lecture","carburant":"lecture","admin":False},
        "est_admin":   False,
        "est_systeme": True,
    },
]


def upgrade() -> None:
    # ── 1. Créer la table roles ───────────────────────────────────
    op.create_table(
        'roles',
        sa.Column('id',            sa.Integer(),    primary_key=True, autoincrement=True),
        sa.Column('nom',           sa.String(100),  nullable=False, unique=True),
        sa.Column('description',   sa.String(300),  nullable=True),
        sa.Column('permissions',   sa.JSON(),        nullable=False, server_default='{}'),
        sa.Column('est_admin',     sa.Boolean(),    nullable=False, server_default='false'),
        sa.Column('est_systeme',   sa.Boolean(),    nullable=False, server_default='false'),
        sa.Column('date_creation', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
    )
    op.create_index('idx_roles_nom', 'roles', ['nom'])

    # ── 2. Insérer les rôles prédéfinis ──────────────────────────
    import json
    bind = op.get_bind()
    for rd in _ROLES_SYSTEME:
        bind.execute(
            sa.text(
                "INSERT INTO roles (nom, description, permissions, est_admin, est_systeme) "
                "VALUES (:nom, :desc, CAST(:perms AS jsonb), :ea, :es)"
            ),
            {
                "nom":  rd["nom"],
                "desc": rd["description"],
                "perms": json.dumps(rd["permissions"]),
                "ea":   rd["est_admin"],
                "es":   rd["est_systeme"],
            }
        )

    # ── 3. Ajouter role_id à utilisateurs ────────────────────────
    op.add_column('utilisateurs', sa.Column('role_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_utilisateurs_role_id', 'utilisateurs', 'roles',
        ['role_id'], ['id'], ondelete='SET NULL'
    )
    op.create_index('idx_utilisateurs_role_id', 'utilisateurs', ['role_id'])

    # ── 4. Mapper les utilisateurs existants vers un rôle ────────
    bind.execute(sa.text("""
        UPDATE utilisateurs u
        SET role_id = (
            SELECT id FROM roles r WHERE r.nom = CASE
                WHEN u.role = 'admin'          THEN 'Administrateur'
                WHEN u.role = 'pdg'            THEN 'Directeur Général'
                WHEN u.poste = 'Comptable'     THEN 'Comptable'
                WHEN u.poste = 'Superviseur'   THEN 'Superviseur'
                WHEN u.poste = 'Pompiste'      THEN 'Pompiste'
                WHEN u.poste = 'Caissier'      THEN 'Barman / Caissier Bar'
                ELSE 'Administrateur'
            END
            LIMIT 1
        )
        WHERE u.role_id IS NULL
    """))


def downgrade() -> None:
    op.drop_index('idx_utilisateurs_role_id', 'utilisateurs')
    op.drop_constraint('fk_utilisateurs_role_id', 'utilisateurs', type_='foreignkey')
    op.drop_column('utilisateurs', 'role_id')
    op.drop_index('idx_roles_nom', 'roles')
    op.drop_table('roles')
