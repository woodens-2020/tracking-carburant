"""
Routes API Module Crédits — préfixe /api/credits

Dettes (l'entreprise doit à un tiers) et créances (un tiers doit à
l'entreprise), avec leurs mouvements : DEPOT (versement) et AUGMENTATION.

Règles (voir uml-module-credits.md) :
  - total_du = montant_initial + Σ AUGMENTATION
  - reste    = total_du − Σ DEPOT  (calculé, jamais stocké)
  - un DEPOT ne dépasse jamais le reste et n'est jamais daté avant le crédit
  - statut SOLDE quand reste = 0, ACTIF sinon ; ANNULE seulement à la main
    (un crédit annulé sort de tous les totaux)
  - supprimer un crédit supprime ses mouvements
  - HTG et USD ne s'additionnent jamais : les totaux sont par devise

Accès : direction seulement (admin, PDG, manager, directeur, assistant
directeur). Toute suppression ou annulation demande un motif, gardé au
journal d'audit.
"""
from __future__ import annotations

import json
from datetime import date as date_type
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session, selectinload

from database import get_db
from models import AuditLog, Credit, CreditMouvement, Utilisateur
from tz_utils import today_haiti

router = APIRouter(prefix="/api/credits", tags=["credits"])

SENS     = ("DETTE", "CREANCE")
DEVISES  = ("HTG", "USD")
STATUTS  = ("ACTIF", "SOLDE", "ANNULE")
TYPES    = ("DEPOT", "AUGMENTATION")
MOTIF_MIN = 5
CENT = Decimal("0.01")

_ROLES_DIRECTION = ("manager", "directeur", "assistant directeur", "pdg", "direction")


# ── Droits ────────────────────────────────────────────────────────────────────

def peut_gerer_credits(u: Utilisateur) -> bool:
    if u.role in ("admin", "pdg"):
        return True
    r = u.role_obj
    if not r:
        return False
    perms = r.permissions or {}
    if r.est_admin or perms.get("admin"):
        return True
    nom = (r.nom or "").strip().lower()
    return nom in _ROLES_DIRECTION or nom.startswith("directeur")


def _direction(request: Request, db: Session = Depends(get_db)) -> Utilisateur:
    proxy = getattr(request.state, "user", None)
    if not proxy:
        raise HTTPException(401, "Non authentifié")
    u = db.get(Utilisateur, proxy.id)
    if not u or not u.actif:
        raise HTTPException(401, "Compte inactif")
    if not peut_gerer_credits(u):
        raise HTTPException(403, "Le module Crédits est réservé à la direction.")
    return u


# ── Schémas ───────────────────────────────────────────────────────────────────

class CreditIn(BaseModel):
    sens:            str
    tiers_nom:       str
    tiers_contact:   Optional[str] = None
    description:     Optional[str] = None
    montant_initial: Decimal
    devise:          str = "HTG"
    date_credit:     Optional[date_type] = None
    date_echeance:   Optional[date_type] = None
    notes:           Optional[str] = None


class CreditPatch(BaseModel):
    tiers_nom:       Optional[str] = None
    tiers_contact:   Optional[str] = None
    description:     Optional[str] = None
    montant_initial: Optional[Decimal] = None
    devise:          Optional[str] = None
    date_credit:     Optional[date_type] = None
    date_echeance:   Optional[date_type] = None
    effacer_echeance: bool = False
    notes:           Optional[str] = None


class MouvementIn(BaseModel):
    type:           str
    montant:        Decimal
    date_mouvement: Optional[date_type] = None
    mode_paiement:  Optional[str] = None
    reference:      Optional[str] = None
    note:           Optional[str] = None


class MotifIn(BaseModel):
    motif: str


# ── Utilitaires ───────────────────────────────────────────────────────────────

def _txt(v: Optional[str], n: int) -> Optional[str]:
    v = " ".join((v or "").split())
    return v[:n] or None


