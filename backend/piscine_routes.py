"""
Département Piscine — vente de cartes d'accès.  Préfixe /api/piscine

  GET/PUT  /config                 prix de la carte (PUT : PDG/admin)
  POST     /ventes                 enregistrer une vente (quantité, lieu, rabais)
  GET      /ventes                 liste filtrable (dates, lieu de vente)
  POST     /ventes/{id}/annuler    annuler une vente (PDG/admin)
  GET      /dashboard              suivi : cartes vendues, CA, rabais, dépenses, caisse
  GET/POST/PUT/DELETE /depenses    dépenses de la Piscine
"""
from __future__ import annotations

from datetime import date as date_type, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from models import PiscineConfig, PiscineVente, PiscineDepense, Utilisateur
from tz_utils import today_haiti, bounds_haiti, HAITI_TZ

router = APIRouter(prefix="/api/piscine", tags=["Piscine"])

LIEUX_VENTE = {"DEVANT": "Devant", "DERRIERE": "Derrière"}
_CENT = Decimal("0.01")


def _d(v) -> Decimal:
    return Decimal(str(v)) if v is not None else Decimal("0")


def _uid(request: Request) -> Optional[int]:
    u = getattr(request.state, "user", None)
    return u.id if u else None


def _require_pdg_ou_admin(request: Request, db: Session = Depends(get_db)) -> Utilisateur:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(403, "Non autorisé")
    if user.role in ("pdg", "admin"):
        return user
    if user.role_id:
        u = db.get(Utilisateur, user.id)
        if u and u.role_obj and u.role_obj.permissions.get("admin", False):
            return u
    raise HTTPException(403, "Accès réservé au PDG et aux administrateurs")


def _prix_carte(db: Session) -> Decimal:
    cfg = db.get(PiscineConfig, 1)
    return _d(cfg.prix_carte) if cfg else Decimal("0")


def _periode(date_debut: Optional[str], date_fin: Optional[str]) -> tuple[date_type, date_type]:
    today = today_haiti()
    try:
        d1 = date_type.fromisoformat(date_debut) if date_debut else today.replace(day=1)
        d2 = date_type.fromisoformat(date_fin) if date_fin else today
    except ValueError:
        raise HTTPException(422, "Dates invalides (AAAA-MM-JJ).")
    if d2 < d1:
        raise HTTPException(422, "date_debut doit précéder date_fin.")
    return d1, d2


def calculer_rabais(brut: Decimal, rabais_type: Optional[str], rabais_valeur) -> tuple[Optional[str], Optional[Decimal], Decimal]:
    """(type, valeur, montant du rabais) — POURCENT : % du brut ; MONTANT : en gourdes."""
    rabais_type = (rabais_type or "").strip().upper() or None
    if rabais_type in ("AUCUN", "NONE"):
        rabais_type = None
    if rabais_type is None or rabais_valeur in (None, ""):
        return None, None, Decimal("0")
    v = _d(rabais_valeur)
    if v < 0:
        raise HTTPException(422, "Le rabais ne peut pas être négatif.")
    if v == 0:
        return None, None, Decimal("0")
    if rabais_type == "POURCENT":
        if v > 100:
            raise HTTPException(422, "Un rabais en pourcentage ne peut pas dépasser 100 %.")
        return rabais_type, v, (brut * v / 100).quantize(_CENT, rounding=ROUND_HALF_UP)
    if rabais_type == "MONTANT":
        if v > brut:
            raise HTTPException(422, f"Le rabais ({v:,.2f} G) dépasse le montant ({brut:,.2f} G).")
        return rabais_type, v, v.quantize(_CENT)
    raise HTTPException(422, "Type de rabais invalide (MONTANT ou POURCENT).")


# ── Paramètres ────────────────────────────────────────────────────

class ConfigIn(BaseModel):
    prix_carte: float = Field(gt=0)


@router.get("/config")
def lire_config(db: Session = Depends(get_db)):
    cfg = db.get(PiscineConfig, 1)
    return {
        "prix_carte": float(_prix_carte(db)),
        "updated_at": cfg.updated_at.isoformat() if cfg and cfg.updated_at else None,
        "updated_by": cfg.updated_by if cfg else None,
    }


