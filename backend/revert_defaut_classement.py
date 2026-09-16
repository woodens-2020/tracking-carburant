"""
Annule le classement PAR DÉFAUT appliqué par classer_produits_sans_lieu.py
(commit "Classe tous les produits restants par défaut") : remet `lieu` à
NULL (visible aux deux bars, comme avant) pour tous les produits qui
avaient été forcés sur un bar sans aucun signal d'historique réel.

Erreur reconnue : ce défaut a retiré de vrais produits (Corona, les plats
Poisson, etc.) du catalogue Bar Piscine alors qu'ils y étaient réellement
vendus — NULL (visible aux deux) est un état sans risque, un mauvais
classement forcé ne l'est pas. Les classements basés sur un vrai signal
d'historique (Generade, et la vague précédente : Rhum, Coca, 360, etc.)
et les overrides confirmés (Aloe) ne sont PAS touchés par ce script —
seuls les 34 produits marqués "(défaut, à vérifier)" dans les logs du
déploiement précédent sont concernés.

Script ponctuel, à usage unique. Idempotent (si rejoué, ne trouve plus
rien à annuler puisque les produits ne seront plus marqués comme
provenant du défaut).
"""
import os

from sqlalchemy import create_engine, text

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Noms exacts (issus des logs du déploiement dc84d7ac) classés par défaut,
# à remettre à NULL. "Carte Piscine" (le seul défaut correct, nom explicite)
# n'est PAS dans cette liste — on le laisse tel quel.
_A_ANNULER = [
    "7UP", "Active", "Aloee", "Atomicc", "Benedicta", "Boppi", "Boulet",
    "Cereser", "Chevalier", "Corona", "Cyclone", "Gold", "Grande mania",
    "Gros Cola Couronne", "Gros Pingless", "Jumex", "Lambi 250", "Miks",
    "Milks", "Pingless Petit", "Poisson - 2000 Gourdes", "Poisson 300$",
    "Poisson 450 $", "Short Grand Manier", "Short Remy Martin",
    "Shot Grey Gcos", "Sigar", "Tas Doudou", "Ti couronne", "Ti Officier",
    "Ti plat", "Vita Lift", "__TEST_LIEU_DELETE_ME__",
]


def main() -> None:
    if not DATABASE_URL:
        print("DATABASE_URL absent — rien à faire.")
        return
    if not DATABASE_URL.startswith("postgresql"):
        print(f"DATABASE_URL non-Postgres ({DATABASE_URL.split(':')[0]}) — ignoré.")
        return

    engine = create_engine(DATABASE_URL)
    with engine.begin() as conn:
        total = 0
        for nom in _A_ANNULER:
            res = conn.execute(text(
                "UPDATE bar_produits SET lieu = NULL WHERE nom = :nom AND lieu IS NOT NULL"
            ), {"nom": nom})
            if res.rowcount:
                total += res.rowcount
                print(f"revert_defaut_classement : « {nom} » remis à NULL (visible aux deux bars).")

        print(f"revert_defaut_classement : {total} produit(s) remis à NULL au total.")


if __name__ == "__main__":
    main()
