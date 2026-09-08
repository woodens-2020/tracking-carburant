"""Endpoints des listes de référence partagées — préfixe /api/listes.

  GET   /api/listes/{cle}            liste (auth) ; ?tous=true inclut les inactifs
  POST  /api/listes/{cle}            ajoute une valeur (manager / admin), dédupliquée
  PATCH /api/listes/{cle}/{id}       renomme / (dés)active (manager / admin)

`cle` ∈ {"categories-depense", "postes"}.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import CategorieDepense, Poste
import listes_reference as lref

router = APIRouter(prefix="/api/listes", tags=["Listes de référence"])

_LISTES = {
    "categories-depense": (CategorieDepense, "catégorie de dépense"),
    "postes":             (Poste,            "poste"),
}


def _resoudre(cle: str):
    if cle not in _LISTES:
        raise HTTPException(404, "Liste de référence inconnue.")
    return _LISTES[cle]


class _NomIn(BaseModel):
    nom: str


class _MajIn(BaseModel):
    nom:   Optional[str]  = None
    actif: Optional[bool] = None


@router.get("/{cle}")
def lister(cle: str, tous: bool = False, db: Session = Depends(get_db)):
    modele, _ = _resoudre(cle)
    return [
        {"id": e.id, "nom": e.nom, "actif": e.actif}
        for e in lref.lister(db, modele, inclure_inactifs=tous)
    ]


@router.post("/{cle}", status_code=201)
def creer(cle: str, data: _NomIn, request: Request, db: Session = Depends(get_db)):
    modele, label = _resoudre(cle)
    user = lref.utilisateur_courant(request, db)
    if not lref.peut_gerer(user):
        raise HTTPException(403, "Seul un manager ou un administrateur peut ajouter une valeur.")
    nom = (data.nom or "").strip()
    if not nom:
        raise HTTPException(400, "Le nom est requis.")
    norm = lref.normaliser(nom)
    existante = db.query(modele).filter(modele.nom_norm == norm).first()
    if existante:
        # Déduplication : on ne recrée jamais, on réactive si besoin.
        if not existante.actif:
            existante.actif = True
            db.commit()
        return {"id": existante.id, "nom": existante.nom, "actif": existante.actif, "cree": False}
    mx = lref._maxlen(modele)
    e = modele(nom=nom[:mx], nom_norm=norm[:mx], actif=True,
               cree_par_id=user.id if user else None)
    db.add(e)
    db.commit()
    db.refresh(e)
    return {"id": e.id, "nom": e.nom, "actif": e.actif, "cree": True}


@router.patch("/{cle}/{ref_id}")
def maj(cle: str, ref_id: int, data: _MajIn, request: Request, db: Session = Depends(get_db)):
    modele, _ = _resoudre(cle)
    user = lref.utilisateur_courant(request, db)
    if not lref.peut_gerer(user):
        raise HTTPException(403, "Seul un manager ou un administrateur peut modifier une valeur.")
    e = db.get(modele, ref_id)
    if not e:
        raise HTTPException(404, "Valeur introuvable.")
    if data.nom is not None:
        nom = data.nom.strip()
        if not nom:
            raise HTTPException(400, "Le nom ne peut pas être vide.")
        norm = lref.normaliser(nom)
        conflit = db.query(modele).filter(modele.nom_norm == norm, modele.id != ref_id).first()
        if conflit:
            raise HTTPException(409, f"« {nom} » existe déjà.")
        mx = lref._maxlen(modele)
        e.nom, e.nom_norm = nom[:mx], norm[:mx]
    if data.actif is not None:
        e.actif = data.actif
    db.commit()
    return {"id": e.id, "nom": e.nom, "actif": e.actif}
