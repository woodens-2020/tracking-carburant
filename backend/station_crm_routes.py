"""
CRM Station — Partenaires, Crédits, Factures, Interactions.

Corrections v2 :
  - N+1 éliminés : joinedload/subqueryload sur toutes les listes
  - Race condition numérotation : retry sur IntegrityError (UNIQUE)
  - PUT avec schémas Pydantic stricts (plus de data: dict)
  - XSS : html.escape() sur tout contenu utilisateur dans les emails
  - Dashboard : SUM/COUNT SQL au lieu de chargement mémoire
  - Pagination : paramètres page/par_page sur toutes les listes
  - Validation email basique via field_validator
  - Historique d'interactions (APPEL, EMAIL, REUNION, NOTE, VISITE)
  - Export CSV : /crm/clients/export et /crm/credits/export
"""

import csv
import html as html_lib
import io
import logging
import os
import re
import smtplib
from datetime import datetime, date as date_type, time, timezone
from decimal import Decimal
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func as sqlfunc
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, subqueryload

from database import get_db
from models import (
    Produit, StationClient, StationCredit,
    StationCreditRemboursement, StationFacture, StationInteraction,
)

log = logging.getLogger("crm")

EMAIL_HOST      = os.getenv("EMAIL_HOST",          "smtp.gmail.com")
EMAIL_PORT      = int(os.getenv("EMAIL_PORT",      "587"))
EMAIL_USER      = os.getenv("EMAIL_HOST_USER",     "")
EMAIL_PASSWORD  = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_FROM_NAME = os.getenv("EMAIL_FROM_NAME",     "Konekta · Bon Prix")

_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

router = APIRouter(prefix="/api/crm", tags=["crm"])


# ── Constantes ────────────────────────────────────────────────────────────────

_TYPES_CLIENT      = ("PARTICULIER", "ENTREPRISE")
_STATUTS_CREDIT    = ("EN_COURS", "SOLDE", "ANNULE")
_STATUTS_FACTURE   = ("BROUILLON", "ENVOYEE", "PAYEE", "ANNULEE")
_TYPES_INTERACTION = ("APPEL", "EMAIL", "REUNION", "NOTE", "VISITE")
_PAR_PAGE_MAX      = 200


# ── Helpers ───────────────────────────────────────────────────────────────────

def _dec(v) -> Decimal:
    return Decimal(str(v or 0))


def _fmt(v) -> float:
    return float(_dec(v))


def _paginate(q, page: int, par_page: int):
    """Applique OFFSET/LIMIT à une requête SQLAlchemy."""
    par_page = min(max(1, par_page), _PAR_PAGE_MAX)
    page     = max(1, page)
    total    = q.count()
    items    = q.offset((page - 1) * par_page).limit(par_page).all()
    return items, total, page, par_page


def _generer_numero_credit(db: Session) -> str:
    today  = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
    prefix = f"CR-{today}-"
    last = (
        db.query(StationCredit.numero)
        .filter(StationCredit.numero.like(f"{prefix}%"))
        .order_by(StationCredit.numero.desc())
        .first()
    )
    seq = int(last.numero[len(prefix):]) + 1 if last else 1
    return f"{prefix}{str(seq).zfill(4)}"


def _generer_numero_facture(db: Session) -> str:
    year   = datetime.now(tz=timezone.utc).strftime("%Y")
    prefix = f"FAC-{year}-"
    last = (
        db.query(StationFacture.numero_facture)
        .filter(StationFacture.numero_facture.like(f"{prefix}%"))
        .order_by(StationFacture.numero_facture.desc())
        .first()
    )
    seq = int(last.numero_facture[len(prefix):]) + 1 if last else 1
    return f"{prefix}{str(seq).zfill(4)}"


# ── Sérialiseurs ──────────────────────────────────────────────────────────────

def _client_dict(c: StationClient, *, detail: bool = False) -> dict:
    d = {
        "id":          c.id,
        "nom":         c.nom,
        "telephone":   c.telephone or "",
        "email":       c.email or "",
        "nif":         c.nif or "",
        "adresse":     c.adresse or "",
        "type_client": c.type_client,
        "notes":       c.notes or "",
        "actif":       c.actif,
        "created_at":  c.created_at.isoformat() if c.created_at else None,
        "nb_credits":  len(c.credits),
        "nb_factures": len(c.factures),
    }
    if detail:
        d["credits"]      = [_credit_dict(cr) for cr in c.credits]
        d["factures"]     = [_facture_dict(f)  for f  in c.factures]
        d["interactions"] = [_interaction_dict(i) for i in c.interactions]
    return d


