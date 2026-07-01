"""
Service métier POS bar/restaurant.

Règles fondamentales (non négociables) :
  - Stock = valeur calculée (somme des mouvements signés), jamais stocké
  - Prix historisés : toujours le prix en vigueur à la date de la transaction
  - COGS via coût moyen pondéré (CMUP) sur les achats
  - encaisser_vente() est une transaction atomique avec rollback sur erreur
"""
from __future__ import annotations

from datetime import datetime, timezone, date as date_type
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import func
from sqlalchemy.orm import Session

from models import (
    BarProduit, BarPrixHistorique, BarAchat, BarMouvementStock,
    BarVente, BarLigneVente, BarCredit, BarRemboursement,
    BarCommande, BarLigneCommande,
)


# ──────────────────────────────────────────────────────────────────
# Helpers de calcul
# ──────────────────────────────────────────────────────────────────

def _dec(v) -> Decimal:
    """Conversion sûre en Decimal."""
    return Decimal(str(v)) if v is not None else Decimal("0")


def stock_courant(produit_id: int, db: Session) -> Decimal:
    """Stock courant = somme algébrique de tous les mouvements (quantité signée)."""
    result = db.query(func.sum(BarMouvementStock.quantite)).filter(
        BarMouvementStock.produit_id == produit_id
    ).scalar()
    return _dec(result)


def stock_tous_produits(db: Session) -> dict[int, Decimal]:
    """Stock courant de tous les produits actifs en une seule requête."""
    rows = (
        db.query(BarMouvementStock.produit_id, func.sum(BarMouvementStock.quantite))
        .group_by(BarMouvementStock.produit_id)
        .all()
    )
    return {pid: _dec(total) for pid, total in rows}


def prix_actif(produit_id: int, db: Session) -> Decimal | None:
    """Prix de vente actif (date_fin IS NULL) pour un produit donné."""
    row = (
        db.query(BarPrixHistorique)
        .filter(
            BarPrixHistorique.produit_id == produit_id,
            BarPrixHistorique.date_fin.is_(None),
        )
        .order_by(BarPrixHistorique.date_debut.desc())
        .first()
    )
    return _dec(row.prix) if row else None


def cmup(produit_id: int, db: Session) -> Decimal:
    """
    Coût moyen pondéré (CMUP) basé sur l'historique des achats.
    Retourne 0 si aucun achat enregistré.
    """
    achats = (
        db.query(BarAchat)
        .filter(BarAchat.produit_id == produit_id, BarAchat.quantite > 0)
        .all()
    )
    total_qte     = sum(_dec(a.quantite) for a in achats)
    total_montant = sum(_dec(a.quantite) * _dec(a.prix_achat_unitaire) for a in achats)
    if total_qte == 0:
        return Decimal("0")
    return (total_montant / total_qte).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def generer_numero_ticket(db: Session) -> str:
    """Génère un numéro de ticket unique au format TK-YYYYMMDD-XXXX."""
    today = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
    count = (
        db.query(BarVente)
        .filter(BarVente.numero_ticket.like(f"TK{today}%"))
        .count()
    )
    return f"TK{today}{str(count + 1).zfill(4)}"


# ──────────────────────────────────────────────────────────────────
# Transaction d'encaissement
# ──────────────────────────────────────────────────────────────────

