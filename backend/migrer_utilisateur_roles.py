"""
Crée la table utilisateur_roles (pool de rôles qu'un employé peut choisir
d'activer à la connexion — voir models.UtilisateurRole, /api/me/roles,
/api/me/choisir-role) si elle n'existe pas encore.

Idempotent : relancé une fois la table créée, ne fait rien. Suit le même
mécanisme que migrer_ajout_volontaire.py (pas d'alembic upgrade head en
production faute de suivi fiable — voir ce fichier pour le détail).
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
        existe = conn.execute(text(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'utilisateur_roles'"
        )).fetchone()
        if existe:
            print("migrer_utilisateur_roles : table déjà présente — rien à faire.")
            return

        conn.execute(text(
            """
            CREATE TABLE utilisateur_roles (
                utilisateur_id INTEGER NOT NULL REFERENCES utilisateurs(id) ON DELETE CASCADE,
                role_id        INTEGER NOT NULL REFERENCES roles(id)        ON DELETE CASCADE,
                PRIMARY KEY (utilisateur_id, role_id)
            )
            """
        ))
        print("migrer_utilisateur_roles : table utilisateur_roles créée.")


if __name__ == "__main__":
    main()
