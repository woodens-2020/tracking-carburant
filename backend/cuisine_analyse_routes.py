"""Analyse avancée cuisine — ventes vs achats, comparaison de périodes, rentabilité par achat.

Complète /api/cuisine/stats (qui ignore le coût réel des achats d'ingrédients) et
/api/cuisine/bilan-rentabilite (qui n'a pas de filtre de date) avec un vrai calcul
de marge nette (CA - coût des achats), une comparaison à la période précédente, et
un suivi de rentabilité par achat individuel (fenêtre de vente jusqu'au prochain
achat du même plat — adapté de /api/pos/analyse/lots/{produit_id}).
"""
from __future__ import annotations

from datetime import datetime, timezone, date as date_type, time, timedelta
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from database import get_db
from models import CuisinePlat, CuisineVente, CuisineLigneVente, CuisineAchat, CuisineDepense, RenflouementDepartement

router = APIRouter(prefix="/api/cuisine/analyse", tags=["Cuisine Analyse"])


def _dec(v) -> Decimal:
    return Decimal(str(v)) if v is not None else Decimal("0")


def _bornes(d1: date_type, d2: date_type) -> tuple[datetime, datetime]:
    return (
        datetime.combine(d1, time.min).replace(tzinfo=timezone.utc),
        datetime.combine(d2, time.max).replace(tzinfo=timezone.utc),
    )


def _periode_bilan(date_debut: date_type, date_fin: date_type, db: Session) -> dict:
    """CA, coût achats, bénéfice, marge pour une période donnée — réutilisé pour la
    période courante et la période précédente (comparaison)."""
    dt_deb, dt_fin = _bornes(date_debut, date_fin)

    ca_total = float(_dec(
        db.query(sqlfunc.sum(CuisineLigneVente.sous_total))
        .join(CuisineVente, CuisineLigneVente.vente_id == CuisineVente.id)
        .filter(CuisineVente.statut == "VALIDEE",
                CuisineVente.date_heure >= dt_deb, CuisineVente.date_heure <= dt_fin)
        .scalar()
    ))
    qte_vendue = float(_dec(
        db.query(sqlfunc.sum(CuisineLigneVente.quantite))
        .join(CuisineVente, CuisineLigneVente.vente_id == CuisineVente.id)
        .filter(CuisineVente.statut == "VALIDEE",
                CuisineVente.date_heure >= dt_deb, CuisineVente.date_heure <= dt_fin)
        .scalar()
    ))
    nb_ventes = (
        db.query(sqlfunc.count(CuisineVente.id))
        .filter(CuisineVente.statut == "VALIDEE",
                CuisineVente.date_heure >= dt_deb, CuisineVente.date_heure <= dt_fin)
        .scalar()
    ) or 0

    cout_achats_plats = float(_dec(
        db.query(sqlfunc.sum(CuisineAchat.total))
        .filter(CuisineAchat.plat_id.isnot(None),
                CuisineAchat.date_achat >= dt_deb, CuisineAchat.date_achat <= dt_fin)
        .scalar()
    ))
    cout_achats_generaux = float(_dec(
        db.query(sqlfunc.sum(CuisineAchat.total))
        .filter(CuisineAchat.plat_id.is_(None),
                CuisineAchat.date_achat >= dt_deb, CuisineAchat.date_achat <= dt_fin)
        .scalar()
    ))
    cout_achats_total = cout_achats_plats + cout_achats_generaux
    benefice = ca_total - cout_achats_total
    marge_pct = round(benefice / ca_total * 100, 1) if ca_total > 0 else 0.0

    return {
        "ca_total":             round(ca_total, 2),
        "qte_vendue":           round(qte_vendue, 2),
        "nb_ventes":            nb_ventes,
        "cout_achats_plats":    round(cout_achats_plats, 2),
        "cout_achats_generaux": round(cout_achats_generaux, 2),
        "cout_achats_total":    round(cout_achats_total, 2),
        "benefice_net":         round(benefice, 2),
        "marge_pct":            marge_pct,
    }