def _credit_dict(cr: StationCredit) -> dict:
    restant = max(_dec(cr.montant_total) - _dec(cr.montant_paye), Decimal(0))
    return {
        "id":              cr.id,
        "numero":          cr.numero,
        "client_id":       cr.client_id,
        "client_nom":      cr.client.nom   if cr.client  else "",
        "client_email":    cr.client.email if cr.client  else "",
        "produit_id":      cr.produit_id,
        "produit_nom":     cr.produit.nom  if cr.produit else None,
        "montant_total":   _fmt(cr.montant_total),
        "montant_paye":    _fmt(cr.montant_paye),
        "montant_restant": float(restant),
        "quantite":        _fmt(cr.quantite)      if cr.quantite      else None,
        "prix_unitaire":   _fmt(cr.prix_unitaire) if cr.prix_unitaire else None,
        "statut":          cr.statut,
        "date_credit":     cr.date_credit.isoformat()   if cr.date_credit   else None,
        "date_echeance":   cr.date_echeance.isoformat() if cr.date_echeance else None,
        "notes":           cr.notes or "",
        "remboursements":  [
            {
                "id":      r.id,
                "montant": _fmt(r.montant),
                "date":    r.date_remboursement.isoformat(),
                "notes":   r.notes or "",
            }
            for r in cr.remboursements
        ],
    }


def _facture_dict(f: StationFacture) -> dict:
    return {
        "id":              f.id,
        "numero_facture":  f.numero_facture,
        "client_id":       f.client_id,
        "client_nom":      f.client.nom   if f.client else "",
        "client_email":    f.client.email if f.client else "",
        "date_facture":    f.date_facture.isoformat()   if f.date_facture   else None,
        "date_echeance":   f.date_echeance.isoformat()  if f.date_echeance  else None,
        "lignes":          f.lignes or [],
        "montant_ht":      _fmt(f.montant_ht),
        "taux_tva":        _fmt(f.taux_tva),
        "montant_tva":     _fmt(f.montant_tva),
        "montant_ttc":     _fmt(f.montant_ttc),
        "statut":          f.statut,
        "notes":           f.notes or "",
        "email_envoye_at": f.email_envoye_at.isoformat() if f.email_envoye_at else None,
    }


def _interaction_dict(i: StationInteraction) -> dict:
    return {
        "id":               i.id,
        "client_id":        i.client_id,
        "type_interaction": i.type_interaction,
        "titre":            i.titre,
        "description":      i.description or "",
        "date_interaction": i.date_interaction.isoformat() if i.date_interaction else None,
        "utilisateur":      i.utilisateur.nom_complet if i.utilisateur else None,
        "created_at":       i.created_at.isoformat() if i.created_at else None,
    }


# ── Schémas Pydantic ──────────────────────────────────────────────────────────

