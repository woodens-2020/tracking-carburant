"""
Module Pâtisserie — articles, stock, achats, ventes comptoir, commandes
spéciales (suivi configurable), dépenses, contrôle de caisse.

Préfixe : /api/patisserie

Architecture reprise du module Bar (source de vérité du stock = ledger de
mouvements) et du module Cuisine (dépenses/achats/tickets), simplifiée là où
la pâtisserie n'a pas demandé crédit/remboursement ni historique de prix.

Nouveauté propre à ce module : les commandes spéciales suivent une chaîne
d'ÉTAPES CONFIGURABLES (PatisserieEtapeSuivi) au lieu d'un statut figé dans
le code — l'admin définit sa propre chaîne (Paramètres > Pâtisserie > Étapes).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, date as date_type, time
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from models import (
    PatisserieCategorie, PatisserieProduit, PatisserieAchat, PatisserieMouvementStock,
    PatisserieSessionCaisse, PatisserieVente, PatisserieLigneVente, PatisserieDepense,
    PatisserieEtapeSuivi, PatisserieCommande, PatisserieLigneCommande, PatisserieCommandeSuivi,
    RenflouementDepartement, Employe, Utilisateur,
)
from tz_utils import today_haiti, bounds_haiti, HAITI_TZ

router = APIRouter(prefix="/api/patisserie", tags=["Patisserie"])

MAX_SESSIONS_PAR_JOUR = int(os.getenv("MAX_SESSIONS_PAR_JOUR", "3"))


# ══════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════

def _dec(v) -> Decimal:
    return Decimal(str(v)) if v is not None else Decimal("0")


def _uid(request: Request) -> Optional[int]:
    user = getattr(request.state, "user", None)
    return user.id if user else None


def _require_pdg_ou_admin_patisserie(request: Request, db: Session = Depends(get_db)) -> Utilisateur:
    """Même logique que main.require_pdg_ou_admin — dupliquée pour éviter un
    import circulaire entre routers (convention déjà suivie par les autres
    modules, voir cuisine_routes.py/caisse_routes.py)."""
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


def _parse_date_saisie(raw):
    """Voir cuisine_routes.py::_parse_date_saisie — même logique (midi UTC
    pour éviter le glissement de jour au réaffichage)."""
    if not raw:
        return None
    try:
        if "T" in raw:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return datetime.combine(date_type.fromisoformat(raw), time(12, 0)).replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


def _generer_ticket(db: Session) -> str:
    today = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
    prefix = f"PT{today}"
    last = (
        db.query(PatisserieVente.numero_ticket)
        .filter(PatisserieVente.numero_ticket.like(f"{prefix}%"))
        .with_for_update()
        .order_by(PatisserieVente.numero_ticket.desc())
        .first()
    )
    seq = int(last.numero_ticket[len(prefix):]) + 1 if last else 1
    return f"{prefix}{str(seq).zfill(4)}"


def _generer_numero_commande(db: Session) -> str:
    today = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
    prefix = f"CMD{today}"
    last = (
        db.query(PatisserieCommande.numero)
        .filter(PatisserieCommande.numero.like(f"{prefix}%"))
        .with_for_update()
        .order_by(PatisserieCommande.numero.desc())
        .first()
    )
    seq = int(last.numero[len(prefix):]) + 1 if last else 1
    return f"{prefix}{str(seq).zfill(3)}"


def _appliquer_mouvement_stock(
    db: Session, *, produit: PatisserieProduit, type_mouvement: str, quantite_signee: Decimal,
    motif: str = None, reference_vente_id: int = None, reference_commande_id: int = None,
    achat_id: int = None, utilisateur_id: int = None,
) -> PatisserieMouvementStock:
    """Point d'entrée UNIQUE pour toucher au stock : écrit le mouvement (source
    de vérité) puis met à jour le cache dénormalisé `stock_actuel`. N'importe
    quelle autre façon de modifier le stock serait un bug — voir docstring de
    PatisserieProduit."""
    mvt = PatisserieMouvementStock(
        produit_id=produit.id, type_mouvement=type_mouvement, quantite=quantite_signee,
        motif=motif, reference_vente_id=reference_vente_id,
        reference_commande_id=reference_commande_id, achat_id=achat_id,
        utilisateur_id=utilisateur_id,
    )
    db.add(mvt)
    produit.stock_actuel = _dec(produit.stock_actuel) + quantite_signee
    return mvt


def _produit_dict(p: PatisserieProduit) -> dict:
    stock = float(_dec(p.stock_actuel))
    seuil = float(_dec(p.seuil_alerte_stock))
    return {
        "id": p.id, "nom": p.nom, "categorie": p.categorie or "",
        "unite": p.unite, "prix_vente": float(_dec(p.prix_vente)),
        "cout_unitaire_estime": float(_dec(p.cout_unitaire_estime)) if p.cout_unitaire_estime else None,
        "stock_actuel": stock, "seuil_alerte_stock": seuil,
        "stock_bas": seuil > 0 and stock <= seuil,
        "actif": p.actif,
        "photo": f"data:{p.photo_mime};base64,{p.photo_base64}" if p.photo_base64 else None,
    }


# ══════════════════════════════════════════════════════════════════
# CATÉGORIES
# ══════════════════════════════════════════════════════════════════

@router.get("/categories")
def liste_categories(db: Session = Depends(get_db)):
    cats = db.query(PatisserieCategorie).order_by(PatisserieCategorie.nom).all()
    return [{"id": c.id, "nom": c.nom, "couleur": c.couleur} for c in cats]


@router.post("/categories", status_code=201)
def creer_categorie(data: dict, db: Session = Depends(get_db)):
    nom = (data.get("nom") or "").strip()
    if not nom:
        raise HTTPException(400, "Le nom est requis")
    if db.query(PatisserieCategorie).filter_by(nom=nom).first():
        raise HTTPException(409, "Cette catégorie existe déjà")
    c = PatisserieCategorie(nom=nom, couleur=(data.get("couleur") or "").strip() or None)
    db.add(c); db.commit(); db.refresh(c)
    return {"id": c.id, "nom": c.nom, "couleur": c.couleur}


@router.delete("/categories/{cat_id}")
def supprimer_categorie(cat_id: int, db: Session = Depends(get_db)):
    c = db.query(PatisserieCategorie).filter_by(id=cat_id).first()
    if not c:
        raise HTTPException(404, "Catégorie introuvable")
    if db.query(PatisserieProduit).filter_by(categorie=c.nom).first():
        raise HTTPException(409, "Des articles utilisent encore cette catégorie")
    db.delete(c); db.commit()
    return {"message": "Catégorie supprimée"}


# ══════════════════════════════════════════════════════════════════
# ARTICLES (PRODUITS)
# ══════════════════════════════════════════════════════════════════

@router.get("/produits")
def liste_produits(actif: Optional[bool] = Query(default=None), db: Session = Depends(get_db)):
    q = db.query(PatisserieProduit).order_by(PatisserieProduit.categorie, PatisserieProduit.nom)
    if actif is not None:
        q = q.filter(PatisserieProduit.actif == actif)
    return [_produit_dict(p) for p in q.all()]


@router.post("/produits", status_code=201)
def creer_produit(data: dict, db: Session = Depends(get_db)):
    nom = (data.get("nom") or "").strip()
    if not nom:
        raise HTTPException(400, "Le nom est requis")
    if db.query(PatisserieProduit).filter_by(nom=nom).first():
        raise HTTPException(409, f"L'article '{nom}' existe déjà")
    prix = float(data.get("prix_vente") or 0)
    if prix <= 0:
        raise HTTPException(400, "Le prix de vente doit être positif")
    cout = data.get("cout_unitaire_estime")
    p = PatisserieProduit(
        nom=nom,
        categorie=(data.get("categorie") or "").strip() or None,
        unite=(data.get("unite") or "unite").strip() or "unite",
        prix_vente=Decimal(str(prix)),
        cout_unitaire_estime=Decimal(str(cout)) if cout else None,
        seuil_alerte_stock=Decimal(str(data.get("seuil_alerte_stock") or 0)),
        photo_base64=data.get("photo_base64") or None,
        photo_mime=data.get("photo_mime") or None,
        actif=True,
    )
    db.add(p); db.commit(); db.refresh(p)
    return _produit_dict(p)


@router.put("/produits/{produit_id}")
def modifier_produit(produit_id: int, data: dict, db: Session = Depends(get_db)):
    p = db.query(PatisserieProduit).filter_by(id=produit_id).first()
    if not p:
        raise HTTPException(404, "Article introuvable")
    if "nom" in data and data["nom"]:
        p.nom = data["nom"].strip()
    if "categorie" in data:
        p.categorie = (data["categorie"] or "").strip() or None
    if "unite" in data and data["unite"]:
        p.unite = data["unite"].strip()
    if "prix_vente" in data:
        pv = float(data["prix_vente"] or 0)
        if pv <= 0:
            raise HTTPException(400, "Le prix de vente doit être > 0")
        p.prix_vente = Decimal(str(pv))
    if "cout_unitaire_estime" in data:
        p.cout_unitaire_estime = Decimal(str(data["cout_unitaire_estime"])) if data.get("cout_unitaire_estime") else None
    if "seuil_alerte_stock" in data:
        p.seuil_alerte_stock = Decimal(str(data["seuil_alerte_stock"] or 0))
    if "photo_base64" in data:
        p.photo_base64 = data["photo_base64"] or None
        p.photo_mime   = data.get("photo_mime") or None
    if "actif" in data:
        p.actif = bool(data["actif"])
    db.commit(); db.refresh(p)
    return _produit_dict(p)


@router.delete("/produits/{produit_id}")
def desactiver_produit(produit_id: int, db: Session = Depends(get_db)):
    p = db.query(PatisserieProduit).filter_by(id=produit_id).first()
    if not p:
        raise HTTPException(404, "Article introuvable")
    p.actif = False
    db.commit()
    return {"message": "Article désactivé"}


@router.post("/produits/{produit_id}/ajustement")
def ajuster_stock(produit_id: int, data: dict, request: Request, db: Session = Depends(get_db)):
    """Correction manuelle du stock (inventaire, perte, casse) — toujours
    tracée par un motif obligatoire, jamais un simple `stock_actuel = X`."""
    p = db.query(PatisserieProduit).filter_by(id=produit_id).first()
    if not p:
        raise HTTPException(404, "Article introuvable")
    motif = (data.get("motif") or "").strip()
    if not motif:
        raise HTTPException(400, "Un motif est requis pour tout ajustement de stock")
    ecart = Decimal(str(data.get("ecart") or 0))
    if ecart == 0:
        raise HTTPException(400, "L'écart doit être différent de zéro")
    type_mvt = "PERTE" if ecart < 0 else "AJUSTEMENT"
    _appliquer_mouvement_stock(
        db, produit=p, type_mouvement=type_mvt, quantite_signee=ecart,
        motif=motif, utilisateur_id=_uid(request),
    )
    db.commit()
    return _produit_dict(p)


@router.get("/produits/{produit_id}/mouvements")
def historique_mouvements(produit_id: int, limit: int = Query(100, le=500), db: Session = Depends(get_db)):
    p = db.query(PatisserieProduit).filter_by(id=produit_id).first()
    if not p:
        raise HTTPException(404, "Article introuvable")
    mvts = (
        db.query(PatisserieMouvementStock)
        .filter_by(produit_id=produit_id)
        .order_by(PatisserieMouvementStock.date_mouvement.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": m.id, "type": m.type_mouvement, "quantite": float(_dec(m.quantite)),
            "motif": m.motif, "date": m.date_mouvement.isoformat(),
            "reference_vente_id": m.reference_vente_id,
            "reference_commande_id": m.reference_commande_id,
            "achat_id": m.achat_id,
        }
        for m in mvts
    ]


# ══════════════════════════════════════════════════════════════════
# ACHATS
# ══════════════════════════════════════════════════════════════════

def _achat_dict(a: PatisserieAchat) -> dict:
    return {
        "id": a.id, "produit_id": a.produit_id, "produit_nom": a.produit.nom if a.produit else "?",
        "quantite": float(_dec(a.quantite)), "prix_achat_unitaire": float(_dec(a.prix_achat_unitaire)),
        "total": float(_dec(a.total)), "fournisseur": a.fournisseur or "",
        "date_achat": a.date_achat.isoformat() if a.date_achat else None,
        "jours": (today_haiti() - a.date_achat.date()).days if a.date_achat else None,
        "statut": a.statut, "notes": a.notes or "",
    }


@router.get("/achats")
def liste_achats(
    produit_id: Optional[int] = Query(default=None),
    date_debut: Optional[date_type] = Query(default=None),
    date_fin:   Optional[date_type] = Query(default=None),
    db: Session = Depends(get_db),
):
    today = today_haiti()
    if not date_debut: date_debut = today.replace(day=1)
    if not date_fin:   date_fin   = today
    dt_deb, _ = bounds_haiti(date_debut)
    _, dt_fin = bounds_haiti(date_fin)

    q = db.query(PatisserieAchat).filter(
        PatisserieAchat.date_achat >= dt_deb, PatisserieAchat.date_achat <= dt_fin,
    )
    if produit_id:
        q = q.filter(PatisserieAchat.produit_id == produit_id)
    achats = q.order_by(PatisserieAchat.date_achat.desc()).all()
    total = sum(float(_dec(a.total)) for a in achats)
    return {"achats": [_achat_dict(a) for a in achats], "total_achats": round(total, 2), "nb_achats": len(achats)}


@router.post("/achats", status_code=201)
def creer_achat(data: dict, request: Request, db: Session = Depends(get_db)):
    produit_id = data.get("produit_id")
    produit = db.query(PatisserieProduit).filter_by(id=produit_id).first() if produit_id else None
    if not produit:
        raise HTTPException(400, "Article introuvable")
    qte = float(data.get("quantite") or 0)
    if qte <= 0:
        raise HTTPException(400, "La quantité doit être positive")
    prix = float(data.get("prix_achat_unitaire") or 0)
    if prix < 0:
        raise HTTPException(400, "Le prix d'achat doit être ≥ 0")

    date_achat = _parse_date_saisie(data.get("date_achat")) or datetime.now(timezone.utc)

    a = PatisserieAchat(
        produit_id=produit.id, quantite=Decimal(str(qte)), prix_achat_unitaire=Decimal(str(prix)),
        total=Decimal(str(round(qte * prix, 2))), fournisseur=(data.get("fournisseur") or "").strip() or None,
        date_achat=date_achat, utilisateur_id=_uid(request), notes=(data.get("notes") or "").strip() or None,
        statut="EN_ATTENTE",
    )
    db.add(a); db.commit(); db.refresh(a)
    return _achat_dict(a)


@router.post("/achats/{achat_id}/confirmer")
def confirmer_achat(achat_id: int, request: Request, db: Session = Depends(get_db)):
    """Fait entrer la marchandise en stock — un seul mouvement ENTREE par
    achat (protégé par le changement de statut, jamais rejouable)."""
    a = db.query(PatisserieAchat).filter_by(id=achat_id).first()
    if not a:
        raise HTTPException(404, "Achat introuvable")
    if a.statut != "EN_ATTENTE":
        raise HTTPException(409, f"Cet achat est déjà '{a.statut}' — impossible de le confirmer à nouveau.")
    _appliquer_mouvement_stock(
        db, produit=a.produit, type_mouvement="ENTREE", quantite_signee=_dec(a.quantite),
        motif=f"Achat #{a.id}" + (f" — {a.fournisseur}" if a.fournisseur else ""),
        achat_id=a.id, utilisateur_id=_uid(request),
    )
    a.statut = "CONFIRME"
    db.commit()
    return _achat_dict(a)


@router.post("/achats/{achat_id}/annuler")
def annuler_achat(achat_id: int, db: Session = Depends(get_db)):
    a = db.query(PatisserieAchat).filter_by(id=achat_id).first()
    if not a:
        raise HTTPException(404, "Achat introuvable")
    if a.statut == "CONFIRME":
        raise HTTPException(409, "Un achat déjà confirmé (stock mis à jour) ne peut pas être annulé — faites un ajustement de stock si besoin.")
    a.statut = "ANNULE"
    db.commit()
    return {"message": "Achat annulé"}


# ══════════════════════════════════════════════════════════════════
# DÉPENSES
# ══════════════════════════════════════════════════════════════════

@router.get("/depenses")
def liste_depenses(
    date_debut: Optional[date_type] = Query(default=None),
    date_fin:   Optional[date_type] = Query(default=None),
    db: Session = Depends(get_db),
):
    today = today_haiti()
    if not date_debut: date_debut = today.replace(day=1)
    if not date_fin:   date_fin   = today
    dt_deb, _ = bounds_haiti(date_debut)
    _, dt_fin = bounds_haiti(date_fin)

    deps = (
        db.query(PatisserieDepense)
        .filter(PatisserieDepense.date_depense >= dt_deb, PatisserieDepense.date_depense <= dt_fin)
        .order_by(PatisserieDepense.date_depense.desc())
        .all()
    )
    lignes = [
        {
            "id": d.id, "description": d.description, "categorie": d.categorie or "AUTRE",
            "montant": float(_dec(d.montant)), "date_depense": d.date_depense.isoformat(),
            "jours": (today - d.date_depense.date()).days,
            "fournisseur": d.fournisseur or "", "notes": d.notes or "",
        }
        for d in deps
    ]
    return {
        "depenses": lignes,
        "total_depenses": round(sum(l["montant"] for l in lignes), 2),
        "nb_depenses": len(lignes),
    }


@router.post("/depenses", status_code=201)
def ajouter_depense(data: dict, db: Session = Depends(get_db)):
    desc = (data.get("description") or "").strip()
    if not desc:
        raise HTTPException(400, "La description est requise")
    montant = float(data.get("montant") or 0)
    if montant <= 0:
        raise HTTPException(400, "Le montant doit être positif")
    d = PatisserieDepense(
        description=desc, categorie=data.get("categorie") or "AUTRE", montant=Decimal(str(montant)),
        date_depense=_parse_date_saisie(data.get("date_depense")) or datetime.now(timezone.utc),
        fournisseur=(data.get("fournisseur") or "").strip() or None,
        notes=(data.get("notes") or "").strip() or None,
    )
    db.add(d); db.commit(); db.refresh(d)
    return {"id": d.id, "message": "Dépense enregistrée"}


@router.put("/depenses/{dep_id}")
def modifier_depense(dep_id: int, data: dict, db: Session = Depends(get_db)):
    d = db.query(PatisserieDepense).filter_by(id=dep_id).first()
    if not d:
        raise HTTPException(404, "Dépense introuvable")
    if "description" in data and data["description"]:
        d.description = data["description"].strip()
    if "categorie" in data: d.categorie = data["categorie"] or "AUTRE"
    if "montant" in data:
        montant = float(data["montant"] or 0)
        if montant <= 0:
            raise HTTPException(400, "Le montant doit être positif")
        d.montant = Decimal(str(montant))
    if "fournisseur" in data: d.fournisseur = (data["fournisseur"] or "").strip() or None
    if "notes" in data:       d.notes       = (data["notes"] or "").strip() or None
    if "date_depense" in data:
        parsed = _parse_date_saisie(data.get("date_depense"))
        if parsed:
            d.date_depense = parsed
    db.commit()
    return {"message": "Dépense modifiée"}


@router.delete("/depenses/{dep_id}")
def supprimer_depense(dep_id: int, db: Session = Depends(get_db)):
    d = db.query(PatisserieDepense).filter_by(id=dep_id).first()
    if not d:
        raise HTTPException(404, "Dépense introuvable")
    db.delete(d); db.commit()
    return {"message": "Dépense supprimée"}


# ══════════════════════════════════════════════════════════════════
# VENTES COMPTOIR (POS)
# ══════════════════════════════════════════════════════════════════

def _session_en_cours(caissier_id: int, db: Session) -> Optional[PatisserieSessionCaisse]:
    aujourd_hui = today_haiti()
    return (
        db.query(PatisserieSessionCaisse)
        .filter_by(caissier_id=caissier_id, date_session=aujourd_hui, statut="EN_COURS")
        .first()
    )


@router.get("/ventes")
def liste_ventes(
    date_debut: Optional[date_type] = Query(default=None),
    date_fin:   Optional[date_type] = Query(default=None),
    caissier_id: Optional[int] = Query(default=None),
    db: Session = Depends(get_db),
):
    today = today_haiti()
    if not date_debut: date_debut = today
    if not date_fin:   date_fin   = today
    dt_deb, _ = bounds_haiti(date_debut)
    _, dt_fin = bounds_haiti(date_fin)

    q = db.query(PatisserieVente).filter(
        PatisserieVente.date_heure >= dt_deb, PatisserieVente.date_heure <= dt_fin,
        PatisserieVente.statut != "ANNULEE",
    )
    if caissier_id:
        q = q.filter(PatisserieVente.caissier_id == caissier_id)
    ventes = q.order_by(PatisserieVente.date_heure.desc()).all()
    return [
        {
            "id": v.id, "numero_ticket": v.numero_ticket, "date_heure": v.date_heure.isoformat(),
            "montant_total": float(_dec(v.montant_total)), "mode_paiement": v.mode_paiement,
            "client_nom": v.client_nom or "", "statut": v.statut,
            "caissier_nom": (v.caissier.nom + " " + v.caissier.prenom) if v.caissier else None,
            "lignes": [
                {
                    "produit_id": l.produit_id, "produit_nom": l.produit.nom if l.produit else "?",
                    "quantite": float(_dec(l.quantite)), "prix_unitaire": float(_dec(l.prix_unitaire_applique)),
                    "sous_total": float(_dec(l.sous_total)),
                }
                for l in v.lignes
            ],
        }
        for v in ventes
    ]


@router.post("/ventes", status_code=201)
def enregistrer_vente(data: dict, request: Request, db: Session = Depends(get_db)):
    lignes_data = data.get("lignes") or []
    if not lignes_data:
        raise HTTPException(400, "La vente doit contenir au moins un article")

    caissier_id = data.get("caissier_id")
    session = _session_en_cours(caissier_id, db) if caissier_id else None

    total = Decimal("0")
    ligne_objs = []
    mouvements_a_faire = []

    for l in lignes_data:
        produit = db.query(PatisserieProduit).filter_by(id=l.get("produit_id")).first()
        if not produit:
            raise HTTPException(400, f"Article introuvable : {l.get('produit_id')}")
        qte = Decimal(str(l.get("quantite") or 0))
        if qte <= 0:
            raise HTTPException(400, f"Quantité invalide pour '{produit.nom}'")
        if _dec(produit.stock_actuel) < qte:
            raise HTTPException(409, f"Stock insuffisant pour '{produit.nom}' (disponible : {produit.stock_actuel})")
        prix = Decimal(str(l.get("prix_unitaire") if l.get("prix_unitaire") is not None else produit.prix_vente))
        sous = (prix * qte).quantize(Decimal("0.01"))
        total += sous
        ligne_objs.append(PatisserieLigneVente(
            produit_id=produit.id, quantite=qte, prix_unitaire_applique=prix, sous_total=sous,
        ))
        mouvements_a_faire.append((produit, qte))

    vente = PatisserieVente(
        numero_ticket=_generer_ticket(db), caissier_id=caissier_id,
        session_id=session.id if session else None,
        montant_total=total, mode_paiement=data.get("mode_paiement") or "CASH",
        client_nom=(data.get("client_nom") or "").strip() or None, statut="PAYEE",
    )
    for l in ligne_objs:
        l.vente = vente
    db.add(vente); db.flush()

    for produit, qte in mouvements_a_faire:
        _appliquer_mouvement_stock(
            db, produit=produit, type_mouvement="SORTIE_VENTE", quantite_signee=-qte,
            motif=f"Vente {vente.numero_ticket}", reference_vente_id=vente.id, utilisateur_id=_uid(request),
        )

    db.commit(); db.refresh(vente)

    if any(_dec(p.stock_actuel) <= _dec(p.seuil_alerte_stock) for p, _ in mouvements_a_faire if p.seuil_alerte_stock and p.seuil_alerte_stock > 0):
        from notifications_service import creer_notification
        bas = [p.nom for p, _ in mouvements_a_faire if p.seuil_alerte_stock and _dec(p.stock_actuel) <= _dec(p.seuil_alerte_stock)]
        if bas:
            creer_notification(
                db, module="patisserie", type_="stock_bas",
                titre="Stock bas — Pâtisserie", message=", ".join(bas), lien="patisserie-produits",
            )
            db.commit()

    return {"id": vente.id, "numero_ticket": vente.numero_ticket, "montant_total": float(total), "message": "Vente enregistrée"}


@router.post("/ventes/{vente_id}/annuler")
def annuler_vente(vente_id: int, request: Request, db: Session = Depends(get_db)):
    """Annule la vente ET restitue le stock — jamais l'un sans l'autre."""
    v = db.query(PatisserieVente).filter_by(id=vente_id).first()
    if not v:
        raise HTTPException(404, "Vente introuvable")
    if v.statut == "ANNULEE":
        raise HTTPException(409, "Vente déjà annulée")
    for l in v.lignes:
        if l.produit_id:
            _appliquer_mouvement_stock(
                db, produit=l.produit, type_mouvement="AJUSTEMENT", quantite_signee=_dec(l.quantite),
                motif=f"Annulation vente {v.numero_ticket}", reference_vente_id=v.id, utilisateur_id=_uid(request),
            )
    v.statut = "ANNULEE"
    db.commit()
    return {"message": "Vente annulée, stock restitué"}


# ══════════════════════════════════════════════════════════════════
# ÉTAPES DE SUIVI (CONFIGURATION)
# ══════════════════════════════════════════════════════════════════

def _etape_dict(e: PatisserieEtapeSuivi) -> dict:
    return {
        "id": e.id, "code": e.code, "libelle": e.libelle, "ordre": e.ordre,
        "couleur": e.couleur or "#64748b", "est_initiale": e.est_initiale,
        "est_finale": e.est_finale, "actif": e.actif,
    }


@router.get("/etapes")
def liste_etapes(actif: Optional[bool] = Query(default=None), db: Session = Depends(get_db)):
    q = db.query(PatisserieEtapeSuivi).order_by(PatisserieEtapeSuivi.ordre)
    if actif is not None:
        q = q.filter(PatisserieEtapeSuivi.actif == actif)
    return [_etape_dict(e) for e in q.all()]


@router.post("/etapes", status_code=201)
def creer_etape(data: dict, _user: Utilisateur = Depends(_require_pdg_ou_admin_patisserie), db: Session = Depends(get_db)):
    libelle = (data.get("libelle") or "").strip()
    if not libelle:
        raise HTTPException(400, "Le libellé est requis")
    code = (data.get("code") or libelle).strip().lower().replace(" ", "-")
    if db.query(PatisserieEtapeSuivi).filter_by(code=code).first():
        raise HTTPException(409, "Une étape avec ce code existe déjà")
    dernier_ordre = db.query(PatisserieEtapeSuivi).count()
    e = PatisserieEtapeSuivi(
        code=code, libelle=libelle, ordre=data.get("ordre") or (dernier_ordre + 1),
        couleur=(data.get("couleur") or "").strip() or None,
        est_initiale=bool(data.get("est_initiale", False)), est_finale=bool(data.get("est_finale", False)),
    )
    if e.est_initiale:
        db.query(PatisserieEtapeSuivi).filter_by(est_initiale=True).update({"est_initiale": False})
    db.add(e); db.commit(); db.refresh(e)
    return _etape_dict(e)


@router.put("/etapes/{etape_id}")
def modifier_etape(etape_id: int, data: dict, _user: Utilisateur = Depends(_require_pdg_ou_admin_patisserie), db: Session = Depends(get_db)):
    e = db.query(PatisserieEtapeSuivi).filter_by(id=etape_id).first()
    if not e:
        raise HTTPException(404, "Étape introuvable")
    if "libelle" in data and data["libelle"]:
        e.libelle = data["libelle"].strip()
    if "ordre" in data:      e.ordre   = int(data["ordre"])
    if "couleur" in data:    e.couleur = (data["couleur"] or "").strip() or None
    if "actif" in data:      e.actif   = bool(data["actif"])
    if "est_finale" in data: e.est_finale = bool(data["est_finale"])
    if data.get("est_initiale"):
        db.query(PatisserieEtapeSuivi).filter(PatisserieEtapeSuivi.id != e.id).update({"est_initiale": False})
        e.est_initiale = True
    db.commit()
    return _etape_dict(e)


@router.delete("/etapes/{etape_id}")
def supprimer_etape(etape_id: int, _user: Utilisateur = Depends(_require_pdg_ou_admin_patisserie), db: Session = Depends(get_db)):
    e = db.query(PatisserieEtapeSuivi).filter_by(id=etape_id).first()
    if not e:
        raise HTTPException(404, "Étape introuvable")
    if db.query(PatisserieCommande).filter_by(etape_id=etape_id).first():
        raise HTTPException(409, "Des commandes utilisent encore cette étape — désactivez-la plutôt.")
    db.delete(e); db.commit()
    return {"message": "Étape supprimée"}


# ══════════════════════════════════════════════════════════════════
# COMMANDES (avec suivi configurable)
# ══════════════════════════════════════════════════════════════════

def _commande_dict(c: PatisserieCommande, detail: bool = False) -> dict:
    out = {
        "id": c.id, "numero": c.numero, "client_nom": c.client_nom, "client_telephone": c.client_telephone or "",
        "date_commande": c.date_commande.isoformat() if c.date_commande else None,
        "date_livraison_prevue": c.date_livraison_prevue.isoformat() if c.date_livraison_prevue else None,
        "montant_total": float(_dec(c.montant_total)), "acompte_verse": float(_dec(c.acompte_verse)),
        "solde_du": float(_dec(c.montant_total) - _dec(c.acompte_verse)),
        "etape_id": c.etape_id, "etape_libelle": c.etape.libelle if c.etape else None,
        "etape_couleur": c.etape.couleur if c.etape else None,
        "statut": c.statut, "notes": c.notes or "",
    }
    if detail:
        out["lignes"] = [
            {
                "id": l.id, "produit_id": l.produit_id, "description": l.description,
                "quantite": float(_dec(l.quantite)), "prix_unitaire": float(_dec(l.prix_unitaire)),
                "sous_total": float(_dec(l.sous_total)),
            }
            for l in c.lignes
        ]
        out["suivis"] = [
            {
                "id": s.id, "etape_libelle": s.etape.libelle if s.etape else None,
                "date_changement": s.date_changement.isoformat(),
                "utilisateur": s.utilisateur.nom_complet if s.utilisateur else None,
                "commentaire": s.commentaire or "",
            }
            for s in c.suivis
        ]
    return out


@router.get("/commandes")
def liste_commandes(
    etape_id: Optional[int] = Query(default=None),
    statut:   Optional[str] = Query(default=None),
    date_debut: Optional[date_type] = Query(default=None),
    date_fin:   Optional[date_type] = Query(default=None),
    db: Session = Depends(get_db),
):
    q = db.query(PatisserieCommande)
    if etape_id:
        q = q.filter(PatisserieCommande.etape_id == etape_id)
    if statut:
        q = q.filter(PatisserieCommande.statut == statut.upper())
    if date_debut:
        deb, _ = bounds_haiti(date_debut)
        q = q.filter(PatisserieCommande.date_commande >= deb)
    if date_fin:
        _, fin = bounds_haiti(date_fin)
        q = q.filter(PatisserieCommande.date_commande <= fin)
    commandes = q.order_by(PatisserieCommande.date_commande.desc()).all()
    return [_commande_dict(c) for c in commandes]


@router.get("/commandes/{commande_id}")
def detail_commande(commande_id: int, db: Session = Depends(get_db)):
    c = db.query(PatisserieCommande).filter_by(id=commande_id).first()
    if not c:
        raise HTTPException(404, "Commande introuvable")
    return _commande_dict(c, detail=True)


@router.post("/commandes", status_code=201)
def creer_commande(data: dict, request: Request, db: Session = Depends(get_db)):
    client_nom = (data.get("client_nom") or "").strip()
    if not client_nom:
        raise HTTPException(400, "Le nom du client est requis")
    lignes_data = data.get("lignes") or []
    if not lignes_data:
        raise HTTPException(400, "La commande doit contenir au moins un article")

    etape_initiale = db.query(PatisserieEtapeSuivi).filter_by(est_initiale=True, actif=True).first() \
        or db.query(PatisserieEtapeSuivi).filter_by(actif=True).order_by(PatisserieEtapeSuivi.ordre).first()
    if not etape_initiale:
        raise HTTPException(409, "Aucune étape de suivi configurée — voir Paramètres > Pâtisserie > Étapes")

    total = Decimal("0")
    ligne_objs = []
    for l in lignes_data:
        produit_id = l.get("produit_id")
        produit = db.query(PatisserieProduit).filter_by(id=produit_id).first() if produit_id else None
        qte = Decimal(str(l.get("quantite") or 1))
        if qte <= 0:
            raise HTTPException(400, "Quantité invalide")
        prix = Decimal(str(l.get("prix_unitaire") if l.get("prix_unitaire") is not None else (produit.prix_vente if produit else 0)))
        if prix <= 0:
            raise HTTPException(400, "Le prix unitaire doit être > 0 pour chaque ligne")
        sous = (prix * qte).quantize(Decimal("0.01"))
        total += sous
        description = (l.get("description") or "").strip() or (produit.nom if produit else "Article")
        ligne_objs.append(PatisserieLigneCommande(
            produit_id=produit.id if produit else None, description=description,
            quantite=qte, prix_unitaire=prix, sous_total=sous,
        ))

    commande = PatisserieCommande(
        numero=_generer_numero_commande(db), client_nom=client_nom,
        client_telephone=(data.get("client_telephone") or "").strip() or None,
        date_livraison_prevue=_parse_date_saisie(data.get("date_livraison_prevue")),
        montant_total=total, acompte_verse=Decimal(str(data.get("acompte_verse") or 0)),
        etape_id=etape_initiale.id, notes=(data.get("notes") or "").strip() or None,
        cree_par_id=_uid(request),
    )
    for l in ligne_objs:
        l.commande = commande
    db.add(commande); db.flush()

    db.add(PatisserieCommandeSuivi(
        commande_id=commande.id, etape_id=etape_initiale.id, utilisateur_id=_uid(request),
        commentaire="Commande créée",
    ))
    db.commit(); db.refresh(commande)
    return _commande_dict(commande, detail=True)


class ChangerEtapeIn(BaseModel):
    etape_id: int
    commentaire: Optional[str] = None


@router.patch("/commandes/{commande_id}/etape")
def changer_etape_commande(commande_id: int, body: ChangerEtapeIn, request: Request, db: Session = Depends(get_db)):
    """Fait avancer (ou reculer) une commande dans la chaîne d'étapes.
    Déduit le stock des articles catalogués UNE SEULE FOIS, la première fois
    que la commande atteint une étape finale (voir PatisserieCommande.stock_deduit)."""
    c = db.query(PatisserieCommande).filter_by(id=commande_id).first()
    if not c:
        raise HTTPException(404, "Commande introuvable")
    if c.statut == "ANNULEE":
        raise HTTPException(409, "Cette commande est annulée")
    nouvelle_etape = db.query(PatisserieEtapeSuivi).filter_by(id=body.etape_id).first()
    if not nouvelle_etape:
        raise HTTPException(404, "Étape introuvable")

    c.etape_id = nouvelle_etape.id
    db.add(PatisserieCommandeSuivi(
        commande_id=c.id, etape_id=nouvelle_etape.id, utilisateur_id=_uid(request),
        commentaire=(body.commentaire or "").strip() or None,
    ))

    if nouvelle_etape.est_finale and not c.stock_deduit:
        for l in c.lignes:
            if l.produit_id:
                _appliquer_mouvement_stock(
                    db, produit=l.produit, type_mouvement="SORTIE_COMMANDE", quantite_signee=-_dec(l.quantite),
                    motif=f"Commande {c.numero}", reference_commande_id=c.id, utilisateur_id=_uid(request),
                )
        c.stock_deduit = True

    db.commit(); db.refresh(c)
    return _commande_dict(c, detail=True)


@router.post("/commandes/{commande_id}/annuler")
def annuler_commande(commande_id: int, request: Request, db: Session = Depends(get_db)):
    c = db.query(PatisserieCommande).filter_by(id=commande_id).first()
    if not c:
        raise HTTPException(404, "Commande introuvable")
    if c.statut == "ANNULEE":
        raise HTTPException(409, "Commande déjà annulée")
    if c.stock_deduit:
        for l in c.lignes:
            if l.produit_id:
                _appliquer_mouvement_stock(
                    db, produit=l.produit, type_mouvement="AJUSTEMENT", quantite_signee=_dec(l.quantite),
                    motif=f"Annulation commande {c.numero}", reference_commande_id=c.id, utilisateur_id=_uid(request),
                )
    c.statut = "ANNULEE"
    db.commit()
    return {"message": "Commande annulée"}


@router.put("/commandes/{commande_id}/acompte")
def enregistrer_acompte(commande_id: int, data: dict, db: Session = Depends(get_db)):
    c = db.query(PatisserieCommande).filter_by(id=commande_id).first()
    if not c:
        raise HTTPException(404, "Commande introuvable")
    montant = float(data.get("acompte_verse") or 0)
    if montant < 0:
        raise HTTPException(400, "L'acompte doit être ≥ 0")
    c.acompte_verse = Decimal(str(montant))
    db.commit()
    return _commande_dict(c)


# ══════════════════════════════════════════════════════════════════
# CONTRÔLE DE CAISSE (sessions par caissier/jour)
# ══════════════════════════════════════════════════════════════════

def _session_dict(s: PatisserieSessionCaisse, stats: dict = None) -> dict:
    return {
        "id": s.id, "caissier_id": s.caissier_id,
        "caissier_nom": (s.caissier.nom + " " + s.caissier.prenom) if s.caissier else None,
        "date_session": str(s.date_session), "numero_session": s.numero_session,
        "statut": s.statut, "soumis_at": s.soumis_at.isoformat() if s.soumis_at else None,
        "valide_at": s.valide_at.isoformat() if s.valide_at else None,
        "valide_par": s.valide_par.nom_complet if s.valide_par else None,
        "cash_attendu_soumission": float(s.cash_attendu_soumission) if s.cash_attendu_soumission is not None else None,
        "montant_compte": float(s.montant_compte) if s.montant_compte is not None else None,
        "ecart": float(s.ecart) if s.ecart is not None else None,
        "notes_admin": s.notes_admin or "",
        **(stats or {}),
    }


def _ventes_session(session: PatisserieSessionCaisse, db: Session):
    _, jour_fin = bounds_haiti(session.date_session)
    suivante = (
        db.query(PatisserieSessionCaisse)
        .filter(
            PatisserieSessionCaisse.caissier_id == session.caissier_id,
            PatisserieSessionCaisse.date_session == session.date_session,
            PatisserieSessionCaisse.numero_session > session.numero_session,
        )
        .order_by(PatisserieSessionCaisse.numero_session)
        .first()
    )
    fin = suivante.created_at if suivante else jour_fin
    return (
        db.query(PatisserieVente)
        .filter(
            PatisserieVente.caissier_id == session.caissier_id,
            PatisserieVente.statut != "ANNULEE",
            PatisserieVente.date_heure >= session.created_at,
            PatisserieVente.date_heure < fin,
        )
        .order_by(PatisserieVente.date_heure)
        .all()
    )


def _stats_session(ventes: list[PatisserieVente]) -> dict:
    cash = sum(float(_dec(v.montant_total)) for v in ventes if v.mode_paiement == "CASH")
    total = sum(float(_dec(v.montant_total)) for v in ventes)
    return {"nb_ventes": len(ventes), "total_ventes": round(total, 2), "cash_attendu": round(cash, 2)}


@router.get("/caisse/caissiers")
def liste_caissiers(db: Session = Depends(get_db)):
    employes = db.query(Employe).filter(Employe.actif == True).order_by(Employe.nom, Employe.prenom).all()
    return [{"id": e.id, "nom": e.nom, "prenom": e.prenom, "poste": e.poste} for e in employes]


@router.get("/caisse/dashboard")
def dashboard_caissier(caissier_id: int = Query(...), db: Session = Depends(get_db)):
    employe = db.query(Employe).filter_by(id=caissier_id).first()
    if not employe:
        raise HTTPException(404, "Caissier introuvable")
    aujourd_hui = today_haiti()
    sessions_du_jour = (
        db.query(PatisserieSessionCaisse)
        .filter_by(caissier_id=caissier_id, date_session=aujourd_hui)
        .order_by(PatisserieSessionCaisse.numero_session)
        .all()
    )
    session = next((s for s in sessions_du_jour if s.statut == "EN_COURS"), None) \
        or (sessions_du_jour[-1] if sessions_du_jour else None)
    ventes = _ventes_session(session, db) if session else []
    stats = _stats_session(ventes)
    return {
        "caissier_id": caissier_id, "caissier_nom": employe.nom + " " + employe.prenom,
        "session_id": session.id if session else None,
        "session_statut": session.statut if session else None,
        "nb_sessions_jour": len(sessions_du_jour),
        "max_sessions_par_jour": MAX_SESSIONS_PAR_JOUR,
        "peut_ouvrir_nouvelle_session": len(sessions_du_jour) < MAX_SESSIONS_PAR_JOUR,
        **stats,
    }


@router.get("/caisse/sessions")
def liste_sessions(
    caissier_id: Optional[int] = Query(default=None),
    statut: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    q = db.query(PatisserieSessionCaisse)
    if caissier_id:
        q = q.filter(PatisserieSessionCaisse.caissier_id == caissier_id)
    if statut:
        q = q.filter(PatisserieSessionCaisse.statut == statut.upper())
    sessions = q.order_by(PatisserieSessionCaisse.date_session.desc(), PatisserieSessionCaisse.id.desc()).all()
    return [_session_dict(s, _stats_session(_ventes_session(s, db))) for s in sessions]


class OuvrirSessionIn(BaseModel):
    caissier_id: int


@router.post("/caisse/sessions/ouvrir")
def ouvrir_session(data: OuvrirSessionIn, db: Session = Depends(get_db)):
    employe = db.query(Employe).filter_by(id=data.caissier_id).first()
    if not employe:
        raise HTTPException(404, "Caissier introuvable")
    aujourd_hui = today_haiti()
    sessions_du_jour = (
        db.query(PatisserieSessionCaisse)
        .filter_by(caissier_id=data.caissier_id, date_session=aujourd_hui)
        .order_by(PatisserieSessionCaisse.numero_session)
        .all()
    )
    session = next((s for s in sessions_du_jour if s.statut == "EN_COURS"), None)
    if not session:
        if len(sessions_du_jour) >= MAX_SESSIONS_PAR_JOUR:
            raise HTTPException(409, f"Limite de {MAX_SESSIONS_PAR_JOUR} sessions de caisse par jour atteinte.")
        session = PatisserieSessionCaisse(
            caissier_id=data.caissier_id, date_session=aujourd_hui, statut="EN_COURS",
            numero_session=len(sessions_du_jour) + 1,
        )
        db.add(session); db.commit(); db.refresh(session)
    return _session_dict(session, _stats_session(_ventes_session(session, db)))


class SoumettreSessionIn(BaseModel):
    montant_compte: float = Field(..., ge=0)
    notes: Optional[str] = None


@router.post("/caisse/sessions/{session_id}/soumettre")
def soumettre_session(session_id: int, body: SoumettreSessionIn, db: Session = Depends(get_db)):
    s = db.query(PatisserieSessionCaisse).filter_by(id=session_id).first()
    if not s:
        raise HTTPException(404, "Session introuvable")
    if s.statut != "EN_COURS":
        raise HTTPException(409, f"Session déjà '{s.statut}'")
    ventes = _ventes_session(s, db)
    stats = _stats_session(ventes)
    cash_attendu = Decimal(str(stats["cash_attendu"]))
    montant_compte = Decimal(str(body.montant_compte))
    s.cash_attendu_soumission = cash_attendu
    s.montant_compte = montant_compte
    s.ecart = montant_compte - cash_attendu
    s.statut = "SOUMIS"
    s.soumis_at = datetime.now(tz=timezone.utc)
    if body.notes:
        s.notes_admin = body.notes
    db.commit()
    return _session_dict(s, stats)


class ValiderSessionIn(BaseModel):
    notes: Optional[str] = None


@router.post("/caisse/sessions/{session_id}/valider")
def valider_session(session_id: int, body: ValiderSessionIn, request: Request,
                     _user: Utilisateur = Depends(_require_pdg_ou_admin_patisserie), db: Session = Depends(get_db)):
    s = db.query(PatisserieSessionCaisse).filter_by(id=session_id).first()
    if not s:
        raise HTTPException(404, "Session introuvable")
    if s.statut == "EN_COURS":
        raise HTTPException(409, "La session doit d'abord être soumise avant validation.")
    s.statut = "VALIDE"
    s.valide_at = datetime.now(tz=timezone.utc)
    s.valide_par_id = _uid(request)
    if body.notes:
        s.notes_admin = body.notes
    db.commit()
    return _session_dict(s, _stats_session(_ventes_session(s, db)))


# ══════════════════════════════════════════════════════════════════
# RENFLOUEMENT (réutilise la table générique par département)
# ══════════════════════════════════════════════════════════════════

@router.get("/renflouements")
def lister_renflouements(db: Session = Depends(get_db)):
    rows = (
        db.query(RenflouementDepartement)
        .filter(RenflouementDepartement.departement == "PATISSERIE")
        .order_by(RenflouementDepartement.date_renflouement.desc())
        .limit(50)
        .all()
    )
    return [
        {
            "id": r.id, "montant": float(r.montant), "source": r.source,
            "date_renflouement": r.date_renflouement.isoformat(),
            "enregistre_par": r.enregistre_par.nom_complet if r.enregistre_par else None,
            "notes": r.notes,
        }
        for r in rows
    ]


@router.post("/renflouements", status_code=201)
def creer_renflouement(data: dict, _user: Utilisateur = Depends(_require_pdg_ou_admin_patisserie), db: Session = Depends(get_db)):
    montant = float(data.get("montant") or 0)
    if montant <= 0:
        raise HTTPException(400, "Le montant doit être positif.")
    r = RenflouementDepartement(
        departement="PATISSERIE", montant=montant,
        source=(data.get("source") or "").strip() or None,
        notes=(data.get("notes") or "").strip() or None,
        enregistre_par_id=_user.id,
    )
    db.add(r); db.commit(); db.refresh(r)
    return {"id": r.id, "message": "Renflouement enregistré"}


# ══════════════════════════════════════════════════════════════════
# TABLEAU DE BORD
# ══════════════════════════════════════════════════════════════════

@router.get("/dashboard")
def dashboard(
    date_debut: Optional[date_type] = Query(default=None),
    date_fin:   Optional[date_type] = Query(default=None),
    db: Session = Depends(get_db),
):
    today = today_haiti()
    if not date_debut: date_debut = today.replace(day=1)
    if not date_fin:   date_fin   = today
    dt_deb, _ = bounds_haiti(date_debut)
    _, dt_fin = bounds_haiti(date_fin)

    ventes = db.query(PatisserieVente).filter(
        PatisserieVente.statut == "PAYEE",
        PatisserieVente.date_heure >= dt_deb, PatisserieVente.date_heure <= dt_fin,
    ).all()
    ca_ventes = sum(float(_dec(v.montant_total)) for v in ventes)

    commandes_periode = db.query(PatisserieCommande).filter(
        PatisserieCommande.statut == "ACTIVE",
        PatisserieCommande.date_commande >= dt_deb, PatisserieCommande.date_commande <= dt_fin,
    ).all()
    ca_commandes = sum(float(_dec(c.montant_total)) for c in commandes_periode)

    depenses = db.query(PatisserieDepense).filter(
        PatisserieDepense.date_depense >= dt_deb, PatisserieDepense.date_depense <= dt_fin,
    ).all()
    total_depenses = sum(float(_dec(d.montant)) for d in depenses)

    achats = db.query(PatisserieAchat).filter(
        PatisserieAchat.statut == "CONFIRME",
        PatisserieAchat.date_achat >= dt_deb, PatisserieAchat.date_achat <= dt_fin,
    ).all()
    total_achats = sum(float(_dec(a.total)) for a in achats)

    ca_total = ca_ventes + ca_commandes
    benefice = ca_total - total_depenses - total_achats

    produits_bas = [
        {"id": p.id, "nom": p.nom, "stock_actuel": float(_dec(p.stock_actuel)), "seuil": float(_dec(p.seuil_alerte_stock))}
        for p in db.query(PatisserieProduit).filter(PatisserieProduit.actif == True).all()
        if p.seuil_alerte_stock and _dec(p.stock_actuel) <= _dec(p.seuil_alerte_stock)
    ]

    etapes = db.query(PatisserieEtapeSuivi).filter_by(actif=True).order_by(PatisserieEtapeSuivi.ordre).all()
    commandes_actives = db.query(PatisserieCommande).filter_by(statut="ACTIVE").all()
    par_etape = []
    for e in etapes:
        cmds = [c for c in commandes_actives if c.etape_id == e.id]
        par_etape.append({
            "etape_id": e.id, "libelle": e.libelle, "couleur": e.couleur or "#64748b",
            "nb": len(cmds), "montant": round(sum(float(_dec(c.montant_total)) for c in cmds), 2),
        })

    evo: dict[str, dict] = {}
    for v in ventes:
        k = v.date_heure.date().isoformat()
        evo.setdefault(k, {"date": k, "ca": 0.0, "nb": 0})
        evo[k]["ca"] += float(_dec(v.montant_total))
        evo[k]["nb"] += 1

    return {
        "date_debut": str(date_debut), "date_fin": str(date_fin),
        "ca_ventes": round(ca_ventes, 2), "ca_commandes": round(ca_commandes, 2), "ca_total": round(ca_total, 2),
        "total_depenses": round(total_depenses, 2), "total_achats": round(total_achats, 2),
        "benefice": round(benefice, 2),
        "marge_pct": round(benefice / ca_total * 100, 1) if ca_total > 0 else 0.0,
        "nb_ventes": len(ventes), "nb_commandes_actives": len(commandes_actives),
        "produits_stock_bas": produits_bas,
        "commandes_par_etape": par_etape,
        "evolution": sorted(evo.values(), key=lambda x: x["date"]),
    }
