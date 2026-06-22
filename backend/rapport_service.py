"""
Moteur de collecte de données et de génération du narratif pour les rapports complets.

INVARIANT : aucun chiffre n'est inventé ici.
  - Stats  : compute_stats() depuis stats.py
  - Stock  : stock_restant() + rentabilite_globale() depuis stock_service.py
  - Anom.  : anomalies_stock() depuis stock_service.py + détection compteurs locale

Un seul point d'entrée public : build_report_payload(db, date_debut, date_fin, produit_id)
Les rendeurs (PDF/DOCX/XLSX) consomment le dict retourné — aucune logique métier dans les rendeurs.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from io import BytesIO
from typing import Optional

from sqlalchemy.orm import Session

from models import Produit, Releve
from stats import compute_stats
from stock_service import (
    anomalies_stock,
    rentabilite_globale,
    stock_restant,
)

_PERIODE_RANG = {"Matin": 0, "Apres-midi": 1}


# ══════════════════════════════════════════════════════════════════════════════
# 1. COLLECTE DES DONNÉES
# ══════════════════════════════════════════════════════════════════════════════

def _anomalies_compteurs(
    db: Session,
    date_debut: date,
    date_fin: date,
    produit_id: Optional[int],
) -> list[dict]:
    releves = db.query(Releve).filter(
        Releve.date >= date_debut, Releve.date <= date_fin
    ).all()
    if produit_id:
        releves = [r for r in releves if r.pompe.produit_id == produit_id]

    par_pompe: dict[int, list] = {}
    for r in releves:
        par_pompe.setdefault(r.pompe_id, []).append(r)

    anomalies: list[dict] = []
    for rels in par_pompe.values():
        rels_tri = sorted(rels, key=lambda x: (x.date, _PERIODE_RANG.get(x.periode, 9)))
        prev_ap: Optional[float] = None
        for r in rels_tri:
            av, ap = float(r.metter_avant), float(r.metter_apres)
            if av > ap:
                anomalies.append({
                    "type": "QUANTITE_NEGATIVE",
                    "gravite": "erreur",
                    "pompe_nom": r.pompe.nom,
                    "produit_id": r.pompe.produit_id,
                    "date": str(r.date),
                    "periode": r.periode,
                    "message": (
                        f"Meter avant ({av:.0f}) > meter après ({ap:.0f}) "
                        f"— {r.pompe.nom} {r.date} {r.periode}"
                    ),
                })
            elif prev_ap is not None and av < prev_ap - 0.001:
                anomalies.append({
                    "type": "REGRESSION_METER",
                    "gravite": "erreur",
                    "pompe_nom": r.pompe.nom,
                    "produit_id": r.pompe.produit_id,
                    "date": str(r.date),
                    "periode": r.periode,
                    "message": (
                        f"Régression compteur — {r.pompe.nom} : "
                        f"attendu ≥ {prev_ap:.0f}, saisi {av:.0f}"
                    ),
                })
            if av <= ap:
                prev_ap = ap
    return anomalies


def build_report_payload(
    db: Session,
    date_debut: date,
    date_fin: date,
    produit_id: Optional[int] = None,
) -> dict:
    """
    Collecte toutes les données nécessaires au rapport.
    Les rendeurs ne font aucun calcul supplémentaire.
    """
    nb_jours = (date_fin - date_debut).days + 1

    # ── Période courante ──────────────────────────────────────────
    stats = compute_stats(db, date_debut, date_fin, produit_id=produit_id)

    # ── Période précédente (même durée) pour comparaison ─────────
    prev_fin = date_debut - timedelta(days=1)
    prev_debut = prev_fin - timedelta(days=nb_jours - 1)
    stats_prev = compute_stats(db, prev_debut, prev_fin, produit_id=produit_id)

    # ── Série journalière de la période courante ──────────────────
    releves_periode = db.query(Releve).filter(
        Releve.date >= date_debut, Releve.date <= date_fin
    ).all()
    if produit_id:
        releves_periode = [r for r in releves_periode if r.pompe.produit_id == produit_id]

    serie_jours: dict[str, dict] = {}
    for r in releves_periode:
        k = str(r.date)
        if k not in serie_jours:
            serie_jours[k] = {"montant": 0.0, "quantite": 0.0}
        if r.quantite >= 0:
            serie_jours[k]["montant"] += r.montant_vente
            serie_jours[k]["quantite"] += r.quantite
    # Arrondir
    for v in serie_jours.values():
        v["montant"] = round(v["montant"], 2)
        v["quantite"] = round(v["quantite"], 3)

    # ── Rentabilité ───────────────────────────────────────────────
    rentab = rentabilite_globale(db, date_debut, date_fin, produit_id)

    # ── Stocks actuels ────────────────────────────────────────────
    produits = db.query(Produit).filter(Produit.actif == True).all()
    if produit_id:
        produits = [p for p in produits if p.id == produit_id]

    stocks = []
    for p in produits:
        s = stock_restant(db, p.id)
        s["produit_nom"] = p.nom
        stocks.append(s)

    # ── Anomalies ─────────────────────────────────────────────────
    anom_cpt = _anomalies_compteurs(db, date_debut, date_fin, produit_id)
    anom_stk = anomalies_stock(db, date_fin)
    if produit_id:
        anom_stk = [a for a in anom_stk if a.get("produit_id") == produit_id]

    return {
        "station_nom": "Station Carburant",
        "date_debut": str(date_debut),
        "date_fin": str(date_fin),
        "date_generation": str(date.today()),
        "nb_jours": nb_jours,
        "stats": stats,
        "stats_prev": stats_prev,
        "stats_prev_debut": str(prev_debut),
        "stats_prev_fin": str(prev_fin),
        "serie_jours": serie_jours,
        "rentab": rentab,
        "stocks": stocks,
        "anomalies": anom_cpt + anom_stk,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 2. NARRATIF DYNAMIQUE
# ══════════════════════════════════════════════════════════════════════════════

def build_narrative(payload: dict) -> dict:
    """
    Génère le texte dynamique des sections du rapport à partir des données réelles.
    Ne produit aucun chiffre qui ne soit dans payload.
    """
    stats = payload["stats"]
    stats_prev = payload["stats_prev"]
    rentab = payload["rentab"]
    stocks = payload["stocks"]
    anomalies = payload["anomalies"]
    total_rentab = rentab.get("total", {})

    # Variation vs période précédente
    var_pct: Optional[float] = None
    if stats_prev["total_montant"] > 0 and stats["nb_releves"] > 0:
        var_pct = round(
            (stats["total_montant"] - stats_prev["total_montant"])
            / stats_prev["total_montant"] * 100, 1
        )

    # ── Résumé exécutif ────────────────────────────────────────────
    if stats["nb_releves"] == 0:
        intro_kpis = (
            f"Aucun relevé de vente enregistré entre le {payload['date_debut']} "
            f"et le {payload['date_fin']}."
        )
    else:
        intro_kpis = (
            f"Sur la période du {payload['date_debut']} au {payload['date_fin']} "
            f"({payload['nb_jours']} jours), la station a enregistré "
            f"{stats['nb_releves']} relevé(s) de compteur couvrant "
            f"{stats['nb_jours_couverts']} jour(s) actif(s). "
            f"Volume total écoulé : {stats['total_quantite']:,.3f} gallons — "
            f"Revenu total : {stats['total_montant']:,.2f} gourdes."
        )
        if var_pct is not None:
            sens = "progressé" if var_pct >= 0 else "reculé"
            intro_kpis += (
                f" Les ventes ont {sens} de {abs(var_pct):.1f} % par rapport "
                f"à la période précédente "
                f"({payload['stats_prev_debut']} — {payload['stats_prev_fin']}, "
                f"{stats_prev['total_montant']:,.2f} G)."
            )

    # ── Analyse des ventes par produit ────────────────────────────
    if not stats["par_produit"]:
        ventes_text = "Aucune donnée de vente sur cette période."
    else:
        lignes = []
        for nom, d in sorted(stats["par_produit"].items(),
                              key=lambda x: x[1]["montant"], reverse=True):
            pct = (round(d["montant"] / stats["total_montant"] * 100, 1)
                   if stats["total_montant"] > 0 else 0.0)
            lignes.append(
                f"{nom} : {d['quantite']:,.3f} gal — {d['montant']:,.2f} G ({pct:.1f} %)"
            )
        ventes_text = "Répartition par produit : " + " | ".join(lignes) + "."

    # ── Analyse par pompe ─────────────────────────────────────────
    if not stats["par_pompe"]:
        pompes_text = "Aucune donnée par pompe."
    else:
        top = max(stats["par_pompe"].items(), key=lambda x: x[1]["montant"])
        pompes_text = (
            f"Pompe la plus productive : {top[0]} "
            f"({top[1]['quantite']:,.3f} gal — {top[1]['montant']:,.2f} G)."
        )
        if len(stats["par_pompe"]) > 1:
            moy = stats["total_montant"] / len(stats["par_pompe"])
            ecarts = [
                n for n, d in stats["par_pompe"].items()
                if d["montant"] < moy * 0.5
            ]
            if ecarts:
                pompes_text += (
                    f" Pompe(s) sous-performante(s) (<50 % de la moyenne) : "
                    + ", ".join(ecarts) + "."
                )

    # ── Analyse par période ───────────────────────────────────────
    par_per = stats.get("par_periode", {})
    matin = par_per.get("Matin", {})
    aprem = par_per.get("Apres-midi", {})
    if matin.get("montant", 0) > 0 or aprem.get("montant", 0) > 0:
        periode_text = (
            f"Matin : {matin.get('quantite', 0):,.3f} gal — {matin.get('montant', 0):,.2f} G | "
            f"Après-midi : {aprem.get('quantite', 0):,.3f} gal — {aprem.get('montant', 0):,.2f} G."
        )
        if matin.get("montant", 0) > aprem.get("montant", 0) * 1.2:
            periode_text += " L'activité du matin est significativement plus élevée."
        elif aprem.get("montant", 0) > matin.get("montant", 0) * 1.2:
            periode_text += " L'activité de l'après-midi est significativement plus élevée."
    else:
        periode_text = "Aucune donnée par période."

    # ── Analyse des anomalies ─────────────────────────────────────
    erreurs = [a for a in anomalies
               if a.get("gravite") == "erreur"
               or a.get("type") in ("QUANTITE_NEGATIVE", "REGRESSION_METER",
                                    "STOCK_NEGATIF", "VENTE_SANS_STOCK")]
    alertes_anom = [a for a in anomalies if a not in erreurs]

    if not anomalies:
        anom_text = (
            "Aucune anomalie détectée sur la période. "
            "La qualité des données est satisfaisante."
        )
    else:
        anom_text = (
            f"{len(anomalies)} anomalie(s) détectée(s) : "
            f"{len(erreurs)} erreur(s) critique(s) et "
            f"{len(alertes_anom)} avertissement(s)."
        )
        if erreurs:
            anom_text += (
                " Les erreurs affectent la fiabilité des chiffres et "
                "doivent être corrigées en priorité."
            )

    # ── Stock & Rentabilité ───────────────────────────────────────
    alertes_stock = [s for s in stocks if s.get("alerte_bas")]
    if alertes_stock:
        details = []
        for s in alertes_stock:
            j = s.get("jours_de_stock")
            details.append(
                s["produit_nom"] + (f" ({j:.1f} j restants)" if j is not None else "")
            )
        stock_text = (
            f"ALERTE : {len(alertes_stock)} produit(s) en stock bas : "
            + ", ".join(details) + ". Réapprovisionnement urgent recommandé."
        )
    else:
        stock_text = "Les niveaux de stock sont satisfaisants sur tous les produits."

    if total_rentab.get("fiable") and total_rentab.get("benefice") is not None:
        marge = total_rentab.get("marge_pct") or 0.0
        benef = total_rentab["benefice"]
        rentab_text = (
            f"Marge brute : {marge:.2f} % — Bénéfice : {benef:,.2f} G "
            f"(sur un revenu de {total_rentab.get('revenu_total', 0):,.2f} G, "
            f"COGS WAC : {total_rentab.get('cogs_total', 0):,.2f} G)."
        )
        if marge < 5:
            rentab_text += " ⚠ Marge très faible — revoir prix de vente ou coûts d'achat."
        elif marge > 20:
            rentab_text += " Marge solide sur cette période."
    else:
        rentab_text = (
            "Rentabilité non calculable : données de livraison ou "
            "prix d'achat manquants. Enregistrer les livraisons pour activer ce calcul."
        )

    # ── Conclusion ────────────────────────────────────────────────
    if stats["nb_releves"] == 0:
        conclusion = (
            "Aucune donnée disponible sur cette période. "
            "Vérifier la saisie des relevés."
        )
    else:
        forts = []
        vigilance = []

        if var_pct is not None and var_pct >= 0:
            forts.append(f"Croissance {var_pct:+.1f} % vs période précédente")
        if not alertes_stock:
            forts.append("Stock satisfaisant sur tous les produits")
        if not anomalies:
            forts.append("Aucune anomalie compteur")
        if total_rentab.get("fiable") and (total_rentab.get("marge_pct") or 0) > 10:
            forts.append(f"Marge brute saine ({total_rentab['marge_pct']:.1f} %)")

        if var_pct is not None and var_pct < -10:
            vigilance.append(
                f"Baisse des ventes de {abs(var_pct):.1f} % — investiguer la cause"
            )
        if alertes_stock:
            vigilance.append(f"{len(alertes_stock)} produit(s) en rupture imminente")
        if erreurs:
            vigilance.append(f"{len(erreurs)} erreur(s) de saisie à corriger")
        if not total_rentab.get("fiable"):
            vigilance.append("Rentabilité indisponible — livraisons à enregistrer")

        parts = []
        if forts:
            parts.append("Points forts : " + " | ".join(forts) + ".")
        if vigilance:
            parts.append("Points de vigilance : " + " | ".join(vigilance) + ".")
        conclusion = " ".join(parts) or "Activité normale sur la période."

    # ── Recommandations ───────────────────────────────────────────
    recommandations: list[str] = []
    for a in anomalies[:5]:
        msg = a.get("message", "")
        if msg:
            recommandations.append(msg[:160])

    for s in alertes_stock:
        j = s.get("jours_de_stock")
        recommandations.append(
            f"Réapprovisionner {s['produit_nom']} dès que possible"
            + (f" (stock estimé à {j:.1f} jour(s))" if j is not None else "") + "."
        )

    if not total_rentab.get("fiable"):
        recommandations.append(
            "Enregistrer toutes les livraisons avec prix d'achat pour activer "
            "le calcul de rentabilité."
        )

    if not recommandations:
        recommandations.append(
            "Maintenir la saisie régulière des relevés pour garantir la fiabilité des données."
        )

    return {
        "intro_kpis": intro_kpis,
        "ventes_text": ventes_text,
        "pompes_text": pompes_text,
        "periode_text": periode_text,
        "anom_text": anom_text,
        "stock_text": stock_text,
        "rentab_text": rentab_text,
        "conclusion": conclusion,
        "recommandations": recommandations,
        "var_pct": var_pct,
        "nb_erreurs": len(erreurs),
        "nb_alertes_anom": len(alertes_anom),
    }


# ══════════════════════════════════════════════════════════════════════════════
# 3. GRAPHIQUES MATPLOTLIB
# ══════════════════════════════════════════════════════════════════════════════

def build_charts(payload: dict) -> dict[str, bytes]:
    """
    Génère les graphiques en mémoire (PNG bytes) depuis les données du payload.
    Retourne un dict {clé: bytes_png}.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker

    charts: dict[str, bytes] = {}

    def _save(fig) -> bytes:
        buf = BytesIO()
        fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
        buf.seek(0)
        data = buf.read()
        plt.close(fig)
        return data

    # ── 1. Évolution journalière ──────────────────────────────────
    serie = payload["serie_jours"]
    if serie:
        dates_tri = sorted(serie.keys())
        montants = [serie[d]["montant"] for d in dates_tri]
        labels = [d[5:] for d in dates_tri]  # MM-DD

        fig, ax = plt.subplots(figsize=(10, 3.5))
        ax.bar(range(len(dates_tri)), montants, color="#f7a93b", alpha=0.88, edgecolor="#c4821e", linewidth=0.6)
        ax.set_xticks(range(len(dates_tri)))
        ax.set_xticklabels(labels, rotation=55, ha="right", fontsize=7)
        ax.set_ylabel("Ventes (G)", fontsize=8)
        ax.set_title("Évolution journalière des ventes", fontsize=10, fontweight="bold", pad=8)
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.set_facecolor("#fafafa")
        fig.patch.set_facecolor("white")
        fig.tight_layout()
        charts["ventes_jours"] = _save(fig)

    # ── 2. Répartition par produit ────────────────────────────────
    par_produit = payload["stats"]["par_produit"]
    if par_produit and len(par_produit) > 0:
        noms = list(par_produit.keys())
        monts = [par_produit[n]["montant"] for n in noms]
        palette = ["#f7a93b", "#3fb6a8", "#6366f1", "#ec4899", "#84cc16"]

        fig, ax = plt.subplots(figsize=(5, 4))
        wedges, texts, autos = ax.pie(
            monts, labels=noms,
            colors=palette[:len(noms)],
            autopct=lambda p: f"{p:.1f}%" if p > 3 else "",
            startangle=90, pctdistance=0.78,
        )
        for at in autos:
            at.set_fontsize(8)
        ax.set_title("Revenu par produit", fontsize=10, fontweight="bold", pad=10)
        fig.tight_layout()
        charts["par_produit"] = _save(fig)

    # ── 3. Ventes par pompe (barres horizontales) ─────────────────
    par_pompe = payload["stats"]["par_pompe"]
    if par_pompe:
        pompes = list(par_pompe.keys())
        monts_p = [par_pompe[p]["montant"] for p in pompes]

        fig, ax = plt.subplots(figsize=(8, max(2.8, len(pompes) * 0.55 + 1)))
        ax.barh(pompes, monts_p, color="#3fb6a8", alpha=0.88, edgecolor="#2a8a7e", linewidth=0.6)
        ax.set_xlabel("Ventes (G)", fontsize=8)
        ax.set_title("Ventes par pompe", fontsize=10, fontweight="bold", pad=8)
        ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.set_facecolor("#fafafa")
        fig.patch.set_facecolor("white")
        fig.tight_layout()
        charts["par_pompe"] = _save(fig)

    # ── 4. Anomalies par type ─────────────────────────────────────
    anomalies = payload.get("anomalies", [])
    if anomalies:
        cpt = Counter(a.get("type", "Autre") for a in anomalies)
        types_a = list(cpt.keys())
        counts_a = [cpt[t] for t in types_a]
        colors_a = [
            "#ef4444" if a.get("gravite") == "erreur" else "#f97316"
            for a in [next(x for x in anomalies if x.get("type") == t) for t in types_a]
        ]

        fig, ax = plt.subplots(figsize=(7, max(2.5, len(types_a) * 0.55 + 1)))
        ax.barh(types_a, counts_a, color=colors_a, alpha=0.88)
        ax.set_xlabel("Nombre", fontsize=8)
        ax.set_title("Anomalies par type", fontsize=10, fontweight="bold", pad=8)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.set_facecolor("#fafafa")
        for i, v in enumerate(counts_a):
            ax.text(v + 0.05, i, str(v), va="center", fontsize=8)
        fig.patch.set_facecolor("white")
        fig.tight_layout()
        charts["anomalies"] = _save(fig)

    return charts
