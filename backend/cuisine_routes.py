"""Routes cuisine — plats, dépenses, ventes, statistiques."""
from __future__ import annotations

from datetime import datetime, timezone, date as date_type, time
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from database import get_db
from models import CuisinePlat, CuisineDepense, CuisineVente, CuisineLigneVente

router = APIRouter(prefix="/api/cuisine", tags=["Cuisine"])


def _dec(v) -> Decimal:
    return Decimal(str(v)) if v is not None else Decimal("0")


def _generer_ticket(db: Session) -> str:
    today = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
    count = db.query(CuisineVente).filter(
        CuisineVente.numero_ticket.like(f"CK{today}%")
    ).count()
    return f"CK{today}{str(count + 1).zfill(4)}"


def _plat_dict(p: CuisinePlat) -> dict:
    pv = float(_dec(p.prix_vente))
    ce = float(_dec(p.cout_estime)) if p.cout_estime else None
    marge = round((pv - ce) / pv * 100, 1) if ce and pv > 0 else None
    return {
        "id":          p.id,
        "nom":         p.nom,
        "categorie":   p.categorie or "",
        "description": p.description or "",
        "prix_vente":  pv,
        "cout_estime": ce,
        "marge_pct":   marge,
        "actif":       p.actif,
        "date_creation": p.date_creation.isoformat() if p.date_creation else None,
    }


# ══════════════════════════════════════════════════════════════════
# PLATS
# ══════════════════════════════════════════════════════════════════

@router.get("/plats")
def liste_plats(
    actif: Optional[bool] = Query(default=None),
    db: Session = Depends(get_db),
):
    q = db.query(CuisinePlat).order_by(CuisinePlat.categorie, CuisinePlat.nom)
    if actif is not None:
        q = q.filter(CuisinePlat.actif == actif)
    return [_plat_dict(p) for p in q.all()]


@router.post("/plats")
def creer_plat(data: dict, db: Session = Depends(get_db)):
    nom = (data.get("nom") or "").strip()
    if not nom:
        raise HTTPException(400, "Le nom est requis")
    prix = float(data.get("prix_vente") or 0)
    if prix <= 0:
        raise HTTPException(400, "Le prix doit être positif")
    cout = data.get("cout_estime")
    p = CuisinePlat(
        nom         = nom,
        categorie   = (data.get("categorie") or "").strip() or None,
        description = (data.get("description") or "").strip() or None,
        prix_vente  = Decimal(str(prix)),
        cout_estime = Decimal(str(cout)) if cout else None,
        actif       = True,
    )
    db.add(p); db.commit(); db.refresh(p)
    return {"id": p.id, "message": "Plat créé", "plat": _plat_dict(p)}


@router.put("/plats/{plat_id}")
def modifier_plat(plat_id: int, data: dict, db: Session = Depends(get_db)):
    p = db.query(CuisinePlat).filter_by(id=plat_id).first()
    if not p:
        raise HTTPException(404, "Plat introuvable")
    if "nom" in data and data["nom"]:
        p.nom = data["nom"].strip()
    if "categorie"   in data: p.categorie   = (data["categorie"] or "").strip() or None
    if "description" in data: p.description = (data["description"] or "").strip() or None
    if "prix_vente"  in data: p.prix_vente  = Decimal(str(data["prix_vente"]))
    if "cout_estime" in data:
        p.cout_estime = Decimal(str(data["cout_estime"])) if data.get("cout_estime") else None
    if "actif" in data:       p.actif = bool(data["actif"])
    db.commit(); db.refresh(p)
    return {"message": "Plat modifié", "plat": _plat_dict(p)}


@router.delete("/plats/{plat_id}")
def desactiver_plat(plat_id: int, db: Session = Depends(get_db)):
    p = db.query(CuisinePlat).filter_by(id=plat_id).first()
    if not p:
        raise HTTPException(404, "Plat introuvable")
    p.actif = False
    db.commit()
    return {"message": "Plat retiré du menu"}


# ══════════════════════════════════════════════════════════════════
# DÉPENSES
# ══════════════════════════════════════════════════════════════════

@router.get("/depenses")
def liste_depenses(
    date_debut: Optional[date_type] = Query(default=None),
    date_fin:   Optional[date_type] = Query(default=None),
    db: Session = Depends(get_db),
):
    today = date_type.today()
    if not date_debut: date_debut = today.replace(day=1)
    if not date_fin:   date_fin   = today
    dt_deb = datetime.combine(date_debut, time.min).replace(tzinfo=timezone.utc)
    dt_fin = datetime.combine(date_fin,   time.max).replace(tzinfo=timezone.utc)

    deps = (
        db.query(CuisineDepense)
        .filter(CuisineDepense.date_depense >= dt_deb,
                CuisineDepense.date_depense <= dt_fin)
        .order_by(CuisineDepense.date_depense.desc())
        .all()
    )
    return [
        {
            "id":          d.id,
            "description": d.description,
            "categorie":   d.categorie or "AUTRE",
            "montant":     float(_dec(d.montant)),
            "date_depense": d.date_depense.isoformat(),
            "fournisseur": d.fournisseur or "",
            "notes":       d.notes or "",
        }
        for d in deps
    ]


