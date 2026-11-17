"""
Département Piscine — vente de cartes d'accès.  Préfixe /api/piscine

  GET/PUT  /config                 prix des cartes Adulte / Jeune / Enfant (PUT : PDG/admin)
  POST     /ventes                 enregistrer une vente (cartes par catégorie, lieu, rabais)
  GET      /ventes                 liste filtrable (dates, lieu de vente)
  PUT      /ventes/{id}            corriger une erreur de saisie (PDG/admin, motif obligatoire)
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


# Catégories de cartes : (code, libellé, colonne quantité, colonne prix).
CATEGORIES = [("ADULTE", "Adulte"), ("JEUNE", "Jeune"), ("ENFANT", "Enfant")]


def _prix_categories(db: Session) -> dict[str, Decimal]:
    """Prix actuel de chaque catégorie. Adulte non renseigné = l'ancien prix
    unique de la carte (prix_carte)."""
    cfg = db.get(PiscineConfig, 1)
    if not cfg:
        return {c: Decimal("0") for c, _ in CATEGORIES}
    return {
        "ADULTE": _d(cfg.prix_adulte) if cfg.prix_adulte else _d(cfg.prix_carte),
        "JEUNE":  _d(cfg.prix_jeune),
        "ENFANT": _d(cfg.prix_enfant),
    }


def _prix_carte(db: Session) -> Decimal:
    return _prix_categories(db)["ADULTE"]


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
    prix_adulte: Optional[float] = Field(default=None, ge=0)
    prix_jeune:  Optional[float] = Field(default=None, ge=0)
    prix_enfant: Optional[float] = Field(default=None, ge=0)
    prix_carte:  Optional[float] = Field(default=None, gt=0)   # ancien champ = prix Adulte


@router.get("/config")
def lire_config(db: Session = Depends(get_db)):
    cfg = db.get(PiscineConfig, 1)
    prix = _prix_categories(db)
    return {
        "prix_carte":  float(prix["ADULTE"]),
        "prix_adulte": float(prix["ADULTE"]),
        "prix_jeune":  float(prix["JEUNE"]),
        "prix_enfant": float(prix["ENFANT"]),
        "categories":  [{"code": c, "libelle": l, "prix": float(prix[c])} for c, l in CATEGORIES],
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
    adulte = data.prix_adulte if data.prix_adulte is not None else data.prix_carte
    if adulte is not None:
        if adulte <= 0:
            raise HTTPException(422, "Le prix Adulte doit être supérieur à 0.")
        cfg.prix_adulte = cfg.prix_carte = Decimal(str(adulte)).quantize(_CENT)
    if data.prix_jeune is not None:
        cfg.prix_jeune = Decimal(str(data.prix_jeune)).quantize(_CENT) if data.prix_jeune > 0 else None
    if data.prix_enfant is not None:
        cfg.prix_enfant = Decimal(str(data.prix_enfant)).quantize(_CENT) if data.prix_enfant > 0 else None
    if not cfg.prix_carte:
        raise HTTPException(422, "Définissez au moins le prix Adulte.")
    cfg.updated_at = datetime.now(timezone.utc)
    cfg.updated_by = user.nom_complet
    db.commit()
    return lire_config(db)


# ── Ventes de cartes ──────────────────────────────────────────────

class VenteIn(BaseModel):
    # Cartes par catégorie ; `quantite` seul (ancien écran) = cartes Adulte.
    adulte:        int = Field(default=0, ge=0)
    jeune:         int = Field(default=0, ge=0)
    enfant:        int = Field(default=0, ge=0)
    quantite:      Optional[int] = Field(default=None, gt=0)
    lieu_vente:    Optional[str] = None     # DEVANT | DERRIERE | vide
    rabais_type:   Optional[str] = None     # MONTANT | POURCENT | vide
    rabais_valeur: Optional[float] = None
    client_nom:    Optional[str] = None
    notes:         Optional[str] = None
    devise:        Optional[str] = "HTG"    # HTG | USD (voir devise.py)
    taux_change:   Optional[float] = None


def _qtes(v: PiscineVente) -> dict[str, int]:
    return {"ADULTE": v.qte_adulte or 0, "JEUNE": v.qte_jeune or 0, "ENFANT": v.qte_enfant or 0}


def _vente_dict(v: PiscineVente) -> dict:
    q = _qtes(v)
    classee = any(q.values())
    prix_v = {"ADULTE": v.prix_adulte, "JEUNE": v.prix_jeune, "ENFANT": v.prix_enfant}
    return {
        "id":             v.id,
        "categories":     ([{"code": c, "libelle": l, "quantite": q[c],
                             "prix": float(prix_v[c]) if prix_v[c] is not None else None,
                             "montant": float((_d(prix_v[c]) * q[c]).quantize(_CENT))}
                            for c, l in CATEGORIES if q[c]] if classee else []),
        "detail":         (" · ".join(f"{q[c]} {l}" for c, l in CATEGORIES if q[c]) if classee
                           else f"{v.quantite} carte(s) (non classée)"),
        "corrige_le":     v.corrige_le.isoformat() if v.corrige_le else None,
        "corrige_par":    v.corrige_par,
        "motif_correction": v.motif_correction,
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


def _appliquer(v: PiscineVente, data: VenteIn, prix: dict[str, Decimal], db: Session):
    """Calcule et écrit une vente : cartes par catégorie × prix, rabais,
    devise. `prix` = prix figés (vente) ou actuels (nouvelle vente)."""
    qtes = {"ADULTE": data.adulte, "JEUNE": data.jeune, "ENFANT": data.enfant}
    if not any(qtes.values()) and data.quantite:
        qtes["ADULTE"] = data.quantite
    total_cartes = sum(qtes.values())
    if total_cartes <= 0:
        raise HTTPException(422, "Indiquez le nombre de cartes (Adulte, Jeune ou Enfant).")
    for c, l in CATEGORIES:
        if qtes[c] and _d(prix.get(c)) <= 0:
            raise HTTPException(422, f"Le prix de la carte {l} n'est pas défini (Piscine › Paramètres).")
    lieu = (data.lieu_vente or "").strip().upper() or None
    if lieu and lieu not in LIEUX_VENTE:
        raise HTTPException(422, "Lieu de vente : DEVANT, DERRIERE ou vide.")
    brut = sum((_d(prix[c]) * qtes[c] for c in qtes), Decimal("0")).quantize(_CENT)
    r_type, r_val, r_mt = calculer_rabais(brut, data.rabais_type, data.rabais_valeur)
    import devise as dv
    pay = dv.paiement_vente(brut - r_mt, data.devise, data.taux_change, db)
    for k, val in pay.items():
        setattr(v, k, val)
    v.quantite = total_cartes
    v.prix_unitaire = (brut / total_cartes).quantize(_CENT)
    v.montant_brut = brut
    v.rabais_type, v.rabais_valeur, v.rabais_montant = r_type, r_val, r_mt
    v.montant_net = brut - r_mt
    v.lieu_vente = lieu
    v.client_nom = (data.client_nom or "").strip() or None
    v.notes = (data.notes or "").strip() or None
    v.qte_adulte, v.qte_jeune, v.qte_enfant = qtes["ADULTE"] or None, qtes["JEUNE"] or None, qtes["ENFANT"] or None
    v.prix_adulte = _d(prix["ADULTE"]) if qtes["ADULTE"] else None
    v.prix_jeune  = _d(prix["JEUNE"]) if qtes["JEUNE"] else None
    v.prix_enfant = _d(prix["ENFANT"]) if qtes["ENFANT"] else None


@router.post("/ventes", status_code=201)
def enregistrer_vente(data: VenteIn, request: Request, db: Session = Depends(get_db)):
    v = PiscineVente(utilisateur_id=_uid(request))
    _appliquer(v, data, _prix_categories(db), db)
    db.add(v)
    db.commit()
    db.refresh(v)
    return _vente_dict(v)


class CorrectionIn(VenteIn):
    motif: str


@router.put("/ventes/{vente_id}")
def corriger_vente(vente_id: int, data: CorrectionIn, db: Session = Depends(get_db),
                   user: Utilisateur = Depends(_require_pdg_ou_admin)):
    """Corriger une erreur de saisie d'une caissière (administrateur
    seulement, motif obligatoire). Les prix restent ceux du jour de la vente ;
    l'ancienne version est gardée au journal d'activité."""
    v = db.get(PiscineVente, vente_id)
    if not v:
        raise HTTPException(404, "Vente introuvable.")
    if v.statut == "ANNULEE":
        raise HTTPException(409, "Vente annulée — elle ne peut plus être corrigée.")
    motif = (data.motif or "").strip()
    if not motif:
        raise HTTPException(422, "Indiquez le motif de la correction.")
    avant = _vente_dict(v)
    actuels = _prix_categories(db)
    prix = {
        "ADULTE": v.prix_adulte if v.prix_adulte is not None else (v.prix_unitaire if not any(_qtes(v).values()) else actuels["ADULTE"]),
        "JEUNE":  v.prix_jeune if v.prix_jeune is not None else actuels["JEUNE"],
        "ENFANT": v.prix_enfant if v.prix_enfant is not None else actuels["ENFANT"],
    }
    _appliquer(v, data, prix, db)
    v.corrige_le = datetime.now(timezone.utc)
    v.corrige_par = user.nom_complet
    v.motif_correction = motif[:300]
    db.flush()
    apres = _vente_dict(v)
    from activity_log import log_event
    try:
        log_event(db, "piscine_vente_corrigee", user_id=user.id, details={
            "vente_id": v.id, "motif": motif,
            "avant": {k: avant[k] for k in ("detail", "montant_net", "rabais_montant", "lieu_vente", "devise", "montant_devise")},
            "apres": {k: apres[k] for k in ("detail", "montant_net", "rabais_montant", "lieu_vente", "devise", "montant_devise")},
        })
    except Exception:
        pass
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
        "par_categorie":  _par_categorie(valides),
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


def _par_categorie(ventes) -> list[dict]:
    out = []
    for c, l in CATEGORIES:
        col_q, col_p = f"qte_{c.lower()}", f"prix_{c.lower()}"
        n = sum(getattr(v, col_q) or 0 for v in ventes)
        mt = sum(float(_d(getattr(v, col_p)) * (getattr(v, col_q) or 0)) for v in ventes)
        out.append({"code": c, "libelle": l, "nb_cartes": n, "montant": round(mt, 2)})
    anc = [v for v in ventes if not any(_qtes(v).values())]
    if anc:
        out.append({"code": "NON_CLASSE", "libelle": "Non classées (avant catégories)",
                    "nb_cartes": sum(v.quantite for v in anc),
                    "montant": round(sum(float(v.montant_brut) for v in anc), 2)})
    return out


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
        "prix":       {c: float(p) for c, p in _prix_categories(db).items()},
        "par_categorie": _par_categorie(ventes),
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
