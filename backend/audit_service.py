"""
Module d'audit IA — PétroSync v2

Métriques calculées et passées à l'IA :
  Opérationnelles : G/jour, gal/jour, G/relevé, prix de vente moyen (G/gal)
  Comptabilité    : CMP/gal par produit, marge unitaire, marge/gallon vendu
  Répartition     : parts produit (%), parts pompe (%), ratio Matin/Après-midi
  Pareto          : top-3 jours = X% du revenu, top-1 jour = X%
  Variabilité     : écart-type, coefficient de variation (risque régularité)
  Stock           : gallons restants, autonomie (jours), valeur stock (G)
  Contrôle        : taux d'erreur (%), taux d'anomalie (%), couverture (%)
  Évolution       : Δ revenu, Δ volume, Δ relevés vs période précédente
"""
from __future__ import annotations

import os
import time
import statistics as _stats
from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from rapport_service import build_report_payload
from stock_service import cout_moyen_pondere
from models import Produit

GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.0-flash-001"]
CLAUDE_MODEL  = "claude-sonnet-4-6"
MAX_TOKENS    = 8192


# ══════════════════════════════════════════════════════════════════════════════
# Formatage
# ══════════════════════════════════════════════════════════════════════════════

def _g(val) -> str:
    if val is None: return "N/D"
    return f"{val:,.0f} G".replace(",", " ")

def _g2(val) -> str:
    if val is None: return "N/D"
    return f"{val:,.2f} G".replace(",", " ")

def _gal(val) -> str:
    if val is None: return "N/D"
    return f"{val:,.3f} gal".replace(",", " ")

def _pct(val) -> str:
    if val is None: return "N/D"
    return f"{val:.1f}%"

def _sign_pct(val) -> str:
    if val is None: return "N/D"
    sign = "+" if val >= 0 else ""
    return f"{sign}{val:.1f}%"

def _sign_g(val) -> str:
    if val is None: return "N/D"
    sign = "+" if val >= 0 else ""
    return f"{sign}{val:,.0f} G".replace(",", " ")


# ══════════════════════════════════════════════════════════════════════════════
# Calcul des métriques enrichies
# ══════════════════════════════════════════════════════════════════════════════

