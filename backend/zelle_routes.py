"""
Routes API Département Zelle — préfixe /api/zelle
"""
from __future__ import annotations

from datetime import date as date_type, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db
from models import ZelleConfig, ZelleTransaction, ZelleFond, ZelleDepense, Utilisateur

router = APIRouter(prefix="/api/zelle", tags=["zelle"])

SOURCES = ("PDG", "Gaz", "Autre")


# ── Schemas ───────────────────────────────────────────────────────────────────

class ConfigIn(BaseModel):
    taux:              float = Field(..., gt=0)
    balance_avant_usd: float = 0.0  # conservé pour compat, non exposé dans l'UI


class TransactionIn(BaseModel):
    numero_int:          Optional[str] = None
    nom_prenom:          str
    identifiant:         Optional[str] = None
    contact:             Optional[str] = None
    expediteur_nom:      Optional[str] = None
    expediteur_contact:  Optional[str] = None
    montant_usd:         float = Field(..., gt=0)
    frais:               float = Field(0.0, ge=0)
    source_fond:         Optional[str] = None
    date_transaction:    Optional[str] = None
    notes:               Optional[str] = None


class StatutIn(BaseModel):
    statut: str


class FondIn(BaseModel):
    montant_ht:     float = Field(..., gt=0)   # montant en HTG
    taux:           float = Field(..., gt=0)   # taux utilisé pour conversion
    source:         str = "PDG"
    date_reception: Optional[str] = None
    notes:          Optional[str] = None


class ZelleDepenseIn(BaseModel):
    description:   str
    montant:       float = Field(..., gt=0)   # dans la devise choisie (devise)
    devise:        str = "USD"                # "USD" ou "HTG"
    categorie:     Optional[str] = None
    date_depense:  Optional[str] = None
    notes:         Optional[str] = None


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
        "id":                  t.id,
        "numero_int":          t.numero_int,
        "nom_prenom":          t.nom_prenom,
        "identifiant":         t.identifiant,
        "contact":             t.contact,
        "expediteur_nom":      t.expediteur_nom,
        "expediteur_contact":  t.expediteur_contact,
        "montant_usd":         mu,
        "montant_ht":          round(mu * tau, 2),
        "frais":               fr,
        "a_remettre":          round(mu - fr, 2),
        "a_remettre_ht":       round((mu - fr) * tau, 2),
        "taux_applique":       tau,
        "statut":              t.statut,
        "source_fond":         t.source_fond,
        "date_transaction":    t.date_transaction.isoformat() if t.date_transaction else None,
        "notes":               t.notes,
    }


def _fond_dict(f: ZelleFond, taux: float = 1.0) -> dict:
    mu = float(f.montant_usd)
    return {
        "id":             f.id,
        "montant_usd":    mu,
        "montant_ht":     round(mu * taux, 2),
        "source":         f.source,
        "date_reception": f.date_reception.isoformat() if f.date_reception else None,
        "notes":          f.notes,
    }


