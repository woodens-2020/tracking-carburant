"""
Service partagé : source de vérité unique pour les gallons vendus, le stock
et la rentabilité.

INVARIANT : gallons_vendus() est la SEULE implémentation qui calcule les ventes
à partir des relevés de compteurs. Tous les autres calculs (stock, bénéfice,
anomalies) l'appellent — jamais de calcul dupliqué.

Méthode de coût : Coût Moyen Pondéré (WAC — Weighted Average Cost).
Justification : le carburant est fongible (les gallons de livraisons différentes
se mélangent physiquement dans la cuve). Il est donc impossible d'identifier
quelle livraison a alimenté quelle vente. Le WAC est la méthode comptable
standard pour les stocks fongibles et donne un coût stable sans tracking FIFO.
Limite : si les prix fluctuent fortement dans la période, le WAC lisse ces
variations — documenter dans les rapports.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from models import Releve, Pompe, Livraison, PrixVente, Produit


# ── Constante configurable (non codée en dur dans les appelants) ──────────
SEUIL_ALERTE_JOURS_PAR_DEFAUT = 7   # stock restant < 7 jours de vente → alerte


# ══════════════════════════════════════════════════════════════════════════
# 1. SOURCE DE VÉRITÉ : gallons vendus
# ══════════════════════════════════════════════════════════════════════════

def gallons_vendus(
    db: Session,
    produit_id: int,
    date_debut: date,
    date_fin: date,
) -> float:
    """
    Gallons vendus pour un produit sur une période, dérivés UNIQUEMENT des
    relevés de compteurs (metter_apres - metter_avant).

    Seuls les relevés avec quantité >= 0 sont comptés (les anomalies
    QUANTITE_NEGATIVE sont ignorées pour ne pas soustraire du stock).
    """
    releves = (
        db.query(Releve)
        .join(Pompe, Releve.pompe_id == Pompe.id)
        .filter(
            Pompe.produit_id == produit_id,
            Releve.date >= date_debut,
            Releve.date <= date_fin,
        )
        .all()
    )
    return round(sum(r.quantite for r in releves if r.quantite >= 0), 3)


def gallons_vendus_par_jour(
    db: Session,
    produit_id: int,
    date_debut: date,
    date_fin: date,
) -> dict[str, float]:
    """Retourne un dict {date_iso: gallons_vendus} pour chaque jour de la période."""
    releves = (
        db.query(Releve)
        .join(Pompe, Releve.pompe_id == Pompe.id)
        .filter(
            Pompe.produit_id == produit_id,
            Releve.date >= date_debut,
            Releve.date <= date_fin,
        )
        .all()
    )
    par_jour: dict[str, float] = {}
    for r in releves:
        if r.quantite >= 0:
            k = str(r.date)
            par_jour[k] = round(par_jour.get(k, 0.0) + r.quantite, 3)
    return par_jour


def revenu_ventes(
    db: Session,
    produit_id: int,
    date_debut: date,
    date_fin: date,
) -> float:
    """Revenu total (gourdes) pour un produit sur une période, depuis les relevés."""
    releves = (
        db.query(Releve)
        .join(Pompe, Releve.pompe_id == Pompe.id)
        .filter(
            Pompe.produit_id == produit_id,
            Releve.date >= date_debut,
            Releve.date <= date_fin,
        )
        .all()
    )
    return round(sum(r.montant_vente for r in releves if r.quantite >= 0), 2)


# ══════════════════════════════════════════════════════════════════════════
# 2. LIVRAISONS
# ══════════════════════════════════════════════════════════════════════════

def gallons_livres(
    db: Session,
    produit_id: int,
    jusqu_a: Optional[date] = None,
) -> float:
    """Total cumulé des gallons livrés pour un produit jusqu'à une date (incluse)."""
    q = db.query(Livraison).filter(Livraison.produit_id == produit_id)
    if jusqu_a is not None:
        q = q.filter(Livraison.date_livraison <= jusqu_a)
    return round(sum(float(l.gallons_recus) for l in q.all()), 3)


# ══════════════════════════════════════════════════════════════════════════
# 3. STOCK
# ══════════════════════════════════════════════════════════════════════════