@router.post("/depenses")
def ajouter_depense(data: dict, db: Session = Depends(get_db)):
    desc = (data.get("description") or "").strip()
    if not desc:
        raise HTTPException(400, "La description est requise")
    montant = float(data.get("montant") or 0)
    if montant <= 0:
        raise HTTPException(400, "Le montant doit être positif")

    date_dep = datetime.now(timezone.utc)
    if data.get("date_depense"):
        try:
            raw = data["date_depense"]
            if "T" in raw:
                date_dep = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            else:
                date_dep = datetime.combine(
                    date_type.fromisoformat(raw), time.min
                ).replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            pass

    d = CuisineDepense(
        description  = desc,
        categorie    = data.get("categorie") or "AUTRE",
        montant      = Decimal(str(montant)),
        date_depense = date_dep,
        fournisseur  = (data.get("fournisseur") or "").strip() or None,
        notes        = (data.get("notes") or "").strip() or None,
    )
    db.add(d); db.commit(); db.refresh(d)
    return {"id": d.id, "message": "Dépense enregistrée"}


@router.put("/depenses/{dep_id}")
def modifier_depense(dep_id: int, data: dict, db: Session = Depends(get_db)):
    d = db.query(CuisineDepense).filter_by(id=dep_id).first()
    if not d:
        raise HTTPException(404, "Dépense introuvable")
    if "description" in data and data["description"]:
        d.description = data["description"].strip()
    if "categorie"   in data: d.categorie   = data["categorie"] or "AUTRE"
    if "montant"     in data: d.montant     = Decimal(str(data["montant"]))
    if "fournisseur" in data: d.fournisseur = (data["fournisseur"] or "").strip() or None
    if "notes"       in data: d.notes       = (data["notes"] or "").strip() or None
    db.commit()
    return {"message": "Dépense modifiée"}


@router.delete("/depenses/{dep_id}")
def supprimer_depense(dep_id: int, db: Session = Depends(get_db)):
    d = db.query(CuisineDepense).filter_by(id=dep_id).first()
    if not d:
        raise HTTPException(404, "Dépense introuvable")
    db.delete(d); db.commit()
    return {"message": "Dépense supprimée"}


# ══════════════════════════════════════════════════════════════════
# VENTES
# ══════════════════════════════════════════════════════════════════

@router.get("/ventes")
def liste_ventes(
    date_debut: Optional[date_type] = Query(default=None),
    date_fin:   Optional[date_type] = Query(default=None),
    db: Session = Depends(get_db),
):
    today = date_type.today()
    if not date_debut: date_debut = today
    if not date_fin:   date_fin   = today
    dt_deb = datetime.combine(date_debut, time.min).replace(tzinfo=timezone.utc)
    dt_fin = datetime.combine(date_fin,   time.max).replace(tzinfo=timezone.utc)

    ventes = (
        db.query(CuisineVente)
        .filter(CuisineVente.date_heure >= dt_deb,
                CuisineVente.date_heure <= dt_fin)
        .order_by(CuisineVente.date_heure.desc())
        .all()
    )
    return [
        {
            "id":            v.id,
            "numero_ticket": v.numero_ticket,
            "date_heure":    v.date_heure.isoformat(),
            "total":         float(_dec(v.total)),
            "mode_paiement": v.mode_paiement,
            "client_nom":    v.client_nom or "",
            "statut":        v.statut,
            "notes":         v.notes or "",
            "lignes": [
                {
                    "plat_id":       l.plat_id,
                    "nom_plat":      l.nom_plat,
                    "quantite":      l.quantite,
                    "prix_unitaire": float(_dec(l.prix_unitaire)),
                    "sous_total":    float(_dec(l.sous_total)),
                }
                for l in v.lignes
            ],
        }
        for v in ventes
    ]