def _require_pdg_or_admin(request: Request, db: Session = Depends(get_db)) -> Utilisateur:
    """Approbation et rejet d'une dépense Zelle sont ouverts au PDG et aux
    administrateurs. Même logique que main.require_admin pour le volet
    admin, dupliquée ici pour éviter un import circulaire."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(403, "Non autorisé")
    if user.role in ("pdg", "admin"):
        return user
    if user.role_id:
        u = db.get(Utilisateur, user.id)
        if u and u.role_obj and u.role_obj.permissions.get("admin", False):
            return u
    raise HTTPException(403, "Accès réservé au PDG ou à un administrateur")


def _parse_dt(s: str) -> Optional[datetime]:
    """Parse une date/heure saisie par l'utilisateur. Une date SEULE (input
    HTML type="date", ex: "2026-07-15", sans heure) est ramenee a MIDI UTC et
    non minuit : la session Postgres reconvertit les timestamptz dans son
    fuseau horaire (America/Los_Angeles, UTC-7/-8) a la lecture, donc minuit
    UTC redevient 17h/16h la VEILLE en heure locale — la date affichee glisse
    d'un jour en arriere a chaque aller-retour. Midi UTC reste toujours le
    meme jour calendaire quel que soit le decalage horaire reel du serveur."""
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo:
            return dt
        if dt.hour == 0 and dt.minute == 0 and dt.second == 0 and "T" not in s and " " not in s:
            dt = dt.replace(hour=12)
        return dt.replace(tzinfo=timezone.utc)
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

def _zelle_depenses_approuvees_usd(db: Session) -> float:
    return float(
        db.query(func.sum(ZelleDepense.montant_usd))
          .filter(ZelleDepense.statut == "APPROUVEE").scalar() or 0
    )


def _zelle_balance_usd(db: Session) -> float:
    """
    Solde disponible = solde de depart + fonds injectes - montants DEJA remis
    - depenses Zelle APPROUVEES par le PDG.
    Le montant qui sort reellement du fonds est le montant NET remis en cash
    au beneficiaire (a_remettre = montant - frais) : les frais restent acquis
    a l'entreprise, ils ne sont jamais physiquement decaisses du fonds. Les
    transactions EN_ATTENTE (pas encore payees) et les depenses EN_ATTENTE/
    REJETEE (pas encore validees) ne sont jamais melangees dans ce calcul —
    seul le PDG, en approuvant une depense, la fait sortir du solde.
    """
    cfg    = _get_or_create_config(db)
    bal_av = float(cfg.balance_avant_usd)
    total_fonds_usd = float(
        db.query(func.sum(ZelleFond.montant_usd)).scalar() or 0
    )
    total_remis_usd = float(
        db.query(func.sum(ZelleTransaction.montant_usd - ZelleTransaction.frais))
          .filter(ZelleTransaction.statut == "REMIS").scalar() or 0
    )
    total_depenses_usd = _zelle_depenses_approuvees_usd(db)
    return round(bal_av + total_fonds_usd - total_remis_usd - total_depenses_usd, 2)


def _zelle_dep_dict(d: ZelleDepense) -> dict:
    mu  = float(d.montant_usd)
    tau = float(d.taux_applique)
    return {
        "id":             d.id,
        "description":    d.description,
        "montant_usd":    mu,
        "montant_htg":    round(mu * tau, 2),
        "taux_applique":  tau,
        "categorie":      d.categorie,
        "date_depense":   d.date_depense.isoformat() if d.date_depense else None,
        "statut":         d.statut,
        "demandeur_nom":  d.demandeur.nom_complet if d.demandeur else None,
        "valide_par_nom": d.valide_par.nom_complet if d.valide_par else None,
        "valide_at":      d.valide_at.isoformat() if d.valide_at else None,
        "notes":          d.notes,
    }


def _zelle_engage_usd(db: Session, exclude_tx_id: Optional[int] = None) -> float:
    """Somme des transactions EN_ATTENTE — deja reservees sur le solde meme si
    pas encore remises, pour eviter de sur-engager le fonds sur plusieurs
    transactions en parallele. Reserve le montant NET (a_remettre), coherent
    avec ce qui sera reellement decaisse a la remise."""
    q = db.query(func.sum(ZelleTransaction.montant_usd - ZelleTransaction.frais)).filter(
        ZelleTransaction.statut == "EN_ATTENTE"
    )
    if exclude_tx_id is not None:
        q = q.filter(ZelleTransaction.id != exclude_tx_id)
    return float(q.scalar() or 0)


def _zelle_engageable_usd(db: Session, exclude_tx_id: Optional[int] = None) -> float:
    """Ce qui reste reellement disponible pour engager une NOUVELLE transaction :
    solde disponible moins ce qui est deja reserve par les transactions en attente."""
    return round(_zelle_balance_usd(db) - _zelle_engage_usd(db, exclude_tx_id), 2)


@router.get("/bilan")
def get_bilan(db: Session = Depends(get_db)):
    cfg    = _get_or_create_config(db)
    taux   = float(cfg.taux)
    bal_av = float(cfg.balance_avant_usd)

    # Toutes les transactions actives
    txs = db.query(ZelleTransaction).filter(
        ZelleTransaction.statut.in_(["EN_ATTENTE", "REMIS"])
    ).all()

    # Total entre Zelle — indicatif uniquement (demandes en attente + remises),
    # n'entre plus dans le calcul du solde disponible.
    entree_usd = sum(float(t.montant_usd) for t in txs)
    entree_ht  = round(entree_usd * taux, 2)

    remis = [t for t in txs if t.statut == "REMIS"]
    # Montant NET verse en cash au beneficiaire (montant - frais) — les frais
    # restent acquis a l'entreprise, ils ne sortent jamais physiquement du fonds.
    # Suivis separement (total_frais_*) pour le tableau de bord.
    total_remis_usd = round(sum(float(t.montant_usd) - float(t.frais) for t in remis), 2)
    total_remis_ht  = round(total_remis_usd * taux, 2)
    # Frais sur toutes les transactions actives (EN_ATTENTE + REMIS) — informatif
    total_frais_usd = round(sum(float(t.frais) for t in txs), 2)
    total_frais_ht  = round(sum(float(t.frais) * float(t.taux_applique) for t in txs), 2)

    # Fonds reçus
    fonds = db.query(ZelleFond).all()
    total_fonds_usd = round(sum(float(f.montant_usd) for f in fonds), 2)
    total_fonds_ht  = round(total_fonds_usd * taux, 2)

    bal_av_ht        = round(bal_av * taux, 2)
    # Solde disponible : depart + fonds - deja remis (montant NET, hors frais).
    # Evolue uniquement quand un paiement est effectivement remis, jamais avec
    # les transactions en attente. Recalcule via le helper partage pour rester
    # garanti coherent avec la logique de blocage a la creation/mise a jour.
    balance_usd      = _zelle_balance_usd(db)
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

    # Ce qui reste reellement engageable sur une NOUVELLE transaction, une fois
    # deduites les transactions deja en attente (reservees mais pas encore remises).
    montant_engage_usd    = _zelle_engage_usd(db)
    solde_engageable_usd  = round(balance_usd - montant_engage_usd, 2)
    solde_engageable_ht   = round(solde_engageable_usd * taux, 2)

    # Alerte transactions en attente depuis trop longtemps (verification
    # opportuniste a chaque chargement du tableau de bord, pas de cron dedie).
    seuil_attente = datetime.now(timezone.utc) - timedelta(hours=24)
    en_attente_vieilles = [
        t for t in txs
        if t.statut == "EN_ATTENTE"
        and (t.date_transaction.replace(tzinfo=timezone.utc) if t.date_transaction.tzinfo is None else t.date_transaction) < seuil_attente
    ]
    if en_attente_vieilles:
        from notifications_service import creer_notification
        creer_notification(
            db, module="zelle", type_="transactions_en_attente",
            titre=f"{len(en_attente_vieilles)} transaction(s) Zelle en attente depuis plus de 24h",
            message=", ".join(t.nom_prenom for t in en_attente_vieilles[:5])
                    + (f" (+{len(en_attente_vieilles)-5} autres)" if len(en_attente_vieilles) > 5 else ""),
            lien="zelle",
            dedupe_minutes=1440,
        )
        db.commit()

    total_depenses_usd = _zelle_depenses_approuvees_usd(db)
    nb_depenses_attente = (
        db.query(func.count(ZelleDepense.id))
          .filter(ZelleDepense.statut == "EN_ATTENTE").scalar() or 0
    )

    return {
        "taux":                  taux,
        "balance_avant_usd":     bal_av,
        "balance_avant_ht":      bal_av_ht,
        "total_fonds_usd":       total_fonds_usd,
        "total_fonds_ht":        total_fonds_ht,
        "entree_usd":            round(entree_usd, 2),
        "entree_ht":             entree_ht,
        "total_remis_usd":       total_remis_usd,
        "total_remis_ht":        total_remis_ht,
        "total_depenses_usd":    total_depenses_usd,
        "total_depenses_ht":     round(total_depenses_usd * taux, 2),
        "nb_depenses_attente":   nb_depenses_attente,
        "balance_usd":           balance_usd,
        "balance_ht":            balance_ht,
        "montant_engage_usd":    montant_engage_usd,
        "solde_engageable_usd":  solde_engageable_usd,
        "solde_engageable_ht":   solde_engageable_ht,
        "total_frais_usd":       total_frais_usd,
        "total_frais_ht":        total_frais_ht,
        "sources":               sources_data,
    }


# ── Évolution (graphique du tableau de bord) ───────────────────────────────────

@router.get("/evolution")
def evolution_zelle(jours: int = Query(30, ge=7, le=180), db: Session = Depends(get_db)):
    """Série quotidienne — paiements remis, dépenses approuvées, renflouements
    reçus — pour le graphique d'évolution du tableau de bord Zelle."""
    cfg   = _get_or_create_config(db)
    taux  = float(cfg.taux)
    fin   = datetime.now(timezone.utc).date()
    debut = fin - timedelta(days=jours - 1)
    debut_dt = datetime.combine(debut, datetime.min.time()).replace(tzinfo=timezone.utc)

    txs = (
        db.query(ZelleTransaction)
        .filter(ZelleTransaction.statut == "REMIS", ZelleTransaction.date_transaction >= debut_dt)
        .all()
    )
    deps = (
        db.query(ZelleDepense)
        .filter(ZelleDepense.statut == "APPROUVEE", ZelleDepense.valide_at >= debut_dt)
        .all()
    )
    fonds = (
        db.query(ZelleFond)
        .filter(ZelleFond.date_reception >= debut_dt)
        .all()
    )

    par_jour: dict[str, dict] = {}
    d = debut
    while d <= fin:
        par_jour[d.isoformat()] = {"paiements": 0.0, "depenses": 0.0, "renflouements": 0.0}
        d += timedelta(days=1)

    for t in txs:
        k = t.date_transaction.date().isoformat()
        if k in par_jour:
            par_jour[k]["paiements"] += float(t.montant_usd) - float(t.frais)
    for dep in deps:
        if not dep.valide_at:
            continue
        k = dep.valide_at.date().isoformat()
        if k in par_jour:
            par_jour[k]["depenses"] += float(dep.montant_usd)
    for f in fonds:
        k = f.date_reception.date().isoformat()
        if k in par_jour:
            par_jour[k]["renflouements"] += float(f.montant_usd)

    serie = []
    for k in sorted(par_jour.keys()):
        v = par_jour[k]
        serie.append({
            "date":                k,
            "paiements_usd":       round(v["paiements"], 2),
            "paiements_ht":        round(v["paiements"] * taux, 2),
            "depenses_usd":        round(v["depenses"], 2),
            "depenses_ht":         round(v["depenses"] * taux, 2),
            "renflouements_usd":   round(v["renflouements"], 2),
            "renflouements_ht":    round(v["renflouements"] * taux, 2),
        })
    return {"serie": serie, "taux": taux}