def _compute_metrics(payload: dict, db: Session, date_debut: date, date_fin: date) -> dict:
    stats       = payload["stats"]
    rentab      = payload.get("rentab")
    stocks      = payload.get("stocks", [])
    anomalies   = payload.get("anomalies", [])
    serie_jours = payload.get("serie_jours", {})
    nb_jours    = payload.get("nb_jours", (date_fin - date_debut).days + 1)
    prev_stats  = payload.get("prev_stats") or {}

    total_montant  = float(stats.get("total_montant",  0) or 0)
    total_quantite = float(stats.get("total_quantite", 0) or 0)
    nb_releves     = int(stats.get("nb_releves",       0) or 0)
    nb_couverts    = int(stats.get("nb_jours_couverts",0) or 0)

    m: dict = {}

    # ── 1. Moyennes opérationnelles ──────────────────────────────────────────
    m["revenu_par_jour"]    = total_montant  / nb_jours    if nb_jours > 0    else None
    m["volume_par_jour"]    = total_quantite / nb_jours    if nb_jours > 0    else None
    m["revenu_par_releve"]  = total_montant  / nb_releves  if nb_releves > 0  else None
    m["volume_par_releve"]  = total_quantite / nb_releves  if nb_releves > 0  else None
    m["taux_couverture"]    = (nb_couverts / nb_jours * 100) if nb_jours > 0  else None
    m["releves_par_jour"]   = (nb_releves / nb_couverts)      if nb_couverts > 0 else None
    m["prix_vente_moyen"]   = total_montant / total_quantite  if total_quantite > 0 else None

    # ── 2. CMP et marge unitaire par produit ─────────────────────────────────
    m["cmp_par_produit"]        = {}
    m["marge_unit_par_produit"] = {}
    for p in db.query(Produit).filter(Produit.actif == True).all():
        cmp = cout_moyen_pondere(db, p.id)
        m["cmp_par_produit"][p.nom] = cmp
        if cmp and m["prix_vente_moyen"]:
            m["marge_unit_par_produit"][p.nom] = m["prix_vente_moyen"] - cmp

    # ── 3. Parts de marché produit et pompe ──────────────────────────────────
    m["parts_produit"] = {}
    for nom, p in stats.get("par_produit", {}).items():
        mont = float(p.get("montant", 0) or 0)
        m["parts_produit"][nom] = (mont / total_montant * 100) if total_montant > 0 else 0.0

    m["parts_pompe"] = {}
    for nom, p in stats.get("par_pompe", {}).items():
        mont = float(p.get("montant", 0) or 0)
        m["parts_pompe"][nom] = (mont / total_montant * 100) if total_montant > 0 else 0.0

    # ── 4. Matin vs Après-midi ───────────────────────────────────────────────
    par_per = stats.get("par_periode", {})
    matin = float(par_per.get("Matin",      {}).get("montant", 0) or 0)
    aprem = float(par_per.get("Apres-midi", {}).get("montant", 0) or 0)
    vol_matin = float(par_per.get("Matin",      {}).get("quantite", 0) or 0)
    vol_aprem = float(par_per.get("Apres-midi", {}).get("quantite", 0) or 0)
    total_per = matin + aprem
    m["revenu_matin"] = matin
    m["revenu_aprem"] = aprem
    m["vol_matin"]    = vol_matin
    m["vol_aprem"]    = vol_aprem
    m["pct_matin"]    = (matin / total_per * 100) if total_per > 0 else None
    m["pct_aprem"]    = (aprem / total_per * 100) if total_per > 0 else None

    # ── 5. Variabilité journalière ───────────────────────────────────────────
    jv = [(d, float(v.get("montant", 0))) for d, v in serie_jours.items()
          if float(v.get("montant", 0)) > 0]
    if jv:
        m["jour_max"]   = max(jv, key=lambda x: x[1])
        m["jour_min"]   = min(jv, key=lambda x: x[1])
        m["ecart_maxmin"] = m["jour_max"][1] - m["jour_min"][1]
        vals = [x[1] for x in jv]
        if len(vals) >= 2:
            m["ecart_type"]     = _stats.stdev(vals)
            moy                 = _stats.mean(vals)
            m["coeff_variation"] = (m["ecart_type"] / moy * 100) if moy > 0 else None
        else:
            m["ecart_type"] = m["coeff_variation"] = None
    else:
        m["jour_max"] = m["jour_min"] = m["ecart_maxmin"] = None
        m["ecart_type"] = m["coeff_variation"] = None

    # ── 6. Concentration Pareto ──────────────────────────────────────────────
    sorted_jours = sorted(
        serie_jours.items(),
        key=lambda x: float(x[1].get("montant", 0)),
        reverse=True,
    )
    top3 = sorted_jours[:3]
    top3_total = sum(float(v.get("montant", 0)) for _, v in top3)
    m["top3_jours"] = [(d, float(v.get("montant", 0)), float(v.get("quantite", 0)))
                       for d, v in top3]
    m["top3_pct"] = (top3_total / total_montant * 100) if total_montant > 0 else None
    m["top1_pct"] = (m["top3_jours"][0][1] / total_montant * 100) if (m["top3_jours"] and total_montant > 0) else None

    # ── 7. Stock enrichi ─────────────────────────────────────────────────────
    m["stocks_enrichis"] = []
    valeur_stock_totale  = 0.0
    for s in stocks:
        se       = dict(s)
        nom      = se.get("produit_nom", "")
        cmp      = m["cmp_par_produit"].get(nom)
        restant  = float(se.get("gallons_restants", 0))
        livre    = float(se.get("gallons_livres",   0))
        vendu    = float(se.get("gallons_vendus",   0))
        moy_j    = float(se.get("moyenne_jour",     0) or 0)
        se["cout_moyen_pondere"] = cmp
        se["valeur_stock"]       = (restant * cmp) if (cmp and restant > 0) else None
        se["taux_ecoulement"]    = (vendu / livre * 100) if livre > 0 else None
        if se["valeur_stock"]:
            valeur_stock_totale += se["valeur_stock"]
        m["stocks_enrichis"].append(se)
    m["valeur_stock_totale"] = valeur_stock_totale if valeur_stock_totale > 0 else None

    # ── 8. Contrôle qualité ──────────────────────────────────────────────────
    anom_err  = [a for a in anomalies if a.get("type") == "ERREUR"]
    anom_ale  = [a for a in anomalies if a.get("type") == "ALERTE"]
    m["nb_erreurs"]  = len(anom_err)
    m["nb_alertes"]  = len(anom_ale)
    m["taux_erreur"] = (len(anom_err)   / nb_releves * 100) if nb_releves > 0 else None
    m["taux_anom"]   = (len(anomalies)  / nb_releves * 100) if nb_releves > 0 else None

    # ── 9. Rentabilité dérivée ───────────────────────────────────────────────
    if rentab and rentab.get("fiable"):
        revenu = float(rentab.get("revenu_total",  0) or 0)
        cogs   = float(rentab.get("cogs_total",    0) or 0)
        benef  = float(rentab.get("benefice_brut", 0) or 0)
        m["revenu_cogs_ratio"] = (revenu / cogs)          if cogs > 0           else None
        m["cogs_par_gallon"]   = (cogs   / total_quantite) if total_quantite > 0 else None
        m["benef_par_gallon"]  = (benef  / total_quantite) if total_quantite > 0 else None
        m["benef_par_jour"]    = (benef  / nb_jours)       if nb_jours > 0       else None
        m["point_mort_jour"]   = (cogs   / nb_jours)       if nb_jours > 0       else None
    else:
        m["revenu_cogs_ratio"] = m["cogs_par_gallon"] = None
        m["benef_par_gallon"]  = m["benef_par_jour"]  = m["point_mort_jour"] = None

    # ── 10. Variation vs période précédente ──────────────────────────────────
    prev_mont  = float(prev_stats.get("total_montant",  0) or 0)
    prev_quant = float(prev_stats.get("total_quantite", 0) or 0)
    prev_nbr   = int(  prev_stats.get("nb_releves",     0) or 0)
    m["prev_montant"]    = prev_mont
    m["prev_quantite"]   = prev_quant
    m["prev_nb_releves"] = prev_nbr
    m["var_revenu_pct"]  = ((total_montant  - prev_mont) / prev_mont  * 100) if prev_mont  > 0 else None
    m["var_volume_pct"]  = ((total_quantite - prev_quant)/ prev_quant * 100) if prev_quant > 0 else None
    m["var_releves_pct"] = ((nb_releves     - prev_nbr)  / prev_nbr   * 100) if prev_nbr   > 0 else None
    m["delta_revenu"]    = (total_montant - prev_mont)    if prev_mont  > 0 else None
    m["delta_volume"]    = (total_quantite - prev_quant)  if prev_quant > 0 else None

    return m


