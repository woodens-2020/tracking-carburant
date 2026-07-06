"""
Routes API Département Zelle — préfixe /api/zelle
"""
from __future__ import annotations

from datetime import date as date_type, datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db
from models import ZelleConfig, ZelleTransaction

router = APIRouter(prefix="/api/zelle", tags=["zelle"])


# ── Schemas ──────────────────────────────────────────────────────────────────

class ConfigIn(BaseModel):
    taux:              float = Field(..., gt=0)
    balance_avant_usd: float = 0.0


class TransactionIn(BaseModel):
    numero_int:       Optional[str] = None
    nom_prenom:       str
    identifiant:      Optional[str] = None
    contact:          Optional[str] = None
    montant_usd:      float = Field(..., gt=0)
    frais:            float = Field(0.0, ge=0)
    date_transaction: Optional[str] = None
    notes:            Optional[str] = None


class StatutIn(BaseModel):
    statut: str


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_or_create_config(db: Session) -> ZelleConfig:
    cfg = db.query(ZelleConfig).first()
    if not cfg:
        cfg = ZelleConfig(taux=130, balance_avant_usd=0)
        db.add(cfg)
        db.commit()
        db.refresh(cfg)
    return cfg


def _tx_dict(t: ZelleTransaction) -> dict:
    mu  = float(t.montant_usd)
    fr  = float(t.frais)
    tau = float(t.taux_applique)
    return {
        "id":               t.id,
        "numero_int":       t.numero_int,
        "nom_prenom":       t.nom_prenom,
        "identifiant":      t.identifiant,
        "contact":          t.contact,
        "montant_usd":      mu,
        "montant_ht":       round(mu * tau, 2),
        "frais":            fr,
        "a_remettre":       round(mu - fr, 2),
        "a_remettre_ht":    round((mu - fr) * tau, 2),
        "taux_applique":    tau,
        "statut":           t.statut,
        "date_transaction": t.date_transaction.isoformat() if t.date_transaction else None,
        "notes":            t.notes,
    }


def _parse_dt(s: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


# ── Config ────────────────────────────────────────────────────────────────────

@router.get("/config")
def get_config(db: Session = Depends(get_db)):
    cfg = _get_or_create_config(db)
    return {
        "taux":              float(cfg.taux),
        "balance_avant_usd": float(cfg.balance_avant_usd),
        "date_maj":          cfg.date_maj.isoformat() if cfg.date_maj else None,
    }


@router.put("/config")
def update_config(data: ConfigIn, db: Session = Depends(get_db)):
    cfg = _get_or_create_config(db)
    cfg.taux              = Decimal(str(data.taux))
    cfg.balance_avant_usd = Decimal(str(data.balance_avant_usd))
    cfg.date_maj          = datetime.now(timezone.utc)
    db.commit()
    return {"ok": True, "taux": float(cfg.taux), "balance_avant_usd": float(cfg.balance_avant_usd)}


# ── Bilan ─────────────────────────────────────────────────────────────────────

@router.get("/bilan")
def get_bilan(db: Session = Depends(get_db)):
    cfg   = _get_or_create_config(db)
    taux  = float(cfg.taux)
    bal_av = float(cfg.balance_avant_usd)

    txs = db.query(ZelleTransaction).filter(
        ZelleTransaction.statut.in_(["EN_ATTENTE", "REMIS"])
    ).all()

    entree_usd = sum(float(t.montant_usd) for t in txs)
    entree_ht  = round(entree_usd * taux, 2)

    remis = [t for t in txs if t.statut == "REMIS"]
    total_paiement_usd = round(sum(float(t.montant_usd) - float(t.frais) for t in remis), 2)
    total_paiement_ht  = round(total_paiement_usd * taux, 2)

    balance_av_ht = round(bal_av * taux, 2)
    balance_usd   = round(bal_av + entree_usd - total_paiement_usd, 2)
    balance_ht    = round(balance_av_ht + entree_ht - total_paiement_ht, 2)

    return {
        "taux":                 taux,
        "balance_avant_usd":    bal_av,
        "balance_avant_ht":     balance_av_ht,
        "entree_usd":           round(entree_usd, 2),
        "entree_ht":            entree_ht,
        "total_paiement_usd":   total_paiement_usd,
        "total_paiement_ht":    total_paiement_ht,
        "balance_usd":          balance_usd,
        "balance_ht":           balance_ht,
    }


# ── Transactions ──────────────────────────────────────────────────────────────

@router.get("/transactions")
def list_transactions(
    statut: Optional[str] = Query(None),
    debut:  Optional[str] = Query(None),
    fin:    Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    q = db.query(ZelleTransaction).order_by(ZelleTransaction.date_transaction.desc())
    if statut:
        q = q.filter(ZelleTransaction.statut == statut)
    if debut:
        q = q.filter(func.date(ZelleTransaction.date_transaction) >= date_type.fromisoformat(debut))
    if fin:
        q = q.filter(func.date(ZelleTransaction.date_transaction) <= date_type.fromisoformat(fin))
    return [_tx_dict(t) for t in q.all()]


@router.post("/transactions")
def create_transaction(data: TransactionIn, db: Session = Depends(get_db)):
    cfg = _get_or_create_config(db)
    t = ZelleTransaction(
        numero_int    = data.numero_int.strip() if data.numero_int else None,
        nom_prenom    = data.nom_prenom.strip(),
        identifiant   = data.identifiant.strip() if data.identifiant else None,
        contact       = data.contact.strip() if data.contact else None,
        montant_usd   = Decimal(str(data.montant_usd)),
        frais         = Decimal(str(data.frais)),
        taux_applique = cfg.taux,
        statut        = "EN_ATTENTE",
        notes         = data.notes.strip() if data.notes else None,
    )
    if data.date_transaction:
        dt = _parse_dt(data.date_transaction)
        if dt:
            t.date_transaction = dt
    db.add(t)
    db.commit()
    db.refresh(t)
    return _tx_dict(t)


@router.put("/transactions/{tx_id}")
def update_transaction(tx_id: int, data: TransactionIn, db: Session = Depends(get_db)):
    t = db.query(ZelleTransaction).filter_by(id=tx_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Transaction introuvable")
    t.numero_int  = data.numero_int.strip()  if data.numero_int  else None
    t.nom_prenom  = data.nom_prenom.strip()
    t.identifiant = data.identifiant.strip() if data.identifiant else None
    t.contact     = data.contact.strip()     if data.contact     else None
    t.montant_usd = Decimal(str(data.montant_usd))
    t.frais       = Decimal(str(data.frais))
    t.notes       = data.notes.strip()       if data.notes       else None
    if data.date_transaction:
        dt = _parse_dt(data.date_transaction)
        if dt:
            t.date_transaction = dt
    db.commit()
    return _tx_dict(t)


@router.patch("/transactions/{tx_id}/statut")
def update_statut(tx_id: int, data: StatutIn, db: Session = Depends(get_db)):
    if data.statut not in ("EN_ATTENTE", "REMIS", "ANNULE"):
        raise HTTPException(status_code=400, detail="Statut invalide")
    t = db.query(ZelleTransaction).filter_by(id=tx_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Transaction introuvable")
    t.statut = data.statut
    db.commit()
    return _tx_dict(t)


@router.delete("/transactions/{tx_id}")
def delete_transaction(tx_id: int, db: Session = Depends(get_db)):
    t = db.query(ZelleTransaction).filter_by(id=tx_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Transaction introuvable")
    db.delete(t)
    db.commit()
    return {"ok": True}