# ── Dépenses Zelle (nécessitent validation PDG/admin avant déduction du solde) ──────

@router.get("/depenses")
def lister_zelle_depenses(statut: Optional[str] = None, db: Session = Depends(get_db)):
    q = db.query(ZelleDepense)
    if statut:
        q = q.filter(ZelleDepense.statut == statut)
    deps = q.order_by(ZelleDepense.date_depense.desc()).all()
    from pieces_jointes_routes import compter_pieces_jointes_par_entite
    nb_pj = compter_pieces_jointes_par_entite(db, "zelle_depense", [d.id for d in deps])
    resultats = []
    for d in deps:
        item = _zelle_dep_dict(d)
        item["sans_justificatif"] = nb_pj.get(d.id, 0) == 0
        resultats.append(item)
    return {"depenses": resultats, "nb": len(deps)}


@router.post("/depenses", status_code=201)
def creer_zelle_depense(data: ZelleDepenseIn, request: Request, db: Session = Depends(get_db)):
    if data.devise not in ("USD", "HTG"):
        raise HTTPException(400, "Devise invalide — 'USD' ou 'HTG'.")
    cfg  = _get_or_create_config(db)
    taux = float(cfg.taux)
    montant_usd = round(data.montant, 2) if data.devise == "USD" else round(data.montant / taux, 2)

    user = getattr(request.state, "user", None)
    d = ZelleDepense(
        description=data.description.strip(),
        montant_usd=Decimal(str(montant_usd)),
        taux_applique=Decimal(str(taux)),
        categorie=data.categorie,
        notes=data.notes,
        demandeur_id=user.id if user else None,
    )
    if data.date_depense:
        dt = _parse_dt(data.date_depense)
        if dt:
            d.date_depense = dt
    db.add(d)

    montant_ht = round(montant_usd * taux, 2)
    from notifications_service import creer_notification
    creer_notification(
        db, module="zelle", type_="depense_en_attente",
        titre=f"Dépense Zelle en attente de validation — ${montant_usd:.2f} ({montant_ht:,.0f} G)",
        message=f"{d.description}" + (f" ({d.categorie})" if d.categorie else ""),
        lien="zelle-depenses",
        dedupe_minutes=None,
    )
    from pieces_jointes_routes import notifier_si_sans_justificatif
    notifier_si_sans_justificatif(db, "zelle", d.description, "zelle-depenses")
    db.commit()
    db.refresh(d)
    return {"id": d.id, "message": "Dépense enregistrée, en attente de validation PDG/admin."}


