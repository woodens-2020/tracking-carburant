"""
Outil ponctuel : remise à zéro des ventes comptoir (Bar + Cuisine +
Pâtisserie) et de leurs sessions de caisse — demandée explicitement par
l'utilisateur pour repartir propre après les tests du nouveau module
Pâtisserie, sans attendre un nouveau cycle de lancement complet.

Portée : bar_ventes (+ crédits/remboursements liés, lignes, sessions de
caisse et leurs évaluations), cuisine_ventes (+ lignes), patisserie_ventes
(+ lignes, sessions de caisse et leurs évaluations).

NE TOUCHE JAMAIS :
  - Le stock (bar_mouvements_stock, patisserie_mouvements_stock,
    cuisine_mouvements_stock) — quantités préservées, demande explicite.
  - Les catalogues (produits/plats/articles).
  - Les commandes en cours (bar_commandes), achats, dépenses, hôtel,
    payroll — hors du périmètre demandé cette fois (voir
    reset_avant_lancement.py, déjà exécuté et retiré, pour ce périmètre).
  - Les comptes (utilisateurs, employés, rôles) et le répertoire clients.

Irréversible. Ordre de suppression respecte les contraintes de clé
étrangère (bar_credits.vente_id est ON DELETE RESTRICT — doit être
supprimé avant bar_ventes ; le reste est CASCADE ou SET NULL).

Idempotent : si une table est déjà vide, elle est simplement ignorée.

Script ponctuel : à retirer du preDeployCommand (railway.json) et du
dépôt après utilisation, comme les outils similaires précédents de cette
session (clean_stock_bar.py, reset_avant_lancement.py, etc.).
"""
import os

from sqlalchemy import create_engine, text

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# (table, description) dans l'ordre de suppression sûr vis-à-vis des FK.
_TABLES = [
    ("bar_remboursements",            "Remboursements de crédit bar"),
    ("bar_credits",                   "Crédits bar (doit précéder bar_ventes — FK RESTRICT)"),
    ("bar_lignes_vente",              "Lignes de vente bar"),
    ("bar_ventes",                    "Ventes bar"),
    ("bar_session_evaluations",       "Évaluations de session de caisse bar"),
    ("bar_sessions_caisse",           "Sessions de caisse bar"),
    ("patisserie_session_evaluations","Évaluations de session de caisse pâtisserie"),
    ("patisserie_lignes_vente",       "Lignes de vente pâtisserie"),
    ("patisserie_ventes",             "Ventes pâtisserie"),
    ("patisserie_sessions_caisse",    "Sessions de caisse pâtisserie"),
    ("cuisine_lignes_vente",          "Lignes de vente cuisine"),
    ("cuisine_ventes",                "Ventes cuisine"),
]


def main() -> None:
    if not DATABASE_URL:
        print("clean_ventes_pos : DATABASE_URL absent — rien à faire.")
        return
    if not DATABASE_URL.startswith("postgresql"):
        print(f"clean_ventes_pos : DATABASE_URL non-Postgres ({DATABASE_URL.split(':')[0]}) — ignoré.")
        return

    engine = create_engine(DATABASE_URL)
    total_supprime = 0
    with engine.begin() as conn:
        for table, description in _TABLES:
            avant = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
            if not avant:
                print(f"clean_ventes_pos : {table} déjà vide — ignoré.")
                continue
            conn.execute(text(f"DELETE FROM {table}"))
            total_supprime += avant
            print(f"clean_ventes_pos : {avant} ligne(s) supprimée(s) dans {table} ({description}).")

    print(f"clean_ventes_pos : terminé — {total_supprime} ligne(s) supprimée(s) au total. "
          f"Stock, catalogues, commandes/achats/dépenses, hôtel, payroll et comptes non touchés.")


if __name__ == "__main__":
    main()