def encaisser_vente(data: dict, db: Session, utilisateur_id: int | None = None) -> BarVente:
    """
    Encaisse une vente de façon atomique.

    Étapes :
      1. Valider stock et prix pour chaque ligne
      2. Créer BarVente + BarLigneVente (prix historisé)
      3. Créer BarMouvementStock SORTIE_VENTE pour chaque ligne
      4. Créer BarCredit si mode CREDIT ou montant_restant > 0

    Lève ValueError en cas d'anomalie bloquante (stock négatif, prix absent).
    Le rollback est géré par l'appelant (route FastAPI).
    """
    lignes_input = data.get("lignes", [])
    if not lignes_input:
        raise ValueError("La vente doit comporter au moins une ligne.")

    lignes_traitees = []
    erreurs = []

    for l in lignes_input:
        pid = int(l["produit_id"])
        qte = _dec(l["quantite"])

        produit = db.query(BarProduit).filter_by(id=pid, actif=True).first()
        if not produit:
            erreurs.append(f"Produit #{pid} introuvable ou inactif.")
            continue

        prix = prix_actif(pid, db)
        if prix is None:
            erreurs.append(f"Aucun prix défini pour « {produit.nom} ».")
            continue

        stk = stock_courant(pid, db)
        if stk < qte:
            erreurs.append(
                f"Stock insuffisant pour « {produit.nom} » "
                f"(disponible : {float(stk):.3f}, demandé : {float(qte):.3f})."
            )
            continue

        sous_total = (qte * prix).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        lignes_traitees.append({
            "produit_id": pid,
            "produit_nom": produit.nom,
            "quantite":    qte,
            "prix":        prix,
            "sous_total":  sous_total,
        })

    if erreurs:
        raise ValueError("; ".join(erreurs))

    montant_total   = sum(l["sous_total"] for l in lignes_traitees)
    montant_paye    = _dec(data.get("montant_paye", montant_total))
    montant_restant = (montant_total - montant_paye).quantize(Decimal("0.01"))
    if montant_restant < 0:
        montant_restant = Decimal("0")

    mode   = data.get("mode_paiement", "CASH").upper()
    statut = "CREDIT_EN_COURS" if montant_restant > 0 else "PAYEE"

    # Créer la vente
    vente = BarVente(
        numero_ticket   = generer_numero_ticket(db),
        caissier_id     = data.get("caissier_id"),
        montant_total   = montant_total,
        mode_paiement   = mode,
        statut          = statut,
        client_nom      = data.get("client_nom"),
        montant_paye    = montant_paye,
        montant_restant = montant_restant,
    )
    db.add(vente)
    db.flush()   # obtenir l'ID avant les lignes

    for l in lignes_traitees:
        db.add(BarLigneVente(
            vente_id               = vente.id,
            produit_id             = l["produit_id"],
            quantite               = l["quantite"],
            prix_unitaire_applique = l["prix"],
            sous_total             = l["sous_total"],
        ))
        db.add(BarMouvementStock(
            produit_id         = l["produit_id"],
            type_mouvement     = "SORTIE_VENTE",
            quantite           = -l["quantite"],   # négatif = sortie
            motif              = f"Vente ticket {vente.numero_ticket}",
            reference_vente_id = vente.id,
            utilisateur_id     = utilisateur_id,
        ))

    if statut == "CREDIT_EN_COURS":
        db.add(BarCredit(
            vente_id          = vente.id,
            client_nom        = data.get("client_nom") or "Client inconnu",
            client_contact    = data.get("client_contact"),
            client_nif        = data.get("client_nif"),
            montant_du        = montant_restant,
            montant_rembourse = Decimal("0"),
            solde             = montant_restant,
            date_echeance     = data.get("date_echeance"),
        ))

    db.commit()
    db.refresh(vente)
    return vente


def annuler_vente(vente_id: int, db: Session, utilisateur_id: int | None = None) -> BarVente:
    """
    Annule une vente et réinjecte le stock pour chaque ligne.
    Lève ValueError si la vente est déjà annulée ou inexistante.
    """
    vente = db.query(BarVente).filter_by(id=vente_id).first()
    if not vente:
        raise ValueError(f"Vente #{vente_id} introuvable.")
    if vente.statut == "ANNULEE":
        raise ValueError(f"Vente #{vente_id} déjà annulée.")

    vente.statut = "ANNULEE"

    for ligne in vente.lignes:
        db.add(BarMouvementStock(
            produit_id         = ligne.produit_id,
            type_mouvement     = "ENTREE",
            quantite           = ligne.quantite,   # positif = réintégration
            motif              = f"Annulation vente {vente.numero_ticket}",
            reference_vente_id = vente.id,
            utilisateur_id     = utilisateur_id,
        ))

    # Clore le crédit éventuel
    if vente.credit and vente.credit.statut != "SOLDE":
        vente.credit.statut = "SOLDE"
        vente.credit.solde  = Decimal("0")

    db.commit()
    db.refresh(vente)
    return vente