def _bucket_evolution(evolution: list[dict], nb_jours: int) -> list[dict]:
    """Regroupe l'évolution quotidienne par semaine (périodes <=90j) ou par mois
    (périodes plus longues) pour garder le graphique lisible — même seuil que le
    regroupement automatique du Rapport Analytique carburant (frontend)."""
    if not evolution:
        return evolution
    mensuel = nb_jours > 90
    buckets: dict[str, dict] = {}
    for e in evolution:
        d = date_type.fromisoformat(e["date"])
        key = d.strftime("%Y-%m") if mensuel else (d - timedelta(days=d.weekday())).isoformat()
        b = buckets.setdefault(key, {"date": key, "ca": 0.0, "achats": 0.0, "benefice": 0.0})
        b["ca"]       += e["ca"]
        b["achats"]   += e["achats"]
        b["benefice"] += e["benefice"]
    result = sorted(buckets.values(), key=lambda x: x["date"])
    for r in result:
        r["ca"]       = round(r["ca"], 2)
        r["achats"]   = round(r["achats"], 2)
        r["benefice"] = round(r["benefice"], 2)
    return result


@router.get("/synthese")
def synthese(
    date_debut: Optional[date_type] = Query(default=None),
    date_fin:   Optional[date_type] = Query(default=None),
    db: Session = Depends(get_db),
):
    today = date_type.today()
    if not date_debut: date_debut = today.replace(day=1)
    if not date_fin:   date_fin   = today
    if date_debut > date_fin:
        raise HTTPException(422, "date_debut doit précéder date_fin")

    actuel = _periode_bilan(date_debut, date_fin, db)

    # Période précédente de durée égale, immédiatement avant date_debut
    nb_jours   = (date_fin - date_debut).days + 1
    prev_fin   = date_debut - timedelta(days=1)
    prev_debut = prev_fin - timedelta(days=nb_jours - 1)
    precedent  = _periode_bilan(prev_debut, prev_fin, db)

    dt_deb, dt_fin = _bornes(date_debut, date_fin)

    # Évolution quotidienne CA vs achats
    lignes = (
        db.query(CuisineLigneVente.sous_total, CuisineVente.date_heure)
        .join(CuisineVente, CuisineLigneVente.vente_id == CuisineVente.id)
        .filter(CuisineVente.statut == "VALIDEE",
                CuisineVente.date_heure >= dt_deb, CuisineVente.date_heure <= dt_fin)
        .all()
    )
    evo_ca: dict[str, float] = {}
    for sous_total, dh in lignes:
        k = dh.date().isoformat()
        evo_ca[k] = evo_ca.get(k, 0.0) + float(_dec(sous_total))

    achats_periode = (
        db.query(CuisineAchat)
        .filter(CuisineAchat.date_achat >= dt_deb, CuisineAchat.date_achat <= dt_fin)
        .all()
    )
    evo_achats: dict[str, float] = {}
    achats_par_cat: dict[str, float] = {}
    for a in achats_periode:
        k = a.date_achat.date().isoformat()
        evo_achats[k] = evo_achats.get(k, 0.0) + float(_dec(a.total))
        cat = a.categorie or "AUTRE"
        achats_par_cat[cat] = achats_par_cat.get(cat, 0.0) + float(_dec(a.total))

    tous_jours = sorted(set(evo_ca) | set(evo_achats))
    evolution = [
        {
            "date":     j,
            "ca":       round(evo_ca.get(j, 0.0), 2),
            "achats":   round(evo_achats.get(j, 0.0), 2),
            "benefice": round(evo_ca.get(j, 0.0) - evo_achats.get(j, 0.0), 2),
        }
        for j in tous_jours
    ]
    if nb_jours > 32:
        evolution = _bucket_evolution(evolution, nb_jours)

    # Top plats par bénéfice réel — regroupé par plat_id (pas nom_plat, contrairement
    # à /stats : un plat renommé ou dupliqué ne fausse plus l'agrégation).
    ventes_par_plat = (
        db.query(
            CuisineLigneVente.plat_id,
            sqlfunc.sum(CuisineLigneVente.sous_total),
            sqlfunc.sum(CuisineLigneVente.quantite),
        )
        .join(CuisineVente, CuisineLigneVente.vente_id == CuisineVente.id)
        .filter(CuisineVente.statut == "VALIDEE", CuisineLigneVente.plat_id.isnot(None),
                CuisineVente.date_heure >= dt_deb, CuisineVente.date_heure <= dt_fin)
        .group_by(CuisineLigneVente.plat_id)
        .all()
    )
    achats_par_plat = dict(
        db.query(CuisineAchat.plat_id, sqlfunc.sum(CuisineAchat.total))
        .filter(CuisineAchat.plat_id.isnot(None),
                CuisineAchat.date_achat >= dt_deb, CuisineAchat.date_achat <= dt_fin)
        .group_by(CuisineAchat.plat_id)
        .all()
    )
    plats_map = {p.id: p.nom for p in db.query(CuisinePlat.id, CuisinePlat.nom).all()}

    top_plats = []
    for pid, ca, qte in ventes_par_plat:
        ca_f  = float(_dec(ca))
        qte_f = float(_dec(qte))
        cout  = float(_dec(achats_par_plat.get(pid, 0)))
        ben   = ca_f - cout
        top_plats.append({
            "plat_id":          pid,
            "plat_nom":         plats_map.get(pid, f"Plat #{pid}"),
            "ca":               round(ca_f, 2),
            "cout_achats":      round(cout, 2),
            "quantite_vendue":  round(qte_f, 2),
            "benefice":         round(ben, 2),
            "marge_pct":        round(ben / ca_f * 100, 1) if ca_f > 0 else 0.0,
        })
    top_plats.sort(key=lambda x: x["benefice"], reverse=True)
    top_plats = top_plats[:10]

    # ── Caisse Cuisine disponible : cash réellement encaissé (mode CASH
    # uniquement — une vente CREDIT n'est pas encore du cash) + renflouements
    # - achats - dépenses. Distinct de "actuel.benefice_net" ci-dessus qui est
    # une marge sur ventes totales (CASH+CREDIT), pas un vrai solde de caisse. ──
    ventes_cash = float(_dec(
        db.query(sqlfunc.sum(CuisineVente.total))
        .filter(CuisineVente.statut == "VALIDEE", CuisineVente.mode_paiement == "CASH",
                CuisineVente.date_heure >= dt_deb, CuisineVente.date_heure <= dt_fin)
        .scalar()
    ))
    depenses_periode = float(_dec(
        db.query(sqlfunc.sum(CuisineDepense.montant))
        .filter(CuisineDepense.date_depense >= dt_deb, CuisineDepense.date_depense <= dt_fin)
        .scalar()
    ))
    renflouements_periode = float(_dec(
        db.query(sqlfunc.sum(RenflouementDepartement.montant))
        .filter(RenflouementDepartement.departement == "CUISINE",
                RenflouementDepartement.date_renflouement >= dt_deb,
                RenflouementDepartement.date_renflouement <= dt_fin)
        .scalar()
    ))
    caisse_disponible = round(ventes_cash + renflouements_periode - actuel["cout_achats_total"] - depenses_periode, 2)

    return {
        "periode":              {"debut": str(date_debut), "fin": str(date_fin)},
        "periode_precedente":   {"debut": str(prev_debut),  "fin": str(prev_fin)},
        "actuel":               actuel,
        "precedent":            precedent,
        "evolution":            evolution,
        "top_plats":            top_plats,
        "achats_par_categorie": [{"categorie": k, "montant": round(v, 2)} for k, v in sorted(achats_par_cat.items())],
        "caisse": {
            "ventes_cash":     round(ventes_cash, 2),
            "achats":          actuel["cout_achats_total"],
            "depenses":        round(depenses_periode, 2),
            "renflouements":   round(renflouements_periode, 2),
            "disponible":      caisse_disponible,
        },
    }


