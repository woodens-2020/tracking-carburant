"""Routes d'analyse et contrôle rigoureux des articles bar."""
from __future__ import annotations

from datetime import datetime, timezone, date as date_type, timedelta, time
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db
from models import BarProduit, BarAchat, BarMouvementStock, BarVente, BarLigneVente
from pos_service import stock_tous_produits, cmup as _cmup, prix_actif as _prix_actif, stock_courant

router = APIRouter(prefix="/api/pos/analyse", tags=["Analyse Bar"])


def _dec(v) -> Decimal:
    return Decimal(str(v)) if v is not None else Decimal("0")


@router.get("/tableau")
def tableau_controle(db: Session = Depends(get_db)):
    """Tableau de contrôle complet par article : stock, CMUP, marge, valeur, ventes 30j."""
    today = date_type.today()
    dt_30j = datetime.combine(today - timedelta(days=30), time.min).replace(tzinfo=timezone.utc)

    produits = (
        db.query(BarProduit)
        .filter(BarProduit.actif == True)
        .order_by(BarProduit.nom)
        .all()
    )

    stocks = stock_tous_produits(db)

    achats_totaux: dict[int, dict] = {}
    for pid, nb, qte, cout, dernier in db.query(
        BarAchat.produit_id,
        func.count(BarAchat.id),
        func.sum(BarAchat.quantite),
        func.sum(BarAchat.quantite * BarAchat.prix_achat_unitaire),
        func.max(BarAchat.date_achat),
    ).filter(BarAchat.produit_id.isnot(None)).group_by(BarAchat.produit_id).all():
        achats_totaux[pid] = {
            "nb": nb or 0,
            "qte": float(_dec(qte)),
            "cout": float(_dec(cout)),
            "dernier": dernier.isoformat() if dernier else None,
        }

    ventes_totales: dict[int, dict] = {}
    for pid, qte, ca, derniere in db.query(
        BarLigneVente.produit_id,
        func.sum(BarLigneVente.quantite),
        func.sum(BarLigneVente.sous_total),
        func.max(BarVente.date_heure),
    ).join(BarVente).filter(BarVente.statut != "ANNULEE").group_by(BarLigneVente.produit_id).all():
        ventes_totales[pid] = {
            "qte": float(_dec(qte)),
            "ca": float(_dec(ca)),
            "derniere": derniere.isoformat() if derniere else None,
        }

    ventes_30j: dict[int, dict] = {}
    for pid, qte, ca in db.query(
        BarLigneVente.produit_id,
        func.sum(BarLigneVente.quantite),
        func.sum(BarLigneVente.sous_total),
    ).join(BarVente).filter(
        BarVente.statut != "ANNULEE",
        BarVente.date_heure >= dt_30j,
    ).group_by(BarLigneVente.produit_id).all():
        ventes_30j[pid] = {"qte": float(_dec(qte)), "ca": float(_dec(ca))}

    articles = []
    total_valeur_stock = Decimal("0")

    for p in produits:
        stk      = stocks.get(p.id, Decimal("0"))
        cout_moy = _cmup(p.id, db)
        prix_v   = _prix_actif(p.id, db) or Decimal("0")
        marge_u  = prix_v - cout_moy
        marge_pct = float(marge_u / prix_v * 100) if prix_v > 0 else 0.0
        valeur   = stk * cout_moy
        total_valeur_stock += valeur

        seuil = _dec(p.seuil_alerte_stock or 0)
        if float(stk) <= 0:
            statut = "VIDE"
        elif seuil > 0 and stk <= seuil:
            statut = "BAS"
        else:
            statut = "OK"

        articles.append({
            "id":           p.id,
            "nom":          p.nom,
            "categorie":    p.categorie or "—",
            "seuil_alerte": float(seuil),
            "stock":        float(stk),
            "cmup":         float(cout_moy),
            "prix_vente":   float(prix_v),
            "marge_unit":   float(marge_u),
            "marge_pct":    round(marge_pct, 1),
            "valeur_stock": float(valeur),
            "statut_stock": statut,
            "achats":     achats_totaux.get(p.id, {"nb": 0, "qte": 0.0, "cout": 0.0, "dernier": None}),
            "ventes":     ventes_totales.get(p.id, {"qte": 0.0, "ca": 0.0, "derniere": None}),
            "ventes_30j": ventes_30j.get(p.id, {"qte": 0.0, "ca": 0.0}),
        })

    return {
        "articles": articles,
        "kpis": {
            "nb_articles":   len(articles),
            "articles_vide": sum(1 for a in articles if a["statut_stock"] == "VIDE"),
            "articles_bas":  sum(1 for a in articles if a["statut_stock"] == "BAS"),
            "valeur_stock":  float(total_valeur_stock),
            "ca_30j":        sum(a["ventes_30j"]["ca"] for a in articles),
        },
    }