def stock_restant(
    db: Session,
    produit_id: int,
    seuil_jours: int = SEUIL_ALERTE_JOURS_PAR_DEFAUT,
) -> dict:
    """
    Stock restant calculé = Σ(gallons livrés) − Σ(gallons vendus des relevés).
    Retourne aussi l'alerte stock bas si < seuil_jours de vente.

    Note : le stock théorique peut être négatif si des ventes sont enregistrées
    sans livraison correspondante (anomalie STOCK_NEGATIF déclenchée ailleurs).
    """
    aujourd_hui  = date.today()
    total_livre  = gallons_livres(db, produit_id, jusqu_a=aujourd_hui)
    total_vendu  = gallons_vendus(db, produit_id, date(2000, 1, 1), aujourd_hui)
    restant      = round(total_livre - total_vendu, 3)

    # Moyenne journalière sur les 30 derniers jours pour l'alerte
    debut_30j    = aujourd_hui - timedelta(days=29)
    vendu_30j    = gallons_vendus(db, produit_id, debut_30j, aujourd_hui)
    moy_jour     = round(vendu_30j / 30, 3)

    # jours_de_stock = restant / moy_jour (None si pas de données de vente)
    jours_de_stock: Optional[float] = None
    if moy_jour > 0:
        jours_de_stock = round(restant / moy_jour, 1)

    alerte_bas = (
        jours_de_stock is not None and jours_de_stock < seuil_jours
    ) or restant < 0

    return {
        "produit_id":      produit_id,
        "gallons_restants": restant,
        "gallons_livres":  total_livre,
        "gallons_vendus":  total_vendu,
        "moyenne_jour":    moy_jour,
        "jours_de_stock":  jours_de_stock,
        "alerte_bas":      alerte_bas,
        "seuil_jours":     seuil_jours,
    }


# ══════════════════════════════════════════════════════════════════════════
# 4. COÛT MOYEN PONDÉRÉ (WAC)
# ══════════════════════════════════════════════════════════════════════════

def cout_moyen_pondere(
    db: Session,
    produit_id: int,
    jusqu_a: Optional[date] = None,
) -> Optional[float]:
    """
    WAC = Σ(gallons_recus × prix_achat_gallon) / Σ(gallons_recus)
    sur toutes les livraisons jusqu'à la date indiquée.

    Retourne None si aucune livraison enregistrée (ne pas inventer de coût).

    Choix WAC vs FIFO : le carburant se mélange physiquement dans la cuve.
    Utiliser FIFO supposerait qu'on peut distinguer quel gallon vient de quelle
    livraison — impossible en pratique. WAC est l'approche standard (IAS 2).
    Impact : sur une période où le prix monte de 50 G à 80 G, le WAC sera
    ~65 G, sous-estimant le coût réel des dernières ventes.
    """
    q = db.query(Livraison).filter(Livraison.produit_id == produit_id)
    if jusqu_a is not None:
        q = q.filter(Livraison.date_livraison <= jusqu_a)
    livraisons = q.all()
    if not livraisons:
        return None
    total_gallons = sum(float(l.gallons_recus) for l in livraisons)
    total_cout    = sum(float(l.gallons_recus) * float(l.prix_achat_gallon) for l in livraisons)
    return round(total_cout / total_gallons, 4) if total_gallons > 0 else None


def prix_vente_effectif(
    db: Session,
    produit_id: int,
    a_la_date: date,
) -> Optional[float]:
    """
    Dernier prix de vente configuré pour ce produit dont la date_effet <= a_la_date.
    Retourne None si aucun prix n'a jamais été configuré.
    """
    pv = (
        db.query(PrixVente)
        .filter(
            PrixVente.produit_id == produit_id,
            PrixVente.date_effet <= a_la_date,
        )
        .order_by(PrixVente.date_effet.desc(), PrixVente.id.desc())
        .first()
    )
    return float(pv.prix_vente_gallon) if pv else None


# ══════════════════════════════════════════════════════════════════════════
# 5. RENTABILITÉ
# ══════════════════════════════════════════════════════════════════════════

