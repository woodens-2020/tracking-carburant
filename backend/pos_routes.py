"""
Routes API POS bar/restaurant — préfixe /api/pos
Protégées automatiquement par AuthMiddleware (session cookie ou X-API-Key).
"""
from __future__ import annotations

from datetime import date as date_type, datetime, timezone, time
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, validator, model_validator
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload, selectinload

from database import get_db
from models import (
    Produit,
    BarCategorie, BarProduit, BarPrixHistorique, BarAchat, BarAchatDepense,
    BarMouvementStock, BarVente, BarLigneVente, BarCredit, BarRemboursement,
    BarCommande, BarLigneCommande, BarPaiementEmploye, Employe, CuisinePlat,
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


class DepenseItem(BaseModel):
    description: str
    montant:     float = Field(gt=0)


class AchatIn(BaseModel):
    # Exactement un des deux doit être renseigné
    produit_id:          Optional[int] = None   # bar_produits (crée mouvement de stock)
    station_produit_id:  Optional[int] = None   # produits — carburants (pas de mouvement stock bar)
    quantite:            float = Field(gt=0)
    quantite_type:       str   = "unite"         # "unite" ou "caisse" (bar seulement)
    prix_achat_unitaire: float = Field(gt=0)
    fournisseur:         Optional[str] = None
    notes:               Optional[str] = None
    depenses:            List[DepenseItem] = []


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
    produit_id:      Optional[int]   = None
    cuisine_plat_id: Optional[int]   = None
    prix_unitaire:   Optional[float] = None   # requis si cuisine_plat_id
    quantite:        float = Field(gt=0)

    @model_validator(mode="after")
    def check_produit_ou_plat(self):
        if self.produit_id is None and self.cuisine_plat_id is None:
            raise ValueError("produit_id ou cuisine_plat_id est requis")
        return self


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


class AjoutLigneIn(BaseModel):
    produit_id: int
    quantite:   float = Field(gt=0, default=1.0)


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
    client_nif:     Optional[str]  = None
    date_echeance:  Optional[date_type] = None


class RemboursementIn(BaseModel):
    montant: float = Field(gt=0)
    notes:   Optional[str] = None


class ModifierCreditIn(BaseModel):
    client_nom:     Optional[str] = None
    client_contact: Optional[str] = None
    client_nif:     Optional[str] = None
    date_echeance:  Optional[str] = None
    statut:         Optional[str] = None


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
# CATÉGORIES
# ══════════════════════════════════════════════════════════════════

_COULEURS_DEFAUT = {
    'boisson': '#3fb6a8', 'alcool': '#a78bfa', 'soft': '#60a5fa',
    'plat': '#f7a93b', 'snack': '#fb923c', 'tabac': '#f87171',
    'dessert': '#f472b6', 'autre': '#94a3b8',
}

def _get_or_create_categorie(nom: str, db: Session) -> BarCategorie:
    """Retourne la catégorie existante ou en crée une nouvelle."""
    nom_clean = nom.strip().lower()
    cat = db.query(BarCategorie).filter_by(nom=nom_clean).first()
    if not cat:
        cat = BarCategorie(nom=nom_clean, couleur=_COULEURS_DEFAUT.get(nom_clean, '#94a3b8'))
        db.add(cat)
        db.flush()
    return cat


@router.get("/categories")
def liste_categories(db: Session = Depends(get_db)):
    cats = db.query(BarCategorie).order_by(BarCategorie.nom).all()
    return [
        {
            "id":       c.id,
            "nom":      c.nom,
            "couleur":  c.couleur,
            "nb_produits": db.query(BarProduit).filter_by(categorie=c.nom).count(),
        }
        for c in cats
    ]


class CategorieIn(BaseModel):
    nom: str
    couleur: Optional[str] = None


@router.post("/categories", status_code=201)
def creer_categorie(data: CategorieIn, db: Session = Depends(get_db)):
    cat = _get_or_create_categorie(data.nom, db)
    if data.couleur:
        cat.couleur = data.couleur
    db.commit()
    return {"id": cat.id, "nom": cat.nom, "couleur": cat.couleur}


@router.put("/categories/{cat_id}", status_code=200)
def renommer_categorie(cat_id: int, data: CategorieIn, db: Session = Depends(get_db)):
    """Renomme une catégorie — ON UPDATE CASCADE met à jour bar_produits.categorie."""
    cat = db.query(BarCategorie).filter_by(id=cat_id).first()
    if not cat:
        raise HTTPException(404, "Catégorie introuvable")
    nouveau_nom = data.nom.strip().lower()
    if not nouveau_nom:
        raise HTTPException(422, "Nom vide.")
    if db.query(BarCategorie).filter(BarCategorie.nom == nouveau_nom, BarCategorie.id != cat_id).first():
        raise HTTPException(409, f"La catégorie '{nouveau_nom}' existe déjà.")
    old_nom = cat.nom
    cat.nom = nouveau_nom
    if data.couleur:
        cat.couleur = data.couleur
    db.commit()
    nb = db.query(BarProduit).filter_by(categorie=nouveau_nom).count()
    return {"ok": True, "ancien_nom": old_nom, "nouveau_nom": nouveau_nom, "nb_produits_mis_a_jour": nb}


@router.delete("/categories/{cat_id}", status_code=200)
def supprimer_categorie(cat_id: int, db: Session = Depends(get_db)):
    from sqlalchemy.exc import IntegrityError
    cat = db.query(BarCategorie).filter_by(id=cat_id).first()
    if not cat:
        raise HTTPException(404, "Catégorie introuvable")
    nb = db.query(BarProduit).filter_by(categorie=cat.nom).count()
    if nb > 0:
        raise HTTPException(409, f"Impossible : {nb} produit(s) utilisent cette catégorie. Réaffectez-les d'abord.")
    try:
        db.delete(cat)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Catégorie utilisée par des produits — impossible de supprimer.")
    return {"ok": True}


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
    # Vérification doublon (insensible à la casse)
    from sqlalchemy import func as _func
    existant = db.query(BarProduit).filter(
        _func.lower(BarProduit.nom) == data.nom.strip().lower()
    ).first()
    if existant:
        raise HTTPException(409, f"Un produit nommé « {existant.nom} » existe déjà dans le catalogue.")
    _get_or_create_categorie(data.categorie, db)
    p = BarProduit(
        nom                = data.nom.strip(),
        categorie          = data.categorie.strip().lower(),
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
    _get_or_create_categorie(data.categorie, db)
    p.nom                = data.nom.strip()
    p.categorie          = data.categorie.strip().lower()
    p.unite              = data.unite.strip()
    p.code_barre         = data.code_barre
    p.actif              = data.actif
    p.seuil_alerte_stock = data.seuil_alerte_stock
    p.vendu_par_caisse   = data.vendu_par_caisse
    p.unites_par_caisse  = data.unites_par_caisse if data.vendu_par_caisse else None
    db.commit()
    return {"ok": True}


@router.delete("/produits/{produit_id}")
def desactiver_produit(produit_id: int, db: Session = Depends(get_db)):
    p = db.query(BarProduit).filter_by(id=produit_id).first()
    if not p:
        raise HTTPException(404, "Produit introuvable")
    p.actif = False
    db.commit()
    return {"id": produit_id, "nom": p.nom, "actif": False}


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


@router.get("/achats/produits-tous")
def tous_les_produits_achetables(db: Session = Depends(get_db)):
    """Retourne tous les produits disponibles pour un achat (bar + station)."""
    bar   = db.query(BarProduit).filter_by(actif=True).order_by(BarProduit.categorie, BarProduit.nom).all()
    stati = db.query(Produit).filter_by(actif=True).order_by(Produit.nom).all()
    return {
        "bar": [
            {
                "id":               p.id,
                "nom":              p.nom,
                "categorie":        p.categorie,
                "unite":            p.unite,
                "vendu_par_caisse": p.vendu_par_caisse,
                "unites_par_caisse":p.unites_par_caisse,
                "type":             "bar",
            }
            for p in bar
        ],
        "station": [
            {
                "id":               p.id,
                "nom":              p.nom,
                "categorie":        "carburant",
                "unite":            "gallon",
                "vendu_par_caisse": False,
                "unites_par_caisse":None,
                "type":             "station",
            }
            for p in stati
        ],
    }


def _achat_dict(a: BarAchat) -> dict:
    """Sérialise un BarAchat en résolvant le produit (bar ou station)."""
    if a.produit_id and a.bar_produit:
        p = a.bar_produit
        nom      = p.nom
        cat      = p.categorie
        unite    = p.unite
        par_caise= p.vendu_par_caisse
        upc      = p.unites_par_caisse
        ptype    = "bar"
    elif a.station_produit_id and a.station_produit:
        p = a.station_produit
        nom      = p.nom
        cat      = "carburant"
        unite    = "gallon"
        par_caise= False
        upc      = None
        ptype    = "station"
    else:
        nom = "Produit inconnu"; cat = None; unite = None; par_caise = False; upc = None; ptype = "?"

    total_dep  = sum(float(d.montant) for d in a.depenses)
    prix_total = float(a.quantite) * float(a.prix_achat_unitaire)
    return {
        "id":                  a.id,
        "produit_id":          a.produit_id,
        "station_produit_id":  a.station_produit_id,
        "produit_type":        ptype,
        "produit_nom":         nom,
        "produit_categorie":   cat,
        "produit_unite":       unite,
        "produit_par_caisse":  par_caise,
        "produit_upc":         upc,
        "quantite":            float(a.quantite),
        "prix_achat_unitaire": float(a.prix_achat_unitaire),
        "prix_marchandise":    prix_total,
        "total_depenses":      total_dep,
        "cout_total":          prix_total + total_dep,
        "fournisseur":         a.fournisseur,
        "notes":               a.notes,
        "statut":              a.statut,
        "date_achat":          a.date_achat.isoformat(),
        "depenses": [
            {"id": d.id, "description": d.description, "montant": float(d.montant)}
            for d in a.depenses
        ],
    }


@router.get("/achats")
def liste_achats(
    produit_id:         Optional[int] = None,
    station_produit_id: Optional[int] = None,
    limit: int = Query(100, le=500),
    db: Session = Depends(get_db),
):
    """Liste des achats (bar + station) avec dépenses et coût total."""
    q = db.query(BarAchat)
    if produit_id:
        q = q.filter(BarAchat.produit_id == produit_id)
    if station_produit_id:
        q = q.filter(BarAchat.station_produit_id == station_produit_id)
    achats = q.order_by(BarAchat.date_achat.desc()).limit(limit).all()
    return [_achat_dict(a) for a in achats]


@router.get("/achats/en-attente")
def achats_en_attente(db: Session = Depends(get_db)):
    """Achats bar en attente de confirmation stock (statut=EN_ATTENTE)."""
    achats = (
        db.query(BarAchat)
        .filter(BarAchat.statut == "EN_ATTENTE", BarAchat.produit_id.isnot(None))
        .order_by(BarAchat.date_achat.asc())
        .all()
    )
    return [_achat_dict(a) for a in achats]


@router.get("/achats/{achat_id}")
def detail_achat(achat_id: int, db: Session = Depends(get_db)):
    a = db.query(BarAchat).filter_by(id=achat_id).first()
    if not a:
        raise HTTPException(404, "Achat introuvable")
    result = _achat_dict(a)
    if a.produit_id:
        result["stock_apres"] = float(stock_courant(a.produit_id, db))
    return result


@router.post("/achats", status_code=201)
def recevoir_marchandises(data: AchatIn, request: Request, db: Session = Depends(get_db)):
    """Enregistre un achat. Stock PAS encore mis à jour pour les produits bar (statut=EN_ATTENTE).
    Les carburants (station) sont directement CONFIRME (pas de stock bar à gérer).
    Appeler POST /achats/{id}/confirmer pour valider le stock."""
    if bool(data.produit_id) == bool(data.station_produit_id):
        raise HTTPException(422, "Fournir soit produit_id (bar) soit station_produit_id (station), pas les deux.")

    qte_unites = Decimal(str(data.quantite))

    if data.produit_id:
        p = db.query(BarProduit).filter_by(id=data.produit_id).first()
        if not p:
            raise HTTPException(404, "Produit bar introuvable")
        upc = p.unites_par_caisse or 1
        if data.quantite_type == "caisse" and p.vendu_par_caisse and upc > 0:
            qte_unites = Decimal(str(data.quantite)) * Decimal(str(upc))
        nom_prod = p.nom
        statut   = "EN_ATTENTE"
    else:
        s = db.query(Produit).filter_by(id=data.station_produit_id).first()
        if not s:
            raise HTTPException(404, "Produit station introuvable")
        nom_prod = s.nom
        statut   = "CONFIRME"  # carburants : pas de stock bar

    achat = BarAchat(
        produit_id          = data.produit_id,
        station_produit_id  = data.station_produit_id,
        quantite            = qte_unites,
        prix_achat_unitaire = Decimal(str(data.prix_achat_unitaire)),
        fournisseur         = data.fournisseur,
        utilisateur_id      = _uid(request),
        notes               = data.notes,
        statut              = statut,
    )
    db.add(achat)
    db.flush()

    for dep in data.depenses:
        db.add(BarAchatDepense(
            achat_id    = achat.id,
            description = dep.description.strip(),
            montant     = Decimal(str(dep.montant)),
        ))

    db.commit()
    total_dep  = sum(float(d.montant) for d in data.depenses)
    prix_total = float(qte_unites) * float(data.prix_achat_unitaire)
    return {
        "achat_id":         achat.id,
        "statut":           statut,
        "produit_type":     "bar" if data.produit_id else "station",
        "produit_nom":      nom_prod,
        "quantite_unites":  float(qte_unites),
        "prix_marchandise": prix_total,
        "total_depenses":   total_dep,
        "cout_total":       prix_total + total_dep,
    }


@router.post("/achats/{achat_id}/confirmer", status_code=200)
def confirmer_achat(achat_id: int, request: Request, db: Session = Depends(get_db)):
    """Confirme la réception d'un achat bar : crée le mouvement ENTREE et met à jour le stock."""
    a = db.query(BarAchat).filter_by(id=achat_id).first()
    if not a:
        raise HTTPException(404, "Achat introuvable")
    if a.statut == "CONFIRME":
        raise HTTPException(409, "Cet achat est déjà confirmé — stock déjà mis à jour.")
    if not a.produit_id:
        raise HTTPException(422, "Seuls les achats de produits bar peuvent être confirmés (stock bar).")

    p       = db.query(BarProduit).filter_by(id=a.produit_id).first()
    unite   = p.unite if p else "unité"

    mouv = BarMouvementStock(
        produit_id     = a.produit_id,
        type_mouvement = "ENTREE",
        quantite       = a.quantite,
        motif          = f"Réception confirmée : {float(a.quantite)} {unite}(s)"
                         + (f" — {a.fournisseur}" if a.fournisseur else ""),
        achat_id       = a.id,
        utilisateur_id = _uid(request),
    )
    db.add(mouv)
    a.statut = "CONFIRME"
    db.commit()

    stk = float(stock_courant(a.produit_id, db))
    return {
        "ok":           True,
        "achat_id":     a.id,
        "mouvement_id": mouv.id,
        "stock_apres":  stk,
        "produit_nom":  p.nom if p else str(a.produit_id),
    }


@router.post("/stock/ajustement", status_code=201)
def ajuster_stock(data: AjustementIn, request: Request, db: Session = Depends(get_db)):
    """Ajustement manuel de stock (perte, casse, correction inventaire)."""
    p = db.query(BarProduit).filter_by(id=data.produit_id).first()
    if not p:
        raise HTTPException(404, "Produit introuvable")
    if not data.motif or len(data.motif.strip()) < 5:
        raise HTTPException(422, "Le motif doit contenir au moins 5 caractères.")

    qte = Decimal(str(abs(data.quantite)))
    if data.type_mouvement in ("PERTE", "CASSE"):
        qte = -qte

    mouv = BarMouvementStock(
        produit_id     = data.produit_id,
        type_mouvement = data.type_mouvement,
        quantite       = qte,
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
    produit_id:    Optional[int]       = None,
    heure_debut:   Optional[str]       = None,   # format "HH:MM"
    heure_fin:     Optional[str]       = None,   # format "HH:MM"
    limit:         int = Query(50, le=500),
    db: Session = Depends(get_db),
):
    from sqlalchemy import extract
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
    if produit_id:
        sub = db.query(BarLigneVente.vente_id).filter(BarLigneVente.produit_id == produit_id)
        q   = q.filter(BarVente.id.in_(sub))
    if heure_debut:
        try:
            h, m = (int(x) for x in heure_debut.split(':'))
            minutes = h * 60 + m
            q = q.filter(
                extract('hour', BarVente.date_heure) * 60 +
                extract('minute', BarVente.date_heure) >= minutes
            )
        except ValueError:
            pass
    if heure_fin:
        try:
            h, m = (int(x) for x in heure_fin.split(':'))
            minutes = h * 60 + m
            q = q.filter(
                extract('hour', BarVente.date_heure) * 60 +
                extract('minute', BarVente.date_heure) <= minutes
            )
        except ValueError:
            pass

    ventes = (
        q.options(
            selectinload(BarVente.lignes)
            .selectinload(BarLigneVente.produit),
            selectinload(BarVente.lignes)
            .selectinload(BarLigneVente.cuisine_plat),
        )
        .order_by(BarVente.date_heure.desc())
        .limit(limit)
        .all()
    )

    def _ligne_nom(l):
        if l.produit:      return l.produit.nom
        if l.cuisine_plat: return l.cuisine_plat.nom
        return "—"

    return [
        {
            "id":              v.id,
            "numero_ticket":   v.numero_ticket,
            "date_heure":      v.date_heure.isoformat(),
            "date_vente":      v.date_heure.date().isoformat(),
            "montant_total":   float(v.montant_total),
            "montant_paye":    float(v.montant_paye),
            "montant_restant": float(v.montant_restant),
            "montant_credit":  float(v.montant_restant) if v.mode_paiement in ("CREDIT", "MIXTE") else 0.0,
            "mode_paiement":   v.mode_paiement,
            "statut":          v.statut,
            "client_nom":      v.client_nom,
            "caissier_id":     v.caissier_id,
            "caissier_nom":    (v.caissier.nom + " " + v.caissier.prenom) if v.caissier else "Sans caissier",
            "nb_lignes":       len(v.lignes),
            "articles": [
                {
                    "nom":      _ligne_nom(l),
                    "quantite": float(l.quantite),
                    "total":    float(l.sous_total),
                }
                for l in v.lignes
            ],
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
            par_caissier[cid] = {"caissier_id": cid, "nom": nom, "nb_ventes": 0, "total": 0.0, "cash": 0.0, "credit": 0.0}
        par_caissier[cid]["nb_ventes"] += 1
        par_caissier[cid]["total"]     += float(v.montant_total)
        if v.mode_paiement in ("CASH", "MIXTE"):
            par_caissier[cid]["cash"]  += float(v.montant_paye)
        if v.mode_paiement in ("CREDIT", "MIXTE"):
            par_caissier[cid]["credit"] += float(v.montant_restant)

    ranked = sorted(par_caissier.values(), key=lambda x: x["total"], reverse=True)

    return {
        "date":         str(today),
        "nb_ventes":    len(ventes),
        "ca_jour":      ca_jour,
        "cash_jour":    cash_jour,
        "par_caissier": ranked,
    }


@router.get("/dashboard/bar")
def dashboard_bar(
    db:            Session       = Depends(get_db),
    date_debut:    Optional[str] = None,
    date_fin:      Optional[str] = None,
    caissier_id:   Optional[int] = None,
    mode_paiement: Optional[str] = None,
):
    """Dashboard complet bar/restaurant — filtrable par date, employé, mode."""
    from datetime import timedelta

    today = datetime.now(tz=timezone.utc).date()

    try:    d_debut = date_type.fromisoformat(date_debut) if date_debut else today
    except ValueError: d_debut = today
    try:    d_fin   = date_type.fromisoformat(date_fin)   if date_fin   else today
    except ValueError: d_fin = today
    if d_fin < d_debut: d_fin = d_debut

    dt_debut = datetime.combine(d_debut, time.min).replace(tzinfo=timezone.utc)
    dt_fin   = datetime.combine(d_fin,   time.max).replace(tzinfo=timezone.utc)

    nb_jours    = (d_fin - d_debut).days + 1
    is_single   = (d_debut == d_fin)
    d_prev_fin  = d_debut - timedelta(days=1)
    d_prev_deb  = d_prev_fin - timedelta(days=nb_jours - 1)
    dt_prev_d   = datetime.combine(d_prev_deb, time.min).replace(tzinfo=timezone.utc)
    dt_prev_f   = datetime.combine(d_prev_fin, time.max).replace(tzinfo=timezone.utc)

    def _base_q():
        q = db.query(BarVente).filter(
            BarVente.date_heure >= dt_debut,
            BarVente.date_heure <= dt_fin,
            BarVente.statut     != "ANNULEE",
        )
        if caissier_id:   q = q.filter(BarVente.caissier_id   == caissier_id)
        if mode_paiement: q = q.filter(BarVente.mode_paiement == mode_paiement)
        return q

    ventes = _base_q().order_by(BarVente.date_heure.desc()).all()

    q_prev = db.query(BarVente).filter(
        BarVente.date_heure >= dt_prev_d,
        BarVente.date_heure <= dt_prev_f,
        BarVente.statut     != "ANNULEE",
    )
    if caissier_id:   q_prev = q_prev.filter(BarVente.caissier_id   == caissier_id)
    if mode_paiement: q_prev = q_prev.filter(BarVente.mode_paiement == mode_paiement)
    ca_prev = sum(float(v.montant_total) for v in q_prev.all())

    ca      = sum(float(v.montant_total)   for v in ventes)
    cash    = sum(float(v.montant_paye)    for v in ventes if v.mode_paiement in ("CASH",   "MIXTE"))
    credit  = sum(float(v.montant_restant) for v in ventes if v.mode_paiement in ("CREDIT", "MIXTE"))

    par_periode: dict = {}
    for v in ventes:
        if is_single:
            key = v.date_heure.astimezone(timezone.utc).strftime("%H")
            lbl = key + "h"
        else:
            key = v.date_heure.date().isoformat()
            lbl = v.date_heure.astimezone(timezone.utc).strftime("%d/%m")
        if key not in par_periode:
            par_periode[key] = {"label": lbl, "ca": 0.0, "nb": 0}
        par_periode[key]["ca"] += float(v.montant_total)
        par_periode[key]["nb"] += 1
    par_periode_list = [v for _, v in sorted(par_periode.items())]

    par_mode: dict = {}
    for v in ventes:
        m = v.mode_paiement or "CASH"
        par_mode[m] = par_mode.get(m, 0.0) + float(v.montant_total)

    _nom_col = func.coalesce(BarProduit.nom, CuisinePlat.nom)
    top_q = (
        db.query(
            _nom_col.label("nom"),
            func.sum(BarLigneVente.sous_total).label("total"),
            func.sum(BarLigneVente.quantite).label("quantite"),
        )
        .select_from(BarLigneVente)
        .outerjoin(BarProduit,  BarProduit.id  == BarLigneVente.produit_id)
        .outerjoin(CuisinePlat, CuisinePlat.id == BarLigneVente.cuisine_plat_id)
        .join(BarVente, BarVente.id == BarLigneVente.vente_id)
        .filter(
            BarVente.date_heure >= dt_debut,
            BarVente.date_heure <= dt_fin,
            BarVente.statut     != "ANNULEE",
        )
    )
    if caissier_id:   top_q = top_q.filter(BarVente.caissier_id   == caissier_id)
    if mode_paiement: top_q = top_q.filter(BarVente.mode_paiement == mode_paiement)
    top_prods = (
        top_q.group_by(_nom_col)
        .order_by(func.sum(BarLigneVente.sous_total).desc())
        .limit(8).all()
    )

    par_caissier: dict = {}
    for v in ventes:
        cid = v.caissier_id or 0
        nom = (v.caissier.nom + " " + v.caissier.prenom) if v.caissier else "Sans caissier"
        if cid not in par_caissier:
            par_caissier[cid] = {"nom": nom, "total": 0.0, "nb_ventes": 0, "cash": 0.0, "credit": 0.0}
        par_caissier[cid]["total"]     += float(v.montant_total)
        par_caissier[cid]["nb_ventes"] += 1
        if v.mode_paiement in ("CASH",   "MIXTE"): par_caissier[cid]["cash"]   += float(v.montant_paye)
        if v.mode_paiement in ("CREDIT", "MIXTE"): par_caissier[cid]["credit"] += float(v.montant_restant)

    # ── Dettes & Remboursements ───────────────────────────────────────
    # Totaux globaux (toutes périodes confondues)
    all_credits = db.query(BarCredit).all()
    total_du        = sum(float(c.montant_du)        for c in all_credits)
    total_rembourse = sum(float(c.montant_rembourse) for c in all_credits)
    solde_total     = sum(float(c.solde)             for c in all_credits)
    nb_ouvertes = sum(1 for c in all_credits if c.statut in ("OUVERT", "EN_RETARD"))
    nb_soldes   = sum(1 for c in all_credits if c.statut == "SOLDE")

    rembs_periode = db.query(BarRemboursement).filter(
        BarRemboursement.date_remb >= dt_debut,
        BarRemboursement.date_remb <= dt_fin,
    ).all()
    rembs_periode_total = sum(float(r.montant) for r in rembs_periode)

    rembs_par_jour: dict = {}
    for r in rembs_periode:
        k = r.date_remb.astimezone(timezone.utc).date().isoformat()
        rembs_par_jour[k] = rembs_par_jour.get(k, 0.0) + float(r.montant)
    rembs_par_jour_list = [{"date": k, "montant": v} for k, v in sorted(rembs_par_jour.items())]

    # par_client : crédits créés dans la période sélectionnée
    credits_periode = db.query(BarCredit).filter(
        BarCredit.date_creation >= dt_debut,
        BarCredit.date_creation <= dt_fin,
    ).all()
    par_client: dict = {}
    for c in credits_periode:
        k = c.client_nom or "Client inconnu"
        if k not in par_client:
            par_client[k] = {"client": k, "montant_du": 0.0, "rembourse": 0.0, "solde": 0.0, "nb": 0, "statut": "SOLDE"}
        par_client[k]["montant_du"] += float(c.montant_du)
        par_client[k]["rembourse"]  += float(c.montant_rembourse)
        par_client[k]["solde"]      += float(c.solde)
        par_client[k]["nb"]         += 1
        if c.statut != "SOLDE":
            par_client[k]["statut"] = "OUVERT"
    par_client_list = sorted(par_client.values(), key=lambda x: x["solde"], reverse=True)

    heure_fmt = "%H:%M" if is_single else "%d/%m %H:%M"
    return {
        "meta": {
            "date_debut":  d_debut.isoformat(),
            "date_fin":    d_fin.isoformat(),
            "nb_jours":    nb_jours,
            "is_single":   is_single,
        },
        "kpis": {
            "ca_jour":     ca,
            "cash_jour":   cash,
            "credit_jour": credit,
            "nb_ventes":   len(ventes),
            "ca_hier":     ca_prev,
        },
        "par_periode":  par_periode_list,
        "par_mode":     par_mode,
        "top_produits": [
            {"nom": p.nom, "total": float(p.total or 0), "quantite": int(p.quantite or 0)}
            for p in top_prods
        ],
        "par_caissier": sorted(par_caissier.values(), key=lambda x: x["total"], reverse=True),
        "recents": [
            {
                "heure":    r.date_heure.astimezone(timezone.utc).strftime(heure_fmt),
                "caissier": (r.caissier.nom + " " + r.caissier.prenom) if r.caissier else "—",
                "montant":  float(r.montant_total),
                "mode":     r.mode_paiement or "—",
                "ticket":   r.numero_ticket or "—",
                "statut":   r.statut or "—",
            }
            for r in ventes[:15]
        ],
        "dettes": {
            "total_du":        total_du,
            "total_rembourse": total_rembourse,
            "solde_total":     solde_total,
            "nb_ouvertes":     nb_ouvertes,
            "nb_soldes":       nb_soldes,
            "rembs_periode":   rembs_periode_total,
            "rembs_par_jour":  rembs_par_jour_list,
            "par_client":      par_client_list,
        },
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
                "cuisine_plat_id":         l.cuisine_plat_id,
                "produit_nom":             (
                    l.produit.nom        if l.produit       else
                    l.cuisine_plat.nom   if l.cuisine_plat  else "—"
                ),
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
                "id":            l.id,
                "produit_id":    l.produit_id,
                "produit_nom":   l.produit.nom if l.produit else f"Produit#{l.produit_id}",
                "quantite":      float(l.quantite),
                "notes":         l.notes,
                "prix_unitaire": float(prix_actif(l.produit_id, db) or 0),
                "sous_total":    float(l.quantite) * float(prix_actif(l.produit_id, db) or 0),
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


@router.post("/commandes/{commande_id}/lignes", status_code=201)
def ajouter_ligne_commande(commande_id: int, data: AjoutLigneIn, db: Session = Depends(get_db)):
    cmd = db.query(BarCommande).filter_by(id=commande_id).first()
    if not cmd:
        raise HTTPException(404, "Commande introuvable")
    if cmd.statut in ("ENCAISSEE", "ANNULEE"):
        raise HTTPException(422, f"Commande déjà {cmd.statut.lower()}")
    p = db.query(BarProduit).filter_by(id=data.produit_id, actif=True).first()
    if not p:
        raise HTTPException(404, f"Produit #{data.produit_id} introuvable")
    existing = next((l for l in cmd.lignes if l.produit_id == data.produit_id), None)
    if existing:
        existing.quantite = float(existing.quantite) + data.quantite
    else:
        db.add(BarLigneCommande(commande_id=cmd.id, produit_id=data.produit_id, quantite=data.quantite))
    cmd.date_modification = datetime.now(tz=timezone.utc)
    db.commit()
    return {"ok": True}


@router.delete("/commandes/{commande_id}/lignes/{ligne_id}")
def retirer_ligne_commande(commande_id: int, ligne_id: int, db: Session = Depends(get_db)):
    cmd = db.query(BarCommande).filter_by(id=commande_id).first()
    if not cmd:
        raise HTTPException(404, "Commande introuvable")
    if cmd.statut in ("ENCAISSEE", "ANNULEE"):
        raise HTTPException(422, f"Commande déjà {cmd.statut.lower()}")
    ligne = db.query(BarLigneCommande).filter_by(id=ligne_id, commande_id=commande_id).first()
    if not ligne:
        raise HTTPException(404, "Article introuvable")
    db.delete(ligne)
    cmd.date_modification = datetime.now(tz=timezone.utc)
    db.commit()
    return {"ok": True}


@router.delete("/commandes/{commande_id}")
def liberer_commande(commande_id: int, db: Session = Depends(get_db)):
    cmd = db.query(BarCommande).filter_by(id=commande_id).first()
    if not cmd:
        raise HTTPException(404, "Commande introuvable")
    if cmd.statut in ("ENCAISSEE", "ANNULEE"):
        raise HTTPException(422, f"Table déjà {cmd.statut.lower()}")
    cmd.statut = "ANNULEE"
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
        "vente_id":        vente.id,
        "numero_ticket":   vente.numero_ticket,
        "montant_total":   float(vente.montant_total),
        "montant_paye":    float(vente.montant_paye),
        "montant_restant": float(vente.montant_restant),
        "mode_paiement":   vente.mode_paiement,
        "statut":          vente.statut,
        "client_nom":      vente.client_nom,
        "date_heure":      vente.date_heure.isoformat() if vente.date_heure else None,
        "lignes": [
            {
                "produit_nom":   l.produit.nom if l.produit else f"#{l.produit_id}",
                "quantite":      float(l.quantite),
                "prix_unitaire": float(l.prix_unitaire_applique),
                "sous_total":    float(l.sous_total),
            }
            for l in vente.lignes
        ],
    }


# ══════════════════════════════════════════════════════════════════
# CRÉDITS
# ══════════════════════════════════════════════════════════════════

@router.get("/credits")
def liste_credits(
    statut:      Optional[str] = None,
    caissier_id: Optional[int] = None,
    db: Session = Depends(get_db),
):
    q = db.query(BarCredit)
    if statut:
        q = q.filter(BarCredit.statut == statut.upper())
    if caissier_id:
        q = q.join(BarVente, BarCredit.vente_id == BarVente.id).filter(BarVente.caissier_id == caissier_id)
    credits = q.order_by(BarCredit.date_creation.desc()).all()

    return [
        {
            "id":               c.id,
            "vente_id":         c.vente_id,
            "client_nom":       c.client_nom,
            "client_contact":   c.client_contact,
            "client_nif":       c.client_nif,
            "montant_du":       float(c.montant_du),
            "montant_rembourse":float(c.montant_rembourse),
            "solde":            float(c.solde),
            "statut":           c.statut,
            "date_creation":    c.date_creation.isoformat(),
            "date_echeance":    str(c.date_echeance) if c.date_echeance else None,
            "nb_remboursements": len(c.remboursements),
            "caissier_id":      c.vente.caissier_id if c.vente else None,
        }
        for c in credits
    ]


@router.get("/credits/{credit_id}")
def detail_credit(credit_id: int, db: Session = Depends(get_db)):
    credit = db.query(BarCredit).filter_by(id=credit_id).first()
    if not credit:
        raise HTTPException(404, "Crédit introuvable")
    rembs = sorted(credit.remboursements, key=lambda r: r.date_remb or datetime.min, reverse=True)
    return {
        "id":                credit.id,
        "vente_id":          credit.vente_id,
        "client_nom":        credit.client_nom,
        "client_contact":    credit.client_contact,
        "client_nif":        credit.client_nif,
        "montant_du":        float(credit.montant_du),
        "montant_rembourse": float(credit.montant_rembourse),
        "solde":             float(credit.solde),
        "statut":            credit.statut,
        "date_creation":     credit.date_creation.isoformat(),
        "date_echeance":     str(credit.date_echeance) if credit.date_echeance else None,
        "caissier_id":       credit.vente.caissier_id if credit.vente else None,
        "remboursements": [
            {
                "id":      r.id,
                "montant": float(r.montant),
                "date":    r.date_remb.isoformat() if r.date_remb else None,
                "notes":   r.notes,
            }
            for r in rembs
        ],
    }


@router.put("/credits/{credit_id}")
def modifier_credit(credit_id: int, data: ModifierCreditIn, db: Session = Depends(get_db)):
    credit = db.query(BarCredit).filter_by(id=credit_id).first()
    if not credit:
        raise HTTPException(404, "Crédit introuvable")
    if data.client_nom     is not None: credit.client_nom     = data.client_nom
    if data.client_contact is not None: credit.client_contact = data.client_contact
    if data.client_nif     is not None: credit.client_nif     = data.client_nif
    if data.date_echeance  is not None:
        from datetime import date as date_type
        try:
            credit.date_echeance = date_type.fromisoformat(data.date_echeance)
        except ValueError:
            raise HTTPException(422, "date_echeance invalide (format attendu : YYYY-MM-DD)")
    if data.statut is not None:
        if data.statut.upper() not in ("OUVERT", "SOLDE", "EN_RETARD"):
            raise HTTPException(422, "statut invalide — valeurs acceptées : OUVERT, SOLDE, EN_RETARD")
        credit.statut = data.statut.upper()
    db.commit()
    return {"ok": True, "id": credit.id, "statut": credit.statut}


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

    # Mettre à jour la vente associée (partiel ou total)
    vente = db.query(BarVente).filter_by(id=credit.vente_id).first()
    if vente:
        paye    = Decimal(str(vente.montant_paye))    + montant
        restant = Decimal(str(vente.montant_restant)) - montant
        vente.montant_paye    = min(Decimal(str(vente.montant_total)), paye)
        vente.montant_restant = max(Decimal("0"), restant)
        if credit.statut == "SOLDE":
            vente.montant_restant = Decimal("0")
            vente.montant_paye    = vente.montant_total
            vente.statut          = "PAYEE"

    db.commit()
    return {
        "remboursement_id": remb.id,
        "solde_restant":    float(credit.solde),
        "credit_statut":    credit.statut,
    }


@router.get("/credits/{credit_id}/remboursements")
def historique_remboursements(credit_id: int, db: Session = Depends(get_db)):
    credit = db.query(BarCredit).filter_by(id=credit_id).first()
    if not credit:
        raise HTTPException(404, "Crédit introuvable")
    rembs = sorted(credit.remboursements, key=lambda r: r.date_remb or datetime.min, reverse=True)
    return {
        "credit_id":      credit_id,
        "client_nom":     credit.client_nom,
        "montant_du":     float(credit.montant_du),
        "solde":          float(credit.solde),
        "statut":         credit.statut,
        "remboursements": [
            {
                "id":      r.id,
                "montant": float(r.montant),
                "date":    r.date_remb.isoformat() if r.date_remb else None,
                "notes":   r.notes,
            }
            for r in rembs
        ],
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


# ══════════════════════════════════════════════════════════════════
# GRANDE CAISSE — tableau de bord financier consolidé
# ══════════════════════════════════════════════════════════════════

@router.get("/grande-caisse")
def grande_caisse(
    date_debut:  date_type    = Query(default=None),
    date_fin:    date_type    = Query(default=None),
    produit_id:  Optional[int] = Query(default=None),
    categorie:   Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    """
    Dashboard financier consolidé :
    coût réel des achats confirmés vs CA ventes sur la période.
    Bénéfice brut = ventes - achats.  Bénéfice net = brut - paie.
    """
    from datetime import date as dt
    today = dt.today()
    if not date_debut:
        date_debut = today.replace(day=1)
    if not date_fin:
        date_fin = today

    dt_debut = datetime.combine(date_debut, time.min).replace(tzinfo=timezone.utc)
    dt_fin   = datetime.combine(date_fin,   time.max).replace(tzinfo=timezone.utc)

    def _d(v) -> Decimal:
        return Decimal(str(v)) if v is not None else Decimal("0")

    # ── Achats bar confirmés sur la période ───────────────────────
    q_achats = (
        db.query(BarAchat)
        .filter(
            BarAchat.date_achat >= dt_debut,
            BarAchat.date_achat <= dt_fin,
            BarAchat.statut == "CONFIRME",
            BarAchat.produit_id.isnot(None),
        )
    )
    if produit_id:
        q_achats = q_achats.filter(BarAchat.produit_id == produit_id)
    if categorie:
        q_achats = q_achats.join(BarProduit, BarAchat.produit_id == BarProduit.id)\
                            .filter(BarProduit.categorie == categorie)

    achats = q_achats.all()

    # ── Ventes bar non annulées sur la période ────────────────────
    q_lignes = (
        db.query(BarLigneVente)
        .join(BarVente)
        .filter(
            BarVente.statut != "ANNULEE",
            BarVente.date_heure >= dt_debut,
            BarVente.date_heure <= dt_fin,
        )
    )
    if produit_id:
        q_lignes = q_lignes.filter(BarLigneVente.produit_id == produit_id)
    if categorie:
        q_lignes = q_lignes.join(BarProduit, BarLigneVente.produit_id == BarProduit.id)\
                            .filter(BarProduit.categorie == categorie)

    lignes_ventes = q_lignes.all()

    # ── Paie bar sur la période ───────────────────────────────────
    paie_total = _d(
        db.query(func.sum(BarPaiementEmploye.montant))
        .filter(
            BarPaiementEmploye.date_paiement >= dt_debut.date(),
            BarPaiementEmploye.date_paiement <= dt_fin.date(),
        )
        .scalar()
    )

    # ── Agrégation achats ─────────────────────────────────────────
    achats_cout_total = Decimal("0")
    par_produit: dict[int, dict] = {}
    achats_detail = []

    for a in achats:
        total_dep        = sum(_d(d.montant) for d in a.depenses)
        cout_marchandise = _d(a.quantite) * _d(a.prix_achat_unitaire)
        cout_total       = cout_marchandise + total_dep
        achats_cout_total += cout_total

        pid  = a.produit_id
        nom  = a.bar_produit.nom        if a.bar_produit else str(pid)
        cat  = a.bar_produit.categorie  if a.bar_produit else ""

        if pid not in par_produit:
            par_produit[pid] = {
                "produit_id":  pid,
                "produit_nom": nom,
                "categorie":   cat or "Autre",
                "achats_cout": Decimal("0"),
                "qte_achetee": Decimal("0"),
                "ventes_ca":   Decimal("0"),
                "qte_vendue":  Decimal("0"),
            }
        par_produit[pid]["achats_cout"] += cout_total
        par_produit[pid]["qte_achetee"] += _d(a.quantite)

        achats_detail.append({
            "id":                  a.id,
            "produit_id":          pid,
            "produit_nom":         nom,
            "categorie":           cat or "Autre",
            "date_achat":          a.date_achat.isoformat(),
            "fournisseur":         a.fournisseur or "—",
            "quantite":            float(_d(a.quantite)),
            "prix_achat_unitaire": float(_d(a.prix_achat_unitaire)),
            "cout_marchandise":    float(cout_marchandise),
            "total_depenses":      float(total_dep),
            "cout_total":          float(cout_total),
            "statut":              a.statut,
        })

    # ── Agrégation ventes ─────────────────────────────────────────
    ventes_ca_total = Decimal("0")

    for lv in lignes_ventes:
        ca  = _d(lv.sous_total)
        # Clé unique : positif = bar_produit, négatif = cuisine_plat
        pid = lv.produit_id if lv.produit_id is not None else -(lv.cuisine_plat_id or 0)
        ventes_ca_total += ca

        if pid not in par_produit:
            par_produit[pid] = {
                "produit_id":  lv.produit_id,
                "produit_nom": (
                    lv.produit.nom      if lv.produit      else
                    lv.cuisine_plat.nom if lv.cuisine_plat else "—"
                ),
                "categorie":   lv.produit.categorie if lv.produit else "Cuisine",
                "achats_cout": Decimal("0"),
                "qte_achetee": Decimal("0"),
                "ventes_ca":   Decimal("0"),
                "qte_vendue":  Decimal("0"),
            }
        par_produit[pid]["ventes_ca"]  += ca
        par_produit[pid]["qte_vendue"] += _d(lv.quantite)

    # ── Synthèse par produit ──────────────────────────────────────
    produits_list = []
    for pp in par_produit.values():
        ben   = pp["ventes_ca"] - pp["achats_cout"]
        marge = (ben / pp["ventes_ca"] * 100).quantize(Decimal("0.01")) \
                if pp["ventes_ca"] > 0 else Decimal("0")
        produits_list.append({
            "produit_id":  pp["produit_id"],
            "produit_nom": pp["produit_nom"],
            "categorie":   pp["categorie"],
            "achats_cout": float(pp["achats_cout"]),
            "qte_achetee": float(pp["qte_achetee"]),
            "ventes_ca":   float(pp["ventes_ca"]),
            "qte_vendue":  float(pp["qte_vendue"]),
            "benefice":    float(ben),
            "marge_pct":   float(marge),
        })

    produits_list.sort(key=lambda x: x["ventes_ca"], reverse=True)

    benefice_brut = ventes_ca_total - achats_cout_total
    benefice_net  = benefice_brut - paie_total

    return {
        "periode":    {"debut": str(date_debut), "fin": str(date_fin)},
        "bar": {
            "achats_cout":   float(achats_cout_total),
            "ventes_ca":     float(ventes_ca_total),
            "paie":          float(paie_total),
            "benefice_brut": float(benefice_brut),
            "benefice_net":  float(benefice_net),
            "nb_achats":     len(achats),
            "nb_ventes":     sum(1 for lv in lignes_ventes),
        },
        "par_produit":   produits_list,
        "achats_detail": sorted(achats_detail, key=lambda x: x["date_achat"], reverse=True),
    }
