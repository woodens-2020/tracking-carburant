"""
Outil ponctuel : annule TOUS les classements automatiques par signal
d'historique faits par les anciennes versions de classer_produits_sans_lieu.py
(avant que la logique ne soit retirée) -- remet `lieu` à NULL pour ces
produits, qui redeviennent vendables aux DEUX bars (quantité calculée
séparément par bar à partir des mouvements lieu-tagués, comme d'habitude).

Erreur reconnue : "n'a eu de mouvements que dans un bar jusqu'ici" n'est
PAS une preuve d'exclusivité (voir Jumex -- classé Devant alors qu'il
doit rester vendable aux deux). Cette meme erreur touchait potentiellement
tous les produits ci-dessous (Coca, Heineken, Sprite, Rhum, etc.) --
NULL (visible aux deux, sans risque) redevient leur etat normal, comme
pour tout produit dont personne n'a explicitement confirme l'exclusivite.

Ne touche PAS aux overrides explicitement confirmes par l'utilisateur
("Aloe" -> PISCINE) ni a "Carte Piscine" (seul cas ou le nom lui-meme
etait la preuve). Script ponctuel, a retirer du preDeployCommand apres
usage.
"""
import os

from sqlalchemy import create_engine, text

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Noms exacts classés par signal d'historique (pas par override confirmé,
# pas "Carte Piscine") lors des déploiements précédents de cette session.
_A_ANNULER = [
    "Prestige", "Prorade", "Rhum 3 Etoiles", "Rhum 5 Etoiles", "Robusto",
    "Shake", "Shot GreyGouce", "Sila", "Sprite", "Toro",
    "__TEST_APPRO_LIEU_DELETE_ME__",
    "360", "Atomik", "Champagne", "Coca", "Eau", "Extrait-de mal", "Fanta",
    "Gros Officier", "Gros vin", "Guinness", "Heineken", "J.P, Chenet",
    "Jus petit", "Kinanm", "Magic", "Malta H", "Vin Champion",
    "Generade", "Gatorade", "Jumex",
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
                print(f"revert_classement_signal : « {nom} » remis à NULL (vendable aux deux bars).")

        print(f"revert_classement_signal : {total} produit(s) remis à NULL au total.")


if __name__ == "__main__":
    main()