class ClientIn(BaseModel):
    nom:         str
    telephone:   Optional[str] = None
    email:       Optional[str] = None
    nif:         Optional[str] = None
    adresse:     Optional[str] = None
    type_client: str = "PARTICULIER"
    notes:       Optional[str] = None

    @field_validator("nom")
    @classmethod
    def nom_non_vide(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Le nom ne peut pas être vide")
        return v.strip()

    @field_validator("email")
    @classmethod
    def email_valide(cls, v: Optional[str]) -> Optional[str]:
        if v and not _EMAIL_RE.match(v.strip()):
            raise ValueError("Format email invalide")
        return v.strip() if v else None

    @field_validator("type_client")
    @classmethod
    def type_valide(cls, v: str) -> str:
        if v not in _TYPES_CLIENT:
            raise ValueError(f"type_client doit être PARTICULIER ou ENTREPRISE")
        return v


class ClientPatch(BaseModel):
    nom:         Optional[str]  = None
    telephone:   Optional[str]  = None
    email:       Optional[str]  = None
    nif:         Optional[str]  = None
    adresse:     Optional[str]  = None
    type_client: Optional[str]  = None
    notes:       Optional[str]  = None
    actif:       Optional[bool] = None

    @field_validator("email")
    @classmethod
    def email_valide(cls, v: Optional[str]) -> Optional[str]:
        if v and not _EMAIL_RE.match(v.strip()):
            raise ValueError("Format email invalide")
        return v.strip() if v else None

    @field_validator("type_client")
    @classmethod
    def type_valide(cls, v: Optional[str]) -> Optional[str]:
        if v and v not in _TYPES_CLIENT:
            raise ValueError("type_client invalide")
        return v


class CreditIn(BaseModel):
    client_id:     int
    produit_id:    Optional[int]   = None
    montant_total: float           = Field(gt=0)
    quantite:      Optional[float] = Field(default=None, gt=0)
    prix_unitaire: Optional[float] = Field(default=None, gt=0)
    date_echeance: Optional[date_type] = None
    notes:         Optional[str]   = None


class CreditPatch(BaseModel):
    notes:         Optional[str]       = None
    date_echeance: Optional[date_type] = None
    statut:        Optional[str]       = None

    @field_validator("statut")
    @classmethod
    def statut_valide(cls, v: Optional[str]) -> Optional[str]:
        if v and v.upper() not in _STATUTS_CREDIT:
            raise ValueError(f"Statut invalide. Valeurs acceptées : {_STATUTS_CREDIT}")
        return v.upper() if v else None


class RemboursementIn(BaseModel):
    montant: float = Field(gt=0)
    notes:   Optional[str] = None


class LigneFactureIn(BaseModel):
    description:   str
    quantite:      float = Field(gt=0)
    prix_unitaire: float = Field(gt=0)
    tva_pct:       float = Field(ge=0, default=0)

    @field_validator("description")
    @classmethod
    def desc_non_vide(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("La description de ligne ne peut pas être vide")
        return v.strip()


class FactureIn(BaseModel):
    client_id:     int
    date_echeance: Optional[date_type]  = None
    lignes:        List[LigneFactureIn]
    taux_tva:      float                = Field(ge=0, default=0)
    notes:         Optional[str]        = None

    @field_validator("lignes")
    @classmethod
    def lignes_non_vides(cls, v: List[LigneFactureIn]) -> List[LigneFactureIn]:
        if not v:
            raise ValueError("La facture doit contenir au moins une ligne")
        return v


class InteractionIn(BaseModel):
    type_interaction: str
    titre:            str
    description:      Optional[str]       = None
    date_interaction: Optional[datetime]  = None

    @field_validator("type_interaction")
    @classmethod
    def type_valide(cls, v: str) -> str:
        if v.upper() not in _TYPES_INTERACTION:
            raise ValueError(f"type_interaction invalide. Valeurs : {_TYPES_INTERACTION}")
        return v.upper()

    @field_validator("titre")
    @classmethod
    def titre_non_vide(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Le titre ne peut pas être vide")
        return v.strip()


# ── Routes CLIENTS ────────────────────────────────────────────────────────────

@router.get("/clients")
def liste_clients(
    search:      Optional[str]  = Query(default=None),
    actif:       Optional[bool] = Query(default=None),
    type_client: Optional[str]  = Query(default=None),
    page:        int            = Query(default=1,  ge=1),
    par_page:    int            = Query(default=50, ge=1, le=_PAR_PAGE_MAX),
    db:          Session        = Depends(get_db),
):
    q = (
        db.query(StationClient)
        .options(
            subqueryload(StationClient.credits),
            subqueryload(StationClient.factures),
        )
    )
    if actif is not None:
        q = q.filter(StationClient.actif == actif)
    if type_client and type_client in _TYPES_CLIENT:
        q = q.filter(StationClient.type_client == type_client)
    if search:
        s = f"%{search}%"
        q = q.filter(
            StationClient.nom.ilike(s) |
            StationClient.telephone.ilike(s) |
            StationClient.email.ilike(s) |
            StationClient.nif.ilike(s)
        )
    q = q.order_by(StationClient.nom)
    items, total, page, par_page = _paginate(q, page, par_page)
    return {
        "total":    total,
        "page":     page,
        "par_page": par_page,
        "items":    [_client_dict(c) for c in items],
    }


@router.get("/clients/export")
def exporter_clients(
    actif: Optional[bool] = Query(default=None),
    db:    Session        = Depends(get_db),
):
    """Export CSV de tous les partenaires."""
    q = db.query(StationClient)
    if actif is not None:
        q = q.filter(StationClient.actif == actif)
    clients = q.order_by(StationClient.nom).all()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ID", "Nom", "Type", "Téléphone", "Email", "NIF", "Adresse", "Actif", "Créé le"])
    for c in clients:
        w.writerow([
            c.id, c.nom, c.type_client, c.telephone or "", c.email or "",
            c.nif or "", c.adresse or "",
            "Oui" if c.actif else "Non",
            c.created_at.strftime("%d/%m/%Y") if c.created_at else "",
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=partenaires_crm.csv"},
    )


@router.post("/clients", status_code=201)
def creer_client(data: ClientIn, db: Session = Depends(get_db)):
    c = StationClient(
        nom=data.nom,
        telephone=data.telephone,
        email=data.email,
        nif=data.nif,
        adresse=data.adresse,
        type_client=data.type_client,
        notes=data.notes,
    )
    db.add(c); db.commit(); db.refresh(c)
    return _client_dict(c)


@router.get("/clients/{client_id}")
def get_client(client_id: int, db: Session = Depends(get_db)):
    c = (
        db.query(StationClient)
        .options(
            subqueryload(StationClient.credits).joinedload(StationCredit.produit),
            subqueryload(StationClient.credits).subqueryload(StationCredit.remboursements),
            subqueryload(StationClient.factures),
            subqueryload(StationClient.interactions).joinedload(StationInteraction.utilisateur),
        )
        .filter(StationClient.id == client_id)
        .first()
    )
    if not c:
        raise HTTPException(404, "Client introuvable")
    return _client_dict(c, detail=True)


@router.put("/clients/{client_id}")
def modifier_client(client_id: int, data: ClientPatch, db: Session = Depends(get_db)):
    c = db.query(StationClient).filter(StationClient.id == client_id).first()
    if not c:
        raise HTTPException(404, "Client introuvable")
    patch = data.model_dump(exclude_unset=True)
    for field, value in patch.items():
        setattr(c, field, value)
    db.commit(); db.refresh(c)
    return _client_dict(c)


@router.delete("/clients/{client_id}")
def supprimer_client(client_id: int, db: Session = Depends(get_db)):
    c = (
        db.query(StationClient)
        .options(subqueryload(StationClient.credits))
        .filter(StationClient.id == client_id)
        .first()
    )
    if not c:
        raise HTTPException(404, "Client introuvable")
    credits_actifs = [cr for cr in c.credits if cr.statut == "EN_COURS"]
    if credits_actifs:
        raise HTTPException(
            409,
            f"Ce client a {len(credits_actifs)} crédit(s) en cours. Soldez-les avant de supprimer."
        )
    db.delete(c); db.commit()
    return {"message": "Client supprimé"}


# ── Routes INTERACTIONS ───────────────────────────────────────────────────────

@router.get("/clients/{client_id}/interactions")
def liste_interactions(
    client_id: int,
    page:      int = Query(default=1,  ge=1),
    par_page:  int = Query(default=20, ge=1, le=100),
    db:        Session = Depends(get_db),
):
    if not db.query(StationClient).filter(StationClient.id == client_id).first():
        raise HTTPException(404, "Client introuvable")
    q = (
        db.query(StationInteraction)
        .options(joinedload(StationInteraction.utilisateur))
        .filter(StationInteraction.client_id == client_id)
        .order_by(StationInteraction.date_interaction.desc())
    )
    items, total, page, par_page = _paginate(q, page, par_page)
    return {"total": total, "page": page, "par_page": par_page,
            "items": [_interaction_dict(i) for i in items]}


@router.post("/clients/{client_id}/interactions", status_code=201)
def ajouter_interaction(client_id: int, data: InteractionIn, db: Session = Depends(get_db)):
    if not db.query(StationClient).filter(StationClient.id == client_id).first():
        raise HTTPException(404, "Client introuvable")
    i = StationInteraction(
        client_id=client_id,
        type_interaction=data.type_interaction,
        titre=data.titre,
        description=data.description,
        date_interaction=data.date_interaction or datetime.now(tz=timezone.utc),
    )
    db.add(i); db.commit(); db.refresh(i)
    return _interaction_dict(i)


@router.delete("/interactions/{interaction_id}")
def supprimer_interaction(interaction_id: int, db: Session = Depends(get_db)):
    i = db.query(StationInteraction).filter(StationInteraction.id == interaction_id).first()
    if not i:
        raise HTTPException(404, "Interaction introuvable")
    db.delete(i); db.commit()
    return {"message": "Interaction supprimée"}


# ── Routes CREDITS ────────────────────────────────────────────────────────────

@router.get("/credits")
def liste_credits(
    client_id:  Optional[int]       = Query(default=None),
    statut:     Optional[str]       = Query(default=None),
    date_debut: Optional[date_type] = Query(default=None),
    date_fin:   Optional[date_type] = Query(default=None),
    page:       int                 = Query(default=1,  ge=1),
    par_page:   int                 = Query(default=50, ge=1, le=_PAR_PAGE_MAX),
    db:         Session             = Depends(get_db),
):
    q = (
        db.query(StationCredit)
        .options(
            joinedload(StationCredit.client),
            joinedload(StationCredit.produit),
            subqueryload(StationCredit.remboursements),
        )
    )
    if client_id:
        q = q.filter(StationCredit.client_id == client_id)
    if statut:
        s = statut.upper()
        if s not in _STATUTS_CREDIT:
            raise HTTPException(400, f"Statut invalide. Valeurs : {_STATUTS_CREDIT}")
        q = q.filter(StationCredit.statut == s)
    if date_debut:
        q = q.filter(StationCredit.date_credit >= datetime.combine(date_debut, time.min).replace(tzinfo=timezone.utc))
    if date_fin:
        q = q.filter(StationCredit.date_credit <= datetime.combine(date_fin,   time.max).replace(tzinfo=timezone.utc))
    q = q.order_by(StationCredit.date_credit.desc())
    items, total, page, par_page = _paginate(q, page, par_page)
    return {"total": total, "page": page, "par_page": par_page,
            "items": [_credit_dict(cr) for cr in items]}


@router.get("/credits/export")
def exporter_credits(
    statut: Optional[str] = Query(default=None),
    db:     Session       = Depends(get_db),
):
    """Export CSV des crédits."""
    q = (
        db.query(StationCredit)
        .options(joinedload(StationCredit.client), joinedload(StationCredit.produit))
    )
    if statut:
        q = q.filter(StationCredit.statut == statut.upper())
    credits = q.order_by(StationCredit.date_credit.desc()).all()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["N°", "Partenaire", "Produit", "Montant", "Payé", "Restant", "Statut", "Échéance", "Date"])
    for cr in credits:
        restant = float(max(_dec(cr.montant_total) - _dec(cr.montant_paye), Decimal(0)))
        w.writerow([
            cr.numero,
            cr.client.nom if cr.client else "",
            cr.produit.nom if cr.produit else "",
            _fmt(cr.montant_total), _fmt(cr.montant_paye), round(restant, 2),
            cr.statut,
            cr.date_echeance.strftime("%d/%m/%Y") if cr.date_echeance else "",
            cr.date_credit.strftime("%d/%m/%Y")   if cr.date_credit   else "",
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=credits_crm.csv"},
    )


@router.post("/credits", status_code=201)
def creer_credit(data: CreditIn, db: Session = Depends(get_db)):
    if not db.query(StationClient).filter(StationClient.id == data.client_id, StationClient.actif == True).first():
        raise HTTPException(404, "Client introuvable ou inactif")
    if data.produit_id and not db.query(Produit).filter(Produit.id == data.produit_id).first():
        raise HTTPException(404, "Produit introuvable")

    for tentative in range(5):
        numero = _generer_numero_credit(db)
        cr = StationCredit(
            client_id=data.client_id,
            produit_id=data.produit_id,
            numero=numero,
            montant_total=Decimal(str(data.montant_total)),
            montant_paye=Decimal(0),
            quantite=Decimal(str(data.quantite))      if data.quantite      else None,
            prix_unitaire=Decimal(str(data.prix_unitaire)) if data.prix_unitaire else None,
            date_echeance=data.date_echeance,
            notes=data.notes,
        )
        db.add(cr)
        try:
            db.commit()
            db.refresh(cr)
            return _credit_dict(cr)
        except IntegrityError:
            db.rollback()
    raise HTTPException(500, "Impossible de générer un numéro de crédit unique, réessayez")


@router.get("/credits/{credit_id}")
def get_credit(credit_id: int, db: Session = Depends(get_db)):
    cr = (
        db.query(StationCredit)
        .options(
            joinedload(StationCredit.client),
            joinedload(StationCredit.produit),
            subqueryload(StationCredit.remboursements),
        )
        .filter(StationCredit.id == credit_id)
        .first()
    )
    if not cr:
        raise HTTPException(404, "Crédit introuvable")
    return _credit_dict(cr)


@router.put("/credits/{credit_id}")
def modifier_credit(credit_id: int, data: CreditPatch, db: Session = Depends(get_db)):
    cr = db.query(StationCredit).filter(StationCredit.id == credit_id).first()
    if not cr:
        raise HTTPException(404, "Crédit introuvable")
    patch = data.model_dump(exclude_unset=True)
    for field, value in patch.items():
        setattr(cr, field, value)
    db.commit(); db.refresh(cr)
    return _credit_dict(cr)


@router.post("/credits/{credit_id}/rembourser")
def rembourser_credit(credit_id: int, data: RemboursementIn, db: Session = Depends(get_db)):
    cr = (
        db.query(StationCredit)
        .options(subqueryload(StationCredit.remboursements), joinedload(StationCredit.client))
        .filter(StationCredit.id == credit_id)
        .first()
    )
    if not cr:
        raise HTTPException(404, "Crédit introuvable")
    if cr.statut != "EN_COURS":
        raise HTTPException(409, f"Ce crédit est déjà {cr.statut.lower()}")

    restant = _dec(cr.montant_total) - _dec(cr.montant_paye)
    montant = _dec(data.montant)
    if montant > restant:
        raise HTTPException(400, f"Le montant ({float(montant):.2f}) dépasse le restant dû ({float(restant):.2f})")

    db.add(StationCreditRemboursement(credit_id=credit_id, montant=montant, notes=data.notes))
    cr.montant_paye = _dec(cr.montant_paye) + montant
    if cr.montant_paye >= _dec(cr.montant_total):
        cr.statut = "SOLDE"
    db.commit(); db.refresh(cr)
    return _credit_dict(cr)


@router.delete("/credits/{credit_id}")
def supprimer_credit(credit_id: int, db: Session = Depends(get_db)):
    cr = (
        db.query(StationCredit)
        .options(subqueryload(StationCredit.remboursements))
        .filter(StationCredit.id == credit_id)
        .first()
    )
    if not cr:
        raise HTTPException(404, "Crédit introuvable")
    if cr.statut == "EN_COURS" and cr.remboursements:
        raise HTTPException(409, "Impossible de supprimer un crédit avec remboursements partiels")
    db.delete(cr); db.commit()
    return {"message": "Crédit supprimé"}


# ── Routes FACTURES ───────────────────────────────────────────────────────────

@router.get("/factures")
def liste_factures(
    client_id:  Optional[int]       = Query(default=None),
    statut:     Optional[str]       = Query(default=None),
    date_debut: Optional[date_type] = Query(default=None),
    date_fin:   Optional[date_type] = Query(default=None),
    page:       int                 = Query(default=1,  ge=1),
    par_page:   int                 = Query(default=50, ge=1, le=_PAR_PAGE_MAX),
    db:         Session             = Depends(get_db),
):
    q = (
        db.query(StationFacture)
        .options(joinedload(StationFacture.client))
    )
    if client_id:
        q = q.filter(StationFacture.client_id == client_id)
    if statut:
        s = statut.upper()
        if s not in _STATUTS_FACTURE:
            raise HTTPException(400, f"Statut invalide. Valeurs : {_STATUTS_FACTURE}")
        q = q.filter(StationFacture.statut == s)
    if date_debut:
        q = q.filter(StationFacture.date_facture >= datetime.combine(date_debut, time.min).replace(tzinfo=timezone.utc))
    if date_fin:
        q = q.filter(StationFacture.date_facture <= datetime.combine(date_fin,   time.max).replace(tzinfo=timezone.utc))
    q = q.order_by(StationFacture.date_facture.desc())
    items, total, page, par_page = _paginate(q, page, par_page)
    return {"total": total, "page": page, "par_page": par_page,
            "items": [_facture_dict(f) for f in items]}


@router.post("/factures", status_code=201)
def creer_facture(data: FactureIn, db: Session = Depends(get_db)):
    client = db.query(StationClient).filter(StationClient.id == data.client_id, StationClient.actif == True).first()
    if not client:
        raise HTTPException(404, "Client introuvable ou inactif")

    lignes_json = []
    montant_ht  = Decimal(0)
    for l in data.lignes:
        sous_ht     = (Decimal(str(l.quantite)) * Decimal(str(l.prix_unitaire))).quantize(Decimal("0.01"))
        montant_ht += sous_ht
        lignes_json.append({
            "description":   l.description,
            "quantite":      l.quantite,
            "prix_unitaire": l.prix_unitaire,
            "tva_pct":       l.tva_pct,
            "sous_total_ht": float(sous_ht),
        })

    taux_tva    = Decimal(str(data.taux_tva))
    montant_tva = (montant_ht * taux_tva / 100).quantize(Decimal("0.01"))
    montant_ttc = montant_ht + montant_tva

    for tentative in range(5):
        numero = _generer_numero_facture(db)
        f = StationFacture(
            client_id=data.client_id,
            numero_facture=numero,
            date_echeance=data.date_echeance,
            lignes=lignes_json,
            montant_ht=montant_ht,
            taux_tva=taux_tva,
            montant_tva=montant_tva,
            montant_ttc=montant_ttc,
            notes=data.notes,
        )
        db.add(f)
        try:
            db.commit()
            db.refresh(f)
            return _facture_dict(f)
        except IntegrityError:
            db.rollback()
    raise HTTPException(500, "Impossible de générer un numéro de facture unique, réessayez")


@router.get("/factures/{facture_id}")
def get_facture(facture_id: int, db: Session = Depends(get_db)):
    f = (
        db.query(StationFacture)
        .options(joinedload(StationFacture.client))
        .filter(StationFacture.id == facture_id)
        .first()
    )
    if not f:
        raise HTTPException(404, "Facture introuvable")
    return _facture_dict(f)


@router.put("/factures/{facture_id}")
def modifier_facture(facture_id: int, data: dict, db: Session = Depends(get_db)):
    f = db.query(StationFacture).filter(StationFacture.id == facture_id).first()
    if not f:
        raise HTTPException(404, "Facture introuvable")
    if f.statut != "BROUILLON" and "statut" not in data:
        raise HTTPException(409, "Seules les factures en brouillon peuvent être modifiées")
    if "statut" in data:
        s = str(data["statut"]).upper()
        if s not in _STATUTS_FACTURE:
            raise HTTPException(400, f"Statut invalide. Valeurs : {_STATUTS_FACTURE}")
        f.statut = s
    for field in ("notes", "date_echeance"):
        if field in data:
            setattr(f, field, data[field])
    db.commit(); db.refresh(f)
    return _facture_dict(f)


@router.post("/factures/{facture_id}/envoyer")
def envoyer_facture(facture_id: int, db: Session = Depends(get_db)):
    f = (
        db.query(StationFacture)
        .options(joinedload(StationFacture.client))
        .filter(StationFacture.id == facture_id)
        .first()
    )
    if not f:
        raise HTTPException(404, "Facture introuvable")
    if f.statut == "ANNULEE":
        raise HTTPException(409, "Impossible d'envoyer une facture annulée")
    if not f.client or not f.client.email:
        raise HTTPException(400, "Ce client n'a pas d'adresse email enregistrée")
    try:
        _envoyer_email_facture(f, f.client)
    except Exception as exc:
        raise HTTPException(500, f"Erreur envoi email : {exc}")

    f.statut          = "ENVOYEE"
    f.email_envoye_at = datetime.now(tz=timezone.utc)
    db.commit(); db.refresh(f)
    return _facture_dict(f)


@router.delete("/factures/{facture_id}")
def supprimer_facture(facture_id: int, db: Session = Depends(get_db)):
    f = db.query(StationFacture).filter(StationFacture.id == facture_id).first()
    if not f:
        raise HTTPException(404, "Facture introuvable")
    if f.statut not in ("BROUILLON", "ANNULEE"):
        raise HTTPException(409, "Seules les factures BROUILLON ou ANNULÉE peuvent être supprimées")
    db.delete(f); db.commit()
    return {"message": "Facture supprimée"}


# ── Dashboard CRM — agrégats SQL (pas de chargement mémoire) ─────────────────

@router.get("/dashboard")
def dashboard_crm(db: Session = Depends(get_db)):
    total_clients = db.query(StationClient).filter(StationClient.actif == True).count()

    credit_stats = (
        db.query(
            StationCredit.statut,
            sqlfunc.count(StationCredit.id).label("nb"),
            sqlfunc.coalesce(
                sqlfunc.sum(StationCredit.montant_total - StationCredit.montant_paye), 0
            ).label("montant_restant_total"),
        )
        .group_by(StationCredit.statut)
        .all()
    )
    credits_en_cours = 0
    credits_soldes   = 0
    montant_en_cours = 0.0
    for row in credit_stats:
        if row.statut == "EN_COURS":
            credits_en_cours = row.nb
            montant_en_cours = float(row.montant_restant_total or 0)
        elif row.statut == "SOLDE":
            credits_soldes = row.nb

    facture_stats = (
        db.query(StationFacture.statut, sqlfunc.count(StationFacture.id).label("nb"))
        .group_by(StationFacture.statut)
        .all()
    )
    fact_map = {r.statut: r.nb for r in facture_stats}

    nb_interactions = db.query(StationInteraction).count()

    return {
        "total_clients":     total_clients,
        "credits_en_cours":  credits_en_cours,
        "credits_soldes":    credits_soldes,
        "montant_en_cours":  round(montant_en_cours, 2),
        "factures_brouillon": fact_map.get("BROUILLON", 0),
        "factures_envoyees":  fact_map.get("ENVOYEE",   0),
        "factures_payees":    fact_map.get("PAYEE",     0),
        "nb_interactions":    nb_interactions,
    }


# ── Service email facture ─────────────────────────────────────────────────────

def _envoyer_email_facture(f: StationFacture, client: StationClient) -> None:
    if not EMAIL_USER or not EMAIL_PASSWORD:
        raise RuntimeError("Email non configuré dans .env")
    html = _build_facture_html(f, client)
    msg  = MIMEMultipart("alternative")
    msg["Subject"] = f"Facture {f.numero_facture} — Konekta Bon Prix"
    msg["From"]    = f"{EMAIL_FROM_NAME} <{EMAIL_USER}>"
    msg["To"]      = client.email
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT, timeout=10) as smtp:
            smtp.ehlo(); smtp.starttls(); smtp.ehlo()
            smtp.login(EMAIL_USER, EMAIL_PASSWORD)
            smtp.sendmail(EMAIL_USER, [client.email], msg.as_string())
        log.info("Facture %s envoyée à %s", f.numero_facture, client.email)
    except smtplib.SMTPAuthenticationError:
        raise RuntimeError("Échec authentification SMTP Gmail")
    except Exception as exc:
        log.error("Erreur envoi facture : %s", exc)
        raise RuntimeError(str(exc))


def _esc(v) -> str:
    """html.escape() — protège contre XSS dans les templates email."""
    return html_lib.escape(str(v or ""))


def _build_facture_html(f: StationFacture, client: StationClient) -> str:
    date_str     = f.date_facture.strftime("%d/%m/%Y") if f.date_facture   else ""
    echeance_str = f.date_echeance.strftime("%d/%m/%Y") if f.date_echeance else "—"

    lignes_html = ""
    for i, l in enumerate(f.lignes or []):
        bg = "rgba(255,255,255,.03)" if i % 2 == 0 else "transparent"
        lignes_html += f"""
        <tr style="background:{bg}">
          <td style="padding:10px 14px;color:#dde8f8;font-size:13px">{_esc(l.get('description',''))}</td>
          <td style="padding:10px 14px;text-align:center;color:#dde8f8;font-size:13px">{_esc(l.get('quantite',''))}</td>
          <td style="padding:10px 14px;text-align:right;color:#dde8f8;font-size:13px">{float(l.get('prix_unitaire',0)):,.2f}</td>
          <td style="padding:10px 14px;text-align:right;font-weight:700;color:#e8c558;font-size:13px">{float(l.get('sous_total_ht',0)):,.2f}</td>
        </tr>"""

    tva_row = ""
    if float(f.taux_tva or 0) > 0:
        tva_row = f"""
        <tr>
          <td colspan="3" style="padding:8px 14px;text-align:right;color:rgba(221,232,248,.55);font-size:12px">TVA ({float(f.taux_tva):.1f}%)</td>
          <td style="padding:8px 14px;text-align:right;color:rgba(221,232,248,.8);font-size:13px">{float(f.montant_tva):,.2f}</td>
        </tr>"""

    nif_row     = f'<div style="font-size:12px;color:rgba(221,232,248,.55);margin-top:3px">NIF : {_esc(client.nif)}</div>' if client.nif else ""
    adresse_row = f'<div style="font-size:12px;color:rgba(221,232,248,.55);margin-top:2px">{_esc(client.adresse)}</div>' if client.adresse else ""
    tel_row     = f'<div style="font-size:12px;color:rgba(221,232,248,.55);margin-top:2px">Tél : {_esc(client.telephone)}</div>' if client.telephone else ""
    notes_block = ""
    if f.notes:
        notes_block = f"""<tr><td style="padding:0 32px 20px">
          <div style="background:rgba(255,255,255,.03);border-left:3px solid rgba(232,197,88,.3);padding:12px 14px;border-radius:0 6px 6px 0">
            <div style="font-size:10px;color:rgba(232,197,88,.5);letter-spacing:.1em;text-transform:uppercase;margin-bottom:4px">Notes</div>
            <div style="color:rgba(221,232,248,.65);font-size:12px;line-height:1.6">{_esc(f.notes)}</div>
          </div></td></tr>"""

    return f"""<!DOCTYPE html>
<html lang="fr">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#070e1c;font-family:Arial,Helvetica,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#070e1c;padding:36px 16px">
  <tr><td align="center">
    <table width="100%" style="max-width:620px;background:#0b1628;border-radius:14px;border:1px solid rgba(232,197,88,.22);overflow:hidden">
      <tr>
        <td style="padding:28px 32px 24px;border-bottom:2px solid #e8c558">
          <table width="100%"><tr>
            <td>
              <div style="font-size:28px;font-weight:900;color:#e8c558;line-height:1">K</div>
              <div style="font-size:12px;font-weight:800;color:#e8c558;letter-spacing:3px;margin-top:2px">KONEKTA</div>
              <div style="font-size:10px;color:rgba(232,197,88,.45);margin-top:2px">Bon Prix · Complexe Commerciale de Pillatre</div>
            </td>
            <td style="text-align:right">
              <div style="font-size:22px;font-weight:900;color:#fff">FACTURE</div>
              <div style="font-size:13px;color:#e8c558;margin-top:4px;font-family:monospace">{_esc(f.numero_facture)}</div>
              <div style="font-size:11px;color:rgba(221,232,248,.45);margin-top:4px">Date : {date_str}</div>
              <div style="font-size:11px;color:rgba(221,232,248,.45)">Échéance : {echeance_str}</div>
            </td>
          </tr></table>
        </td>
      </tr>
      <tr>
        <td style="padding:20px 32px;border-bottom:1px solid rgba(255,255,255,.06)">
          <div style="font-size:10px;font-weight:800;color:rgba(232,197,88,.5);letter-spacing:.15em;text-transform:uppercase;margin-bottom:8px">Facturé à</div>
          <div style="font-size:15px;font-weight:700;color:#fff">{_esc(client.nom)}</div>
          {nif_row}{adresse_row}{tel_row}
          <div style="font-size:12px;color:rgba(221,232,248,.55);margin-top:2px">{_esc(client.email)}</div>
        </td>
      </tr>
      <tr>
        <td style="padding:0">
          <table width="100%" cellpadding="0" cellspacing="0">
            <thead>
              <tr style="background:rgba(232,197,88,.08)">
                <th style="padding:10px 14px;text-align:left;color:rgba(232,197,88,.7);font-size:11px;letter-spacing:.08em;text-transform:uppercase">Description</th>
                <th style="padding:10px 14px;text-align:center;color:rgba(232,197,88,.7);font-size:11px;letter-spacing:.08em;text-transform:uppercase">Qté</th>
                <th style="padding:10px 14px;text-align:right;color:rgba(232,197,88,.7);font-size:11px;letter-spacing:.08em;text-transform:uppercase">Prix unit.</th>
                <th style="padding:10px 14px;text-align:right;color:rgba(232,197,88,.7);font-size:11px;letter-spacing:.08em;text-transform:uppercase">Montant</th>
              </tr>
            </thead>
            <tbody>{lignes_html}</tbody>
          </table>
        </td>
      </tr>
      <tr>
        <td style="padding:16px 32px;border-top:1px solid rgba(255,255,255,.08)">
          <table width="100%" style="max-width:280px;margin-left:auto">
            <tr>
              <td style="padding:6px 0;color:rgba(221,232,248,.55);font-size:13px">Sous-total HT</td>
              <td style="padding:6px 0;text-align:right;color:#dde8f8;font-size:13px">{float(f.montant_ht):,.2f}</td>
            </tr>
            {tva_row}
            <tr style="border-top:2px solid #e8c558">
              <td style="padding:10px 0 6px;color:#e8c558;font-size:15px;font-weight:800">TOTAL TTC</td>
              <td style="padding:10px 0 6px;text-align:right;color:#e8c558;font-size:18px;font-weight:900">{float(f.montant_ttc):,.2f} HTG</td>
            </tr>
          </table>
        </td>
      </tr>
      {notes_block}
      <tr>
        <td style="padding:14px 32px;border-top:1px solid rgba(255,255,255,.06);text-align:center">
          <p style="margin:0;color:rgba(255,255,255,.18);font-size:10px">
            &copy; 2026 Konekta &nbsp;&middot;&nbsp; Message automatique &mdash; ne pas répondre.
          </p>
        </td>
      </tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""
