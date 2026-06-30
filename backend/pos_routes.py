"""
Routes API POS bar/restaurant — préfixe /api/pos
Protégées automatiquement par AuthMiddleware (session cookie ou X-API-Key).
"""
from __future__ import annotations

from datetime import date as date_type, datetime, timezone, time
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, validator
from sqlalchemy.orm import Session, joinedload

from database import get_db
from models import (
    BarProduit, BarPrixHistorique, BarAchat, BarMouvementStock,
    BarVente, BarLigneVente, BarCredit, BarRemboursement,
    BarCommande, BarLigneCommande, BarPaiementEmploye, Employe,
)
from pos_service import (
    stock_courant, stock_tous_produits, prix_actif, cmup,
    encaisser_vente as _encaisser, annuler_vente as _annuler,
    encaisser_commande as _enc_commande, stats_bar,
)

router = APIRouter(prefix="/api/pos", tags=["POS Bar"])


def _user(request: Request):
    return getattr(request.state, "user", None)


def _uid(request: Request) -> int | None:
    u = _user(request)
    return u.id if u else None


# ══════════════════════════════════════════════════════════════════
# Schémas Pydantic
# ══════════════════════════════════════════════════════════════════

class ProduitIn(BaseModel):
    nom:                 str
    categorie:           str
    unite:               str = "unite"
    code_barre:          Optional[str] = None
    actif:               bool = True
    seuil_alerte_stock:  float = 0.0
    prix_initial:        Optional[float] = None
    vendu_par_caisse:    bool = False
    unites_par_caisse:   Optional[int] = None

    @validator('unites_par_caisse')
    def valider_caisse(cls, v, values):
        if values.get('vendu_par_caisse') and (v is None or v < 1):
            raise ValueError('unites_par_caisse est obligatoire et doit être ≥ 1 lorsque vendu_par_caisse=True')
        return v


class ApprovisionnementIn(BaseModel):
    nb_caisses:        int   = Field(0, ge=0)
    nb_unites_vrac:    int   = Field(0, ge=0)
    prix_achat_caisse: Optional[float] = Field(None, gt=0)
    notes:             Optional[str]   = None

    @validator('nb_unites_vrac')
    def valider_quantite(cls, v, values):
        if v == 0 and values.get('nb_caisses', 0) == 0:
            raise ValueError('Saisir au moins nb_caisses > 0 ou nb_unites_vrac > 0')
        return v


class PrixIn(BaseModel):
    prix: float = Field(gt=0)


class AchatIn(BaseModel):
    produit_id:          int
    quantite:            float = Field(gt=0)
    prix_achat_unitaire: float = Field(ge=0)
    fournisseur:         Optional[str] = None
    notes:               Optional[str] = None


class AjustementIn(BaseModel):
    produit_id:     int
    quantite:       float   # signée : + pour ajustement positif, - pour perte
    type_mouvement: str = "AJUSTEMENT"   # AJUSTEMENT, PERTE, CASSE
    motif:          str

    @validator("type_mouvement")
    def check_type(cls, v):
        if v not in ("AJUSTEMENT", "PERTE", "CASSE"):
            raise ValueError("type_mouvement doit être AJUSTEMENT, PERTE ou CASSE")
        return v


class LigneVenteIn(BaseModel):
    produit_id: int
    quantite:   float = Field(gt=0)


class VenteIn(BaseModel):
    lignes:          List[LigneVenteIn]
    caissier_id:     Optional[int]  = None
    mode_paiement:   str            = "CASH"
    montant_paye:    Optional[float]= None
    client_nom:      Optional[str]  = None
    client_contact:  Optional[str]  = None
    date_echeance:   Optional[date_type] = None

    @validator("mode_paiement")
    def check_mode(cls, v):
        if v.upper() not in ("CASH", "CREDIT", "MIXTE"):
            raise ValueError("mode_paiement doit être CASH, CREDIT ou MIXTE")
        return v.upper()


class LigneCommandeIn(BaseModel):
    produit_id: int
    quantite:   float = Field(gt=0)
    notes:      Optional[str] = None


class CommandeIn(BaseModel):
    numero_table: Optional[str] = None
    client:       Optional[str] = None
    caissier_id:  Optional[int] = None
    lignes:       List[LigneCommandeIn] = []


class EncaisserCommandeIn(BaseModel):
    mode_paiement:  str            = "CASH"
    montant_paye:   Optional[float]= None
    client_nom:     Optional[str]  = None
    client_contact: Optional[str]  = None
    date_echeance:  Optional[date_type] = None