@router.get("/evolution")
def evolution_ventes(
    date_debut:  Optional[date_type] = Query(default=None),
    date_fin:    Optional[date_type] = Query(default=None),
    produit_id:  Optional[int]       = Query(default=None),
    granularite: str                 = Query(default="jour"),
    db: Session = Depends(get_db),
):
    """Évolution temporelle des ventes groupée par jour, semaine ou mois."""
    today = date_type.today()
    if not date_debut:
        date_debut = today - timedelta(days=29)
    if not date_fin:
        date_fin = today

    dt_debut = datetime.combine(date_debut, time.min).replace(tzinfo=timezone.utc)
    dt_fin   = datetime.combine(date_fin,   time.max).replace(tzinfo=timezone.utc)

    q = (
        db.query(BarLigneVente)
        .join(BarVente)
        .filter(
            BarVente.statut != "ANNULEE",
            BarVente.date_heure >= dt_debut,
            BarVente.date_heure <= dt_fin,
        )
    )
    if produit_id:
        q = q.filter(BarLigneVente.produit_id == produit_id)

    lignes = q.all()

    par_periode: dict[str, dict] = {}
    for l in lignes:
        d = l.vente.date_heure.date()
        if granularite == "semaine":
            iso = d.isocalendar()
            k = f"{iso[0]}-S{iso[1]:02d}"
        elif granularite == "mois":
            k = d.strftime("%Y-%m")
        else:
            k = str(d)

        if k not in par_periode:
            par_periode[k] = {"periode": k, "ca": 0.0, "quantite": 0.0, "_tx": set()}
        par_periode[k]["ca"]       += float(_dec(l.sous_total))
        par_periode[k]["quantite"] += float(_dec(l.quantite))
        par_periode[k]["_tx"].add(l.vente_id)

    series = []
    for k in sorted(par_periode.keys()):
        v = par_periode[k]
        series.append({
            "periode":         k,
            "ca":              round(v["ca"], 2),
            "quantite":        round(v["quantite"], 3),
            "nb_transactions": len(v["_tx"]),
        })

    return {
        "date_debut":  str(date_debut),
        "date_fin":    str(date_fin),
        "granularite": granularite,
        "produit_id":  produit_id,
        "series":      series,
        "total_ca":    round(sum(s["ca"] for s in series), 2),
        "total_qte":   round(sum(s["quantite"] for s in series), 3),
    }


@router.get("/lots/{produit_id}")
def suivi_lots_produit(produit_id: int, db: Session = Depends(get_db)):
    """Suivi des ventes générées dans la fenêtre temporelle de chaque lot d'achat."""
    produit = db.query(BarProduit).filter_by(id=produit_id).first()
    if not produit:
        raise HTTPException(404, "Produit introuvable")

    achats = (
        db.query(BarAchat)
        .filter(BarAchat.produit_id == produit_id)
        .order_by(BarAchat.date_achat)
        .all()
    )

    lignes_vente = (
        db.query(BarLigneVente)
        .join(BarVente)
        .filter(BarLigneVente.produit_id == produit_id, BarVente.statut != "ANNULEE")
        .order_by(BarVente.date_heure)
        .all()
    )

    stk      = stock_courant(produit_id, db)
    cout_moy = _cmup(produit_id, db)
    prix_v   = _prix_actif(produit_id, db) or Decimal("0")
    now_utc  = datetime.now(timezone.utc)

    lots = []
    for i, achat in enumerate(achats):
        debut = achat.date_achat
        fin   = achats[i + 1].date_achat if i + 1 < len(achats) else now_utc

        ventes_lot = [lv for lv in lignes_vente if debut <= lv.vente.date_heure < fin]
        qte_vendue = sum(float(_dec(lv.quantite)) for lv in ventes_lot)
        ca_genere  = sum(float(_dec(lv.sous_total)) for lv in ventes_lot)

        cout_lot   = float(_dec(achat.quantite)) * float(_dec(achat.prix_achat_unitaire))
        dep_total  = sum(float(_dec(d.montant)) for d in (achat.depenses or []))
        cout_total = cout_lot + dep_total

        lots.append({
            "achat_id":         achat.id,
            "date_achat":       achat.date_achat.isoformat(),
            "fournisseur":      achat.fournisseur,
            "statut":           achat.statut,
            "qte_achetee":      float(_dec(achat.quantite)),
            "prix_unitaire":    float(_dec(achat.prix_achat_unitaire)),
            "cout_marchandise": cout_lot,
            "depenses":         dep_total,
            "cout_total":       cout_total,
            "notes":            achat.notes,
            "ventes": {
                "qte_vendue":       round(qte_vendue, 3),
                "ca_genere":        round(ca_genere, 2),
                "nb_lignes":        len(ventes_lot),
                "recuperation_pct": round(ca_genere / cout_total * 100, 1) if cout_total > 0 else 0.0,
                "benefice_estime":  round(ca_genere - cout_total, 2),
            },
        })

    return {
        "produit_id":    produit_id,
        "produit_nom":   produit.nom,
        "categorie":     produit.categorie,
        "stock_courant": float(stk),
        "cmup":          float(cout_moy),
        "prix_vente":    float(prix_v),
        "valeur_stock":  float(stk * cout_moy),
        "nb_lots":       len(lots),
        "lots":          lots,
    }
