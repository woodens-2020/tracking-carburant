"""Autres entrées du Rapport journalier : argent en plus des ventes de carburant
(montant, description, note), ajouté par la direction avec le « + » du rapport.
Compté dans TOTAL ENTRÉES et le DÉPÔT, imprimé sous « Autres Entrées »."""
from __future__ import annotations

from datetime import date as date_type
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import AutreEntreeRapport
from pompiste_routes import _require_superviseur

router = APIRouter(prefix="/api/carburant/autres-entrees", tags=["autres-entrees"])


class AutreEntreeIn(BaseModel):
    date: date_type
    montant: float
    description: str
    note: Optional[str] = None
    activite: Optional[str] = None


class AutreEntreeModif(BaseModel):
    montant: Optional[float] = None
    description: Optional[str] = None
    note: Optional[str] = None


def entree_dict(x: AutreEntreeRapport) -> dict:
    return {"id": x.id, "date": str(x.date), "montant": float(x.montant), "description": x.description,
            "note": x.note or "", "activite": x.activite}


def _propre(txt: Optional[str], n: int) -> str:
    return " ".join((txt or "").split())[:n]


def _activite(a: Optional[str]) -> Optional[str]:
    return "propane" if (a or "").strip().lower() == "propane" else None


def autres_entrees_du_jour(db: Session, d: date_type, activite: Optional[str]) -> list[dict]:
    q = db.query(AutreEntreeRapport).filter(AutreEntreeRapport.date == d)
    a = (activite or "").strip().lower()
    if a == "propane":
        q = q.filter(AutreEntreeRapport.activite == "propane")
    elif a:                                   # carburant seulement
        q = q.filter(AutreEntreeRapport.activite.is_(None))
    return [entree_dict(x) for x in q.order_by(AutreEntreeRapport.id).all()]


@router.post("", status_code=201)
def ajouter(data: AutreEntreeIn, request: Request, db: Session = Depends(get_db)):
    u = _require_superviseur(request, db)
    desc = _propre(data.description, 255)
    if len(desc) < 2:
        raise HTTPException(400, "Indiquez la description.")
    if not data.montant or data.montant <= 0:
        raise HTTPException(400, "Le montant doit être supérieur à 0.")
    x = AutreEntreeRapport(date=data.date, montant=round(float(data.montant), 2), description=desc,
                           note=_propre(data.note, 500) or None, activite=_activite(data.activite),
                           cree_par_id=getattr(u, "id", None))
    db.add(x)
    db.commit()
    db.refresh(x)
    return entree_dict(x)


@router.put("/{eid}")
def modifier(eid: int, data: AutreEntreeModif, request: Request, db: Session = Depends(get_db)):
    _require_superviseur(request, db)
    x = db.get(AutreEntreeRapport, eid)
    if not x:
        raise HTTPException(404, "Entrée introuvable.")
    if data.description is not None:
        desc = _propre(data.description, 255)
        if len(desc) < 2:
            raise HTTPException(400, "Indiquez la description.")
        x.description = desc
    if data.montant is not None:
        if data.montant <= 0:
            raise HTTPException(400, "Le montant doit être supérieur à 0.")
        x.montant = round(float(data.montant), 2)
    if data.note is not None:
        x.note = _propre(data.note, 500) or None
    db.commit()
    db.refresh(x)
    return entree_dict(x)


@router.delete("/{eid}")
def supprimer(eid: int, request: Request, db: Session = Depends(get_db)):
    import main
    _require_superviseur(request, db)
    x = db.get(AutreEntreeRapport, eid)
    if not x:
        raise HTTPException(404, "Entrée introuvable.")
    main._tracer(db, request, "autre_entree_supprimee", entree_dict(x))
    db.delete(x)
    db.commit()
    return {"ok": True}