def encaisser_commande(commande_id: int, data: dict,
                       db: Session, utilisateur_id: int | None = None) -> BarVente:
    """
    Transforme une commande ouverte en vente encaissée.
    Les lignes de la commande deviennent les lignes de la vente.
    """
    commande = db.query(BarCommande).filter_by(id=commande_id).first()
    if not commande:
        raise ValueError(f"Commande #{commande_id} introuvable.")
    if commande.statut in ("ENCAISSEE", "ANNULEE"):
        raise ValueError(f"Commande #{commande_id} déjà {commande.statut.lower()}.")

    vente_data = {
        "lignes": [
            {"produit_id": l.produit_id, "quantite": float(l.quantite)}
            for l in commande.lignes
        ],
        "caissier_id":    commande.caissier_id,
        "mode_paiement":  data.get("mode_paiement", "CASH"),
        "montant_paye":   data.get("montant_paye"),
        "client_nom":     data.get("client_nom") or commande.client,
        "client_contact": data.get("client_contact"),
        "date_echeance":  data.get("date_echeance"),
    }
    vente = encaisser_vente(vente_data, db, utilisateur_id)

    commande.statut = "ENCAISSEE"
    db.commit()
    return vente


# ──────────────────────────────────────────────────────────────────
# Détection d'anomalies bar
# ──────────────────────────────────────────────────────────────────

def detecter_anomalies_bar(date_cible: date_type, db: Session) -> list[dict]:
    """
    Détecte les anomalies spécifiques au module bar pour une date donnée.
    Retourne une liste de dicts au même format que les anomalies existantes.
    """
    anomalies = []

    # ── STOCK_NEGATIF_BAR & VENTE_SANS_PRIX & CONFIG_CAISSE_INVALIDE ─
    produits = db.query(BarProduit).filter_by(actif=True).all()
    for p in produits:
        stk  = stock_courant(p.id, db)
        prix = prix_actif(p.id, db)

        if p.vendu_par_caisse and (not p.unites_par_caisse or p.unites_par_caisse < 1):
            anomalies.append({
                "type":        "CONFIG_CAISSE_INVALIDE",
                "gravite":     "erreur",
                "produit_id":  p.id,
                "produit_nom": p.nom,
                "date":        str(date_cible),
                "message": (
                    f"Produit « {p.nom} » marqué vendu_par_caisse mais unites_par_caisse "
                    f"est nul ou invalide ({p.unites_par_caisse}). Impossible de calculer le stock en caisses."
                ),
            })

        if stk < 0:
            anomalies.append({
                "type":         "STOCK_NEGATIF_BAR",
                "gravite":      "erreur",
                "produit_id":   p.id,
                "produit_nom":  p.nom,
                "date":         str(date_cible),
                "stock_courant": float(stk),
                "message": (
                    f"Stock négatif pour « {p.nom} » : {float(stk):.3f} unités. "
                    f"Vente enregistrée sans stock suffisant."
                ),
            })

        if prix is None:
            anomalies.append({
                "type":        "VENTE_SANS_PRIX",
                "gravite":     "avertissement",
                "produit_id":  p.id,
                "produit_nom": p.nom,
                "date":        str(date_cible),
                "message": f"Produit actif sans prix de vente défini : « {p.nom} ».",
            })

    # ── CREDIT_EN_RETARD ─────────────────────────────────────────
    credits_retard = (
        db.query(BarCredit)
        .filter(
            BarCredit.statut == "OUVERT",
            BarCredit.date_echeance.isnot(None),
            BarCredit.date_echeance < date_cible,
        )
        .all()
    )
    for c in credits_retard:
        anomalies.append({
            "type":           "CREDIT_EN_RETARD",
            "gravite":        "avertissement",
            "credit_id":      c.id,
            "client_nom":     c.client_nom,
            "date":           str(date_cible),
            "solde":          float(c.solde),
            "date_echeance":  str(c.date_echeance),
            "message": (
                f"Crédit en retard pour {c.client_nom} : "
                f"{float(c.solde):.2f} G (échéance : {c.date_echeance})."
            ),
        })
        # Mise à jour automatique du statut
        c.statut = "EN_RETARD"

    if credits_retard:
        db.commit()

    # ── DECALAGE_STOCK_BAR ───────────────────────────────────────
    # Vérifie les produits dont le stock théorique < seuil_alerte_stock
    for p in produits:
        stk = stock_courant(p.id, db)
        if p.seuil_alerte_stock and stk <= _dec(p.seuil_alerte_stock):
            anomalies.append({
                "type":              "DECALAGE_STOCK_BAR",
                "gravite":           "avertissement",
                "produit_id":        p.id,
                "produit_nom":       p.nom,
                "date":              str(date_cible),
                "stock_courant":     float(stk),
                "seuil_alerte":      float(p.seuil_alerte_stock),
                "message": (
                    f"Stock bas pour « {p.nom} » : {float(stk):.3f} unités "
                    f"(seuil d'alerte : {float(p.seuil_alerte_stock):.3f})."
                ),
            })

    return anomalies