def _montant(v: Decimal, champ: str = "Le montant") -> Decimal:
    try:
        m = Decimal(v).quantize(CENT, rounding=ROUND_HALF_UP)
    except Exception:
        raise HTTPException(400, f"{champ} est invalide.")
    if m <= 0:
        raise HTTPException(400, f"{champ} doit être supérieur à 0.")
    if m >= Decimal("1000000000000"):
        raise HTTPException(400, f"{champ} est trop grand.")
    return m


def _motif(m: str) -> str:
    m = " ".join((m or "").split())
    if len(m) < MOTIF_MIN:
        raise HTTPException(400, f"Indiquez le motif ({MOTIF_MIN} caractères minimum).")
    return m[:300]


def _f(d: Decimal) -> float:
    return float(Decimal(d).quantize(CENT))


def _tracer(db: Session, request: Request, u: Utilisateur, action: str, details: dict) -> None:
    db.add(AuditLog(user_id=u.id, action=action[:50],
                    ip_address=(request.client.host if request.client else None),
                    details=json.dumps(details, ensure_ascii=False, default=str)))


def _get(db: Session, credit_id: int) -> Credit:
    c = (db.query(Credit).options(selectinload(Credit.mouvements))
         .filter(Credit.id == credit_id).first())
    if not c:
        raise HTTPException(404, "Crédit introuvable.")
    return c


def _nom(u: Optional[Utilisateur]) -> Optional[str]:
    return (u.nom_complet or u.username) if u else None


