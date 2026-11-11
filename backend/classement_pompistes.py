"""Classement des pompistes.

Pour chaque pompiste (fiche employé) sur une période : gallons vendus par
carburant (jamais additionnés entre gazoline, diesel et propane), montant des
ventes, dépenses qu'il a faites et ce qui reste après ses dépenses ; les
pompistes sont classés par montant vendu.

Les ventes viennent des relevés du journal attribués au pompiste. Les
dépenses sont celles qui lui sont attribuées (dépense saisie pour lui, ou
dépense payée sur un de ses rapports pompiste).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date as date_type
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from models import Depense, Employe, RapportPompiste, Releve

router = APIRouter()
MAX_JOURS = 366
NON_ATTRIBUE = "Non attribué"


def _nom(e: Optional[Employe]) -> str:
    return f"{e.prenom or ''} {e.nom or ''}".strip() if e else NON_ATTRIBUE


def classement(db: Session, d0: date_type, d1: date_type, periode: Optional[str] = None) -> dict:
    import main
    from models import Utilisateur
    lignes: dict = {}

    def ligne(eid):
        if eid not in lignes:
            e = db.get(Employe, eid) if eid else None
            lignes[eid] = {"pompiste_id": eid, "pompiste": _nom(e), "poste": e.poste if e else None,
                           "par_carburant": defaultdict(lambda: {"gallons": 0.0, "montant": 0.0, "releves": 0}),
                           "montant": 0.0, "releves": 0, "creneaux": set(), "jours": set(),
                           "depenses": 0.0, "nb_depenses": 0, "liste_depenses": [], "detail": []}
        return lignes[eid]

    totaux = defaultdict(lambda: {"gallons": 0.0, "montant": 0.0})
    # Ventes officielles seulement : même règle que tous les totaux (sauts non
    # confirmés exclus, voir main.releves_comptes)
    q_rel = db.query(Releve).filter(Releve.date >= d0, Releve.date <= d1)
    if periode:
        q_rel = q_rel.filter(Releve.periode == periode)
    for r in main.releves_comptes(db, q_rel.all()):
        g = max(0.0, float(r.metter_apres) - float(r.metter_avant))
        mt = g * float(r.prix_gallon or 0)
        fam = main._famille_carburant(r.pompe.produit.nom if r.pompe and r.pompe.produit else "") or "Autre"
        x = ligne(r.pompiste_id)
        c = x["par_carburant"][fam]
        c["gallons"] += g; c["montant"] += mt; c["releves"] += 1
        x["montant"] += mt; x["releves"] += 1
        x["creneaux"].add((r.date, r.periode)); x["jours"].add(r.date)
        x["detail"].append({"date": str(r.date), "periode": r.periode, "famille": fam,
                            "meeter": (f"{r.pompe.produit.nom} · {r.pompe.nom}" if r.pompe and r.pompe.produit
                                       else (r.pompe.nom if r.pompe else "")),
                            "avant": float(r.metter_avant), "apres": float(r.metter_apres),
                            "gallons": round(g, 3), "montant": round(mt, 2)})
        totaux[fam]["gallons"] += g; totaux[fam]["montant"] += mt
    # Dépenses : attribuées au pompiste, sinon au pompiste du rapport qui les a payées
    emp_du_compte = {}
    for d in db.query(Depense).filter(Depense.date_depense >= d0, Depense.date_depense <= d1).all():
        if periode:                       # une vacation : seules les dépenses de ses rapports
            rap_v = db.get(RapportPompiste, d.rapport_pompiste_id) if d.rapport_pompiste_id else None
            if not rap_v or rap_v.periode != periode:
                continue
        eid = d.pompiste_id
        if eid is None and d.rapport_pompiste_id:
            rap = db.get(RapportPompiste, d.rapport_pompiste_id)
            if rap and rap.utilisateur_id:
                if rap.utilisateur_id not in emp_du_compte:
                    e = db.query(Employe.id).filter(Employe.utilisateur_id == rap.utilisateur_id).first()
                    emp_du_compte[rap.utilisateur_id] = e[0] if e else None
                eid = emp_du_compte[rap.utilisateur_id]
        if eid is None:
            continue                      # dépense de la station, pas d'un pompiste
        x = ligne(eid)
        x["depenses"] += float(d.montant); x["nb_depenses"] += 1
        x["liste_depenses"].append({"id": d.id, "date": str(d.date_depense), "description": d.description,
                                    "categorie": d.categorie, "montant": round(float(d.montant), 2)})
    total_ventes = sum(x["montant"] for x in lignes.values())
    out = []
    for x in lignes.values():
        out.append({
            "pompiste_id": x["pompiste_id"], "pompiste": x["pompiste"], "poste": x["poste"],
            "par_carburant": {f: {"gallons": round(c["gallons"], 3), "montant": round(c["montant"], 2),
                                  "releves": c["releves"]} for f, c in sorted(x["par_carburant"].items())},
            "montant": round(x["montant"], 2), "releves": x["releves"],
            "creneaux": len(x["creneaux"]), "jours": len(x["jours"]),
            "part": round(100 * x["montant"] / total_ventes, 1) if total_ventes else 0.0,
            "depenses": round(x["depenses"], 2), "nb_depenses": x["nb_depenses"],
            "liste_depenses": sorted(x["liste_depenses"], key=lambda y: y["date"]),
            "releves_detail": sorted(x["detail"], key=lambda y: (y["date"], y["periode"] != "Matin", y["meeter"])),
            "net": round(x["montant"] - x["depenses"], 2),
            "moyenne_creneau": round(x["montant"] / len(x["creneaux"]), 2) if x["creneaux"] else 0.0,
        })
    # Classés par montant vendu ; « Non attribué » toujours à la fin, sans rang
    out.sort(key=lambda y: (y["pompiste_id"] is None, -y["montant"], y["pompiste"]))
    rang = 0
    for y in out:
        if y["pompiste_id"] is not None:
            rang += 1
            y["rang"] = rang
        else:
            y["rang"] = None
    return {"date_debut": str(d0), "date_fin": str(d1),
            "totaux": {f: {"gallons": round(v["gallons"], 3), "montant": round(v["montant"], 2)}
                       for f, v in sorted(totaux.items())},
            "total_ventes": round(total_ventes, 2),
            "total_depenses": round(sum(y["depenses"] for y in out), 2),
            "pompistes": out}


@router.get("/api/pompistes/classement")
def classement_pompistes(date_debut: date_type, date_fin: date_type, periode: Optional[str] = None,
                         db: Session = Depends(get_db)):
    if date_fin < date_debut:
        raise HTTPException(400, "La date de fin est avant la date de début.")
    if (date_fin - date_debut).days > MAX_JOURS:
        raise HTTPException(400, f"Période trop longue ({MAX_JOURS} jours au plus).")
    if periode and periode not in ("Matin", "Apres-midi"):
        raise HTTPException(400, "Période invalide")
    return classement(db, date_debut, date_fin, periode or None)