# ──────────────────────────────────────────────────────────────────
# Calcul de rentabilité
# ──────────────────────────────────────────────────────────────────

def stats_bar(date_debut: date_type, date_fin: date_type, db: Session) -> dict:
    """
    Calcule les statistiques de rentabilité pour une période donnée.
    CA, COGS (coût moyen pondéré), bénéfice, marges par produit.
    Uniquement des données réelles — jamais de valeurs fabriquées.
    """
    from datetime import datetime, time

    dt_debut = datetime.combine(date_debut, time.min).replace(tzinfo=timezone.utc)
    dt_fin   = datetime.combine(date_fin,   time.max).replace(tzinfo=timezone.utc)

    # Ventes non annulées sur la période
    lignes = (
        db.query(BarLigneVente)
        .join(BarVente)
        .filter(
            BarVente.statut != "ANNULEE",
            BarVente.date_heure >= dt_debut,
            BarVente.date_heure <= dt_fin,
        )
        .all()
    )

    ca_total    = Decimal("0")
    cogs_total  = Decimal("0")
    par_produit: dict[int, dict] = {}

    for l in lignes:
        ca         = _dec(l.sous_total)
        cout_unit  = cmup(l.produit_id, db)
        cogs_ligne = (_dec(l.quantite) * cout_unit).quantize(Decimal("0.01"))

        ca_total   += ca
        cogs_total += cogs_ligne

        pid = l.produit_id
        if pid not in par_produit:
            par_produit[pid] = {
                "produit_id":  pid,
                "produit_nom": l.produit.nom if l.produit else str(pid),
                "categorie":   l.produit.categorie if l.produit else "",
                "quantite":    Decimal("0"),
                "ca":          Decimal("0"),
                "cogs":        Decimal("0"),
                "benefice":    Decimal("0"),
                "marge_pct":   Decimal("0"),
            }
        par_produit[pid]["quantite"] += _dec(l.quantite)
        par_produit[pid]["ca"]       += ca
        par_produit[pid]["cogs"]     += cogs_ligne

    # Calcul des bénéfices et marges par produit
    for pp in par_produit.values():
        pp["benefice"]  = pp["ca"] - pp["cogs"]
        pp["marge_pct"] = (
            (pp["benefice"] / pp["ca"] * 100).quantize(Decimal("0.01"))
            if pp["ca"] > 0 else Decimal("0")
        )
        # Conversion en float pour JSON
        for k in ("quantite", "ca", "cogs", "benefice", "marge_pct"):
            pp[k] = float(pp[k])

    benefice_net = ca_total - cogs_total

    top_produits = sorted(par_produit.values(), key=lambda x: x["ca"], reverse=True)[:10]

    return {
        "periode":      {"debut": str(date_debut), "fin": str(date_fin)},
        "ca_total":     float(ca_total),
        "cogs_total":   float(cogs_total),
        "benefice_net": float(benefice_net),
        "marge_globale": float(
            (benefice_net / ca_total * 100).quantize(Decimal("0.01"))
            if ca_total > 0 else Decimal("0")
        ),
        "nb_ventes":     db.query(BarVente).filter(
            BarVente.statut != "ANNULEE",
            BarVente.date_heure >= dt_debut,
            BarVente.date_heure <= dt_fin,
        ).count(),
        "top_produits":  top_produits,
        "par_produit":   list(par_produit.values()),
    }
