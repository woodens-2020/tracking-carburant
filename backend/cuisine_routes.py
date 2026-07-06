"""Routes cuisine — plats, dépenses, ventes, statistiques."""
from __future__ import annotations

from datetime import datetime, timezone, date as date_type, time
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from database import get_db
from models import CuisinePlat, CuisineDepense, CuisineVente, CuisineLigneVente, CuisineAchat

router = APIRouter(prefix="/api/cuisine", tags=["Cuisine"])


def _dec(v) -> Decimal:
    return Decimal(str(v)) if v is not None else Decimal("0")


def _generer_ticket(db: Session) -> str:
    """Génère un numéro de ticket cuisine unique — SELECT FOR UPDATE évite la race condition."""
    today  = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
    prefix = f"CK{today}"
    last = (
        db.query(CuisineVente.numero_ticket)
        .filter(CuisineVente.numero_ticket.like(f"{prefix}%"))
        .with_for_update()
        .order_by(CuisineVente.numero_ticket.desc())
        .first()
    )
    seq = int(last.numero_ticket[len(prefix):]) + 1 if last else 1
    return f"{prefix}{str(seq).zfill(4)}"


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
    if "prix_vente" in data:
        pv = float(data["prix_vente"] or 0)
        if pv <= 0:
            raise HTTPException(400, "Le prix de vente doit être > 0")
        p.prix_vente = Decimal(str(pv))
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
    if "montant" in data:
        montant = float(data["montant"] or 0)
        if montant <= 0:
            raise HTTPException(400, "Le montant doit être positif")
        d.montant = Decimal(str(montant))
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
                CuisineVente.date_heure <= dt_fin,
                CuisineVente.statut != "ANNULEE")
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
        if prix <= 0:
            raise HTTPException(400, f"Le prix unitaire doit être > 0 pour chaque ligne")
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

    # Toutes les ventes cuisine validées (caisse directe + via bar)
    ventes = db.query(CuisineVente).filter(
        CuisineVente.statut == "VALIDEE",
        CuisineVente.date_heure >= dt_deb,
        CuisineVente.date_heure <= dt_fin,
    ).all()

    ventes_directes = [v for v in ventes if not (v.notes or "").startswith("Via Bar")]
    ventes_bar      = [v for v in ventes if (v.notes or "").startswith("Via Bar")]

    ca_total   = sum(float(_dec(v.total)) for v in ventes)
    ca_cuisine = sum(float(_dec(v.total)) for v in ventes_directes)
    ca_bar     = sum(float(_dec(v.total)) for v in ventes_bar)
    nb_ventes  = len(ventes_directes)
    nb_bar     = len(ventes_bar)

    # Dépenses
    depenses = db.query(CuisineDepense).filter(
        CuisineDepense.date_depense >= dt_deb,
        CuisineDepense.date_depense <= dt_fin,
    ).all()

    total_depenses = sum(float(_dec(d.montant)) for d in depenses)
    benefice       = ca_total - total_depenses
    marge_pct      = round(benefice / ca_total * 100, 1) if ca_total > 0 else 0.0

    # Top plats (toutes sources : caisse directe + via bar)
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
            evo[k] = {"date": k, "ca": 0.0, "nb": 0, "ca_bar": 0.0}
        evo[k]["ca"] += float(_dec(v.total))
        evo[k]["nb"] += 1
        if (v.notes or "").startswith("Via Bar"):
            evo[k]["ca_bar"] += float(_dec(v.total))

    return {
        "date_debut":     str(date_debut),
        "date_fin":       str(date_fin),
        "ca_total":       round(ca_total, 2),
        "ca_cuisine":     round(ca_cuisine, 2),
        "ca_bar":         round(ca_bar, 2),
        "nb_ventes":      nb_ventes,
        "nb_ventes_bar":  nb_bar,
        "total_depenses": round(total_depenses, 2),
        "benefice":       round(benefice, 2),
        "marge_pct":      marge_pct,
        "ticket_moyen":   round(ca_cuisine / nb_ventes, 2) if nb_ventes > 0 else 0.0,
        "top_plats":      top_plats_list,
        "deps_par_cat":   [{"categorie": k, "montant": round(v, 2)} for k, v in sorted(deps_cat.items())],
        "evolution":      sorted(evo.values(), key=lambda x: x["date"]),
    }


# ══════════════════════════════════════════════════════════════════
# ACHATS / MATIÈRES PREMIÈRES
# ══════════════════════════════════════════════════════════════════

def _achat_dict(a: CuisineAchat) -> dict:
    return {
        "id":            a.id,
        "plat_id":       a.plat_id,
        "plat_nom":      a.plat.nom if a.plat else None,
        "description":   a.description,
        "categorie":     a.categorie or "INGREDIENTS",
        "quantite":      float(_dec(a.quantite)),
        "unite":         a.unite or "kg",
        "cout_unitaire": float(_dec(a.cout_unitaire)),
        "total":         float(_dec(a.total)),
        "date_achat":    a.date_achat.isoformat() if a.date_achat else None,
        "fournisseur":   a.fournisseur or "",
        "notes":         a.notes or "",
    }


