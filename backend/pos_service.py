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
    CuisinePlat, CuisineVente, CuisineLigneVente,
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
    Coût moyen pondéré (CMUP) basé sur les achats CONFIRMÉS uniquement.
    Retourne 0 si aucun achat confirmé.
    """
    achats = (
        db.query(BarAchat)
        .filter(
            BarAchat.produit_id == produit_id,
            BarAchat.quantite > 0,
            BarAchat.statut == 'CONFIRME',
        )
        .all()
    )
    total_qte     = sum(_dec(a.quantite) for a in achats)
    total_montant = sum(_dec(a.quantite) * _dec(a.prix_achat_unitaire) for a in achats)
    if total_qte == 0:
        return Decimal("0")
    return (total_montant / total_qte).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def cmup_batch(db: Session) -> dict[int, Decimal]:
    """CMUP de tous les produits bar en une seule requête GROUP BY (évite N+1)."""
    rows = (
        db.query(
            BarAchat.produit_id,
            func.sum(BarAchat.quantite * BarAchat.prix_achat_unitaire),
            func.sum(BarAchat.quantite),
        )
        .filter(
            BarAchat.produit_id.isnot(None),
            BarAchat.quantite > 0,
            BarAchat.statut == 'CONFIRME',
        )
        .group_by(BarAchat.produit_id)
        .all()
    )
    result: dict[int, Decimal] = {}
    for pid, total_montant, total_qte in rows:
        if total_qte and _dec(total_qte) > 0:
            result[pid] = (
                _dec(total_montant) / _dec(total_qte)
            ).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        else:
            result[pid] = Decimal("0")
    return result


def prix_actif_batch(db: Session) -> dict[int, Decimal]:
    """Prix de vente actif (date_fin IS NULL) de tous les produits en une requête."""
    rows = (
        db.query(BarPrixHistorique)
        .filter(BarPrixHistorique.date_fin.is_(None))
        .order_by(BarPrixHistorique.produit_id, BarPrixHistorique.date_debut.desc())
        .all()
    )
    result: dict[int, Decimal] = {}
    for r in rows:
        if r.produit_id not in result:
            result[r.produit_id] = _dec(r.prix)
    return result


def generer_numero_ticket(db: Session) -> str:
    """Génère un numéro de ticket bar unique — SELECT FOR UPDATE évite la race condition."""
    today  = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
    prefix = f"TK{today}"
    last = (
        db.query(BarVente.numero_ticket)
        .filter(BarVente.numero_ticket.like(f"{prefix}%"))
        .with_for_update()
        .order_by(BarVente.numero_ticket.desc())
        .first()
    )
    seq = int(last.numero_ticket[len(prefix):]) + 1 if last else 1
    return f"{prefix}{str(seq).zfill(4)}"


def _generer_ticket_cuisine(db: Session) -> str:
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
        qte = _dec(l["quantite"])

        # ── Ligne Plat Cuisine (vendu via bar) ───────────────────────
        if l.get("cuisine_plat_id"):
            cid = int(l["cuisine_plat_id"])
            plat = db.query(CuisinePlat).filter_by(id=cid, actif=True).first()
            if not plat:
                erreurs.append(f"Plat cuisine #{cid} introuvable ou inactif.")
                continue
            prix = _dec(l.get("prix_unitaire") or plat.prix_vente)
            sous_total = (qte * prix).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            lignes_traitees.append({
                "cuisine_plat_id": cid,
                "produit_id":      None,
                "produit_nom":     plat.nom,
                "quantite":        qte,
                "prix":            prix,
                "sous_total":      sous_total,
            })
            continue

        # ── Ligne Produit Bar (logique habituelle) ────────────────────
        pid = int(l["produit_id"])

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
            "cuisine_plat_id": None,
            "produit_id":      pid,
            "produit_nom":     produit.nom,
            "quantite":        qte,
            "prix":            prix,
            "sous_total":      sous_total,
        })

    if erreurs:
        raise ValueError("; ".join(erreurs))

    montant_total = sum(l["sous_total"] for l in lignes_traitees)

    _MODES_VALIDES = {"CASH", "CREDIT", "MIXTE"}
    mode = data.get("mode_paiement", "CASH").upper()
    if mode not in _MODES_VALIDES:
        raise ValueError(f"mode_paiement invalide : '{mode}'. Valeurs acceptées : {sorted(_MODES_VALIDES)}")

    raw_paye = data.get("montant_paye")
    if raw_paye is None:
        # CASH sans montant explicite → client paie l'intégralité
        # CREDIT → aucun paiement immédiat
        montant_paye = montant_total if mode == "CASH" else Decimal("0")
    else:
        montant_paye = _dec(raw_paye)

    montant_restant = (montant_total - montant_paye).quantize(Decimal("0.01"))
    if montant_restant < 0:
        montant_restant = Decimal("0")

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
            cuisine_plat_id        = l["cuisine_plat_id"],
            quantite               = l["quantite"],
            prix_unitaire_applique = l["prix"],
            sous_total             = l["sous_total"],
        ))
        # Mouvement stock uniquement pour les produits bar (pas les plats cuisine)
        if l["produit_id"]:
            db.add(BarMouvementStock(
                produit_id         = l["produit_id"],
                type_mouvement     = "SORTIE_VENTE",
                quantite           = -l["quantite"],
                motif              = f"Vente ticket {vente.numero_ticket}",
                reference_vente_id = vente.id,
                utilisateur_id     = utilisateur_id,
            ))

    # ── CuisineVente automatique pour les plats cuisine vendus via bar ──
    lignes_cuisine = [l for l in lignes_traitees if l["cuisine_plat_id"]]
    if lignes_cuisine:
        total_cuisine = sum(l["sous_total"] for l in lignes_cuisine)
        cv = CuisineVente(
            numero_ticket = _generer_ticket_cuisine(db),
            total         = total_cuisine,
            mode_paiement = "CASH" if mode in ("CASH", "MIXTE") else "CREDIT",
            client_nom    = data.get("client_nom"),
            notes         = f"Via Bar — {vente.numero_ticket}",
            statut        = "VALIDEE",
        )
        db.add(cv)
        db.flush()
        for l in lignes_cuisine:
            db.add(CuisineLigneVente(
                vente_id      = cv.id,
                plat_id       = l["cuisine_plat_id"],
                nom_plat      = l["produit_nom"],
                quantite      = int(l["quantite"]),
                prix_unitaire = l["prix"],
                sous_total    = l["sous_total"],
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
        if ligne.produit_id:   # plats cuisine n'ont pas de stock bar
            db.add(BarMouvementStock(
                produit_id         = ligne.produit_id,
                type_mouvement     = "ENTREE",
                quantite           = ligne.quantite,
                motif              = f"Annulation vente {vente.numero_ticket}",
                reference_vente_id = vente.id,
                utilisateur_id     = utilisateur_id,
            ))

    # Annuler la CuisineVente liée (si des plats cuisine étaient dans ce ticket)
    cv_ref = f"Via Bar — {vente.numero_ticket}"
    cv = db.query(CuisineVente).filter(CuisineVente.notes == cv_ref).first()
    if cv and cv.statut != "ANNULEE":
        cv.statut = "ANNULEE"

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
        "client_nif":     data.get("client_nif"),
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
    Utilise des requêtes batch pour éviter le N+1.
    """
    anomalies = []

    produits = db.query(BarProduit).filter_by(actif=True).all()
    stocks   = stock_tous_produits(db)
    prix_map = prix_actif_batch(db)

    # ── STOCK_NEGATIF_BAR & VENTE_SANS_PRIX & CONFIG_CAISSE_INVALIDE & DECALAGE ─
    for p in produits:
        stk  = stocks.get(p.id, Decimal("0"))
        prix = prix_map.get(p.id)

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

        if p.seuil_alerte_stock and stk <= _dec(p.seuil_alerte_stock):
            anomalies.append({
                "type":          "DECALAGE_STOCK_BAR",
                "gravite":       "avertissement",
                "produit_id":    p.id,
                "produit_nom":   p.nom,
                "date":          str(date_cible),
                "stock_courant": float(stk),
                "seuil_alerte":  float(p.seuil_alerte_stock),
                "message": (
                    f"Stock bas pour « {p.nom} » : {float(stk):.3f} unités "
                    f"(seuil d'alerte : {float(p.seuil_alerte_stock):.3f})."
                ),
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
            "type":          "CREDIT_EN_RETARD",
            "gravite":       "avertissement",
            "credit_id":     c.id,
            "client_nom":    c.client_nom,
            "date":          str(date_cible),
            "solde":         float(c.solde),
            "date_echeance": str(c.date_echeance),
            "message": (
                f"Crédit en retard pour {c.client_nom} : "
                f"{float(c.solde):.2f} G (échéance : {c.date_echeance})."
            ),
        })
        c.statut = "EN_RETARD"

    if credits_retard:
        db.commit()

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

    cmup_map: dict[int, Decimal] = cmup_batch(db)

    ca_total    = Decimal("0")
    cogs_total  = Decimal("0")
    par_produit: dict = {}

    for l in lignes:
        ca = _dec(l.sous_total)

        if l.produit_id:
            cout_unit  = cmup_map.get(l.produit_id, Decimal("0"))
            cogs_ligne = (_dec(l.quantite) * cout_unit).quantize(Decimal("0.01"))
            key        = l.produit_id
            nom        = l.produit.nom if l.produit else str(l.produit_id)
            cat        = l.produit.categorie if l.produit else ""
        else:
            # Plat cuisine vendu via bar — utilise cout_estime si disponible
            cout_estime = (
                _dec(l.cuisine_plat.cout_estime)
                if l.cuisine_plat and l.cuisine_plat.cout_estime
                else Decimal("0")
            )
            cogs_ligne = (_dec(l.quantite) * cout_estime).quantize(Decimal("0.01"))
            key        = -(l.cuisine_plat_id or 0)
            nom        = l.cuisine_plat.nom if l.cuisine_plat else "Plat inconnu"
            cat        = "cuisine"

        ca_total   += ca
        cogs_total += cogs_ligne

        if key not in par_produit:
            par_produit[key] = {
                "produit_id":  l.produit_id,
                "produit_nom": nom,
                "categorie":   cat,
                "quantite":    Decimal("0"),
                "ca":          Decimal("0"),
                "cogs":        Decimal("0"),
                "benefice":    Decimal("0"),
                "marge_pct":   Decimal("0"),
            }
        par_produit[key]["quantite"] += _dec(l.quantite)
        par_produit[key]["ca"]       += ca
        par_produit[key]["cogs"]     += cogs_ligne

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