@router.post("/depenses/{depense_id}/approuver")
def approuver_zelle_depense(
    depense_id: int, db: Session = Depends(get_db),
    _user: Utilisateur = Depends(_require_pdg_or_admin),
):
    d = db.query(ZelleDepense).filter(ZelleDepense.id == depense_id).first()
    if not d:
        raise HTTPException(404, "Dépense introuvable.")
    if d.statut != "EN_ATTENTE":
        raise HTTPException(400, f"Cette dépense est déjà {d.statut.lower()}.")
    solde_avant = _zelle_balance_usd(db)
    if float(d.montant_usd) > solde_avant:
        raise HTTPException(
            400,
            f"Solde Zelle insuffisant : ${float(d.montant_usd):.2f} demandé, "
            f"${solde_avant:.2f} disponible.",
        )
    d.statut         = "APPROUVEE"
    d.valide_par_id   = _user.id
    d.valide_at       = datetime.now(timezone.utc)

    from notifications_service import creer_notification
    creer_notification(
        db, module="zelle", type_="depense_approuvee",
        titre=f"Dépense Zelle approuvée — ${float(d.montant_usd):.2f}",
        message=d.description, lien="zelle-depenses", dedupe_minutes=None,
    )
    db.commit()
    return {"message": "Dépense approuvée et déduite du solde Zelle."}