# ══════════════════════════════════════════════════════════════════════════════
# Construction du prompt
# ══════════════════════════════════════════════════════════════════════════════

def build_audit_prompt(payload: dict, metrics: dict, date_debut: date, date_fin: date) -> str:
    stats       = payload["stats"]
    rentab      = payload.get("rentab")
    anomalies   = payload.get("anomalies", [])
    nb_jours    = payload.get("nb_jours", (date_fin - date_debut).days + 1)
    m           = metrics

    total_montant  = float(stats.get("total_montant",  0) or 0)
    total_quantite = float(stats.get("total_quantite", 0) or 0)
    nb_releves     = int(stats.get("nb_releves",       0) or 0)
    nb_couverts    = int(stats.get("nb_jours_couverts",0) or 0)

    # ── Bloc 1 : KPIs globaux ────────────────────────────────────────────────
    b_global = f"""\
━━━ VENTES GLOBALES ━━━
  • Volume vendu total        : {_gal(total_quantite)}
  • Revenu brut total         : {_g(total_montant)}
  • Nombre de relevés         : {nb_releves}
  • Jours avec données        : {nb_couverts} / {nb_jours}  ({_pct(m['taux_couverture'])} de couverture)

━━━ MOYENNES OPÉRATIONNELLES ━━━
  • Revenu moyen / jour       : {_g(m['revenu_par_jour'])}
  • Volume moyen / jour       : {_gal(m['volume_par_jour'])}
  • Revenu moyen / relevé     : {_g(m['revenu_par_releve'])}
  • Volume moyen / relevé     : {_gal(m['volume_par_releve'])}
  • Relevés / jour couvert    : {f"{m['releves_par_jour']:.1f}" if m['releves_par_jour'] else 'N/D'}
  • Prix de vente moyen       : {_g2(m['prix_vente_moyen'])} / gal"""

    # ── Bloc 2 : Répartition produit ─────────────────────────────────────────
    lignes_produit = ""
    for nom, p in stats.get("par_produit", {}).items():
        part = m["parts_produit"].get(nom, 0)
        cmp  = m["cmp_par_produit"].get(nom)
        mu   = m["marge_unit_par_produit"].get(nom)
        lignes_produit += (
            f"  • {nom} : {_gal(p.get('quantite', 0))} vendus — "
            f"revenu {_g(p.get('montant', 0))} ({_pct(part)} du total)"
        )
        if cmp:
            lignes_produit += f" | CMP {_g2(cmp)}/gal"
        if mu:
            lignes_produit += f" | marge unitaire {_g2(mu)}/gal"
        lignes_produit += "\n"
    if not lignes_produit:
        lignes_produit = "  Aucun détail par produit disponible.\n"

    # ── Bloc 3 : Répartition pompe ───────────────────────────────────────────
    lignes_pompe = ""
    for nom, p in stats.get("par_pompe", {}).items():
        part = m["parts_pompe"].get(nom, 0)
        lignes_pompe += (
            f"  • Pompe {nom} : {_gal(p.get('quantite', 0))} — "
            f"revenu {_g(p.get('montant', 0))} ({_pct(part)} du total)\n"
        )
    if not lignes_pompe:
        lignes_pompe = "  Aucune pompe enregistrée.\n"

    # ── Bloc 4 : Matin / Après-midi ──────────────────────────────────────────
    b_periode = f"""\
━━━ MATIN vs APRÈS-MIDI ━━━
  • Matin      : {_g(m['revenu_matin'])} ({_pct(m['pct_matin'])})  — {_gal(m['vol_matin'])}
  • Après-midi : {_g(m['revenu_aprem'])} ({_pct(m['pct_aprem'])})  — {_gal(m['vol_aprem'])}"""

    # ── Bloc 5 : Variabilité et Pareto ───────────────────────────────────────
    jmax = m["jour_max"]
    jmin = m["jour_min"]
    b_var = f"""\
━━━ VARIABILITÉ JOURNALIÈRE ━━━
  • Meilleure journée  : {jmax[0] if jmax else 'N/D'} → {_g(jmax[1]) if jmax else 'N/D'}
  • Journée la plus faible : {jmin[0] if jmin else 'N/D'} → {_g(jmin[1]) if jmin else 'N/D'}
  • Écart max-min       : {_g(m['ecart_maxmin'])}
  • Écart-type (σ)      : {_g(m['ecart_type'])}
  • Coefficient de variation : {_pct(m['coeff_variation'])} (plus haut = activité plus irrégulière)

━━━ CONCENTRATION (PARETO) ━━━
  • Top 1 jour = {_pct(m['top1_pct'])} du revenu total
  • Top 3 jours = {_pct(m['top3_pct'])} du revenu total"""
    for d, mont, vol in (m["top3_jours"] or []):
        b_var += f"\n    – {d} : {_g(mont)} ({_gal(vol)})"

    # ── Bloc 6 : Évolution ───────────────────────────────────────────────────
    if m["prev_montant"] > 0:
        b_evol = f"""\
━━━ ÉVOLUTION VS PÉRIODE PRÉCÉDENTE (mêmes {nb_jours} jours) ━━━
  • Revenu précédent    : {_g(m['prev_montant'])}
  • Revenu actuel       : {_g(total_montant)}
  • Variation revenu    : {_sign_pct(m['var_revenu_pct'])}  ({_sign_g(m['delta_revenu'])})
  • Variation volume    : {_sign_pct(m['var_volume_pct'])}  ({_sign_g(m['delta_volume']) if m['delta_volume'] is not None else 'N/D'})
  • Variation relevés   : {_sign_pct(m['var_releves_pct'])}  ({m['prev_nb_releves']} → {nb_releves})"""
    else:
        b_evol = "━━━ ÉVOLUTION ━━━\n  Aucune donnée sur la période précédente."

    # ── Bloc 7 : Rentabilité ─────────────────────────────────────────────────
    if rentab and rentab.get("fiable"):
        b_rentab = f"""\
━━━ RENTABILITÉ (Méthode CMP / WAC) ━━━
  • Revenu total brut   : {_g(rentab.get('revenu_total'))}
  • COGS total (coût)   : {_g(rentab.get('cogs_total'))}
  • Bénéfice brut       : {_g(rentab.get('benefice_brut'))}
  • Marge brute         : {_pct(rentab.get('marge_pct'))}
  • Ratio Revenu/COGS   : {f"{m['revenu_cogs_ratio']:.3f}" if m['revenu_cogs_ratio'] else 'N/D'}  (>1 = rentable)
  • COGS / gallon       : {_g2(m['cogs_par_gallon'])} / gal
  • Bénéfice / gallon   : {_g2(m['benef_par_gallon'])} / gal
  • Bénéfice / jour     : {_g(m['benef_par_jour'])}
  • Point mort / jour   : {_g(m['point_mort_jour'])}  (revenu min. à couvrir)"""
    elif rentab and not rentab.get("fiable"):
        b_rentab = "━━━ RENTABILITÉ ━━━\n  Calcul approximatif — données de livraison incomplètes (COGS non fiable)."
    else:
        b_rentab = "━━━ RENTABILITÉ ━━━\n  Non calculable — aucune livraison enregistrée (COGS absent).\n  ACTION REQUISE : saisir les livraisons pour activer cette analyse."

    # ── Bloc 8 : Stock ───────────────────────────────────────────────────────
    lignes_stock = ""
    for s in m["stocks_enrichis"]:
        alerte = " ⚠ ALERTE STOCK BAS" if s.get("alerte_bas") else ""
        jours_de_stock = s.get("jours_de_stock")
        aut = (f"{jours_de_stock:.0f} jours" if jours_de_stock else "N/D")
        lignes_stock += f"  • {s.get('produit_nom', '')} :\n"
        lignes_stock += f"      Restant         : {_gal(s.get('gallons_restants', 0))}{alerte}\n"
        lignes_stock += f"      Autonomie       : {aut}\n"
        lignes_stock += f"      Total livré     : {_gal(s.get('gallons_livres', 0))}\n"
        lignes_stock += f"      Total vendu     : {_gal(s.get('gallons_vendus', 0))}\n"
        if s.get("cout_moyen_pondere"):
            lignes_stock += f"      CMP             : {_g2(s['cout_moyen_pondere'])} / gal\n"
        if s.get("valeur_stock"):
            lignes_stock += f"      Valeur du stock : {_g(s['valeur_stock'])}\n"
        if s.get("taux_ecoulement"):
            lignes_stock += f"      Taux d'écoulement : {_pct(s['taux_ecoulement'])}\n"
    if not lignes_stock:
        lignes_stock = "  Aucun stock enregistré.\n"
    if m.get("valeur_stock_totale"):
        lignes_stock += f"\n  Valeur totale du stock immobilisé : {_g(m['valeur_stock_totale'])}"

    # ── Bloc 9 : Anomalies ───────────────────────────────────────────────────
    lignes_anom = ""
    for a in anomalies[:15]:
        lignes_anom += f"  • [{a.get('type', '')}] {a.get('message', '')} ({a.get('date', '')})\n"
    if not lignes_anom:
        lignes_anom = "  Aucune anomalie détectée.\n"

    b_anom = f"""\
━━━ CONTRÔLE INTERNE ET ANOMALIES ━━━
  • Total anomalies    : {len(anomalies)}  (Erreurs : {m['nb_erreurs']}, Alertes : {m['nb_alertes']})
  • Taux d'erreur      : {_pct(m['taux_erreur'])} des relevés
  • Taux global anom.  : {_pct(m['taux_anom'])} des relevés
{lignes_anom}"""

    # ══════════════════════════════════════════════════════════════════════════
    # Prompt complet
    # ══════════════════════════════════════════════════════════════════════════

    prompt = f"""Tu es un EXPERT-COMPTABLE et AUDITEUR FINANCIER SENIOR, spécialiste des stations-service et du secteur pétrolier haïtien.

Ta mission : rédiger un **RAPPORT D'AUDIT PROFESSIONNEL COMPLET** destiné à la direction générale.

RÈGLES ABSOLUES :
① Zéro chiffre inventé — utilise UNIQUEMENT les données fournies ci-dessous.
② Met en **gras** TOUS les chiffres clés, montants, pourcentages et mots-clés comptables.
③ Sois analytique : explique le POURQUOI derrière chaque chiffre.
④ Cite les ratios et métriques calculés dans ton analyse.
⑤ Chaque section doit être substantielle (5-10 phrases minimum).

═══════════════════════════════════════════════════════════════════════
DONNÉES FINANCIÈRES ET OPÉRATIONNELLES COMPLÈTES
═══════════════════════════════════════════════════════════════════════
Période      : {date_debut.strftime("%d/%m/%Y")} — {date_fin.strftime("%d/%m/%Y")} ({nb_jours} jours calendaires)
Date d'audit : {date.today().strftime("%d/%m/%Y")}
Monnaie      : Gourde haïtienne (G) | Volumes : gallons (gal)

{b_global}

━━━ RÉPARTITION PAR PRODUIT ━━━
{lignes_produit}
━━━ RÉPARTITION PAR POMPE ━━━
{lignes_pompe}
{b_periode}

{b_var}

{b_evol}

{b_rentab}

━━━ STOCK ET APPROVISIONNEMENT ━━━
{lignes_stock}
{b_anom}
═══════════════════════════════════════════════════════════════════════

═══════════════════════════════════════════════════════════════════════
STRUCTURE REQUISE — RESPECTE CES TITRES EXACTEMENT
═══════════════════════════════════════════════════════════════════════

# RAPPORT D'AUDIT — STATION CARBURANT PÉTROSYNC
## Période : {date_debut.strftime("%d/%m/%Y")} – {date_fin.strftime("%d/%m/%Y")}

### 1. INTRODUCTION ET MANDAT D'AUDIT
[Mission, périmètre, sources de données utilisées, méthode d'analyse. Cite la période exacte et les indicateurs couverts. Mentionne le **prix de vente moyen** et le **CMP** comme socle de l'analyse.]

### 2. ANALYSE COMMERCIALE

#### 2.1 Performance globale des ventes
[Commente **revenu total**, **volume vendu**, **revenu/jour** et **taux de couverture**. Interprète le niveau d'activité. Si taux de couverture < 90%, analyse les jours manquants.]

#### 2.2 Répartition par produit — parts de marché
[Cite les **parts de marché %** de chaque produit. Identifie le produit dominant, la marge unitaire de chacun (G/gal), et les implications pour la politique tarifaire.]

#### 2.3 Répartition par pompe — efficacité opérationnelle
[Compare les **parts de chaque pompe (%)** et les écarts de performance. Un écart significatif entre pompes peut indiquer une panne, une préférence client ou une anomalie technique.]

#### 2.4 Profil temporel : Matin vs Après-midi
[Analyse le **ratio Matin/Après-midi**. Une surpondération d'une période indique un pic d'activité à exploiter ou une opportunité de renforcement opérationnel.]

#### 2.5 Tendances et variabilité journalière
[Commente le **coefficient de variation** (régularité), l'**écart max-min**, et la concentration **Pareto** (top-3 jours = X% du revenu). Un CV élevé signale une activité erratique.]

#### 2.6 Évolution vs période précédente
[Chiffre précisément la **variation en % et en G**. Contextualise : est-ce une tendance, une anomalie, ou des conditions de marché ? Si pas de comparatif disponible, explique l'impact analytique.]

### 3. ANALYSE DE LA RENTABILITÉ FINANCIÈRE

#### 3.1 Marge brute et structure des coûts
[Développe la **marge brute %**, le **ratio Revenu/COGS**, le **COGS/gallon** et le **bénéfice/gallon**. Compare au prix de vente moyen pour calculer la marge unitaire réelle.]

#### 3.2 Point mort et seuil de rentabilité
[Si disponible : le **point mort journalier** est le revenu minimum à réaliser chaque jour pour couvrir le COGS. Rapproche-le du **revenu moyen/jour** pour évaluer la marge de sécurité.]

#### 3.3 Immobilisation et rotation du stock
[Valorise le **stock immobilisé en G**. Évalue la **rotation** et l'**autonomie en jours**. Un stock trop long immobilise du capital ; trop court expose à la rupture.]

### 4. CONTRÔLE INTERNE ET QUALITÉ DES DONNÉES
[Analyse le **taux d'erreur %** et le **taux d'anomalie %**. Détaille chaque type d'anomalie, sa cause probable (quantité négative = resaisie erronée / régression compteur = défaut technique), et son impact sur la fiabilité des chiffres. Formule un avis sur la qualité du contrôle interne.]

### 5. GESTION DES STOCKS ET APPROVISIONNEMENT
[Commente l'**autonomie en jours** par produit, la **valeur du stock** immobilisé, le **taux d'écoulement** et les alertes. Évalue la politique d'approvisionnement : est-elle adaptée au rythme de vente ? Risque de rupture ?]

### 6. CONCLUSION ET APPRÉCIATION GÉNÉRALE
[Synthèse structurée : 3 points forts chiffrés + 3 points de vigilance chiffrés. Donne une **appréciation globale** de la performance (excellente / satisfaisante / à améliorer) avec justification quantitative. 6-8 phrases.]

### 7. RECOMMANDATIONS PRIORITAIRES
[7 recommandations numérotées, concrètes, actionnables, avec un indicateur cible quand possible. Classées par priorité décroissante (1 = urgente). Format : **ACTION** : description → objectif mesurable.]

---
*Rapport d'audit généré par PétroSync Intelligence Artificielle · {date.today().strftime("%d/%m/%Y")}*
═══════════════════════════════════════════════════════════════════════

Rédige maintenant le document COMPLET. Chaque section doit être substantielle et analytique. Met en gras (**) tous les chiffres, ratios, pourcentages et termes comptables clés.
"""
    return prompt