@router.put("/config")
def modifier_config(data: ConfigIn, db: Session = Depends(get_db),
                    user: Utilisateur = Depends(_require_pdg_ou_admin)):
    cfg = db.get(PiscineConfig, 1)
    if not cfg:
        cfg = PiscineConfig(id=1)
        db.add(cfg)
    cfg.prix_carte = Decimal(str(data.prix_carte)).quantize(_CENT)
    cfg.updated_at = datetime.now(timezone.utc)
    cfg.updated_by = user.nom_complet
    db.commit()
    return lire_config(db)


# ── Ventes de cartes ──────────────────────────────────────────────

class VenteIn(BaseModel):
    quantite:      int = Field(gt=0)
    lieu_vente:    Optional[str] = None     # DEVANT | DERRIERE | vide
    rabais_type:   Optional[str] = None     # MONTANT | POURCENT | vide
    rabais_valeur: Optional[float] = None
    client_nom:    Optional[str] = None
    notes:         Optional[str] = None
    devise:        Optional[str] = "HTG"    # HTG | USD (voir devise.py)
    taux_change:   Optional[float] = None


def _vente_dict(v: PiscineVente) -> dict:
    return {
        "id":             v.id,
        "date_vente":     v.date_vente.isoformat() if v.date_vente else None,
        "quantite":       v.quantite,
        "prix_unitaire":  float(v.prix_unitaire),
        "montant_brut":   float(v.montant_brut),
        "rabais_type":    v.rabais_type,
        "rabais_valeur":  float(v.rabais_valeur) if v.rabais_valeur is not None else None,
        "rabais_montant": float(v.rabais_montant),
        "montant_net":    float(v.montant_net),
        "lieu_vente":     v.lieu_vente,
        "lieu_libelle":   LIEUX_VENTE.get(v.lieu_vente, "Non précisé"),
        "client_nom":     v.client_nom,
        "notes":          v.notes,
        "statut":         v.statut,
        "devise":         v.devise or "HTG",
        "montant_devise": float(v.montant_devise) if v.montant_devise is not None else float(v.montant_net),
        "taux_change":    float(v.taux_change) if v.taux_change is not None else None,
        "vendeur":        v.utilisateur.nom_complet if v.utilisateur else None,
    }


@router.post("/ventes", status_code=201)
def enregistrer_vente(data: VenteIn, request: Request, db: Session = Depends(get_db)):
    prix = _prix_carte(db)
    if prix <= 0:
        raise HTTPException(422, "Le prix de la carte n'est pas encore défini (Piscine › Paramètres).")
    lieu = (data.lieu_vente or "").strip().upper() or None
    if lieu and lieu not in LIEUX_VENTE:
        raise HTTPException(422, "Lieu de vente : DEVANT, DERRIERE ou vide.")
    brut = (prix * data.quantite).quantize(_CENT)
    r_type, r_val, r_mt = calculer_rabais(brut, data.rabais_type, data.rabais_valeur)
    import devise as dv
    pay = dv.paiement_vente(brut - r_mt, data.devise, data.taux_change, db)
    v = PiscineVente(
        **pay,
        quantite=data.quantite, prix_unitaire=prix, montant_brut=brut,
        rabais_type=r_type, rabais_valeur=r_val, rabais_montant=r_mt,
        montant_net=brut - r_mt, lieu_vente=lieu,
        client_nom=(data.client_nom or "").strip() or None,
        notes=(data.notes or "").strip() or None,
        utilisateur_id=_uid(request),
    )
    db.add(v)
    db.commit()
    db.refresh(v)
    return _vente_dict(v)


def _ventes_periode(db: Session, d1: date_type, d2: date_type, lieu: Optional[str] = None,
                    avec_annulees: bool = False):
    q = db.query(PiscineVente).filter(
        PiscineVente.date_vente >= bounds_haiti(d1)[0],
        PiscineVente.date_vente <= bounds_haiti(d2)[1],
    )
    if not avec_annulees:
        q = q.filter(PiscineVente.statut == "VALIDEE")
    if lieu:
        lieu = lieu.upper()
        q = q.filter(PiscineVente.lieu_vente.is_(None)) if lieu == "NON_PRECISE" else q.filter(PiscineVente.lieu_vente == lieu)
    return q.order_by(PiscineVente.date_vente.desc()).all()