@router.get("/achats")
def liste_achats(
    plat_id:    Optional[int]       = Query(default=None),
    date_debut: Optional[date_type] = Query(default=None),
    date_fin:   Optional[date_type] = Query(default=None),
    db: Session = Depends(get_db),
):
    today = date_type.today()
    if not date_debut: date_debut = today.replace(day=1)
    if not date_fin:   date_fin   = today
    dt_deb = datetime.combine(date_debut, time.min).replace(tzinfo=timezone.utc)
    dt_fin = datetime.combine(date_fin,   time.max).replace(tzinfo=timezone.utc)

    q = db.query(CuisineAchat).filter(
        CuisineAchat.date_achat >= dt_deb,
        CuisineAchat.date_achat <= dt_fin,
    )
    if plat_id:
        q = q.filter(CuisineAchat.plat_id == plat_id)
    achats = q.order_by(CuisineAchat.date_achat.desc()).all()

    total = sum(float(_dec(a.total)) for a in achats)
    return {
        "achats": [_achat_dict(a) for a in achats],
        "total_achats": round(total, 2),
        "nb_achats": len(achats),
    }


@router.post("/achats", status_code=201)
def creer_achat(data: dict, db: Session = Depends(get_db)):
    desc = (data.get("description") or "").strip()
    if not desc:
        raise HTTPException(400, "La description est requise")
    qte = float(data.get("quantite") or 0)
    if qte <= 0:
        raise HTTPException(400, "La quantité doit être positive")
    cout = float(data.get("cout_unitaire") or 0)
    if cout <= 0:
        raise HTTPException(400, "Le coût unitaire doit être > 0")

    date_achat = datetime.now(timezone.utc)
    if data.get("date_achat"):
        try:
            raw = data["date_achat"]
            if "T" in raw:
                date_achat = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            else:
                date_achat = datetime.combine(
                    date_type.fromisoformat(raw), time.min
                ).replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            pass

    a = CuisineAchat(
        plat_id       = data.get("plat_id") or None,
        description   = desc,
        categorie     = data.get("categorie") or "INGREDIENTS",
        quantite      = Decimal(str(qte)),
        unite         = (data.get("unite") or "kg").strip() or "kg",
        cout_unitaire = Decimal(str(cout)),
        total         = Decimal(str(round(qte * cout, 2))),
        date_achat    = date_achat,
        fournisseur   = (data.get("fournisseur") or "").strip() or None,
        notes         = (data.get("notes") or "").strip() or None,
    )
    db.add(a); db.commit(); db.refresh(a)
    return {"id": a.id, "message": "Achat enregistré", "total": float(a.total)}


@router.put("/achats/{achat_id}")
def modifier_achat(achat_id: int, data: dict, db: Session = Depends(get_db)):
    a = db.query(CuisineAchat).filter_by(id=achat_id).first()
    if not a:
        raise HTTPException(404, "Achat introuvable")

    if "description"   in data and data["description"]:
        a.description   = data["description"].strip()
    if "categorie"     in data: a.categorie     = data["categorie"] or "INGREDIENTS"
    if "plat_id"       in data: a.plat_id       = data["plat_id"] or None
    if "fournisseur"   in data: a.fournisseur   = (data["fournisseur"] or "").strip() or None
    if "notes"         in data: a.notes         = (data["notes"] or "").strip() or None
    if "unite"         in data: a.unite         = data["unite"] or "kg"

    if "quantite" in data or "cout_unitaire" in data:
        if "quantite" in data:
            qte = float(data["quantite"])
            if qte <= 0:
                raise HTTPException(400, "La quantité doit être > 0")
        else:
            qte = float(a.quantite)
        if "cout_unitaire" in data:
            cout = float(data["cout_unitaire"])
            if cout <= 0:
                raise HTTPException(400, "Le coût unitaire doit être > 0")
        else:
            cout = float(a.cout_unitaire)
        a.quantite      = Decimal(str(qte))
        a.cout_unitaire = Decimal(str(cout))
        a.total         = Decimal(str(round(qte * cout, 2)))

    db.commit()
    return {"message": "Achat modifié"}


@router.delete("/achats/{achat_id}")
def supprimer_achat(achat_id: int, db: Session = Depends(get_db)):
    a = db.query(CuisineAchat).filter_by(id=achat_id).first()
    if not a:
        raise HTTPException(404, "Achat introuvable")
    db.delete(a); db.commit()
    return {"message": "Achat supprimé"}


# ══════════════════════════════════════════════════════════════════
# BILAN DE RENTABILITÉ PAR PLAT
# ══════════════════════════════════════════════════════════════════