def rentabilite_produit(
    db: Session,
    produit_id: int,
    date_debut: date,
    date_fin: date,
) -> dict:
    """
    Calcule la rentabilité pour un produit sur une période.

    Sources :
    - Revenu   : Releve.prix_gallon × quantité (prix réel au moment de la vente)
    - COGS     : gallons_vendus × WAC des livraisons jusqu'à date_fin
    - Bénéfice : Revenu − COGS

    Si le WAC est None (pas de livraison) : bénéfice = None avec fiable=False.
    Si aucun relevé : bénéfice = None avec fiable=False.
    Ne jamais inventer un chiffre.
    """
    vendus  = gallons_vendus(db, produit_id, date_debut, date_fin)
    revenu  = revenu_ventes(db, produit_id, date_debut, date_fin)
    cmp     = cout_moyen_pondere(db, produit_id, jusqu_a=date_fin)

    cogs_total: Optional[float] = None
    benefice:   Optional[float] = None
    marge_pct:  Optional[float] = None
    b_par_gal:  Optional[float] = None
    fiable = False

    if cmp is not None and vendus > 0:
        cogs_total = round(vendus * cmp, 2)
        benefice   = round(revenu - cogs_total, 2)
        marge_pct  = round(benefice / revenu * 100, 2) if revenu > 0 else 0.0
        b_par_gal  = round(benefice / vendus, 4) if vendus > 0 else 0.0
        fiable     = True

    # Détail par jour
    par_jour_gal = gallons_vendus_par_jour(db, produit_id, date_debut, date_fin)

    return {
        "produit_id":     produit_id,
        "date_debut":     str(date_debut),
        "date_fin":       str(date_fin),
        "gallons_vendus": vendus,
        "revenu_total":   revenu,
        "cmp":            cmp,
        "cogs_total":     cogs_total,
        "benefice":       benefice,
        "marge_pct":      marge_pct,
        "b_par_gallon":   b_par_gal,
        "fiable":         fiable,
        "par_jour":       par_jour_gal,
        "avertissement":  None if fiable else (
            "Aucune livraison enregistrée — COGS non calculable."
            if cmp is None else
            "Aucun relevé de vente sur cette période."
        ),
    }


def rentabilite_globale(
    db: Session,
    date_debut: date,
    date_fin: date,
    produit_id: Optional[int] = None,
) -> dict:
    """Agrège la rentabilité de tous les produits (ou d'un seul si filtré)."""
    if produit_id:
        produits = db.query(Produit).filter(Produit.id == produit_id, Produit.actif == True).all()
    else:
        produits = db.query(Produit).filter(Produit.actif == True).all()

    details = []
    tot_revenu   = 0.0
    tot_cogs     = 0.0
    tot_vendus   = 0.0
    tout_fiable  = True

    for p in produits:
        r = rentabilite_produit(db, p.id, date_debut, date_fin)
        r["produit_nom"] = p.nom
        details.append(r)
        tot_vendus += r["gallons_vendus"]
        tot_revenu += r["revenu_total"]
        if r["cogs_total"] is not None:
            tot_cogs += r["cogs_total"]
        else:
            tout_fiable = False

    tot_benefice  = round(tot_revenu - tot_cogs, 2) if tout_fiable else None
    tot_marge     = round(tot_benefice / tot_revenu * 100, 2) if (tout_fiable and tot_revenu > 0) else None

    return {
        "date_debut":    str(date_debut),
        "date_fin":      str(date_fin),
        "produits":      details,
        "total": {
            "gallons_vendus": round(tot_vendus, 3),
            "revenu_total":   round(tot_revenu, 2),
            "cogs_total":     round(tot_cogs, 2) if tout_fiable else None,
            "benefice":       tot_benefice,
            "marge_pct":      tot_marge,
            "fiable":         tout_fiable,
        },
    }


# ══════════════════════════════════════════════════════════════════════════
# 6. ANOMALIES STOCK (utilisées par /api/anomalies)
# ══════════════════════════════════════════════════════════════════════════

SEUIL_DECALAGE_STOCK = 5   # même logique que SEUIL_SAUT pour les compteurs


