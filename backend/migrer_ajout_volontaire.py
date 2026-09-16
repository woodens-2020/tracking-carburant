"""
Ajoute VOLONTAIRE à la liste des type_mouvement autorisés sur
bar_mouvements_stock (contrainte chk_bar_mouv_type).

Pourquoi un script à part plutôt que `alembic upgrade head` en pre-deploy :
ce projet n'a jamais eu de table de suivi alembic tenue à jour en production
(les 23 migrations précédentes ont été appliquées manuellement, pas de
`alembic_version` fiable) — lancer `alembic upgrade head` à l'aveugle
rejouerait potentiellement les 23 migrations depuis zéro, dont plusieurs
`CREATE TABLE` sans garde d'existence, ce qui échouerait sur un schéma déjà
en place. Ce script fait UNIQUEMENT le changement nécessaire, en toute
sécurité :

Le script est idempotent : relancé une fois la contrainte déjà à jour, il
ne fait rien (affiche un message et sort proprement) — donc réexécutable à
chaque déploiement sans risque.

Usage : `python migrer_ajout_volontaire.py` (lit DATABASE_URL depuis
l'environnement, comme le reste de l'application).
"""
import os

from sqlalchemy import create_engine, text

DATABASE_URL = os.environ.get("DATABASE_URL", "")


def main() -> None:
    if not DATABASE_URL:
        print("DATABASE_URL absent — rien à faire (environnement de build sans base ?).")
        return
    if not DATABASE_URL.startswith("postgresql"):
        print(f"DATABASE_URL non-Postgres ({DATABASE_URL.split(':')[0]}) — script sans objet, on ignore.")
        return

    engine = create_engine(DATABASE_URL)
    with engine.begin() as conn:
        deja_a_jour = conn.execute(text(
            "SELECT 1 FROM pg_constraint "
            "WHERE conname = 'chk_bar_mouv_type' "
            "AND pg_get_constraintdef(oid) LIKE '%VOLONTAIRE%'"
        )).fetchone()
        if deja_a_jour:
            print("chk_bar_mouv_type déjà à jour (VOLONTAIRE déjà autorisé) — rien à faire.")
            return

        conn.execute(text("ALTER TABLE bar_mouvements_stock DROP CONSTRAINT IF EXISTS chk_bar_mouv_type"))
        conn.execute(text(
            "ALTER TABLE bar_mouvements_stock ADD CONSTRAINT chk_bar_mouv_type "
            "CHECK (type_mouvement IN ('ENTREE','SORTIE_VENTE','AJUSTEMENT','PERTE','CASSE','VOLONTAIRE'))"
        ))
        print("chk_bar_mouv_type mis à jour : VOLONTAIRE ajouté aux types de mouvement autorisés.")


if __name__ == "__main__":
    main()
