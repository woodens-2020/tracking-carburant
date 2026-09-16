"""
Classe automatiquement, à chaque déploiement, les BarProduit dont `lieu`
est encore NULL (articles créés avant la séparation Bar Devant/Bar
Piscine — voir models.py).

Un produit est reclassé quand son historique de mouvements de stock
(bar_mouvements_stock.lieu — renseigné pour les ajustements/pertes/casses
et, en best-effort, les ventes ; jamais pour les achats/réceptions,
volontairement communs aux deux bars) ne pointe que vers UN SEUL bar.
Les produits avec un signal mixte (mouvements dans les deux bars) ou sans
aucun signal exploitable restent NULL et sont listés dans la sortie pour
reclassement manuel (page Produits Bar → Modifier → champ Bar).

Idempotent et sans risque : relancé une fois tous les produits classés
(ou pour les cas ambigus/sans signal, qui ne changent pas), il n'a plus
rien à faire. Complète l'outil GET/POST /api/admin/classer-lieu (page
Produits Bar) en l'exécutant automatiquement, sans action manuelle.
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
        rows = conn.execute(text(
            """
            SELECT p.id, p.nom,
                   COALESCE(s.n_devant, 0)  AS n_devant,
                   COALESCE(s.n_piscine, 0) AS n_piscine
            FROM bar_produits p
            LEFT JOIN (
                SELECT produit_id,
                       COUNT(*) FILTER (WHERE lieu = 'DEVANT')  AS n_devant,
                       COUNT(*) FILTER (WHERE lieu = 'PISCINE') AS n_piscine
                FROM bar_mouvements_stock
                WHERE lieu IS NOT NULL
                GROUP BY produit_id
            ) s ON s.produit_id = p.id
            WHERE p.lieu IS NULL
            ORDER BY p.nom
            """
        )).fetchall()

        if not rows:
            print("classer_produits_sans_lieu : aucun produit sans bar — rien à faire.")
            return

        classes, ambigus, sans_signal = [], [], []
        for produit_id, nom, n_devant, n_piscine in rows:
            if n_devant > 0 and n_piscine > 0:
                ambigus.append(nom)
            elif n_devant > 0:
                conn.execute(text("UPDATE bar_produits SET lieu = 'DEVANT' WHERE id = :id"), {"id": produit_id})
                classes.append(f"{nom} -> DEVANT")
            elif n_piscine > 0:
                conn.execute(text("UPDATE bar_produits SET lieu = 'PISCINE' WHERE id = :id"), {"id": produit_id})
                classes.append(f"{nom} -> PISCINE")
            else:
                sans_signal.append(nom)

        print(f"classer_produits_sans_lieu : {len(classes)} classé(s) automatiquement.")
        for ligne in classes:
            print(f"  - {ligne}")
        if ambigus:
            print(f"  {len(ambigus)} ambigu(s) (historique mélangé, à classer manuellement) : {', '.join(ambigus)}")
        if sans_signal:
            print(f"  {len(sans_signal)} sans signal (aucun historique lieu-tagué, à classer manuellement) : {', '.join(sans_signal)}")


if __name__ == "__main__":
    main()