@router.get("/lots")
def rentabilite_par_achat(
    plat_id:    Optional[int]       = Query(default=None),
    date_debut: Optional[date_type] = Query(default=None),
    date_fin:   Optional[date_type] = Query(default=None),
    db: Session = Depends(get_db),
):
    """
    Rentabilité de chaque achat individuel : la fenêtre de vente d'un achat s'étend
    jusqu'au prochain achat du même plat (ou jusqu'à maintenant si c'est le plus
    récent — en_cours=true dans ce cas, pas de verdict final). Les achats sans plat
    lié (achats généraux : gaz, équipement...) sont exclus — impossible de leur
    associer une fenêtre de vente précise ; ils restent comptés dans le coût total
    de /synthese.
    """
    q = db.query(CuisineAchat).filter(CuisineAchat.plat_id.isnot(None))
    if plat_id:
        q = q.filter(CuisineAchat.plat_id == plat_id)
    if date_debut:
        q = q.filter(CuisineAchat.date_achat >= _bornes(date_debut, date_debut)[0])
    if date_fin:
        q = q.filter(CuisineAchat.date_achat <= _bornes(date_fin, date_fin)[1])
    achats_filtres = q.all()

    if not achats_filtres:
        return {"lots": [], "resume": {"total_investi": 0.0, "total_recupere": 0.0,
                                        "taux_moyen_pct": 0.0, "nb_achats": 0}}

    achats_filtres_ids = {a.id for a in achats_filtres}
    plats_concernes    = {a.plat_id for a in achats_filtres}

    # Historique COMPLET (non filtré par date) des achats de ces plats — indispensable
    # pour calculer correctement la borne de fin de fenêtre (achat suivant), même si
    # celui-ci tombe hors de la période filtrée.
    tous_achats_plats = (
        db.query(CuisineAchat)
        .filter(CuisineAchat.plat_id.in_(plats_concernes))
        .order_by(CuisineAchat.plat_id, CuisineAchat.date_achat)
        .all()
    )
    par_plat: dict[int, list[CuisineAchat]] = {}
    for a in tous_achats_plats:
        par_plat.setdefault(a.plat_id, []).append(a)

    lignes_ventes = (
        db.query(CuisineLigneVente, CuisineVente.date_heure)
        .join(CuisineVente, CuisineLigneVente.vente_id == CuisineVente.id)
        .filter(CuisineVente.statut == "VALIDEE", CuisineLigneVente.plat_id.in_(plats_concernes))
        .all()
    )
    ventes_par_plat: dict[int, list] = {}
    for l, dh in lignes_ventes:
        ventes_par_plat.setdefault(l.plat_id, []).append((l, dh))

    plats_map = {
        p.id: p.nom for p in
        db.query(CuisinePlat.id, CuisinePlat.nom).filter(CuisinePlat.id.in_(plats_concernes)).all()
    }
    now_utc = datetime.now(timezone.utc)

    lots = []
    for pid, liste in par_plat.items():
        for i, achat in enumerate(liste):
            if achat.id not in achats_filtres_ids:
                continue
            debut    = achat.date_achat
            en_cours = (i + 1 >= len(liste))
            fin      = liste[i + 1].date_achat if not en_cours else now_utc

            ventes_lot = [(l, dh) for l, dh in ventes_par_plat.get(pid, []) if debut <= dh < fin]
            qte_vendue = sum(float(_dec(l.quantite))   for l, _ in ventes_lot)
            ca_genere  = sum(float(_dec(l.sous_total)) for l, _ in ventes_lot)

            cout_total = float(_dec(achat.total))
            recup_pct  = round(ca_genere / cout_total * 100, 1) if cout_total > 0 else 0.0
            benefice   = round(ca_genere - cout_total, 2)

            if en_cours:
                statut = "en_cours"
            elif recup_pct >= 100:
                statut = "rentable"
            elif recup_pct >= 60:
                statut = "a_surveiller"
            else:
                statut = "perte"

            lots.append({
                "achat_id":         achat.id,
                "date_achat":       achat.date_achat.isoformat(),
                "plat_id":          pid,
                "plat_nom":         plats_map.get(pid, f"Plat #{pid}"),
                "description":      achat.description,
                "fournisseur":      achat.fournisseur,
                "cout_achat":       round(cout_total, 2),
                "quantite_vendue":  round(qte_vendue, 2),
                "ca_genere":        round(ca_genere, 2),
                "recuperation_pct": recup_pct,
                "benefice_estime":  benefice,
                "en_cours":         en_cours,
                "statut":           statut,
            })

    lots.sort(key=lambda x: x["date_achat"], reverse=True)

    total_investi  = sum(l["cout_achat"] for l in lots)
    total_recupere = sum(l["ca_genere"]  for l in lots)
    return {
        "lots": lots,
        "resume": {
            "total_investi":  round(total_investi, 2),
            "total_recupere": round(total_recupere, 2),
            "taux_moyen_pct": round(total_recupere / total_investi * 100, 1) if total_investi > 0 else 0.0,
            "nb_achats":      len(lots),
        },
    }
