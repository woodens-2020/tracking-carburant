"""
Applique UNIQUEMENT les classifications de bar explicitement confirmées
par l'utilisateur (_OVERRIDES_CONFIRMES) — jamais de déduction automatique
à partir de l'historique de mouvements.

Principe (clarifié après une erreur concrète sur "Jumex") : le fait qu'un
produit n'ait eu jusqu'ici des mouvements que dans UN SEUL bar ne prouve
PAS qu'il lui est exclusif — ça peut juste vouloir dire que l'autre bar
n'a pas encore reçu de stock pour cet article. NULL (visible aux deux
bars, quantité par bar calculée séparément à partir des mouvements
lieu-tagués) est l'état NORMAL et PERMANENT pour la grande majorité des
produits, pas un état temporaire "à résoudre". Seul un produit qu'un
humain confirme explicitement comme exclusif à un bar doit devenir
non-NULL.

Une version précédente de ce script devinait via le signal d'historique
(ne pointe que vers un seul bar => classé sur ce bar) — ça a produit deux
erreurs concrètes : "Corona"/"Poisson" etc. (retirés à tort de Piscine)
et "Jumex" (rendu exclusif à Devant alors qu'il doit rester vendable aux
deux, avec un stock à 0 côté Piscine tant que rien n'y est reçu). Cette
logique est retirée définitivement.

Idempotent et sans risque : relancé une fois tous les overrides
confirmés appliqués, il n'a plus rien à faire.
"""
import os

from sqlalchemy import create_engine, text

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Confirmations explicites reçues côté utilisateur — SEULE source de
# classification automatique. Nom comparé en minuscules (insensible à la
# casse). Ajouter une ligne uniquement quand l'utilisateur a lui-même
# confirmé qu'un produit est exclusif à un bar.
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
        total = 0
        for nom_lower, lieu_confirme in _OVERRIDES_CONFIRMES.items():
            res = conn.execute(text(
                "UPDATE bar_produits SET lieu = :lieu "
                "WHERE lower(nom) = :nom AND (lieu IS DISTINCT FROM :lieu)"
            ), {"lieu": lieu_confirme, "nom": nom_lower})
            if res.rowcount:
                total += res.rowcount
                print(f"classer_produits_sans_lieu : override confirmé — « {nom_lower} » -> {lieu_confirme} ({res.rowcount} ligne(s)).")
        if not total:
            print("classer_produits_sans_lieu : rien à faire (overrides déjà appliqués).")


if __name__ == "__main__":
    main()