@router.post("/depenses/{depense_id}/rejeter")
def rejeter_zelle_depense(
    depense_id: int, db: Session = Depends(get_db),
    _user: Utilisateur = Depends(_require_pdg_or_admin),
):
    d = db.query(ZelleDepense).filter(ZelleDepense.id == depense_id).first()
    if not d:
        raise HTTPException(404, "Dépense introuvable.")
    if d.statut != "EN_ATTENTE":
        raise HTTPException(400, f"Cette dépense est déjà {d.statut.lower()}.")
    d.statut       = "REJETEE"
    d.valide_par_id = _user.id
    d.valide_at     = datetime.now(timezone.utc)
    db.commit()
    return {"message": "Dépense rejetée."}


@router.delete("/depenses/{depense_id}")
def supprimer_zelle_depense(depense_id: int, db: Session = Depends(get_db)):
    d = db.query(ZelleDepense).filter(ZelleDepense.id == depense_id).first()
    if not d:
        raise HTTPException(404, "Dépense introuvable.")
    if d.statut != "EN_ATTENTE":
        raise HTTPException(400, "Seules les dépenses en attente peuvent être supprimées.")
    db.delete(d)
    db.commit()
    return {"ok": True}


# ── Fonds (réception) ─────────────────────────────────────────────────────────