# ══════════════════════════════════════════════════════════════════════════════
# Appels IA
# ══════════════════════════════════════════════════════════════════════════════

def _call_gemini(prompt: str, api_key: str) -> str:
    from google import genai
    from google.genai import types

    client   = genai.Client(api_key=api_key)
    last_err = None
    for model_name in GEMINI_MODELS:
        try:
            resp = client.models.generate_content(
                model=model_name,
                contents=[types.Content(role="user", parts=[types.Part(text=prompt)])],
                config=types.GenerateContentConfig(
                    temperature=0.2,
                    max_output_tokens=MAX_TOKENS,
                ),
            )
            return resp.text
        except Exception as e:
            err = str(e)
            if any(k in err for k in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED")):
                last_err = e
                time.sleep(1)
                continue
            raise
    raise RuntimeError(f"Tous les modèles Gemini indisponibles : {last_err}")


def _call_anthropic(prompt: str, api_key: str) -> str:
    from anthropic import Anthropic
    client = Anthropic(api_key=api_key)
    resp   = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in resp.content if b.type == "text")


# ══════════════════════════════════════════════════════════════════════════════
# Point d'entrée public
# ══════════════════════════════════════════════════════════════════════════════

def generate_audit(
    db: Session,
    date_debut: date,
    date_fin: date,
    produit_id: Optional[int] = None,
) -> dict:
    """
    Génère l'audit IA complet.
    Returns: {"text": str, "payload": dict, "metrics": dict, "kpis": dict}
    """
    payload = build_report_payload(db, date_debut, date_fin, produit_id)
    metrics = _compute_metrics(payload, db, date_debut, date_fin)
    prompt  = build_audit_prompt(payload, metrics, date_debut, date_fin)

    gemini_key    = os.environ.get("GEMINI_API_KEY",    "").strip()
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()

    text = None
    if gemini_key:
        try:
            text = _call_gemini(prompt, gemini_key)
        except Exception:
            if anthropic_key:
                text = _call_anthropic(prompt, anthropic_key)
            else:
                raise
    elif anthropic_key:
        text = _call_anthropic(prompt, anthropic_key)
    else:
        raise RuntimeError(
            "Aucune clé API configurée. "
            "Ajoutez GEMINI_API_KEY ou ANTHROPIC_API_KEY dans backend/.env"
        )

    stats  = payload["stats"]
    rentab = payload.get("rentab")
    kpis   = {
        "revenu_total":    float(stats.get("total_montant",  0) or 0),
        "volume_total":    float(stats.get("total_quantite", 0) or 0),
        "nb_releves":      int(stats.get("nb_releves",       0) or 0),
        "revenu_par_jour": metrics.get("revenu_par_jour"),
        "prix_vente_moyen":metrics.get("prix_vente_moyen"),
        "taux_couverture": metrics.get("taux_couverture"),
        "marge_pct":       (rentab.get("marge_pct")    if rentab and rentab.get("fiable") else None),
        "benefice_brut":   (float(rentab.get("benefice_brut", 0)) if rentab and rentab.get("fiable") else None),
        "var_revenu_pct":  metrics.get("var_revenu_pct"),
        "nb_anomalies":    len(payload.get("anomalies", [])),
        "nb_erreurs":      metrics.get("nb_erreurs", 0),
    }

    return {"text": text, "payload": payload, "metrics": metrics, "kpis": kpis}