@router.get("/ventes")
def liste_ventes(
    date_debut: Optional[str] = Query(None), date_fin: Optional[str] = Query(None),
    lieu_vente: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    d1, d2 = _periode(date_debut, date_fin)
    ventes = _ventes_periode(db, d1, d2, lieu_vente, avec_annulees=True)
    valides = [v for v in ventes if v.statut == "VALIDEE"]
    return {
        "periode":        {"debut": str(d1), "fin": str(d2)},
        "nb_cartes":      sum(v.quantite for v in valides),
        "montant_net":    round(sum(float(v.montant_net) for v in valides), 2),
        "recu_htg":       round(sum(float(v.montant_net) for v in valides if (v.devise or "HTG") != "USD"), 2),
        "recu_usd":       round(sum(float(v.montant_devise or 0) for v in valides if v.devise == "USD"), 2),
        "ventes":         [_vente_dict(v) for v in ventes],
    }


@router.post("/ventes/{vente_id}/annuler")
def annuler_vente(vente_id: int, db: Session = Depends(get_db),
                  _u: Utilisateur = Depends(_require_pdg_ou_admin)):
    v = db.get(PiscineVente, vente_id)
    if not v:
        raise HTTPException(404, "Vente introuvable.")
    if v.statut == "ANNULEE":
        raise HTTPException(409, "Vente déjà annulée.")
    v.statut = "ANNULEE"
    db.commit()
    return _vente_dict(v)


# ── Tableau de bord ───────────────────────────────────────────────

def _depenses_periode(db: Session, d1: date_type, d2: date_type):
    return (db.query(PiscineDepense)
            .filter(PiscineDepense.date_depense >= bounds_haiti(d1)[0],
                    PiscineDepense.date_depense <= bounds_haiti(d2)[1])
            .order_by(PiscineDepense.date_depense.desc()).all())


def _resume(ventes, depenses) -> dict:
    cartes  = sum(v.quantite for v in ventes)
    brut    = sum(float(v.montant_brut) for v in ventes)
    rabais  = sum(float(v.rabais_montant) for v in ventes)
    net     = sum(float(v.montant_net) for v in ventes)
    dep     = sum(float(d.montant) for d in depenses)
    # Gourdes et dollars reçus, séparés (la caisse gourdes n'inclut pas les $).
    net_htg = sum(float(v.montant_net) for v in ventes if (v.devise or "HTG") != "USD")
    net_usd = sum(float(v.montant_devise or 0) for v in ventes if v.devise == "USD")
    return {
        "nb_ventes":     len(ventes),
        "nb_cartes":     cartes,
        "montant_brut":  round(brut, 2),
        "rabais":        round(rabais, 2),
        "montant_net":   round(net, 2),
        "recu_htg":      round(net_htg, 2),
        "recu_usd":      round(net_usd, 2),
        "depenses":      round(dep, 2),
        "solde":         round(net - dep, 2),
        "solde_htg":     round(net_htg - dep, 2),
    }


@router.get("/dashboard")
def tableau_de_bord(date_debut: Optional[str] = Query(None), date_fin: Optional[str] = Query(None),
                    db: Session = Depends(get_db)):
    d1, d2 = _periode(date_debut, date_fin)
    ventes   = _ventes_periode(db, d1, d2)
    depenses = _depenses_periode(db, d1, d2)

    nb_jours = (d2 - d1).days + 1
    p_fin = d1 - timedelta(days=1)
    p_deb = p_fin - timedelta(days=nb_jours - 1)
    prec = _resume(_ventes_periode(db, p_deb, p_fin), _depenses_periode(db, p_deb, p_fin))

    today = today_haiti()
    du_jour = _ventes_periode(db, today, today)

    par_lieu = {}
    for code, lib in list(LIEUX_VENTE.items()) + [(None, "Non précisé")]:
        vs = [v for v in ventes if v.lieu_vente == code]
        par_lieu[code or "NON_PRECISE"] = {
            "libelle": lib, "nb_cartes": sum(v.quantite for v in vs),
            "montant_net": round(sum(float(v.montant_net) for v in vs), 2),
        }

    jours: dict[str, dict] = {}
    cur = d1
    while cur <= d2:
        jours[cur.isoformat()] = {"date": cur.isoformat(), "nb_cartes": 0, "montant_net": 0.0, "depenses": 0.0}
        cur += timedelta(days=1)
    for v in ventes:
        k = v.date_vente.astimezone(HAITI_TZ).date().isoformat()
        if k in jours:
            jours[k]["nb_cartes"] += v.quantite
            jours[k]["montant_net"] = round(jours[k]["montant_net"] + float(v.montant_net), 2)
    for dpn in depenses:
        k = dpn.date_depense.astimezone(HAITI_TZ).date().isoformat()
        if k in jours:
            jours[k]["depenses"] = round(jours[k]["depenses"] + float(dpn.montant), 2)

    return {
        "periode":    {"debut": str(d1), "fin": str(d2)},
        "prix_carte": float(_prix_carte(db)),
        "kpi":        _resume(ventes, depenses),
        "precedent":  prec,
        "aujourd_hui": {"nb_cartes": sum(v.quantite for v in du_jour),
                        "montant_net": round(sum(float(v.montant_net) for v in du_jour), 2)},
        "par_lieu":   par_lieu,
        "evolution":  list(jours.values()),
        "recentes":   [_vente_dict(v) for v in ventes[:15]],
    }


# ── Dépenses ──────────────────────────────────────────────────────

class DepenseIn(BaseModel):
    description:  str
    montant:      float = Field(gt=0)
    categorie:    Optional[str] = None
    fournisseur:  Optional[str] = None
    notes:        Optional[str] = None
    date_depense: Optional[date_type] = None


def _depense_dict(d: PiscineDepense) -> dict:
    return {
        "id": d.id, "lieu": "PISCINE", "departement": "Piscine",
        "description": d.description, "categorie": d.categorie,
        "montant": float(d.montant),
        "date_depense": d.date_depense.isoformat() if d.date_depense else None,
        "fournisseur": d.fournisseur, "notes": d.notes,
        "enregistre_par": d.utilisateur.nom_complet if d.utilisateur else None,
    }


def _date_dep(d: Optional[date_type]) -> datetime:
    if not d:
        return datetime.now(timezone.utc)
    return datetime.combine(d, time(12, 0), tzinfo=HAITI_TZ)


@router.get("/depenses")
def liste_depenses(date_debut: Optional[str] = Query(None), date_fin: Optional[str] = Query(None),
                   db: Session = Depends(get_db)):
    d1, d2 = _periode(date_debut, date_fin)
    rows = _depenses_periode(db, d1, d2)
    par_cat: dict[str, float] = {}
    for d in rows:
        k = d.categorie or "Autre"
        par_cat[k] = round(par_cat.get(k, 0.0) + float(d.montant), 2)
    return {
        "departement": "Piscine",
        "total": round(sum(float(d.montant) for d in rows), 2),
        "nb": len(rows),
        "par_categorie": [{"categorie": k, "montant": v} for k, v in sorted(par_cat.items(), key=lambda x: -x[1])],
        "depenses": [_depense_dict(d) for d in rows],
    }


@router.post("/depenses", status_code=201)
def creer_depense(data: DepenseIn, request: Request, db: Session = Depends(get_db)):
    if not data.description.strip():
        raise HTTPException(422, "Description requise.")
    d = PiscineDepense(
        description=data.description.strip(),
        categorie=(data.categorie or "").strip() or None,
        montant=Decimal(str(data.montant)).quantize(_CENT),
        date_depense=_date_dep(data.date_depense),
        fournisseur=(data.fournisseur or "").strip() or None,
        notes=(data.notes or "").strip() or None,
        utilisateur_id=_uid(request),
    )
    db.add(d)
    db.commit()
    db.refresh(d)
    return _depense_dict(d)


@router.put("/depenses/{dep_id}")
def modifier_depense(dep_id: int, data: DepenseIn, db: Session = Depends(get_db)):
    d = db.get(PiscineDepense, dep_id)
    if not d:
        raise HTTPException(404, "Dépense introuvable.")
    if not data.description.strip():
        raise HTTPException(422, "Description requise.")
    d.description = data.description.strip()
    d.categorie   = (data.categorie or "").strip() or None
    d.montant     = Decimal(str(data.montant)).quantize(_CENT)
    if data.date_depense:
        d.date_depense = _date_dep(data.date_depense)
    d.fournisseur = (data.fournisseur or "").strip() or None
    d.notes       = (data.notes or "").strip() or None
    db.commit()
    db.refresh(d)
    return _depense_dict(d)


@router.delete("/depenses/{dep_id}")
def supprimer_depense(dep_id: int, db: Session = Depends(get_db),
                      _u: Utilisateur = Depends(_require_pdg_ou_admin)):
    d = db.get(PiscineDepense, dep_id)
    if not d:
        raise HTTPException(404, "Dépense introuvable.")
    db.delete(d)
    db.commit()
    return {"ok": True, "id": dep_id}
