"""
Outil ponctuel : retire la permission "admin" du rôle "Manager" (accès à
l'espace Administration du système) — demandé explicitement par
l'utilisateur.

Ne touche à aucun autre domaine de permissions du rôle (bar, finance,
cuisine, hôtel, employés, carburant restent inchangés) — seul le
booléen `permissions.admin` passe à false. Les comptes utilisateurs avec
le rôle "Manager" perdent donc l'accès à /api/admin/* et à l'espace
Administration du frontend, sans rien perdre d'autre.

Idempotent : si le rôle n'existe pas ou n'a déjà pas la permission admin,
ne fait rien.
"""
import json
import os

from sqlalchemy import create_engine, text

DATABASE_URL = os.environ.get("DATABASE_URL", "")


def main() -> None:
    if not DATABASE_URL:
        print("DATABASE_URL absent — rien à faire.")
        return
    if not DATABASE_URL.startswith("postgresql"):
        print(f"DATABASE_URL non-Postgres ({DATABASE_URL.split(':')[0]}) — ignoré.")
        return

    engine = create_engine(DATABASE_URL)
    with engine.begin() as conn:
        row = conn.execute(text(
            "SELECT id, permissions FROM roles WHERE lower(nom) = 'manager'"
        )).fetchone()
        if not row:
            print("retirer_admin_manager : aucun rôle « Manager » trouvé — rien à faire.")
            return

        role_id, permissions = row
        if not permissions or not permissions.get("admin"):
            print("retirer_admin_manager : le rôle « Manager » n'a déjà pas la permission admin — rien à faire.")
            return

        permissions = dict(permissions)
        permissions["admin"] = False
        conn.execute(text(
            "UPDATE roles SET permissions = CAST(:perms AS json) WHERE id = :id"
        ), {"perms": json.dumps(permissions), "id": role_id})
        print(f"retirer_admin_manager : permission admin retirée du rôle « Manager » (id={role_id}).")


if __name__ == "__main__":
    main()
