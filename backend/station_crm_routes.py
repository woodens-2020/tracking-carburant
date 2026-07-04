"""
CRM Station — Partenaires, Crédits, Factures.

Routes :
  GET/POST         /api/crm/clients
  GET/PUT/DELETE   /api/crm/clients/{id}
  GET/POST         /api/crm/credits
  GET/PUT          /api/crm/credits/{id}
  POST             /api/crm/credits/{id}/rembourser
  DELETE           /api/crm/credits/{id}
  GET/POST         /api/crm/factures
  GET/PUT          /api/crm/factures/{id}
  POST             /api/crm/factures/{id}/envoyer
  DELETE           /api/crm/factures/{id}
  GET              /api/crm/dashboard
"""

import logging
import os
import smtplib
from datetime import datetime, date as date_type, timezone
from decimal import Decimal
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from models import StationClient, StationCredit, StationCreditRemboursement, StationFacture, Produit

log = logging.getLogger("crm")

EMAIL_HOST      = os.getenv("EMAIL_HOST",          "smtp.gmail.com")
EMAIL_PORT      = int(os.getenv("EMAIL_PORT",      "587"))
EMAIL_USER      = os.getenv("EMAIL_HOST_USER",     "")
EMAIL_PASSWORD  = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_FROM_NAME = os.getenv("EMAIL_FROM_NAME",     "Konekta · Bon Prix")

router = APIRouter(prefix="/api/crm", tags=["crm"])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _dec(v) -> Decimal:
    return Decimal(str(v or 0))


def _fmt(v) -> float:
    return float(_dec(v))


def _generer_numero_credit(db: Session) -> str:
    today = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
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
    year = datetime.now(tz=timezone.utc).strftime("%Y")
    prefix = f"FAC-{year}-"
    last = (
        db.query(StationFacture.numero_facture)
        .filter(StationFacture.numero_facture.like(f"{prefix}%"))
        .order_by(StationFacture.numero_facture.desc())
        .first()
    )
    seq = int(last.numero_facture[len(prefix):]) + 1 if last else 1
    return f"{prefix}{str(seq).zfill(4)}"