@router.get("/fonds")
def list_fonds(
    source: Optional[str] = Query(None),
    debut:  Optional[str] = Query(None),
    fin:    Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    cfg  = _get_or_create_config(db)
    taux = float(cfg.taux)
    q = db.query(ZelleFond).order_by(ZelleFond.date_reception.desc())
    if source:
        q = q.filter(ZelleFond.source == source)
    if debut:
        q = q.filter(func.date(ZelleFond.date_reception) >= date_type.fromisoformat(debut))
    if fin:
        q = q.filter(func.date(ZelleFond.date_reception) <= date_type.fromisoformat(fin))
    return [_fond_dict(f, taux) for f in q.all()]


@router.post("/fonds")
def create_fond(data: FondIn, db: Session = Depends(get_db)):
    if data.source not in SOURCES:
        raise HTTPException(status_code=400, detail="Source invalide")
    # montant_ht / taux = montant_usd
    montant_usd = round(data.montant_ht / data.taux, 6)
    f = ZelleFond(
        montant_usd = Decimal(str(montant_usd)),
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
    return _fond_dict(f, data.taux)


@router.put("/fonds/{fond_id}")
def update_fond(fond_id: int, data: FondIn, db: Session = Depends(get_db)):
    f = db.query(ZelleFond).filter_by(id=fond_id).first()
    if not f:
        raise HTTPException(status_code=404, detail="Fond introuvable")
    if data.source not in SOURCES:
        raise HTTPException(status_code=400, detail="Source invalide")
    montant_usd = round(data.montant_ht / data.taux, 6)
    f.montant_usd = Decimal(str(montant_usd))
    f.source      = data.source
    f.notes       = data.notes.strip() if data.notes else None
    if data.date_reception:
        dt = _parse_dt(data.date_reception)
        if dt:
            f.date_reception = dt
    db.commit()
    return _fond_dict(f, data.taux)


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
    # Verification des le debut de la transaction, pas seulement au moment de
    # la remise : bloque la creation si le fond ne suffira pas a la couvrir,
    # en tenant compte des transactions deja en attente sur le meme solde.
    engageable = _zelle_engageable_usd(db)
    # Comparaison sur le montant NET (ce qui sortira reellement du fonds a la
    # remise) — coherent avec engageable/solde qui sont deja calcules net de frais.
    montant_net = float(data.montant_usd) - float(data.frais)
    if montant_net > engageable:
        raise HTTPException(
            status_code=400,
            detail=f"Solde insuffisant pour cette transaction : ${montant_net:.2f} (net) demandé, ${engageable:.2f} disponible.",
        )
    cfg = _get_or_create_config(db)
    sf  = data.source_fond if data.source_fond in SOURCES else None
    t = ZelleTransaction(
        nom_prenom         = data.nom_prenom.strip(),
        identifiant        = data.identifiant.strip()          if data.identifiant        else None,
        contact            = data.contact.strip()              if data.contact            else None,
        expediteur_nom     = data.expediteur_nom.strip()       if data.expediteur_nom     else None,
        expediteur_contact = data.expediteur_contact.strip()   if data.expediteur_contact else None,
        montant_usd        = Decimal(str(data.montant_usd)),
        frais              = Decimal(str(data.frais)),
        taux_applique      = cfg.taux,
        statut             = "EN_ATTENTE",
        source_fond        = sf,
        notes              = data.notes.strip()                if data.notes              else None,
    )
    if data.date_transaction:
        dt = _parse_dt(data.date_transaction)
        if dt:
            t.date_transaction = dt
    db.add(t)
    db.flush()  # attribue l'id auto-incremente sans committer
    # Code interne unique genere automatiquement — jamais saisi manuellement,
    # garanti unique car derive de la cle primaire.
    t.numero_int = f"ZL-{t.id:06d}"

    from notifications_service import creer_notification
    creer_notification(
        db, module="zelle", type_="nouvelle_transaction",
        titre=f"Nouvelle transaction Zelle — {t.nom_prenom}",
        message=f"${t.montant_usd} USD · {t.numero_int}",
        lien="zelle",
        dedupe_minutes=None,
    )

    db.commit()
    db.refresh(t)
    return _tx_dict(t)


@router.put("/transactions/{tx_id}")
def update_transaction(tx_id: int, data: TransactionIn, db: Session = Depends(get_db)):
    t = db.query(ZelleTransaction).filter_by(id=tx_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Transaction introuvable")
    if t.statut == "EN_ATTENTE":
        # Reverifie le solde si le montant est augmente (exclut la reservation
        # actuelle de cette transaction pour ne pas la compter deux fois).
        engageable = _zelle_engageable_usd(db, exclude_tx_id=tx_id)
        montant_net = float(data.montant_usd) - float(data.frais)
        if montant_net > engageable:
            raise HTTPException(
                status_code=400,
                detail=f"Solde insuffisant pour ce montant : ${montant_net:.2f} (net) demandé, ${engageable:.2f} disponible.",
            )
    sf = data.source_fond if data.source_fond in SOURCES else None
    # numero_int n'est jamais modifie ici : genere une seule fois a la creation
    t.nom_prenom         = data.nom_prenom.strip()
    t.identifiant        = data.identifiant.strip()          if data.identifiant        else None
    t.contact            = data.contact.strip()              if data.contact            else None
    t.expediteur_nom     = data.expediteur_nom.strip()       if data.expediteur_nom     else None
    t.expediteur_contact = data.expediteur_contact.strip()   if data.expediteur_contact else None
    t.montant_usd        = Decimal(str(data.montant_usd))
    t.frais              = Decimal(str(data.frais))
    t.source_fond        = sf
    t.notes              = data.notes.strip()                if data.notes              else None
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
    if data.statut == "REMIS" and t.statut != "REMIS":
        solde = _zelle_balance_usd(db)
        montant_net = float(t.montant_usd) - float(t.frais)
        if montant_net > solde:
            raise HTTPException(
                status_code=400,
                detail=f"Solde insuffisant : ${montant_net:.2f} (net) demandé, ${solde:.2f} disponible.",
            )
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
