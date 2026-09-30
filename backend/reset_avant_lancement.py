"""
Outil ponctuel : remise à zéro complète des données transactionnelles
(ventes, crédits, dépenses, payroll, séjours hôtel) avant le lancement
réel du système — demandée explicitement par l'utilisateur, avec le
détail exact des catégories à nettoyer (tableau de bord + rapport de
caisse : Ventes Bar/Cuisine/Hôtel, Achats Bar/Cuisine, Dépenses
Cuisine/Hôtel, Paiements employés Bar).

Portée : Bar, Cuisine, Hôtel, Payroll général (fiches_paie). Patisserie
et Zelle ne sont PAS touchés (pas mentionnés par l'utilisateur).

NE TOUCHE JAMAIS :
  - bar_mouvements_stock (le STOCK — quantités préservées, demande
    explicite de l'utilisateur : "pa touche stock yo")
  - Les catalogues (bar_produits, cuisine_plats, hotel_chambres)
  - Les comptes (utilisateurs, employes, roles) et le répertoire clients
    (clients.id lui-même reste — seuls bar_credits/remboursements, qui
    représentent leur dette, sont supprimés)

Irréversible. Ordre de suppression respecte les contraintes de clé
étrangère (bar_credits.vente_id est ON DELETE RESTRICT — doit être
supprimé avant bar_ventes ; le reste est CASCADE ou SET NULL, donc sans
contrainte d'ordre stricte, mais l'ordre ci-dessous reste explicite pour
la clarté du journal).

Idempotent : si une table est déjà vide, elle est simplement ignorée
(rien ne casse si ce script tourne deux fois).

Script ponctuel : à retirer du preDeployCommand (railway.json) et du
dépôt après utilisation, comme les outils similaires précédents de
cette session (clean_stock_bar.py, classer_produits_sans_lieu.py, etc.).
"""
import os

from sqlalchemy import create_engine, text

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# (table, description) dans l'ordre de suppression sûr vis-à-vis des FK.
_TABLES = [
    ("bar_remboursements",      "Remboursements de crédit bar"),
    ("bar_credits",             "Crédits bar (doit précéder bar_ventes — FK RESTRICT)"),
    ("bar_lignes_vente",        "Lignes de vente bar"),
    ("bar_ventes",              "Ventes bar"),
    ("bar_session_evaluations", "Évaluations de session de caisse"),
    ("bar_sessions_caisse",     "Sessions de caisse (rapports)"),
    ("bar_lignes_commande",     "Lignes de commande bar (tables en cours)"),
    ("bar_commandes",           "Commandes bar en cours"),
    ("bar_achat_depenses",      "Dépenses liées à un achat bar"),
    ("bar_achats",              "Achats/réceptions bar"),
    ("bar_paiements_employes",  "Paiements employés bar (salaire/avance/bonus/commission)"),
    ("cuisine_lignes_vente",    "Lignes de vente cuisine"),
    ("cuisine_ventes",          "Ventes cuisine"),
    ("cuisine_depenses",        "Dépenses cuisine"),
    ("cuisine_achats",          "Achats cuisine"),
    ("hotel_proformas",         "Pro forma / réservations à venir hôtel"),
    ("hotel_reservations",      "Séjours hôtel (+ historique)"),
    ("hotel_depenses",          "Dépenses hôtel"),
    ("fiches_paie",             "Fiches de paie (payroll général)"),
]


def main() -> None:
    if not DATABASE_URL:
        print("reset_avant_lancement : DATABASE_URL absent — rien à faire.")
        return
    if not DATABASE_URL.startswith("postgresql"):
        print(f"reset_avant_lancement : DATABASE_URL non-Postgres ({DATABASE_URL.split(':')[0]}) — ignoré.")
        return

    engine = create_engine(DATABASE_URL)
    total_supprime = 0
    with engine.begin() as conn:
        for table, description in _TABLES:
            avant = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
            if not avant:
                print(f"reset_avant_lancement : {table} déjà vide — ignoré.")
                continue
            conn.execute(text(f"DELETE FROM {table}"))
            total_supprime += avant
            print(f"reset_avant_lancement : {avant} ligne(s) supprimée(s) dans {table} ({description}).")

    print(f"reset_avant_lancement : terminé — {total_supprime} ligne(s) supprimée(s) au total. "
          f"Stock (bar_mouvements_stock) et catalogues non touchés.")


if __name__ == "__main__":
    main()