def _mvt_dict(m: CreditMouvement) -> dict:
    return {
        "id": m.id, "credit_id": m.credit_id, "type": m.type, "montant": _f(m.montant),
        "date_mouvement": str(m.date_mouvement), "mode_paiement": m.mode_paiement,
        "reference": m.reference, "note": m.note,
        "createur_nom": _nom(m.createur),
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


def _credit_dict(c: Credit, aujourdhui: date_type, avec_mouvements: bool = False) -> dict:
    total_du, depose = c.total_du(), c.total_depose()
    d = {
        "id": c.id, "sens": c.sens, "tiers_nom": c.tiers_nom, "tiers_contact": c.tiers_contact,
        "description": c.description, "devise": c.devise,
        "montant_initial": _f(c.montant_initial),
        "total_augmentations": _f(total_du - c.montant_initial),
        "total_du": _f(total_du), "total_depose": _f(depose), "reste": _f(total_du - depose),
        "date_credit": str(c.date_credit),
        "date_echeance": str(c.date_echeance) if c.date_echeance else None,
        "jours_retard": (aujourdhui - c.date_echeance).days if c.en_retard(aujourdhui) else 0,
        "en_retard": c.en_retard(aujourdhui),
        "statut": c.statut, "notes": c.notes,
        "nb_mouvements": len(c.mouvements),
        "dernier_mouvement": str(max(m.date_mouvement for m in c.mouvements)) if c.mouvements else None,
        "createur_nom": _nom(c.createur),
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }
    if avec_mouvements:
        d["mouvements"] = [_mvt_dict(m) for m in c.mouvements]
    return d


def _resume(credits: list[Credit], aujourdhui: date_type) -> dict:
    """Totaux par sens ET par devise (jamais d'addition HTG + USD), hors
    crédits annulés."""
    res = {s: {dv: {"nb": 0, "nb_actifs": 0, "nb_en_retard": 0, "total_du": Decimal("0"),
                    "total_depose": Decimal("0"), "reste": Decimal("0"), "reste_en_retard": Decimal("0")}
               for dv in DEVISES} for s in SENS}
    for c in credits:
        if c.statut == "ANNULE":
            continue
        b = res[c.sens][c.devise]
        du, dep = c.total_du(), c.total_depose()
        b["nb"] += 1
        b["nb_actifs"] += c.statut == "ACTIF"
        b["total_du"] += du
        b["total_depose"] += dep
        b["reste"] += du - dep
        if c.en_retard(aujourdhui):
            b["nb_en_retard"] += 1
            b["reste_en_retard"] += du - dep
    return {s: {dv: {k: (_f(v) if isinstance(v, Decimal) else v) for k, v in b.items()}
                for dv, b in par.items()} for s, par in res.items()}


# ── Lecture ───────────────────────────────────────────────────────────────────

@router.get("")
def lister_credits(
    sens: Optional[str] = None,
    statut: Optional[str] = None,          # ACTIF | SOLDE | ANNULE | EN_RETARD ; vide = tous sauf annulés
    devise: Optional[str] = None,
    q: Optional[str] = None,
    db: Session = Depends(get_db),
    _u: Utilisateur = Depends(_direction),
):
    aujourdhui = today_haiti()
    base = db.query(Credit).options(selectinload(Credit.mouvements), selectinload(Credit.createur))
    tous = base.all()
    query = base
    if sens:
        if sens not in SENS:
            raise HTTPException(400, "sens : DETTE ou CREANCE.")
        query = query.filter(Credit.sens == sens)
    if devise:
        if devise not in DEVISES:
            raise HTTPException(400, "devise : HTG ou USD.")
        query = query.filter(Credit.devise == devise)
    if statut == "EN_RETARD":
        query = query.filter(Credit.statut == "ACTIF", Credit.date_echeance.isnot(None),
                             Credit.date_echeance < aujourdhui)
    elif statut:
        if statut not in STATUTS:
            raise HTTPException(400, "statut : ACTIF, SOLDE, ANNULE ou EN_RETARD.")
        query = query.filter(Credit.statut == statut)
    else:
        query = query.filter(Credit.statut != "ANNULE")
    if q and q.strip():
        motif = f"%{q.strip()}%"
        query = query.filter(or_(Credit.tiers_nom.ilike(motif), Credit.tiers_contact.ilike(motif),
                                 Credit.description.ilike(motif)))
    rows = query.all()
    # En retard d'abord, puis échéance la plus proche, puis les plus récents
    rows.sort(key=lambda c: (c.statut != "ACTIF", not c.en_retard(aujourdhui),
                             c.date_echeance or date_type.max, -c.id))
    return {
        "aujourdhui": str(aujourdhui),
        "credits": [_credit_dict(c, aujourdhui) for c in rows],
        "resume": _resume(tous, aujourdhui),
    }


@router.get("/{credit_id}")
def detail_credit(credit_id: int, db: Session = Depends(get_db), _u: Utilisateur = Depends(_direction)):
    return _credit_dict(_get(db, credit_id), today_haiti(), avec_mouvements=True)


# ── Écriture : crédits ────────────────────────────────────────────────────────

@router.post("")
def creer_credit(data: CreditIn, request: Request, db: Session = Depends(get_db),
                 u: Utilisateur = Depends(_direction)):
    if data.sens not in SENS:
        raise HTTPException(400, "Choisissez : dette (nous devons) ou créance (on nous doit).")
    if data.devise not in DEVISES:
        raise HTTPException(400, "Devise : HTG ou USD.")
    nom = _txt(data.tiers_nom, 150)
    if not nom:
        raise HTTPException(400, "Indiquez le nom du tiers.")
    aujourdhui = today_haiti()
    d = data.date_credit or aujourdhui
    if d > aujourdhui:
        raise HTTPException(400, "La date du crédit ne peut pas être dans le futur.")
    if data.date_echeance and data.date_echeance < d:
        raise HTTPException(400, "L'échéance ne peut pas précéder la date du crédit.")
    c = Credit(sens=data.sens, tiers_nom=nom, tiers_contact=_txt(data.tiers_contact, 150),
               description=_txt(data.description, 500),
               montant_initial=_montant(data.montant_initial, "Le montant du crédit"),
               devise=data.devise, date_credit=d, date_echeance=data.date_echeance,
               statut="ACTIF", notes=(data.notes or "").strip()[:4000] or None, createur_id=u.id)
    db.add(c)
    db.flush()
    _tracer(db, request, u, "credit_cree", {"credit_id": c.id, "sens": c.sens, "tiers": nom,
                                            "montant": str(c.montant_initial), "devise": c.devise})
    db.commit()
    return _credit_dict(_get(db, c.id), aujourdhui, avec_mouvements=True)


@router.put("/{credit_id}")
def modifier_credit(credit_id: int, data: CreditPatch, request: Request, db: Session = Depends(get_db),
                    u: Utilisateur = Depends(_direction)):
    c = _get(db, credit_id)
    avant = _credit_dict(c, today_haiti())
    if data.tiers_nom is not None:
        nom = _txt(data.tiers_nom, 150)
        if not nom:
            raise HTTPException(400, "Indiquez le nom du tiers.")
        c.tiers_nom = nom
    if data.tiers_contact is not None:
        c.tiers_contact = _txt(data.tiers_contact, 150)
    if data.description is not None:
        c.description = _txt(data.description, 500)
    if data.notes is not None:
        c.notes = data.notes.strip()[:4000] or None
    if data.devise is not None and data.devise != c.devise:
        if data.devise not in DEVISES:
            raise HTTPException(400, "Devise : HTG ou USD.")
        if c.mouvements:
            raise HTTPException(409, "La devise ne peut plus changer : ce crédit a déjà des mouvements.")
        c.devise = data.devise
    if data.montant_initial is not None:
        m = _montant(data.montant_initial, "Le montant du crédit")
        deja = c.total_depose()
        if m + (c.total_du() - c.montant_initial) < deja:
            raise HTTPException(409, f"Impossible : {_f(deja):,.2f} {c.devise} ont déjà été déposés, "
                                     "le total dû ne peut pas devenir inférieur.".replace(",", " "))
        c.montant_initial = m
    if data.date_credit is not None:
        if data.date_credit > today_haiti():
            raise HTTPException(400, "La date du crédit ne peut pas être dans le futur.")
        premier = min((mv.date_mouvement for mv in c.mouvements), default=None)
        if premier and data.date_credit > premier:
            raise HTTPException(409, f"Un mouvement est daté du {premier.strftime('%d/%m/%Y')} : "
                                     "la date du crédit ne peut pas être après.")
        c.date_credit = data.date_credit
    if data.effacer_echeance:
        c.date_echeance = None
    elif data.date_echeance is not None:
        c.date_echeance = data.date_echeance
    if c.date_echeance and c.date_echeance < c.date_credit:
        raise HTTPException(400, "L'échéance ne peut pas précéder la date du crédit.")
    c.recalculer_statut()
    db.flush()
    _tracer(db, request, u, "credit_modifie", {"credit_id": c.id, "avant": avant,
                                               "apres": _credit_dict(c, today_haiti())})
    db.commit()
    return _credit_dict(_get(db, c.id), today_haiti(), avec_mouvements=True)


@router.post("/{credit_id}/annuler")
def annuler_credit(credit_id: int, data: MotifIn, request: Request, db: Session = Depends(get_db),
                   u: Utilisateur = Depends(_direction)):
    c = _get(db, credit_id)
    motif = _motif(data.motif)
    if c.statut == "ANNULE":
        raise HTTPException(409, "Ce crédit est déjà annulé.")
    c.statut = "ANNULE"
    note = f"[Annulé le {today_haiti().strftime('%d/%m/%Y')} par {_nom(u)}] {motif}"
    c.notes = f"{c.notes}\n{note}" if c.notes else note
    _tracer(db, request, u, "credit_annule", {"credit_id": c.id, "tiers": c.tiers_nom, "motif": motif,
                                              "reste": str(c.reste()), "devise": c.devise})
    db.commit()
    return _credit_dict(_get(db, c.id), today_haiti(), avec_mouvements=True)


@router.post("/{credit_id}/reactiver")
def reactiver_credit(credit_id: int, request: Request, db: Session = Depends(get_db),
                     u: Utilisateur = Depends(_direction)):
    c = _get(db, credit_id)
    if c.statut != "ANNULE":
        raise HTTPException(409, "Seul un crédit annulé peut être réactivé.")
    c.statut = "ACTIF"
    c.recalculer_statut()
    _tracer(db, request, u, "credit_reactive", {"credit_id": c.id, "tiers": c.tiers_nom})
    db.commit()
    return _credit_dict(_get(db, c.id), today_haiti(), avec_mouvements=True)


@router.post("/{credit_id}/supprimer")
def supprimer_credit(credit_id: int, data: MotifIn, request: Request, db: Session = Depends(get_db),
                     u: Utilisateur = Depends(_direction)):
    c = _get(db, credit_id)
    motif = _motif(data.motif)
    trace = _credit_dict(c, today_haiti(), avec_mouvements=True)
    db.delete(c)                    # cascade : ses mouvements partent avec lui
    _tracer(db, request, u, "credit_supprime", {"motif": motif, "credit": trace})
    db.commit()
    return {"supprime": credit_id}


# ── Écriture : mouvements ─────────────────────────────────────────────────────

@router.post("/{credit_id}/mouvements")
def ajouter_mouvement(credit_id: int, data: MouvementIn, request: Request, db: Session = Depends(get_db),
                      u: Utilisateur = Depends(_direction)):
    c = _get(db, credit_id)
    if c.statut == "ANNULE":
        raise HTTPException(409, "Ce crédit est annulé : réactivez-le avant d'y ajouter un mouvement.")
    if data.type not in TYPES:
        raise HTTPException(400, "Type : DEPOT ou AUGMENTATION.")
    montant = _montant(data.montant)
    aujourdhui = today_haiti()
    d = data.date_mouvement or aujourdhui
    if d > aujourdhui:
        raise HTTPException(400, "La date ne peut pas être dans le futur.")
    if d < c.date_credit:
        raise HTTPException(400, f"La date ne peut pas précéder la date du crédit "
                                 f"({c.date_credit.strftime('%d/%m/%Y')}).")
    if data.type == "DEPOT":
        reste = c.reste()
        if reste <= 0:
            raise HTTPException(409, "Ce crédit est déjà soldé : il ne reste rien à déposer.")
        if montant > reste:
            raise HTTPException(409, f"Le dépôt ({_f(montant):,.2f} {c.devise}) dépasse le reste "
                                     f"({_f(reste):,.2f} {c.devise}).".replace(",", " "))
    m = CreditMouvement(credit_id=c.id, type=data.type, montant=montant, date_mouvement=d,
                        mode_paiement=_txt(data.mode_paiement, 40), reference=_txt(data.reference, 100),
                        note=_txt(data.note, 500), createur_id=u.id)
    db.add(m)
    c.mouvements.append(m)
    c.recalculer_statut()
    db.flush()
    _tracer(db, request, u, "credit_mouvement_ajoute", {"credit_id": c.id, "mouvement_id": m.id,
                                                        "type": m.type, "montant": str(montant),
                                                        "devise": c.devise, "date": str(d)})
    db.commit()
    return _credit_dict(_get(db, c.id), aujourdhui, avec_mouvements=True)


@router.post("/{credit_id}/mouvements/{mouvement_id}/supprimer")
def supprimer_mouvement(credit_id: int, mouvement_id: int, data: MotifIn, request: Request,
                        db: Session = Depends(get_db), u: Utilisateur = Depends(_direction)):
    """Mouvement saisi par erreur : retiré avec un motif (trace d'audit). Une
    augmentation ne peut pas être retirée si les dépôts dépasseraient alors
    le total dû."""
    c = _get(db, credit_id)
    motif = _motif(data.motif)
    m = next((x for x in c.mouvements if x.id == mouvement_id), None)
    if not m:
        raise HTTPException(404, "Mouvement introuvable.")
    if m.type == "AUGMENTATION" and c.reste() - m.montant < 0:
        raise HTTPException(409, "Impossible : sans cette augmentation, les dépôts dépasseraient le total dû. "
                                 "Retirez d'abord le dépôt en trop.")
    trace = _mvt_dict(m)
    c.mouvements.remove(m)
    db.delete(m)
    c.recalculer_statut()
    _tracer(db, request, u, "credit_mouvement_supprime", {"credit_id": c.id, "motif": motif, "mouvement": trace})
    db.commit()
    return _credit_dict(_get(db, c.id), today_haiti(), avec_mouvements=True)