def _client_dict(c: StationClient) -> dict:
    return {
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


def _credit_dict(cr: StationCredit) -> dict:
    montant_restant = max(_dec(cr.montant_total) - _dec(cr.montant_paye), Decimal(0))
    return {
        "id":             cr.id,
        "numero":         cr.numero,
        "client_id":      cr.client_id,
        "client_nom":     cr.client.nom if cr.client else "",
        "client_email":   cr.client.email if cr.client else "",
        "produit_id":     cr.produit_id,
        "produit_nom":    cr.produit.nom if cr.produit else None,
        "montant_total":  _fmt(cr.montant_total),
        "montant_paye":   _fmt(cr.montant_paye),
        "montant_restant": float(montant_restant),
        "quantite":       _fmt(cr.quantite) if cr.quantite else None,
        "prix_unitaire":  _fmt(cr.prix_unitaire) if cr.prix_unitaire else None,
        "statut":         cr.statut,
        "date_credit":    cr.date_credit.isoformat() if cr.date_credit else None,
        "date_echeance":  cr.date_echeance.isoformat() if cr.date_echeance else None,
        "notes":          cr.notes or "",
        "remboursements": [
            {
                "id":     r.id,
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
        "client_nom":      f.client.nom if f.client else "",
        "client_email":    f.client.email if f.client else "",
        "date_facture":    f.date_facture.isoformat() if f.date_facture else None,
        "date_echeance":   f.date_echeance.isoformat() if f.date_echeance else None,
        "lignes":          f.lignes or [],
        "montant_ht":      _fmt(f.montant_ht),
        "taux_tva":        _fmt(f.taux_tva),
        "montant_tva":     _fmt(f.montant_tva),
        "montant_ttc":     _fmt(f.montant_ttc),
        "statut":          f.statut,
        "notes":           f.notes or "",
        "email_envoye_at": f.email_envoye_at.isoformat() if f.email_envoye_at else None,
    }


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class ClientIn(BaseModel):
    nom:         str
    telephone:   Optional[str] = None
    email:       Optional[str] = None
    nif:         Optional[str] = None
    adresse:     Optional[str] = None
    type_client: str = "PARTICULIER"
    notes:       Optional[str] = None


class CreditIn(BaseModel):
    client_id:     int
    produit_id:    Optional[int] = None
    montant_total: float = Field(gt=0)
    quantite:      Optional[float] = Field(default=None, gt=0)
    prix_unitaire: Optional[float] = Field(default=None, gt=0)
    date_echeance: Optional[date_type] = None
    notes:         Optional[str] = None


class RemboursementIn(BaseModel):
    montant: float = Field(gt=0)
    notes:   Optional[str] = None


class LigneFactureIn(BaseModel):
    description:   str
    quantite:      float = Field(gt=0)
    prix_unitaire: float = Field(gt=0)
    tva_pct:       float = Field(ge=0, default=0)


class FactureIn(BaseModel):
    client_id:     int
    date_echeance: Optional[date_type] = None
    lignes:        List[LigneFactureIn]
    taux_tva:      float = Field(ge=0, default=0)
    notes:         Optional[str] = None


# ── Routes CLIENTS ────────────────────────────────────────────────────────────

@router.get("/clients")
def liste_clients(
    search: Optional[str] = Query(default=None),
    actif:  Optional[bool] = Query(default=None),
    db:     Session = Depends(get_db),
):
    q = db.query(StationClient)
    if actif is not None:
        q = q.filter(StationClient.actif == actif)
    if search:
        s = f"%{search}%"
        q = q.filter(
            StationClient.nom.ilike(s) |
            StationClient.telephone.ilike(s) |
            StationClient.email.ilike(s) |
            StationClient.nif.ilike(s)
        )
    clients = q.order_by(StationClient.nom).all()
    return [_client_dict(c) for c in clients]


@router.post("/clients", status_code=201)
def creer_client(data: ClientIn, db: Session = Depends(get_db)):
    if data.type_client not in ("PARTICULIER", "ENTREPRISE"):
        raise HTTPException(400, "type_client doit être PARTICULIER ou ENTREPRISE")
    c = StationClient(
        nom=data.nom.strip(),
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
    c = db.query(StationClient).filter_by(id=client_id).first()
    if not c:
        raise HTTPException(404, "Client introuvable")
    d = _client_dict(c)
    d["credits"]  = [_credit_dict(cr) for cr in c.credits]
    d["factures"] = [_facture_dict(f)  for f  in c.factures]
    return d


@router.put("/clients/{client_id}")
def modifier_client(client_id: int, data: dict, db: Session = Depends(get_db)):
    c = db.query(StationClient).filter_by(id=client_id).first()
    if not c:
        raise HTTPException(404, "Client introuvable")
    for field in ("nom", "telephone", "email", "nif", "adresse", "type_client", "notes", "actif"):
        if field in data:
            if field == "type_client" and data[field] not in ("PARTICULIER", "ENTREPRISE"):
                raise HTTPException(400, "type_client invalide")
            setattr(c, field, data[field])
    db.commit(); db.refresh(c)
    return _client_dict(c)


@router.delete("/clients/{client_id}")
def supprimer_client(client_id: int, db: Session = Depends(get_db)):
    c = db.query(StationClient).filter_by(id=client_id).first()
    if not c:
        raise HTTPException(404, "Client introuvable")
    credits_actifs = [cr for cr in c.credits if cr.statut == "EN_COURS"]
    if credits_actifs:
        raise HTTPException(409, f"Ce client a {len(credits_actifs)} crédit(s) en cours. Soldez-les avant de supprimer.")
    db.delete(c); db.commit()
    return {"message": "Client supprimé"}


# ── Routes CREDITS ────────────────────────────────────────────────────────────

@router.get("/credits")
def liste_credits(
    client_id:   Optional[int] = Query(default=None),
    statut:      Optional[str] = Query(default=None),
    date_debut:  Optional[date_type] = Query(default=None),
    date_fin:    Optional[date_type] = Query(default=None),
    db:          Session = Depends(get_db),
):
    from datetime import time
    q = db.query(StationCredit)
    if client_id:
        q = q.filter(StationCredit.client_id == client_id)
    if statut:
        q = q.filter(StationCredit.statut == statut.upper())
    if date_debut:
        dt_deb = datetime.combine(date_debut, time.min).replace(tzinfo=timezone.utc)
        q = q.filter(StationCredit.date_credit >= dt_deb)
    if date_fin:
        dt_fin = datetime.combine(date_fin, time.max).replace(tzinfo=timezone.utc)
        q = q.filter(StationCredit.date_credit <= dt_fin)
    credits = q.order_by(StationCredit.date_credit.desc()).all()
    return [_credit_dict(cr) for cr in credits]


@router.post("/credits", status_code=201)
def creer_credit(data: CreditIn, db: Session = Depends(get_db)):
    client = db.query(StationClient).filter_by(id=data.client_id, actif=True).first()
    if not client:
        raise HTTPException(404, "Client introuvable ou inactif")
    if data.produit_id:
        if not db.query(Produit).filter_by(id=data.produit_id).first():
            raise HTTPException(404, "Produit introuvable")
    numero = _generer_numero_credit(db)
    cr = StationCredit(
        client_id=data.client_id,
        produit_id=data.produit_id,
        numero=numero,
        montant_total=Decimal(str(data.montant_total)),
        montant_paye=Decimal(0),
        quantite=Decimal(str(data.quantite)) if data.quantite else None,
        prix_unitaire=Decimal(str(data.prix_unitaire)) if data.prix_unitaire else None,
        date_echeance=data.date_echeance,
        notes=data.notes,
    )
    db.add(cr); db.commit(); db.refresh(cr)
    return _credit_dict(cr)


@router.get("/credits/{credit_id}")
def get_credit(credit_id: int, db: Session = Depends(get_db)):
    cr = db.query(StationCredit).filter_by(id=credit_id).first()
    if not cr:
        raise HTTPException(404, "Crédit introuvable")
    return _credit_dict(cr)


@router.put("/credits/{credit_id}")
def modifier_credit(credit_id: int, data: dict, db: Session = Depends(get_db)):
    cr = db.query(StationCredit).filter_by(id=credit_id).first()
    if not cr:
        raise HTTPException(404, "Crédit introuvable")
    for field in ("notes", "date_echeance"):
        if field in data:
            setattr(cr, field, data[field])
    if "statut" in data:
        s = data["statut"].upper()
        if s not in ("EN_COURS", "SOLDE", "ANNULE"):
            raise HTTPException(400, "Statut invalide")
        cr.statut = s
    db.commit(); db.refresh(cr)
    return _credit_dict(cr)


@router.post("/credits/{credit_id}/rembourser")
def rembourser_credit(credit_id: int, data: RemboursementIn, db: Session = Depends(get_db)):
    cr = db.query(StationCredit).filter_by(id=credit_id).first()
    if not cr:
        raise HTTPException(404, "Crédit introuvable")
    if cr.statut != "EN_COURS":
        raise HTTPException(409, f"Ce crédit est déjà {cr.statut.lower()}")

    montant_restant = _dec(cr.montant_total) - _dec(cr.montant_paye)
    montant = _dec(data.montant)
    if montant > montant_restant:
        raise HTTPException(400, f"Le montant dépasse le restant dû ({float(montant_restant):.2f})")

    remb = StationCreditRemboursement(
        credit_id=credit_id,
        montant=montant,
        notes=data.notes,
    )
    db.add(remb)
    cr.montant_paye = _dec(cr.montant_paye) + montant
    if cr.montant_paye >= _dec(cr.montant_total):
        cr.statut = "SOLDE"
    db.commit(); db.refresh(cr)
    return _credit_dict(cr)


@router.delete("/credits/{credit_id}")
def supprimer_credit(credit_id: int, db: Session = Depends(get_db)):
    cr = db.query(StationCredit).filter_by(id=credit_id).first()
    if not cr:
        raise HTTPException(404, "Crédit introuvable")
    if cr.statut == "EN_COURS" and cr.remboursements:
        raise HTTPException(409, "Impossible de supprimer un crédit avec remboursements partiels")
    db.delete(cr); db.commit()
    return {"message": "Crédit supprimé"}


# ── Routes FACTURES ───────────────────────────────────────────────────────────

@router.get("/factures")
def liste_factures(
    client_id:  Optional[int] = Query(default=None),
    statut:     Optional[str] = Query(default=None),
    date_debut: Optional[date_type] = Query(default=None),
    date_fin:   Optional[date_type] = Query(default=None),
    db:         Session = Depends(get_db),
):
    from datetime import time
    q = db.query(StationFacture)
    if client_id:
        q = q.filter(StationFacture.client_id == client_id)
    if statut:
        q = q.filter(StationFacture.statut == statut.upper())
    if date_debut:
        dt_deb = datetime.combine(date_debut, time.min).replace(tzinfo=timezone.utc)
        q = q.filter(StationFacture.date_facture >= dt_deb)
    if date_fin:
        dt_fin = datetime.combine(date_fin, time.max).replace(tzinfo=timezone.utc)
        q = q.filter(StationFacture.date_facture <= dt_fin)
    factures = q.order_by(StationFacture.date_facture.desc()).all()
    return [_facture_dict(f) for f in factures]


@router.post("/factures", status_code=201)
def creer_facture(data: FactureIn, db: Session = Depends(get_db)):
    client = db.query(StationClient).filter_by(id=data.client_id, actif=True).first()
    if not client:
        raise HTTPException(404, "Client introuvable ou inactif")
    if not data.lignes:
        raise HTTPException(400, "La facture doit contenir au moins une ligne")

    lignes_json = []
    montant_ht  = Decimal(0)
    for l in data.lignes:
        sous_total_ht = round(Decimal(str(l.quantite)) * Decimal(str(l.prix_unitaire)), 2)
        montant_ht   += sous_total_ht
        lignes_json.append({
            "description":   l.description,
            "quantite":      l.quantite,
            "prix_unitaire": l.prix_unitaire,
            "tva_pct":       l.tva_pct,
            "sous_total_ht": float(sous_total_ht),
        })

    taux_tva   = Decimal(str(data.taux_tva))
    montant_tva = (montant_ht * taux_tva / 100).quantize(Decimal("0.01"))
    montant_ttc = montant_ht + montant_tva

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
    db.add(f); db.commit(); db.refresh(f)
    return _facture_dict(f)


@router.get("/factures/{facture_id}")
def get_facture(facture_id: int, db: Session = Depends(get_db)):
    f = db.query(StationFacture).filter_by(id=facture_id).first()
    if not f:
        raise HTTPException(404, "Facture introuvable")
    return _facture_dict(f)


@router.put("/factures/{facture_id}")
def modifier_facture(facture_id: int, data: dict, db: Session = Depends(get_db)):
    f = db.query(StationFacture).filter_by(id=facture_id).first()
    if not f:
        raise HTTPException(404, "Facture introuvable")
    if f.statut != "BROUILLON" and "statut" not in data:
        raise HTTPException(409, "Seules les factures en brouillon peuvent être modifiées")
    if "statut" in data:
        s = data["statut"].upper()
        if s not in ("BROUILLON", "ENVOYEE", "PAYEE", "ANNULEE"):
            raise HTTPException(400, "Statut invalide")
        f.statut = s
    for field in ("notes", "date_echeance"):
        if field in data:
            setattr(f, field, data[field])
    db.commit(); db.refresh(f)
    return _facture_dict(f)


@router.post("/factures/{facture_id}/envoyer")
def envoyer_facture(facture_id: int, db: Session = Depends(get_db)):
    f = db.query(StationFacture).filter_by(id=facture_id).first()
    if not f:
        raise HTTPException(404, "Facture introuvable")
    if f.statut == "ANNULEE":
        raise HTTPException(409, "Impossible d'envoyer une facture annulée")
    client = f.client
    if not client or not client.email:
        raise HTTPException(400, "Ce client n'a pas d'adresse email enregistrée")

    try:
        _envoyer_email_facture(f, client)
    except Exception as exc:
        raise HTTPException(500, f"Erreur envoi email : {exc}")

    f.statut          = "ENVOYEE"
    f.email_envoye_at = datetime.now(tz=timezone.utc)
    db.commit(); db.refresh(f)
    return _facture_dict(f)


@router.delete("/factures/{facture_id}")
def supprimer_facture(facture_id: int, db: Session = Depends(get_db)):
    f = db.query(StationFacture).filter_by(id=facture_id).first()
    if not f:
        raise HTTPException(404, "Facture introuvable")
    if f.statut not in ("BROUILLON", "ANNULEE"):
        raise HTTPException(409, "Seules les factures BROUILLON ou ANNULÉE peuvent être supprimées")
    db.delete(f); db.commit()
    return {"message": "Facture supprimée"}


# ── Dashboard CRM ─────────────────────────────────────────────────────────────

@router.get("/dashboard")
def dashboard_crm(db: Session = Depends(get_db)):
    from sqlalchemy import func as sqlfunc

    total_clients  = db.query(StationClient).filter_by(actif=True).count()
    credits_query  = db.query(StationCredit)
    en_cours       = credits_query.filter_by(statut="EN_COURS").all()
    soldes         = credits_query.filter_by(statut="SOLDE").count()
    factures_query = db.query(StationFacture)
    fact_envoyees  = factures_query.filter_by(statut="ENVOYEE").count()
    fact_payees    = factures_query.filter_by(statut="PAYEE").count()

    montant_en_cours = sum(
        float(_dec(cr.montant_total) - _dec(cr.montant_paye)) for cr in en_cours
    )
    return {
        "total_clients":    total_clients,
        "credits_en_cours": len(en_cours),
        "credits_soldes":   soldes,
        "montant_en_cours": round(montant_en_cours, 2),
        "factures_envoyees": fact_envoyees,
        "factures_payees":   fact_payees,
    }


# ── Email facture ─────────────────────────────────────────────────────────────

def _envoyer_email_facture(f: StationFacture, client: StationClient) -> None:
    if not EMAIL_USER or not EMAIL_PASSWORD:
        raise RuntimeError("Email non configuré dans .env")

    html = _build_facture_html(f, client)
    msg = MIMEMultipart("alternative")
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


def _build_facture_html(f: StationFacture, client: StationClient) -> str:
    date_str = f.date_facture.strftime("%d/%m/%Y") if f.date_facture else ""
    echeance_str = f.date_echeance.strftime("%d/%m/%Y") if f.date_echeance else "—"

    lignes_html = ""
    for i, l in enumerate(f.lignes or []):
        bg = "rgba(255,255,255,.03)" if i % 2 == 0 else "transparent"
        lignes_html += f"""
        <tr style="background:{bg}">
          <td style="padding:10px 14px;color:#dde8f8;font-size:13px">{l.get('description','')}</td>
          <td style="padding:10px 14px;text-align:center;color:#dde8f8;font-size:13px">{l.get('quantite','')}</td>
          <td style="padding:10px 14px;text-align:right;color:#dde8f8;font-size:13px">{l.get('prix_unitaire',''):,.2f}</td>
          <td style="padding:10px 14px;text-align:right;font-weight:700;color:#e8c558;font-size:13px">{l.get('sous_total_ht',''):,.2f}</td>
        </tr>"""

    tva_row = ""
    if float(f.taux_tva or 0) > 0:
        tva_row = f"""
        <tr>
          <td colspan="3" style="padding:8px 14px;text-align:right;color:rgba(221,232,248,.55);font-size:12px">TVA ({float(f.taux_tva):.1f}%)</td>
          <td style="padding:8px 14px;text-align:right;color:rgba(221,232,248,.8);font-size:13px">{float(f.montant_tva):,.2f}</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="fr">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#070e1c;font-family:Arial,Helvetica,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#070e1c;padding:36px 16px">
  <tr><td align="center">
    <table width="100%" style="max-width:620px;background:#0b1628;border-radius:14px;border:1px solid rgba(232,197,88,.22);overflow:hidden">

      <!-- En-tête -->
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
              <div style="font-size:13px;color:#e8c558;margin-top:4px;font-family:monospace">{f.numero_facture}</div>
              <div style="font-size:11px;color:rgba(221,232,248,.45);margin-top:4px">Date : {date_str}</div>
              <div style="font-size:11px;color:rgba(221,232,248,.45)">Échéance : {echeance_str}</div>
            </td>
          </tr></table>
        </td>
      </tr>

      <!-- Destinataire -->
      <tr>
        <td style="padding:20px 32px;border-bottom:1px solid rgba(255,255,255,.06)">
          <div style="font-size:10px;font-weight:800;color:rgba(232,197,88,.5);letter-spacing:.15em;text-transform:uppercase;margin-bottom:8px">Facturé à</div>
          <div style="font-size:15px;font-weight:700;color:#fff">{client.nom}</div>
          {'<div style="font-size:12px;color:rgba(221,232,248,.55);margin-top:3px">NIF : ' + client.nif + '</div>' if client.nif else ''}
          {'<div style="font-size:12px;color:rgba(221,232,248,.55);margin-top:2px">' + client.adresse + '</div>' if client.adresse else ''}
          {'<div style="font-size:12px;color:rgba(221,232,248,.55);margin-top:2px">Tél : ' + client.telephone + '</div>' if client.telephone else ''}
          <div style="font-size:12px;color:rgba(221,232,248,.55);margin-top:2px">{client.email}</div>
        </td>
      </tr>

      <!-- Lignes -->
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
            <tbody>
              {lignes_html}
            </tbody>
          </table>
        </td>
      </tr>

      <!-- Totaux -->
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

      <!-- Notes -->
      {'<tr><td style="padding:0 32px 20px"><div style="background:rgba(255,255,255,.03);border-left:3px solid rgba(232,197,88,.3);padding:12px 14px;border-radius:0 6px 6px 0"><div style="font-size:10px;color:rgba(232,197,88,.5);letter-spacing:.1em;text-transform:uppercase;margin-bottom:4px">Notes</div><div style="color:rgba(221,232,248,.65);font-size:12px;line-height:1.6">' + f.notes + '</div></div></td></tr>' if f.notes else ''}

      <!-- Pied -->
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