class RemboursementIn(BaseModel):
    montant: float = Field(gt=0)
    notes:   Optional[str] = None


class PaiementEmployeIn(BaseModel):
    employe_id:    int
    montant:       float = Field(gt=0)
    type_paiement: str   = "SALAIRE"
    mode:          str   = "CASH"
    periode_debut: Optional[date_type] = None
    periode_fin:   Optional[date_type] = None
    notes:         Optional[str] = None

    @validator("type_paiement")
    def check_type_paie(cls, v):
        if v.upper() not in ("SALAIRE", "AVANCE", "BONUS", "COMMISSION"):
            raise ValueError("type_paiement invalide")
        return v.upper()


# ══════════════════════════════════════════════════════════════════
# PRODUITS & PRIX
# ══════════════════════════════════════════════════════════════════

def _produit_dict(p: BarProduit, stk: Decimal, db: Session) -> dict:
    """Sérialise un BarProduit avec tous les champs calculés."""
    prix_u  = prix_actif(p.id, db) or Decimal("0")
    stk_int = int(stk)
    upc     = p.unites_par_caisse or 0
    return {
        "id":                  p.id,
        "nom":                 p.nom,
        "categorie":           p.categorie,
        "unite":               p.unite,
        "code_barre":          p.code_barre,
        "actif":               p.actif,
        "seuil_alerte_stock":  float(p.seuil_alerte_stock),
        "stock_courant":       float(stk),
        "stock_unites":        stk_int,
        "vendu_par_caisse":    p.vendu_par_caisse,
        "unites_par_caisse":   upc if p.vendu_par_caisse else None,
        "caisses_completes":   (stk_int // upc) if (p.vendu_par_caisse and upc > 0) else None,
        "unites_restantes":    (stk_int %  upc) if (p.vendu_par_caisse and upc > 0) else None,
        "prix_actif":          float(prix_u),
        "prix_vente_unite":    float(prix_u),
        "prix_vente_caisse":   float(prix_u * upc) if (p.vendu_par_caisse and upc > 0) else None,
        "stock_bas":           float(stk) <= float(p.seuil_alerte_stock) and float(p.seuil_alerte_stock) > 0,
        "cmup":                float(cmup(p.id, db)),
        "date_creation":       p.date_creation.isoformat() if p.date_creation else None,
    }


@router.get("/produits")
def liste_produits(actif: Optional[bool] = None, db: Session = Depends(get_db)):
    """Tous les produits du bar, avec stock courant, caisse/unité et prix actif."""
    q = db.query(BarProduit)
    if actif is not None:
        q = q.filter(BarProduit.actif == actif)
    produits = q.order_by(BarProduit.categorie, BarProduit.nom).all()
    stocks   = stock_tous_produits(db)
    return [_produit_dict(p, stocks.get(p.id, Decimal("0")), db) for p in produits]


@router.get("/produits/{produit_id}")
def detail_produit(produit_id: int, db: Session = Depends(get_db)):
    """Détail d'un produit avec stock et prix."""
    p = db.query(BarProduit).filter_by(id=produit_id).first()
    if not p:
        raise HTTPException(404, "Produit introuvable")
    return _produit_dict(p, stock_courant(produit_id, db), db)


@router.post("/produits", status_code=201)
def creer_produit(data: ProduitIn, request: Request, db: Session = Depends(get_db)):
    if not data.prix_initial or data.prix_initial <= 0:
        raise HTTPException(422, "Un prix de vente initial valide est requis pour créer un produit.")
    if data.vendu_par_caisse and (not data.unites_par_caisse or data.unites_par_caisse < 1):
        raise HTTPException(422, "unites_par_caisse est obligatoire (≥ 1) pour un produit vendu par caisse.")
    p = BarProduit(
        nom                = data.nom.strip(),
        categorie          = data.categorie.strip(),
        unite              = data.unite.strip(),
        code_barre         = data.code_barre,
        actif              = data.actif,
        seuil_alerte_stock = data.seuil_alerte_stock,
        vendu_par_caisse   = data.vendu_par_caisse,
        unites_par_caisse  = data.unites_par_caisse if data.vendu_par_caisse else None,
    )
    db.add(p)
    db.flush()
    db.add(BarPrixHistorique(
        produit_id     = p.id,
        prix           = Decimal(str(data.prix_initial)),
        date_debut     = datetime.now(tz=timezone.utc),
        date_fin       = None,
        utilisateur_id = _uid(request),
    ))
    db.commit()
    db.refresh(p)
    return {"id": p.id, "nom": p.nom, "categorie": p.categorie}


@router.put("/produits/{produit_id}")
def modifier_produit(produit_id: int, data: ProduitIn, db: Session = Depends(get_db)):
    p = db.query(BarProduit).filter_by(id=produit_id).first()
    if not p:
        raise HTTPException(404, "Produit introuvable")
    if data.vendu_par_caisse and (not data.unites_par_caisse or data.unites_par_caisse < 1):
        raise HTTPException(422, "unites_par_caisse est obligatoire (≥ 1) pour un produit vendu par caisse.")
    p.nom                = data.nom.strip()
    p.categorie          = data.categorie.strip()
    p.unite              = data.unite.strip()
    p.code_barre         = data.code_barre
    p.actif              = data.actif
    p.seuil_alerte_stock = data.seuil_alerte_stock
    p.vendu_par_caisse   = data.vendu_par_caisse
    p.unites_par_caisse  = data.unites_par_caisse if data.vendu_par_caisse else None
    db.commit()
    return {"ok": True}


@router.delete("/produits/{produit_id}", status_code=204)
def desactiver_produit(produit_id: int, db: Session = Depends(get_db)):
    p = db.query(BarProduit).filter_by(id=produit_id).first()
    if not p:
        raise HTTPException(404, "Produit introuvable")
    p.actif = False
    db.commit()


@router.post("/produits/{produit_id}/prix", status_code=201)
def changer_prix(produit_id: int, data: PrixIn, request: Request, db: Session = Depends(get_db)):
    """Historise l'ancien prix (date_fin = maintenant) et insère le nouveau."""
    p = db.query(BarProduit).filter_by(id=produit_id).first()
    if not p:
        raise HTTPException(404, "Produit introuvable")

    now = datetime.now(tz=timezone.utc)

    # Clore le prix actuel
    ancien = (
        db.query(BarPrixHistorique)
        .filter(BarPrixHistorique.produit_id == produit_id,
                BarPrixHistorique.date_fin.is_(None))
        .first()
    )
    if ancien:
        ancien.date_fin = now

    # Vérifier cohérence : changement de prix > 50% → note dans les anomalies (non bloquant)
    if ancien and ancien.prix > 0:
        variation = abs(float(data.prix) - float(ancien.prix)) / float(ancien.prix) * 100
    else:
        variation = 0.0

    nouveau = BarPrixHistorique(
        produit_id     = produit_id,
        prix           = data.prix,
        date_debut     = now,
        utilisateur_id = _uid(request),
    )
    db.add(nouveau)
    db.commit()
    db.refresh(nouveau)
    return {
        "id":          nouveau.id,
        "prix":        float(nouveau.prix),
        "date_debut":  nouveau.date_debut.isoformat(),
        "variation_pct": round(variation, 2),
    }


@router.get("/produits/{produit_id}/historique-prix")
def historique_prix(produit_id: int, db: Session = Depends(get_db)):
    rows = (
        db.query(BarPrixHistorique)
        .filter(BarPrixHistorique.produit_id == produit_id)
        .order_by(BarPrixHistorique.date_debut.desc())
        .all()
    )
    return [
        {
            "id":         r.id,
            "prix":       float(r.prix),
            "date_debut": r.date_debut.isoformat(),
            "date_fin":   r.date_fin.isoformat() if r.date_fin else None,
            "actif":      r.date_fin is None,
        }
        for r in rows
    ]


# ══════════════════════════════════════════════════════════════════
# APPROVISIONNEMENT (logique caisse/unité)
# ══════════════════════════════════════════════════════════════════

@router.post("/produits/{produit_id}/approvisionnement", status_code=201)
def approvisionner(produit_id: int, data: ApprovisionnementIn, request: Request, db: Session = Depends(get_db)):
    """
    Ajoute du stock pour un produit.
    - Si vendu_par_caisse : nb_caisses × unites_par_caisse + nb_unites_vrac
    - Sinon : nb_unites_vrac uniquement (nb_caisses ignoré)
    Stock toujours stocké en UNITÉS dans BarMouvementStock.
    """
    p = db.query(BarProduit).filter_by(id=produit_id).first()
    if not p:
        raise HTTPException(404, "Produit introuvable")

    upc = p.unites_par_caisse or 1
    if p.vendu_par_caisse:
        if p.unites_par_caisse is None or p.unites_par_caisse < 1:
            raise HTTPException(422, "CONFIG_CAISSE_INVALIDE : unites_par_caisse non défini pour ce produit.")
        total_unites = data.nb_caisses * upc + data.nb_unites_vrac
    else:
        total_unites = data.nb_unites_vrac

    if total_unites <= 0:
        raise HTTPException(422, "Le total d'unités à ajouter doit être > 0.")

    now = datetime.now(tz=timezone.utc)
    motif = (
        f"Appro {data.nb_caisses} caisse(s) × {upc} u."
        + (f" + {data.nb_unites_vrac} vrac" if data.nb_unites_vrac else "")
        if p.vendu_par_caisse
        else f"Appro {total_unites} unités"
    ) + (f" — {data.notes}" if data.notes else "")

    achat_id = None
    if data.prix_achat_caisse and data.nb_caisses > 0 and p.vendu_par_caisse:
        prix_unitaire = Decimal(str(data.prix_achat_caisse)) / Decimal(str(upc))
        achat = BarAchat(
            produit_id          = produit_id,
            quantite            = Decimal(str(total_unites)),
            prix_achat_unitaire = prix_unitaire,
            utilisateur_id      = _uid(request),
            notes               = f"Caisse G{data.prix_achat_caisse:.2f} / {upc} u.",
        )
        db.add(achat)
        db.flush()
        achat_id = achat.id

    db.add(BarMouvementStock(
        produit_id     = produit_id,
        quantite       = Decimal(str(total_unites)),
        type_mouvement = "ENTREE",
        motif          = motif,
        achat_id       = achat_id,
        date_mouvement = now,
        utilisateur_id = _uid(request),
    ))

    db.commit()
    stk_apres = stock_courant(produit_id, db)
    return {
        "ok":               True,
        "total_unites_ajoutes": total_unites,
        "nb_caisses":       data.nb_caisses,
        "nb_unites_vrac":   data.nb_unites_vrac,
        "stock_apres":      float(stk_apres),
        "caisses_apres":    (int(stk_apres) // upc) if p.vendu_par_caisse else None,
        "unites_restantes": (int(stk_apres) %  upc) if p.vendu_par_caisse else None,
    }


# ══════════════════════════════════════════════════════════════════
# STOCK
# ══════════════════════════════════════════════════════════════════

@router.get("/stock")
def stock_global(db: Session = Depends(get_db)):
    """Stock courant calculé pour tous les produits actifs."""
    produits = db.query(BarProduit).filter_by(actif=True).order_by(BarProduit.categorie, BarProduit.nom).all()
    stocks   = stock_tous_produits(db)
    return [
        {
            "produit_id":         p.id,
            "nom":                p.nom,
            "categorie":          p.categorie,
            "unite":              p.unite,
            "stock_courant":      float(stocks.get(p.id, Decimal("0"))),
            "seuil_alerte_stock": float(p.seuil_alerte_stock),
            "alerte":             stocks.get(p.id, Decimal("0")) <= Decimal(str(p.seuil_alerte_stock)),
            "cmup":               float(cmup(p.id, db)),
        }
        for p in produits
    ]


@router.get("/stock/{produit_id}")
def stock_produit(produit_id: int, db: Session = Depends(get_db)):
    """Détail du stock + historique des mouvements pour un produit."""
    p = db.query(BarProduit).filter_by(id=produit_id).first()
    if not p:
        raise HTTPException(404, "Produit introuvable")

    mouvements = (
        db.query(BarMouvementStock)
        .filter(BarMouvementStock.produit_id == produit_id)
        .order_by(BarMouvementStock.date_mouvement.desc())
        .limit(100)
        .all()
    )
    return {
        "produit_id":    produit_id,
        "nom":           p.nom,
        "stock_courant": float(stock_courant(produit_id, db)),
        "prix_actif":    float(prix_actif(produit_id, db) or 0),
        "cmup":          float(cmup(produit_id, db)),
        "mouvements": [
            {
                "id":            m.id,
                "type":          m.type_mouvement,
                "quantite":      float(m.quantite),
                "motif":         m.motif,
                "date":          m.date_mouvement.isoformat(),
                "vente_id":      m.reference_vente_id,
                "achat_id":      m.achat_id,
            }
            for m in mouvements
        ],
    }


@router.post("/achats", status_code=201)
def recevoir_marchandises(data: AchatIn, request: Request, db: Session = Depends(get_db)):
    """Réceptionne des marchandises : crée un BarAchat + mouvement ENTREE."""
    p = db.query(BarProduit).filter_by(id=data.produit_id).first()
    if not p:
        raise HTTPException(404, "Produit introuvable")

    achat = BarAchat(
        produit_id          = data.produit_id,
        quantite            = data.quantite,
        prix_achat_unitaire = data.prix_achat_unitaire,
        fournisseur         = data.fournisseur,
        utilisateur_id      = _uid(request),
        notes               = data.notes,
    )
    db.add(achat)
    db.flush()

    mouv = BarMouvementStock(
        produit_id     = data.produit_id,
        type_mouvement = "ENTREE",
        quantite       = Decimal(str(data.quantite)),
        motif          = f"Réception de {data.quantite} {p.unite}(s)"
                         + (f" — {data.fournisseur}" if data.fournisseur else ""),
        achat_id       = achat.id,
        utilisateur_id = _uid(request),
    )
    db.add(mouv)
    db.commit()

    return {
        "achat_id":    achat.id,
        "mouvement_id": mouv.id,
        "stock_apres": float(stock_courant(data.produit_id, db)),
    }


@router.post("/stock/ajustement", status_code=201)
def ajuster_stock(data: AjustementIn, request: Request, db: Session = Depends(get_db)):
    """Ajustement manuel de stock (perte, casse, correction inventaire)."""
    p = db.query(BarProduit).filter_by(id=data.produit_id).first()
    if not p:
        raise HTTPException(404, "Produit introuvable")
    if not data.motif or len(data.motif.strip()) < 5:
        raise HTTPException(422, "Le motif doit contenir au moins 5 caractères.")

    mouv = BarMouvementStock(
        produit_id     = data.produit_id,
        type_mouvement = data.type_mouvement,
        quantite       = Decimal(str(data.quantite)),
        motif          = data.motif.strip(),
        utilisateur_id = _uid(request),
    )
    db.add(mouv)
    db.commit()

    return {
        "mouvement_id": mouv.id,
        "stock_apres":  float(stock_courant(data.produit_id, db)),
    }


# ══════════════════════════════════════════════════════════════════
# VENTES & CAISSE
# ══════════════════════════════════════════════════════════════════

@router.post("/ventes", status_code=201)
def creer_vente(data: VenteIn, request: Request, db: Session = Depends(get_db)):
    """Encaisse une vente. Transaction atomique avec rollback sur erreur."""
    try:
        vente = _encaisser(data.dict(), db, utilisateur_id=_uid(request))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {
        "vente_id":      vente.id,
        "numero_ticket": vente.numero_ticket,
        "montant_total": float(vente.montant_total),
        "statut":        vente.statut,
    }


@router.get("/ventes")
def liste_ventes(
    date_debut:    Optional[date_type] = None,
    date_fin:      Optional[date_type] = None,
    caissier_id:   Optional[int]       = None,
    mode_paiement: Optional[str]       = None,
    statut:        Optional[str]       = None,
    limit:         int = Query(50, le=500),
    db: Session = Depends(get_db),
):
    q = db.query(BarVente)
    if date_debut:
        dt = datetime.combine(date_debut, time.min).replace(tzinfo=timezone.utc)
        q  = q.filter(BarVente.date_heure >= dt)
    if date_fin:
        dt = datetime.combine(date_fin, time.max).replace(tzinfo=timezone.utc)
        q  = q.filter(BarVente.date_heure <= dt)
    if caissier_id:
        q = q.filter(BarVente.caissier_id == caissier_id)
    if mode_paiement:
        q = q.filter(BarVente.mode_paiement == mode_paiement.upper())
    if statut:
        q = q.filter(BarVente.statut == statut.upper())

    ventes = q.order_by(BarVente.date_heure.desc()).limit(limit).all()

    return [
        {
            "id":              v.id,
            "numero_ticket":   v.numero_ticket,
            "date_heure":      v.date_heure.isoformat(),
            "montant_total":   float(v.montant_total),
            "montant_paye":    float(v.montant_paye),
            "montant_restant": float(v.montant_restant),
            "mode_paiement":   v.mode_paiement,
            "statut":          v.statut,
            "client_nom":      v.client_nom,
            "caissier_id":     v.caissier_id,
            "nb_lignes":       len(v.lignes),
        }
        for v in ventes
    ]


@router.get("/ventes/temps-reel")
def ventes_temps_reel(db: Session = Depends(get_db)):
    """Ventes du jour en cours, agrégées par caissier."""
    today     = datetime.now(tz=timezone.utc).date()
    dt_debut  = datetime.combine(today, time.min).replace(tzinfo=timezone.utc)
    dt_fin    = datetime.combine(today, time.max).replace(tzinfo=timezone.utc)

    ventes = (
        db.query(BarVente)
        .filter(
            BarVente.date_heure >= dt_debut,
            BarVente.date_heure <= dt_fin,
            BarVente.statut != "ANNULEE",
        )
        .all()
    )

    ca_jour   = sum(float(v.montant_total) for v in ventes)
    cash_jour = sum(float(v.montant_paye)  for v in ventes if v.mode_paiement in ("CASH", "MIXTE"))

    par_caissier: dict = {}
    for v in ventes:
        cid  = v.caissier_id or 0
        nom  = v.caissier.nom + " " + v.caissier.prenom if v.caissier else "Sans caissier"
        if cid not in par_caissier:
            par_caissier[cid] = {"caissier_id": cid, "nom": nom, "nb_ventes": 0, "total": 0.0}
        par_caissier[cid]["nb_ventes"] += 1
        par_caissier[cid]["total"]     += float(v.montant_total)

    return {
        "date":        str(today),
        "nb_ventes":   len(ventes),
        "ca_jour":     ca_jour,
        "cash_jour":   cash_jour,
        "par_caissier": list(par_caissier.values()),
    }


@router.get("/ventes/{vente_id}")
def detail_vente(vente_id: int, db: Session = Depends(get_db)):
    v = db.query(BarVente).filter_by(id=vente_id).first()
    if not v:
        raise HTTPException(404, "Vente introuvable")
    return {
        "id":              v.id,
        "numero_ticket":   v.numero_ticket,
        "date_heure":      v.date_heure.isoformat(),
        "montant_total":   float(v.montant_total),
        "montant_paye":    float(v.montant_paye),
        "montant_restant": float(v.montant_restant),
        "mode_paiement":   v.mode_paiement,
        "statut":          v.statut,
        "client_nom":      v.client_nom,
        "caissier_id":     v.caissier_id,
        "lignes": [
            {
                "produit_id":              l.produit_id,
                "produit_nom":             l.produit.nom if l.produit else str(l.produit_id),
                "quantite":                float(l.quantite),
                "prix_unitaire_applique":  float(l.prix_unitaire_applique),
                "sous_total":              float(l.sous_total),
            }
            for l in v.lignes
        ],
    }


@router.put("/ventes/{vente_id}/annuler")
def annuler_vente_route(vente_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        vente = _annuler(vente_id, db, utilisateur_id=_uid(request))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"ok": True, "statut": vente.statut}


# ══════════════════════════════════════════════════════════════════
# COMMANDES
# ══════════════════════════════════════════════════════════════════

@router.get("/commandes/{commande_id}")
def get_commande(commande_id: int, db: Session = Depends(get_db)):
    cmd = db.query(BarCommande).filter_by(id=commande_id).first()
    if not cmd:
        raise HTTPException(404, "Commande introuvable")
    return {
        "id":             cmd.id,
        "numero_table":   cmd.numero_table,
        "client":         cmd.client,
        "statut":         cmd.statut,
        "date_ouverture": cmd.date_ouverture.isoformat(),
        "lignes": [
            {
                "produit_id":   l.produit_id,
                "produit_nom":  l.produit.nom if l.produit else f"Produit#{l.produit_id}",
                "quantite":     float(l.quantite),
                "notes":        l.notes,
                "sous_total":   float(l.quantite) * float(prix_actif(l.produit_id, db) or 0),
            }
            for l in cmd.lignes
        ],
    }


@router.get("/commandes")
def liste_commandes(
    statut: Optional[str] = None,
    db: Session = Depends(get_db),
):
    q = db.query(BarCommande)
    if statut:
        q = q.filter(BarCommande.statut == statut.upper())
    else:
        q = q.filter(BarCommande.statut.notin_(["ENCAISSEE", "ANNULEE"]))
    cmds = q.order_by(BarCommande.date_ouverture.desc()).all()

    return [
        {
            "id":             c.id,
            "numero_table":   c.numero_table,
            "client":         c.client,
            "statut":         c.statut,
            "caissier_id":    c.caissier_id,
            "date_ouverture": c.date_ouverture.isoformat(),
            "nb_lignes":      len(c.lignes),
            "total_estime":   sum(
                float(l.quantite) * float(prix_actif(l.produit_id, db) or 0)
                for l in c.lignes
            ),
        }
        for c in cmds
    ]


@router.post("/commandes", status_code=201)
def creer_commande(data: CommandeIn, request: Request, db: Session = Depends(get_db)):
    cmd = BarCommande(
        numero_table = data.numero_table,
        client       = data.client,
        caissier_id  = data.caissier_id,
    )
    db.add(cmd)
    db.flush()

    for l in data.lignes:
        p = db.query(BarProduit).filter_by(id=l.produit_id, actif=True).first()
        if not p:
            db.rollback()
            raise HTTPException(404, f"Produit #{l.produit_id} introuvable")
        db.add(BarLigneCommande(
            commande_id = cmd.id,
            produit_id  = l.produit_id,
            quantite    = l.quantite,
            notes       = l.notes,
        ))

    db.commit()
    db.refresh(cmd)
    return {"id": cmd.id, "statut": cmd.statut}


@router.put("/commandes/{commande_id}")
def modifier_commande(commande_id: int, data: CommandeIn, db: Session = Depends(get_db)):
    cmd = db.query(BarCommande).filter_by(id=commande_id).first()
    if not cmd:
        raise HTTPException(404, "Commande introuvable")
    if cmd.statut in ("ENCAISSEE", "ANNULEE"):
        raise HTTPException(422, f"Commande déjà {cmd.statut.lower()}")

    cmd.numero_table = data.numero_table
    cmd.client       = data.client

    # Remplace les lignes
    for l in cmd.lignes:
        db.delete(l)
    db.flush()

    for l in data.lignes:
        db.add(BarLigneCommande(
            commande_id = cmd.id,
            produit_id  = l.produit_id,
            quantite    = l.quantite,
            notes       = l.notes,
        ))

    from datetime import datetime, timezone
    cmd.date_modification = datetime.now(tz=timezone.utc)
    db.commit()
    return {"ok": True}


@router.put("/commandes/{commande_id}/encaisser")
def encaisser_commande_route(
    commande_id: int,
    data: EncaisserCommandeIn,
    request: Request,
    db: Session = Depends(get_db),
):
    try:
        vente = _enc_commande(commande_id, data.dict(), db, utilisateur_id=_uid(request))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {
        "vente_id":      vente.id,
        "numero_ticket": vente.numero_ticket,
        "montant_total": float(vente.montant_total),
        "statut":        vente.statut,
    }


# ══════════════════════════════════════════════════════════════════
# CRÉDITS
# ══════════════════════════════════════════════════════════════════

@router.get("/credits")
def liste_credits(
    statut: Optional[str] = None,
    db: Session = Depends(get_db),
):
    q = db.query(BarCredit)
    if statut:
        q = q.filter(BarCredit.statut == statut.upper())
    credits = q.order_by(BarCredit.date_creation.desc()).all()

    return [
        {
            "id":               c.id,
            "vente_id":         c.vente_id,
            "client_nom":       c.client_nom,
            "client_contact":   c.client_contact,
            "montant_du":       float(c.montant_du),
            "montant_rembourse":float(c.montant_rembourse),
            "solde":            float(c.solde),
            "statut":           c.statut,
            "date_creation":    c.date_creation.isoformat(),
            "date_echeance":    str(c.date_echeance) if c.date_echeance else None,
            "nb_remboursements": len(c.remboursements),
        }
        for c in credits
    ]


@router.post("/credits/{credit_id}/remboursement", status_code=201)
def enregistrer_remboursement(
    credit_id: int,
    data: RemboursementIn,
    request: Request,
    db: Session = Depends(get_db),
):
    credit = db.query(BarCredit).filter_by(id=credit_id).first()
    if not credit:
        raise HTTPException(404, "Crédit introuvable")
    if credit.statut == "SOLDE":
        raise HTTPException(422, "Ce crédit est déjà soldé.")

    montant = Decimal(str(data.montant))
    if montant > credit.solde:
        raise HTTPException(422, f"Montant ({montant}) supérieur au solde ({credit.solde}).")

    remb = BarRemboursement(
        credit_id      = credit_id,
        montant        = montant,
        utilisateur_id = _uid(request),
        notes          = data.notes,
    )
    db.add(remb)

    credit.montant_rembourse += montant
    credit.solde             -= montant
    if credit.solde <= 0:
        credit.solde  = Decimal("0")
        credit.statut = "SOLDE"

        # Mettre à jour la vente associée
        vente = db.query(BarVente).filter_by(id=credit.vente_id).first()
        if vente:
            vente.montant_restant = Decimal("0")
            vente.montant_paye    = vente.montant_total
            vente.statut          = "PAYEE"

    db.commit()
    return {
        "remboursement_id": remb.id,
        "solde_restant":    float(credit.solde),
        "credit_statut":    credit.statut,
    }


# ══════════════════════════════════════════════════════════════════
# PAIE EMPLOYÉS BAR
# ══════════════════════════════════════════════════════════════════

@router.get("/paiements-employes")
def liste_paiements_employes(db: Session = Depends(get_db)):
    paiements = (
        db.query(BarPaiementEmploye)
        .order_by(BarPaiementEmploye.date_paiement.desc())
        .limit(200)
        .all()
    )
    return [
        {
            "id":            p.id,
            "employe_id":    p.employe_id,
            "employe_nom":   (p.employe.prenom + " " + p.employe.nom) if p.employe else str(p.employe_id),
            "montant":       float(p.montant),
            "type_paiement": p.type_paiement,
            "mode":          p.mode,
            "date_paiement": p.date_paiement.isoformat(),
            "periode_debut": str(p.periode_debut) if p.periode_debut else None,
            "periode_fin":   str(p.periode_fin)   if p.periode_fin   else None,
            "notes":         p.notes,
        }
        for p in paiements
    ]


@router.post("/paiements-employes", status_code=201)
def creer_paiement_employe(data: PaiementEmployeIn, request: Request, db: Session = Depends(get_db)):
    emp = db.query(Employe).filter_by(id=data.employe_id, actif=True).first()
    if not emp:
        raise HTTPException(404, "Employé introuvable ou inactif")

    p = BarPaiementEmploye(
        employe_id     = data.employe_id,
        montant        = data.montant,
        type_paiement  = data.type_paiement,
        mode           = data.mode,
        periode_debut  = data.periode_debut,
        periode_fin    = data.periode_fin,
        utilisateur_id = _uid(request),
        notes          = data.notes,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return {"id": p.id, "montant": float(p.montant)}


@router.get("/paiements-employes/{employe_id}")
def historique_paiements_employe(employe_id: int, db: Session = Depends(get_db)):
    emp = db.query(Employe).filter_by(id=employe_id).first()
    if not emp:
        raise HTTPException(404, "Employé introuvable")

    paiements = (
        db.query(BarPaiementEmploye)
        .filter(BarPaiementEmploye.employe_id == employe_id)
        .order_by(BarPaiementEmploye.date_paiement.desc())
        .all()
    )
    return {
        "employe": {
            "id":    emp.id,
            "nom":   emp.prenom + " " + emp.nom,
            "poste": emp.poste,
        },
        "total_verse": float(sum(p.montant for p in paiements)),
        "paiements": [
            {
                "id":            p.id,
                "montant":       float(p.montant),
                "type_paiement": p.type_paiement,
                "mode":          p.mode,
                "date_paiement": p.date_paiement.isoformat(),
                "notes":         p.notes,
            }
            for p in paiements
        ],
    }


# ══════════════════════════════════════════════════════════════════
# STATISTIQUES & RENTABILITÉ
# ══════════════════════════════════════════════════════════════════

@router.get("/stats")
def statistiques_bar(
    date_debut: date_type = Query(default=None),
    date_fin:   date_type = Query(default=None),
    db: Session = Depends(get_db),
):
    """CA, bénéfice, COGS, top produits sur la période (données réelles uniquement)."""
    from datetime import date as dt
    today = dt.today()
    if not date_debut:
        date_debut = today.replace(day=1)
    if not date_fin:
        date_fin = today

    return stats_bar(date_debut, date_fin, db)


@router.get("/benefices")
def benefices_detail(
    date_debut: date_type = Query(default=None),
    date_fin:   date_type = Query(default=None),
    db: Session = Depends(get_db),
):
    """Bénéfices détaillés par produit et catégorie, avec CMUP réel."""
    from datetime import date as dt
    today = dt.today()
    if not date_debut:
        date_debut = today.replace(day=1)
    if not date_fin:
        date_fin = today

    data = stats_bar(date_debut, date_fin, db)

    # Regroupement par catégorie
    par_categorie: dict = {}
    for p in data["par_produit"]:
        cat = p["categorie"] or "Autre"
        if cat not in par_categorie:
            par_categorie[cat] = {"categorie": cat, "ca": 0.0, "cogs": 0.0, "benefice": 0.0}
        par_categorie[cat]["ca"]       += p["ca"]
        par_categorie[cat]["cogs"]     += p["cogs"]
        par_categorie[cat]["benefice"] += p["benefice"]

    return {
        **data,
        "par_categorie": list(par_categorie.values()),
    }
