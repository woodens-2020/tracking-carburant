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
from models import ZelleConfig, ZelleTransaction, ZelleFond

router = APIRouter(prefix="/api/zelle", tags=["zelle"])

SOURCES = ("PDG", "Gaz", "Autre")


# ── Schemas ───────────────────────────────────────────────────────────────────

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
    source_fond:      Optional[str] = None
    date_transaction: Optional[str] = None
    notes:            Optional[str] = None


class StatutIn(BaseModel):
    statut: str


class FondIn(BaseModel):
    montant_usd:    float = Field(..., gt=0)
    source:         str = "PDG"
    date_reception: Optional[str] = None
    notes:          Optional[str] = None


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
        "source_fond":      t.source_fond,
        "date_transaction": t.date_transaction.isoformat() if t.date_transaction else None,
        "notes":            t.notes,
    }


def _fond_dict(f: ZelleFond) -> dict:
    return {
        "id":             f.id,
        "montant_usd":    float(f.montant_usd),
        "source":         f.source,
        "date_reception": f.date_reception.isoformat() if f.date_reception else None,
        "notes":          f.notes,
    }


def _parse_dt(s: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
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


# ── Bilan complet ─────────────────────────────────────────────────────────────

@router.get("/bilan")
def get_bilan(db: Session = Depends(get_db)):
    cfg    = _get_or_create_config(db)
    taux   = float(cfg.taux)
    bal_av = float(cfg.balance_avant_usd)

    # Toutes les transactions actives
    txs = db.query(ZelleTransaction).filter(
        ZelleTransaction.statut.in_(["EN_ATTENTE", "REMIS"])
    ).all()

    entree_usd = sum(float(t.montant_usd) for t in txs)
    entree_ht  = round(entree_usd * taux, 2)

    remis = [t for t in txs if t.statut == "REMIS"]
    total_remis_usd = round(sum(float(t.montant_usd) - float(t.frais) for t in remis), 2)
    total_remis_ht  = round(total_remis_usd * taux, 2)

    # Fonds reçus
    fonds = db.query(ZelleFond).all()
    total_fonds_usd = round(sum(float(f.montant_usd) for f in fonds), 2)
    total_fonds_ht  = round(total_fonds_usd * taux, 2)

    bal_av_ht        = round(bal_av * taux, 2)
    balance_usd      = round(bal_av + total_fonds_usd + entree_usd - total_remis_usd, 2)
    balance_ht       = round(balance_usd * taux, 2)

    # Répartition par source
    sources_data: dict[str, dict] = {}
    for src in SOURCES:
        fonds_src  = round(sum(float(f.montant_usd) for f in fonds if f.source == src), 2)
        remis_src  = round(sum(float(t.montant_usd) - float(t.frais) for t in remis if t.source_fond == src), 2)
        sources_data[src] = {
            "fonds_usd": fonds_src,
            "fonds_ht":  round(fonds_src * taux, 2),
            "remis_usd": remis_src,
            "remis_ht":  round(remis_src * taux, 2),
            "net_usd":   round(fonds_src - remis_src, 2),
            "net_ht":    round((fonds_src - remis_src) * taux, 2),
        }

    return {
        "taux":               taux,
        "balance_avant_usd":  bal_av,
        "balance_avant_ht":   bal_av_ht,
        "total_fonds_usd":    total_fonds_usd,
        "total_fonds_ht":     total_fonds_ht,
        "entree_usd":         round(entree_usd, 2),
        "entree_ht":          entree_ht,
        "total_remis_usd":    total_remis_usd,
        "total_remis_ht":     total_remis_ht,
        "balance_usd":        balance_usd,
        "balance_ht":         balance_ht,
        "sources":            sources_data,
    }


# ── Fonds (réception) ─────────────────────────────────────────────────────────

@router.get("/fonds")
def list_fonds(
    source: Optional[str] = Query(None),
    debut:  Optional[str] = Query(None),
    fin:    Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    q = db.query(ZelleFond).order_by(ZelleFond.date_reception.desc())
    if source:
        q = q.filter(ZelleFond.source == source)
    if debut:
        q = q.filter(func.date(ZelleFond.date_reception) >= date_type.fromisoformat(debut))
    if fin:
        q = q.filter(func.date(ZelleFond.date_reception) <= date_type.fromisoformat(fin))
    return [_fond_dict(f) for f in q.all()]


@router.post("/fonds")
def create_fond(data: FondIn, db: Session = Depends(get_db)):
    if data.source not in SOURCES:
        raise HTTPException(status_code=400, detail="Source invalide")
    f = ZelleFond(
        montant_usd = Decimal(str(data.montant_usd)),
        source      = data.source,
        notes       = data.notes.strip() if data.notes else None,
    )
    if data.date_reception:
        dt = _parse_dt(data.date_reception)
        if dt:
            f.date_reception = dt
    db.add(f)
    db.commit()
    db.refresh(f)
    return _fond_dict(f)


@router.put("/fonds/{fond_id}")
def update_fond(fond_id: int, data: FondIn, db: Session = Depends(get_db)):
    f = db.query(ZelleFond).filter_by(id=fond_id).first()
    if not f:
        raise HTTPException(status_code=404, detail="Fond introuvable")
    if data.source not in SOURCES:
        raise HTTPException(status_code=400, detail="Source invalide")
    f.montant_usd = Decimal(str(data.montant_usd))
    f.source      = data.source
    f.notes       = data.notes.strip() if data.notes else None
    if data.date_reception:
        dt = _parse_dt(data.date_reception)
        if dt:
            f.date_reception = dt
    db.commit()
    return _fond_dict(f)


@router.delete("/fonds/{fond_id}")
def delete_fond(fond_id: int, db: Session = Depends(get_db)):
    f = db.query(ZelleFond).filter_by(id=fond_id).first()
    if not f:
        raise HTTPException(status_code=404, detail="Fond introuvable")
    db.delete(f)
    db.commit()
    return {"ok": True}


# ── Transactions ──────────────────────────────────────────────────────────────

@router.get("/transactions")
def list_transactions(
    statut: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    debut:  Optional[str] = Query(None),
    fin:    Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    q = db.query(ZelleTransaction).order_by(ZelleTransaction.date_transaction.desc())
    if statut:
        q = q.filter(ZelleTransaction.statut == statut)
    if source:
        q = q.filter(ZelleTransaction.source_fond == source)
    if debut:
        q = q.filter(func.date(ZelleTransaction.date_transaction) >= date_type.fromisoformat(debut))
    if fin:
        q = q.filter(func.date(ZelleTransaction.date_transaction) <= date_type.fromisoformat(fin))
    return [_tx_dict(t) for t in q.all()]


@router.post("/transactions")
def create_transaction(data: TransactionIn, db: Session = Depends(get_db)):
    cfg = _get_or_create_config(db)
    sf  = data.source_fond if data.source_fond in SOURCES else None
    t = ZelleTransaction(
        numero_int    = data.numero_int.strip()    if data.numero_int    else None,
        nom_prenom    = data.nom_prenom.strip(),
        identifiant   = data.identifiant.strip()   if data.identifiant   else None,
        contact       = data.contact.strip()       if data.contact       else None,
        montant_usd   = Decimal(str(data.montant_usd)),
        frais         = Decimal(str(data.frais)),
        taux_applique = cfg.taux,
        statut        = "EN_ATTENTE",
        source_fond   = sf,
        notes         = data.notes.strip()         if data.notes         else None,
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
    sf = data.source_fond if data.source_fond in SOURCES else None
    t.numero_int  = data.numero_int.strip()    if data.numero_int    else None
    t.nom_prenom  = data.nom_prenom.strip()
    t.identifiant = data.identifiant.strip()   if data.identifiant   else None
    t.contact     = data.contact.strip()       if data.contact       else None
    t.montant_usd = Decimal(str(data.montant_usd))
    t.frais       = Decimal(str(data.frais))
    t.source_fond = sf
    t.notes       = data.notes.strip()         if data.notes         else None
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
