"""
Classe automatiquement, à chaque déploiement, TOUS les BarProduit dont
`lieu` est encore NULL (articles créés avant la séparation Bar Devant/Bar
Piscine — voir models.py) — plus aucun ne reste "visible aux deux bars"
après ce script.

Ordre de résolution, du plus au moins fiable :
  1. _OVERRIDES_CONFIRMES (confirmations explicites de l'utilisateur pour
     des cas ambigus qu'on connaît par ailleurs, ex. "Aloe").
  2. Signal d'historique (bar_mouvements_stock.lieu — renseigné pour les
     ajustements/pertes/casses et, en best-effort, les ventes ; jamais
     pour les achats/réceptions, volontairement communs) : reclassé
     directement s'il ne pointe que vers UN SEUL bar.
  3. Défaut raisonnable pour tout le reste (signal mixte ou absent) : Bar
     Piscine si "piscine" apparaît dans le nom, Bar Devant (bar principal)
     sinon. Chaque application du défaut est journalisée pour révision —
     à corriger en un clic (Produits Bar → Modifier) si besoin, mais ça
     n'empêche plus la séparation de fonctionner en attendant.

Idempotent et sans risque : une fois tous les produits classés, il n'a
plus rien à faire. Complète l'outil GET/POST /api/admin/classer-lieu
(page Produits Bar) en l'exécutant automatiquement, sans action manuelle.
"""
import os

from sqlalchemy import create_engine, text

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Confirmations explicites reçues côté utilisateur pour des articles dont
# l'historique de mouvements est mélangé (donc non résolubles tout seuls
# par la logique automatique plus bas) — appliquées en priorité et sans
# condition, avant le classement par signal. Nom comparé en minuscules
# (insensible à la casse). Retirer une ligne une fois l'article vraiment
# stable si cette liste devient inutile.
_OVERRIDES_CONFIRMES = {
    "aloe": "PISCINE",
}


def main() -> None:
    if not DATABASE_URL:
        print("DATABASE_URL absent — rien à faire (environnement de build sans base ?).")
        return
    if not DATABASE_URL.startswith("postgresql"):
        print(f"DATABASE_URL non-Postgres ({DATABASE_URL.split(':')[0]}) — script sans objet, on ignore.")
        return

    engine = create_engine(DATABASE_URL)
    with engine.begin() as conn:
        for nom_lower, lieu_confirme in _OVERRIDES_CONFIRMES.items():
            res = conn.execute(text(
                "UPDATE bar_produits SET lieu = :lieu "
                "WHERE lower(nom) = :nom AND (lieu IS DISTINCT FROM :lieu)"
            ), {"lieu": lieu_confirme, "nom": nom_lower})
            if res.rowcount:
                print(f"classer_produits_sans_lieu : override confirmé — « {nom_lower} » -> {lieu_confirme} ({res.rowcount} ligne(s)).")

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

        classes, a_defaut = [], []
        for produit_id, nom, n_devant, n_piscine in rows:
            if n_devant > 0 and not n_piscine:
                conn.execute(text("UPDATE bar_produits SET lieu = 'DEVANT' WHERE id = :id"), {"id": produit_id})
                classes.append(f"{nom} -> DEVANT (signal)")
            elif n_piscine > 0 and not n_devant:
                conn.execute(text("UPDATE bar_produits SET lieu = 'PISCINE' WHERE id = :id"), {"id": produit_id})
                classes.append(f"{nom} -> PISCINE (signal)")
            else:
                # Ni signal exploitable, ni override confirmé : plutôt que de
                # laisser l'article visible aux deux bars indéfiniment (ce qui
                # entretient le symptôme "mélangé" tant que personne ne le
                # reclasse à la main), on applique un défaut raisonnable —
                # Bar Piscine si le nom le mentionne explicitement, Bar Devant
                # (bar principal) sinon. Réversible en un clic (Produits Bar →
                # Modifier) si le défaut ne convient pas pour un article donné.
                defaut = "PISCINE" if "piscine" in nom.lower() else "DEVANT"
                conn.execute(text("UPDATE bar_produits SET lieu = :lieu WHERE id = :id"), {"lieu": defaut, "id": produit_id})
                a_defaut.append(f"{nom} -> {defaut} (défaut, à vérifier)")

        print(f"classer_produits_sans_lieu : {len(classes)} classé(s) par signal, {len(a_defaut)} classé(s) par défaut.")
        for ligne in classes + a_defaut:
            print(f"  - {ligne}")


if __name__ == "__main__":
    main()
