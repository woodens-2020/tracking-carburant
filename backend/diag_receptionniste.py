"""
Outil ponctuel, LECTURE SEULE : diagnostique comment les comptes
réceptionniste(s) sont réellement configurés en production, pour
confirmer si le niveau "operationnel" (hotel) — déjà resserré pour
exclure dépenses/employés/rapport — s'applique bien à eux, ou si leur
compte utilise un chemin différent (poste legacy sans role_id, ou un
rôle personnalisé à un autre niveau que "operationnel").

N'écrit rien en base. Script ponctuel : à retirer du preDeployCommand
(railway.json) et du dépôt après consultation des logs, comme les
autres outils ponctuels de cette session.
"""
import os

from sqlalchemy import create_engine, text

DATABASE_URL = os.environ.get("DATABASE_URL", "")


def main() -> None:
    if not DATABASE_URL:
        print("diag_receptionniste : DATABASE_URL absent — rien à faire.")
        return
    if not DATABASE_URL.startswith("postgresql"):
        print(f"diag_receptionniste : DATABASE_URL non-Postgres ({DATABASE_URL.split(':')[0]}) — ignoré.")
        return

    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        print("=== Rôles personnalisés contenant 'recep' ou 'récep' dans le nom ===")
        roles = conn.execute(text(
            "SELECT id, nom, permissions FROM roles WHERE LOWER(nom) LIKE '%recep%'"
        )).fetchall()
        if not roles:
            print("  (aucun rôle trouvé avec ce nom)")
        for r in roles:
            print(f"  Role id={r[0]} nom={r[1]!r} permissions={r[2]!r}")

        print()
        print("=== Comptes utilisateurs (poste/role_id) contenant 'recep' dans poste OU nom_complet ===")
        users = conn.execute(text(
            "SELECT u.id, u.nom_complet, u.poste, u.role_id, r.nom AS role_nom, r.permissions "
            "FROM utilisateurs u LEFT JOIN roles r ON r.id = u.role_id "
            "WHERE LOWER(COALESCE(u.poste,'')) LIKE '%recep%' OR LOWER(COALESCE(u.nom_complet,'')) LIKE '%recep%'"
        )).fetchall()
        if not users:
            print("  (aucun utilisateur trouvé avec ce critère)")
        for u in users:
            print(f"  User id={u[0]} nom={u[1]!r} poste={u[2]!r} role_id={u[3]} role_nom={u[4]!r} role_permissions={u[5]!r}")

        print()
        print("=== Tous les comptes utilisateurs actifs, avec leur rôle (vue d'ensemble) ===")
        all_users = conn.execute(text(
            "SELECT u.id, u.nom_complet, u.poste, u.role, u.role_id, r.nom AS role_nom, r.permissions "
            "FROM utilisateurs u LEFT JOIN roles r ON r.id = u.role_id "
            "WHERE u.actif = true ORDER BY u.id"
        )).fetchall()
        for u in all_users:
            print(f"  User id={u[0]} nom={u[1]!r} poste={u[2]!r} role_natif={u[3]!r} role_id={u[4]} role_nom={u[5]!r} role_permissions={u[6]!r}")

    print()
    print("diag_receptionniste : terminé (lecture seule, rien modifié).")


if __name__ == "__main__":
    main()
