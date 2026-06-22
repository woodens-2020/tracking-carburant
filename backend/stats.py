"""
Agregation des statistiques de ventes depuis la base.
Sert de source de verite unique : tous les chiffres viennent d'ici, jamais
d'une estimation. Utilise par l'endpoint /api/stats et par le chatbot.
"""
from datetime import date as date_type
from typing import Optional

from sqlalchemy.orm import Session
from models import Produit, Pompe, Releve


def compute_stats(db: Session, date_debut: date_type, date_fin: date_type,
                  produit_id: Optional[int] = None,
                  periode: Optional[str] = None,
                  pompe_id: Optional[int] = None) -> dict:
    """
    Calcule les totaux de ventes sur l'intervalle [date_debut, date_fin] inclus.
    Filtres optionnels : produit_id et periode ("Matin" / "Apres-midi").
    Tout est calcule depuis la base, jamais estime.
    """
    q = db.query(Releve).filter(
        Releve.date >= date_debut, Releve.date <= date_fin
    )
    if periode:
        q = q.filter(Releve.periode == periode)
    releves = q.all()

    if produit_id:
        releves = [r for r in releves if r.pompe.produit_id == produit_id]
    if pompe_id:
        releves = [r for r in releves if r.pompe_id == pompe_id]

    total_quantite = 0.0
    total_montant = 0.0
    par_produit = {}   # produit_nom -> {quantite, montant}
    par_pompe = {}     # pompe_nom   -> {produit, quantite, montant}
    par_periode = {"Matin": {"quantite": 0.0, "montant": 0.0},
                   "Apres-midi": {"quantite": 0.0, "montant": 0.0}}
    jours = set()

    for r in releves:
        q_gal = r.quantite
        montant = r.montant_vente
        total_quantite += q_gal
        total_montant += montant
        jours.add(str(r.date))

        pnom = r.pompe.produit.nom
        d = par_produit.setdefault(pnom, {"quantite": 0.0, "montant": 0.0})
        d["quantite"] += q_gal
        d["montant"] += montant

        pompe_nom = r.pompe.nom
        dp = par_pompe.setdefault(pompe_nom, {"produit": pnom, "quantite": 0.0, "montant": 0.0})
        dp["quantite"] += q_gal
        dp["montant"] += montant

        if r.periode in par_periode:
            par_periode[r.periode]["quantite"] += q_gal
            par_periode[r.periode]["montant"] += montant

    def rnd(d):
        # Bug 5 fix : quantite arrondie à 3 décimales (gal), montant à 2 (G)
        # Évite que sum(par_produit.quantite) ≠ total_quantite par troncature précoce.
        result = {}
        for k, v in d.items():
            if not isinstance(v, float):
                result[k] = v
            elif k == "quantite":
                result[k] = round(v, 3)
            else:
                result[k] = round(v, 2)
        return result

    return {
        "date_debut": str(date_debut),
        "date_fin": str(date_fin),
        "filtre_produit_id": produit_id,
        "filtre_periode": periode,
        "nb_jours_couverts": len(jours),
        "nb_releves": len(releves),
        "total_quantite": round(total_quantite, 3),
        "total_montant": round(total_montant, 2),
        "par_produit": {k: rnd(v) for k, v in par_produit.items()},
        "par_pompe": {k: rnd(v) for k, v in par_pompe.items()},
        "par_periode": {k: rnd(v) for k, v in par_periode.items()},
    }


def liste_produits_pompes(db: Session) -> dict:
    """Renvoie la liste des produits et pompes pour contexte du chatbot."""
    out = {}
    for p in db.query(Produit).all():
        out[p.nom] = {"id": p.id, "pompes": [q.nom for q in p.pompes]}
    return out
