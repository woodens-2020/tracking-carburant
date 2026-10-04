"""
Outil ponctuel : corrige le niveau "hotel" du rôle "Réceptionniste".

Diagnostic (diag_receptionniste.py, déjà exécuté et retiré) a révélé que ce
rôle avait permissions.hotel == "aucun" — zéro accès au module Hôtel,
y compris réception/séjours/historique, pourtant nécessaires au travail
quotidien d'un réceptionniste. C'est pour ça que le resserrement fait côté
frontend (narrowing du preset "operationnel" dans _AREA_PAGES.hotel,
commit 19f3f95, pour exclure dépenses/rapport/employés) n'avait aucun
effet réel : ce rôle n'utilisait pas du tout ce niveau.

Corrige en mettant permissions.hotel = "operationnel", qui donne
désormais exactement : tableau de bord + réception + séjours + historique
— sans dépenses, sans rapport, sans employés hôtel, sans chambres, sans
pro forma. C'est exactement l'accès demandé.

Cible le rôle par son nom exact ("Réceptionniste"), pas par un id fixe,
pour fonctionner correctement sur la base de production ET la base de
staging (deux bases Postgres distinctes partageant ce même
preDeployCommand, voir railway.json).

Idempotent : si le rôle est déjà au niveau "operationnel" (ou n'existe
pas), ne fait rien.

Script ponctuel : à retirer du preDeployCommand (railway.json) et du
dépôt après exécution en production, comme les autres outils ponctuels
de cette session.
"""
import json
import os

from sqlalchemy import create_engine, text

DATABASE_URL = os.environ.get("DATABASE_URL", "")

NOM_ROLE = "Réceptionniste"


def main() -> None:
    if not DATABASE_URL:
        print("fixer_role_receptionniste : DATABASE_URL absent — rien à faire.")
        return
    if not DATABASE_URL.startswith("postgresql"):
        print(f"fixer_role_receptionniste : DATABASE_URL non-Postgres ({DATABASE_URL.split(':')[0]}) — ignoré.")
        return

    engine = create_engine(DATABASE_URL)
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT id, permissions FROM roles WHERE nom = :nom"),
            {"nom": NOM_ROLE},
        ).fetchone()
        if not row:
            print(f"fixer_role_receptionniste : aucun rôle nommé {NOM_ROLE!r} — rien à faire.")
            return

        role_id, permissions = row
        if isinstance(permissions, str):
            permissions = json.loads(permissions)
        permissions = dict(permissions or {})

        avant = permissions.get("hotel")
        if avant == "operationnel":
            print(f"fixer_role_receptionniste : rôle id={role_id} déjà au niveau 'operationnel' — rien à faire.")
            return

        permissions["hotel"] = "operationnel"
        conn.execute(
            text("UPDATE roles SET permissions = :permissions WHERE id = :id"),
            {"permissions": json.dumps(permissions), "id": role_id},
        )
        print(f"fixer_role_receptionniste : rôle id={role_id} ({NOM_ROLE}) — "
              f"hotel changé de {avant!r} à 'operationnel'.")

    print("fixer_role_receptionniste : terminé.")


if __name__ == "__main__":
    main()
