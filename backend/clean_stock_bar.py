"""
Outil ponctuel : supprime TOUT l'historique de mouvements de stock bar
(bar_mouvements_stock) -- Reception, Vente, Ajustement, Perte, Casse,
Volontaire -- pour repartir d'un stock totalement propre (0 partout,
Bar Devant et Bar Piscine) apres la serie de tests sur la separation des
catalogues.

Irreversible : aucun historique n'est conserve (contrairement au
"reset-exploitation" precedent qui gardait l'historique via des
mouvements compensatoires). Demande explicitement par l'utilisateur --
voir conversation. Ne touche ni au catalogue produits, ni aux ventes
(bar_ventes/bar_lignes_vente), ni aux achats (bar_achats) -- seulement
la table de mouvements elle-meme (bar_mouvements_stock.achat_id et
.reference_vente_id sont ON DELETE SET NULL sur ces tables, donc aucune
contrainte de cle etrangere ne bloque cette suppression directe).

Script ponctuel : a retirer du preDeployCommand (railway.json) apres
usage, comme les outils similaires precedents de cette session.
"""
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
        avant = conn.execute(text("SELECT COUNT(*) FROM bar_mouvements_stock")).scalar()
        if not avant:
            print("clean_stock_bar : bar_mouvements_stock déjà vide — rien à faire.")
            return
        conn.execute(text("DELETE FROM bar_mouvements_stock"))
        print(f"clean_stock_bar : {avant} mouvement(s) de stock bar supprimé(s) — stock repart à 0 (Devant et Piscine).")


if __name__ == "__main__":
    main()