@router.post("/ventes")
def enregistrer_vente(data: dict, db: Session = Depends(get_db)):
    lignes_data = data.get("lignes") or []
    if not lignes_data:
        raise HTTPException(400, "La vente doit contenir au moins un plat")

    total      = Decimal("0")
    ligne_objs = []

    for l in lignes_data:
        plat_id   = l.get("plat_id")
        nom_plat  = (l.get("nom_plat") or "").strip()
        qte       = int(l.get("quantite") or 1)
        prix      = Decimal(str(l.get("prix_unitaire") or 0))
        sous      = prix * qte
        total    += sous

        if plat_id and not nom_plat:
            plat = db.query(CuisinePlat).filter_by(id=plat_id).first()
            nom_plat = plat.nom if plat else f"Plat #{plat_id}"

        ligne_objs.append(CuisineLigneVente(
            plat_id       = plat_id,
            nom_plat      = nom_plat or "—",
            quantite      = qte,
            prix_unitaire = prix,
            sous_total    = sous,
        ))

    vente = CuisineVente(
        numero_ticket = _generer_ticket(db),
        total         = total,
        mode_paiement = data.get("mode_paiement") or "CASH",
        client_nom    = (data.get("client_nom") or "").strip() or None,
        notes         = (data.get("notes") or "").strip() or None,
        statut        = "VALIDEE",
    )
    for l in ligne_objs:
        l.vente = vente
    db.add(vente); db.commit(); db.refresh(vente)

    return {
        "id":            vente.id,
        "numero_ticket": vente.numero_ticket,
        "total":         float(vente.total),
        "message":       "Vente enregistrée",
    }


@router.put("/ventes/{vente_id}/annuler")
def annuler_vente(vente_id: int, db: Session = Depends(get_db)):
    v = db.query(CuisineVente).filter_by(id=vente_id).first()
    if not v:
        raise HTTPException(404, "Vente introuvable")
    if v.statut == "ANNULEE":
        raise HTTPException(400, "Vente déjà annulée")
    v.statut = "ANNULEE"
    db.commit()
    return {"message": "Vente annulée"}


# ══════════════════════════════════════════════════════════════════
# STATISTIQUES
# ══════════════════════════════════════════════════════════════════

@router.get("/stats")
def statistiques(
    date_debut: Optional[date_type] = Query(default=None),
    date_fin:   Optional[date_type] = Query(default=None),
    db: Session = Depends(get_db),
):
    today = date_type.today()
    if not date_debut: date_debut = today.replace(day=1)
    if not date_fin:   date_fin   = today

    dt_deb = datetime.combine(date_debut, time.min).replace(tzinfo=timezone.utc)
    dt_fin = datetime.combine(date_fin,   time.max).replace(tzinfo=timezone.utc)

    # Ventes validées
    ventes = db.query(CuisineVente).filter(
        CuisineVente.statut == "VALIDEE",
        CuisineVente.date_heure >= dt_deb,
        CuisineVente.date_heure <= dt_fin,
    ).all()

    ca_total  = sum(float(_dec(v.total)) for v in ventes)
    nb_ventes = len(ventes)

    # Dépenses
    depenses = db.query(CuisineDepense).filter(
        CuisineDepense.date_depense >= dt_deb,
        CuisineDepense.date_depense <= dt_fin,
    ).all()

    total_depenses = sum(float(_dec(d.montant)) for d in depenses)
    benefice       = ca_total - total_depenses
    marge_pct      = round(benefice / ca_total * 100, 1) if ca_total > 0 else 0.0

    # Top plats
    top_plats: dict[str, dict] = {}
    for v in ventes:
        for l in v.lignes:
            k = l.nom_plat
            if k not in top_plats:
                top_plats[k] = {"nom": k, "qte": 0, "ca": 0.0}
            top_plats[k]["qte"] += l.quantite
            top_plats[k]["ca"]  += float(_dec(l.sous_total))

    top_plats_list = sorted(top_plats.values(), key=lambda x: x["ca"], reverse=True)[:10]

    # Dépenses par catégorie
    deps_cat: dict[str, float] = {}
    for d in depenses:
        cat = d.categorie or "AUTRE"
        deps_cat[cat] = deps_cat.get(cat, 0.0) + float(_dec(d.montant))

    # Évolution journalière
    evo: dict[str, dict] = {}
    for v in ventes:
        k = v.date_heure.date().isoformat()
        if k not in evo:
            evo[k] = {"date": k, "ca": 0.0, "nb": 0}
        evo[k]["ca"] += float(_dec(v.total))
        evo[k]["nb"] += 1

    return {
        "date_debut":     str(date_debut),
        "date_fin":       str(date_fin),
        "ca_total":       round(ca_total, 2),
        "nb_ventes":      nb_ventes,
        "total_depenses": round(total_depenses, 2),
        "benefice":       round(benefice, 2),
        "marge_pct":      marge_pct,
        "ticket_moyen":   round(ca_total / nb_ventes, 2) if nb_ventes > 0 else 0.0,
        "top_plats":      top_plats_list,
        "deps_par_cat":   [{"categorie": k, "montant": round(v, 2)} for k, v in sorted(deps_cat.items())],
        "evolution":      sorted(evo.values(), key=lambda x: x["date"]),
    }
