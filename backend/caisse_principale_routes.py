"""
Caisse principale — préfixe /api/caisse-principale

Les sources (stations Zo Vincent, Champin, autres…) apportent de l'argent à
la caisse principale. Chaque entrée = une source, un montant, une date ; le
total des entrées s'affiche par source, par jour et pour la période.
Les sources se configurent (ajouter, renommer, désactiver). Une entrée n'est
jamais supprimée : une erreur s'annule avec un motif, la trace reste.

Accès : administrateurs et direction (même règle que la vérification des
rapports pompistes : pompiste_routes.peut_superviser).
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date as date_type
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import EntreeCaisse, SourceEntree, Utilisateur
from pompiste_routes import _require_superviseur
from tz_utils import now_haiti, today_haiti

router = APIRouter(prefix="/api/caisse-principale", tags=["caisse-principale"])

SOURCES_PAR_DEFAUT = ("Zo Vincent", "Champin", "Autres")


def _norm(txt: str) -> str:
    t = unicodedata.normalize("NFKD", txt or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", t)).strip()


def amorcer_sources(db: Session) -> None:
    """Premier démarrage : sources par défaut (rien si la table a déjà des lignes)."""
    if db.query(SourceEntree).count():
        return
    for i, nom in enumerate(SOURCES_PAR_DEFAUT):
        db.add(SourceEntree(nom=nom, nom_norm=_norm(nom), ordre=i, actif=True))
    db.commit()


def _nom(u: Optional[Utilisateur]) -> Optional[str]:
    return (u.nom_complet or u.username) if u else None


def _source_dict(s: SourceEntree, nb: int = 0) -> dict:
    return {"id": s.id, "nom": s.nom, "actif": bool(s.actif), "ordre": s.ordre, "nb_entrees": nb}


def _entree_dict(e: EntreeCaisse) -> dict:
    return {
        "id": e.id, "source_id": e.source_id, "source_nom": e.source.nom if e.source else None,
        "montant": float(e.montant), "date": str(e.date_entree),
        "reference": e.reference, "note": e.note,
        "saisi_par_nom": _nom(e.saisi_par),
        "saisi_le": e.created_at.isoformat() if e.created_at else None,
        "annulee": bool(e.annulee), "motif_annulation": e.motif_annulation,
        "annulee_par_nom": _nom(e.annulee_par),
        "annulee_le": e.annulee_at.isoformat() if e.annulee_at else None,
    }


# ── Sources ─────────────────────────────────────────────────────────────

@router.get("/sources")
def lister_sources(inclure_inactives: bool = True, _s: Utilisateur = Depends(_require_superviseur),
                   db: Session = Depends(get_db)):
    amorcer_sources(db)
    q = db.query(SourceEntree)
    if not inclure_inactives:
        q = q.filter(SourceEntree.actif.is_(True))
    out = []
    for s in q.order_by(SourceEntree.ordre, SourceEntree.nom).all():
        nb = db.query(EntreeCaisse).filter_by(source_id=s.id, annulee=False).count()
        out.append(_source_dict(s, nb))
    return out


class SourceIn(BaseModel):
    nom: str


class SourcePatch(BaseModel):
    nom: Optional[str] = None
    actif: Optional[bool] = None


def _verifier_nom(db: Session, nom: str, sauf_id: Optional[int] = None) -> str:
    nom = (nom or "").strip()
    if len(nom) < 2:
        raise HTTPException(400, "Le nom de la source doit faire au moins 2 caractères.")
    if len(nom) > 120:
        raise HTTPException(400, "Nom trop long (120 caractères au plus).")
    autre = db.query(SourceEntree).filter(SourceEntree.nom_norm == _norm(nom)).first()
    if autre and autre.id != sauf_id:
        raise HTTPException(409, f"La source « {autre.nom} » existe déjà"
                                 + ("" if autre.actif else " (désactivée : réactivez-la)") + ".")
    return nom


@router.post("/sources", status_code=201)
def creer_source(data: SourceIn, _s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    amorcer_sources(db)
    nom = _verifier_nom(db, data.nom)
    ordre = (db.query(SourceEntree).count() or 0)
    s = SourceEntree(nom=nom, nom_norm=_norm(nom), ordre=ordre, actif=True)
    db.add(s)
    db.commit()
    db.refresh(s)
    return _source_dict(s)


@router.patch("/sources/{sid}")
def modifier_source(sid: int, data: SourcePatch, _s: Utilisateur = Depends(_require_superviseur),
                    db: Session = Depends(get_db)):
    s = db.get(SourceEntree, sid)
    if not s:
        raise HTTPException(404, "Source introuvable")
    if data.nom is not None:
        s.nom = _verifier_nom(db, data.nom, sauf_id=s.id)
        s.nom_norm = _norm(s.nom)
    if data.actif is not None:
        s.actif = data.actif
    db.commit()
    db.refresh(s)
    return _source_dict(s, db.query(EntreeCaisse).filter_by(source_id=s.id, annulee=False).count())


# ── Entrées ─────────────────────────────────────────────────────────────

class EntreeIn(BaseModel):
    source_id: int
    montant: float
    date_entree: date_type
    reference: Optional[str] = None
    note: Optional[str] = None


@router.post("/entrees", status_code=201)
def creer_entree(data: EntreeIn, s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    src = db.get(SourceEntree, data.source_id)
    if not src:
        raise HTTPException(404, "Source introuvable")
    if not src.actif:
        raise HTTPException(409, f"La source « {src.nom} » est désactivée.")
    if not (data.montant > 0):
        raise HTTPException(400, "Le montant doit être supérieur à 0.")
    if data.montant > 1_000_000_000:
        raise HTTPException(400, "Montant anormal.")
    if data.date_entree > today_haiti():
        raise HTTPException(400, "Une entrée ne peut pas être datée dans le futur.")
    e = EntreeCaisse(source_id=src.id, montant=round(data.montant, 2), date_entree=data.date_entree,
                     reference=(data.reference or "").strip()[:120] or None,
                     note=(data.note or "").strip()[:500] or None, saisi_par_id=s.id)
    db.add(e)
    db.commit()
    db.refresh(e)
    return _entree_dict(e)


class AnnulerIn(BaseModel):
    motif: str


@router.post("/entrees/{eid}/annuler")
def annuler_entree(eid: int, data: AnnulerIn, s: Utilisateur = Depends(_require_superviseur),
                   db: Session = Depends(get_db)):
    e = db.get(EntreeCaisse, eid)
    if not e:
        raise HTTPException(404, "Entrée introuvable")
    if e.annulee:
        raise HTTPException(409, "Cette entrée est déjà annulée.")
    motif = (data.motif or "").strip()
    if len(motif) < 5:
        raise HTTPException(400, "Indiquez le motif de l'annulation.")
    e.annulee, e.annulee_par_id, e.annulee_at, e.motif_annulation = True, s.id, now_haiti(), motif[:300]
    db.commit()
    db.refresh(e)
    return _entree_dict(e)


@router.get("/entrees")
def lister_entrees(date_debut: Optional[date_type] = None, date_fin: Optional[date_type] = None,
                   source_id: Optional[int] = None, inclure_annulees: bool = False,
                   _s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    """Entrées de la période + totaux (par source, par jour, général).
    Les totaux ne comptent jamais les entrées annulées."""
    amorcer_sources(db)
    fin = date_fin or today_haiti()
    debut = date_debut or fin.replace(day=1)
    if debut > fin:
        raise HTTPException(400, "La date de début est après la date de fin.")
    q = db.query(EntreeCaisse).filter(EntreeCaisse.date_entree >= debut, EntreeCaisse.date_entree <= fin)
    if source_id:
        q = q.filter(EntreeCaisse.source_id == source_id)
    rows = q.order_by(EntreeCaisse.date_entree.desc(), EntreeCaisse.id.desc()).all()
    valides = [e for e in rows if not e.annulee]
    par_source: dict = {}
    for src in db.query(SourceEntree).order_by(SourceEntree.ordre, SourceEntree.nom).all():
        if source_id and src.id != source_id:
            continue
        par_source[src.id] = {"source_id": src.id, "source_nom": src.nom, "actif": bool(src.actif),
                              "montant": 0.0, "nb": 0}
    par_jour: dict = {}
    for e in valides:
        d = par_source.setdefault(e.source_id, {"source_id": e.source_id, "source_nom": e.source.nom,
                                                "actif": bool(e.source.actif), "montant": 0.0, "nb": 0})
        d["montant"] += float(e.montant)
        d["nb"] += 1
        j = par_jour.setdefault(str(e.date_entree), {"date": str(e.date_entree), "montant": 0.0, "nb": 0})
        j["montant"] += float(e.montant)
        j["nb"] += 1
    sources = [dict(v, montant=round(v["montant"], 2)) for v in par_source.values()
               if v["actif"] or v["nb"]]
    return {
        "date_debut": str(debut), "date_fin": str(fin),
        "total": round(sum(float(e.montant) for e in valides), 2),
        "nb": len(valides),
        "par_source": sources,
        "par_jour": sorted((dict(v, montant=round(v["montant"], 2)) for v in par_jour.values()),
                           key=lambda x: x["date"], reverse=True),
        "entrees": [_entree_dict(e) for e in rows if inclure_annulees or not e.annulee],
        "nb_annulees": sum(1 for e in rows if e.annulee),
    }