@router.get("/bilan-rentabilite")
def bilan_rentabilite(db: Session = Depends(get_db)):
    """
    Tableau de bord rentabilité : coût des achats vs CA ventes, par plat actif.
    Affiché avant la déclaration d'un nouvel achat cuisine.
    """
    from sqlalchemy import func as sqlfunc

    plats = db.query(CuisinePlat).filter_by(actif=True).order_by(CuisinePlat.nom).all()

    achats_par_plat = dict(
        db.query(CuisineAchat.plat_id, sqlfunc.sum(CuisineAchat.total))
        .filter(CuisineAchat.plat_id.isnot(None))
        .group_by(CuisineAchat.plat_id)
        .all()
    )

    derniers_achats = dict(
        db.query(CuisineAchat.plat_id, sqlfunc.max(CuisineAchat.date_achat))
        .filter(CuisineAchat.plat_id.isnot(None))
        .group_by(CuisineAchat.plat_id)
        .all()
    )

    ventes_rows = (
        db.query(
            CuisineLigneVente.plat_id,
            sqlfunc.sum(CuisineLigneVente.sous_total),
            sqlfunc.sum(CuisineLigneVente.quantite),
        )
        .join(CuisineVente, CuisineLigneVente.vente_id == CuisineVente.id)
        .filter(
            CuisineLigneVente.plat_id.isnot(None),
            CuisineVente.statut == "VALIDEE",
        )
        .group_by(CuisineLigneVente.plat_id)
        .all()
    )
    ca_par_plat = {pid: float(_dec(ca)) for pid, ca, _ in ventes_rows}
    nb_par_plat = {pid: int(nb or 0)    for pid, _, nb in ventes_rows}

    achats_generaux = float(_dec(
        db.query(sqlfunc.sum(CuisineAchat.total))
        .filter(CuisineAchat.plat_id.is_(None))
        .scalar()
    ))

    resultats = []
    for p in plats:
        cout_achats  = float(_dec(achats_par_plat.get(p.id, 0)))
        ca_ventes    = ca_par_plat.get(p.id, 0.0)
        nb_portions  = nb_par_plat.get(p.id, 0)
        benefice     = ca_ventes - cout_achats
        marge_pct    = round(benefice / ca_ventes * 100, 1) if ca_ventes > 0 else 0.0
        dernier_achat = derniers_achats.get(p.id)
        resultats.append({
            "plat_id":             p.id,
            "plat_nom":            p.nom,
            "prix_vente":          float(_dec(p.prix_vente)),
            "cout_estime":         float(_dec(p.cout_estime)) if p.cout_estime else None,
            "cout_achats":         round(cout_achats, 2),
            "ca_ventes":           round(ca_ventes, 2),
            "nb_portions_vendues": nb_portions,
            "benefice":            round(benefice, 2),
            "marge_pct":           marge_pct,
            "dernier_achat":       dernier_achat.isoformat() if dernier_achat else None,
        })

    total_cout = sum(r["cout_achats"] for r in resultats) + achats_generaux
    total_ca   = sum(r["ca_ventes"]   for r in resultats)
    total_ben  = total_ca - total_cout
    return {
        "plats":             resultats,
        "achats_generaux":   round(achats_generaux, 2),
        "total_cout_achats": round(total_cout, 2),
        "total_ca":          round(total_ca, 2),
        "total_benefice":    round(total_ben, 2),
        "marge_globale_pct": round(total_ben / total_ca * 100, 1) if total_ca > 0 else 0.0,
    }


# ══════════════════════════════════════════════════════════════════
# VENTES VIA BAR (cross-selling) — lecture seule pour le dashboard
# ══════════════════════════════════════════════════════════════════

@router.get("/ventes-bar")
def ventes_via_bar(
    date_debut: Optional[date_type] = Query(default=None),
    date_fin:   Optional[date_type] = Query(default=None),
    db: Session = Depends(get_db),
):
    """Ventes cuisine enregistrées via la caisse bar."""
    today = date_type.today()
    if not date_debut: date_debut = today
    if not date_fin:   date_fin   = today

    dt_deb = datetime.combine(date_debut, time.min).replace(tzinfo=timezone.utc)
    dt_fin = datetime.combine(date_fin,   time.max).replace(tzinfo=timezone.utc)

    ventes = db.query(CuisineVente).filter(
        CuisineVente.statut == "VALIDEE",
        CuisineVente.notes.like("Via Bar%"),
        CuisineVente.date_heure >= dt_deb,
        CuisineVente.date_heure <= dt_fin,
    ).order_by(CuisineVente.date_heure.desc()).all()

    return [
        {
            "id":            v.id,
            "numero_ticket": v.numero_ticket,
            "date_heure":    v.date_heure.isoformat(),
            "total":         float(_dec(v.total)),
            "ticket_bar":    (v.notes or "").replace("Via Bar — ", ""),
            "lignes": [
                {
                    "nom_plat":  l.nom_plat,
                    "quantite":  l.quantite,
                    "sous_total": float(_dec(l.sous_total)),
                }
                for l in v.lignes
            ],
        }
        for v in ventes
    ]