def anomalies_stock(
    db: Session,
    jusqu_a: date,
) -> list[dict]:
    """
    Calcule les anomalies de cohérence stock pour tous les produits actifs.
    Appelé depuis /api/anomalies pour enrichir les anomalies compteurs.

    Types retournés :
      STOCK_NEGATIF     — gallons vendus > gallons livrés (stock théorique < 0)
      VENTE_SANS_STOCK  — ventes enregistrées sans aucune livraison
      PRIX_MANQUANT     — ventes sans prix d'achat défini (COGS incalculable)
      DECALAGE_STOCK    — vente journalière anormalement haute vs moyenne produit
    """
    produits = db.query(Produit).filter(Produit.actif == True).all()
    anomalies_list: list[dict] = []

    for p in produits:
        total_livre = gallons_livres(db, p.id, jusqu_a=jusqu_a)
        total_vendu = gallons_vendus(db, p.id, date(2000, 1, 1), jusqu_a)

        # ── VENTE_SANS_STOCK ──────────────────────────────────────
        if total_vendu > 0 and total_livre == 0:
            anomalies_list.append({
                "type":        "VENTE_SANS_STOCK",
                "gravite":     "erreur",
                "produit_nom": p.nom,
                "produit_id":  p.id,
                "date":        str(jusqu_a),
                "valeur":      round(total_vendu, 3),
                "message": (
                    f"{round(total_vendu, 3)} gal vendus (relevés) pour '{p.nom}' "
                    f"sans aucune livraison enregistrée. "
                    f"Saisir les livraisons dans le module Stock."
                ),
            })

        # ── STOCK_NEGATIF ─────────────────────────────────────────
        elif total_livre > 0 and total_vendu > total_livre:
            deficit = round(total_vendu - total_livre, 3)
            anomalies_list.append({
                "type":        "STOCK_NEGATIF",
                "gravite":     "erreur",
                "produit_nom": p.nom,
                "produit_id":  p.id,
                "date":        str(jusqu_a),
                "gallons_livres": total_livre,
                "gallons_vendus": total_vendu,
                "deficit":     deficit,
                "message": (
                    f"Stock négatif pour '{p.nom}' : {total_vendu} gal vendus "
                    f"pour {total_livre} gal livrés (déficit de {deficit} gal). "
                    f"Livraison(s) manquante(s) ou anomalie de compteur."
                ),
            })

        # ── PRIX_MANQUANT ─────────────────────────────────────────
        cmp = cout_moyen_pondere(db, p.id, jusqu_a=jusqu_a)
        if total_vendu > 0 and cmp is None:
            anomalies_list.append({
                "type":        "PRIX_MANQUANT",
                "gravite":     "avertissement",
                "produit_nom": p.nom,
                "produit_id":  p.id,
                "date":        str(jusqu_a),
                "message": (
                    f"'{p.nom}' a {round(total_vendu, 3)} gal vendus mais "
                    f"aucun prix d'achat enregistré — COGS non calculable, "
                    f"rentabilité indisponible. Ajouter une livraison avec prix."
                ),
            })

        # ── DECALAGE_STOCK (vente journalière >> moyenne produit) ─
        par_jour = gallons_vendus_par_jour(db, p.id, date(2000, 1, 1), jusqu_a)
        valeurs_jour = [v for v in par_jour.values() if v > 0]
        if len(valeurs_jour) >= 5:
            moy_jour = sum(valeurs_jour) / len(valeurs_jour)
            for jour_str, qte in par_jour.items():
                if qte > SEUIL_DECALAGE_STOCK * moy_jour:
                    anomalies_list.append({
                        "type":        "DECALAGE_STOCK",
                        "gravite":     "avertissement",
                        "produit_nom": p.nom,
                        "produit_id":  p.id,
                        "date":        jour_str,
                        "valeur_saisie": round(qte, 3),
                        "moyenne_jour":  round(moy_jour, 3),
                        "seuil_utilise": round(SEUIL_DECALAGE_STOCK * moy_jour, 3),
                        "message": (
                            f"Consommation journalière de '{p.nom}' le {jour_str} : "
                            f"{round(qte, 3)} gal, soit {round(qte/moy_jour, 1)}× "
                            f"la moyenne ({round(moy_jour, 3)} gal/jour). "
                            f"Possible fuite, vol ou erreur de saisie."
                        ),
                    })

    return anomalies_list


def corr_saut_decalage(
    anomalies_compteurs: list[dict],
    anomalies_stk: list[dict],
) -> tuple[list[dict], list[dict]]:
    """
    Corrèle les SAUT_ANORMAL et DECALAGE_STOCK survenant le même jour pour le
    même produit. Les deux anomalies reçoivent un champ `incident_lie` avec
    l'ID de l'autre et un message explicatif.

    Retourne (anomalies_compteurs_enrichies, anomalies_stock_enrichies).
    """
    incident_counter = [0]

    def new_id():
        incident_counter[0] += 1
        return f"INC-{incident_counter[0]:03d}"

    # Index DECALAGE_STOCK par (date, produit_id)
    decalages_idx: dict[tuple, dict] = {}
    for a in anomalies_stk:
        if a["type"] == "DECALAGE_STOCK":
            decalages_idx[(a["date"], a["produit_id"])] = a

    for anom_c in anomalies_compteurs:
        if anom_c["type"] != "SAUT_ANORMAL":
            continue
        # Récupérer le produit_id via la pompe — non disponible ici directement.
        # On transmet produit_id dans l'anomalie depuis l'appelant (main.py).
        produit_id = anom_c.get("produit_id")
        if produit_id is None:
            continue
        cle = (anom_c["date"], produit_id)
        anom_stk = decalages_idx.get(cle)
        if anom_stk:
            inc_id = new_id()
            anom_c["incident_lie"]  = inc_id
            anom_stk["incident_lie"] = inc_id
            anom_c["message"] += (
                f" ⚠ Corrélé avec DECALAGE_STOCK {inc_id} : "
                f"même produit, même jour — incident unique probable."
            )
            anom_stk["message"] += (
                f" ⚠ Corrélé avec SAUT_ANORMAL {inc_id} : "
                f"même produit, même jour — vérifier la pompe concernée."
            )

    return anomalies_compteurs, list(anomalies_stk)
