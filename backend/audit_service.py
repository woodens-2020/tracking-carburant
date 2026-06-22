"""
Module d'audit IA — PétroSync

Collecte toutes les données de la période, construit un prompt expert,
envoie à Gemini (ou Anthropic en fallback) et retourne le texte d'audit
structuré en Markdown professionnel.

Aucun chiffre inventé : toutes les valeurs dans le prompt viennent
exclusivement de build_report_payload() → compute_stats() + services métier.
"""
from __future__ import annotations

import os
import time
from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from rapport_service import build_report_payload

GEMINI_MODELS  = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.0-flash-001"]
CLAUDE_MODEL   = "claude-sonnet-4-6"
MAX_TOKENS     = 4096


# ══════════════════════════════════════════════════════════════════════════════
# Formatage
# ══════════════════════════════════════════════════════════════════════════════

def _g(val) -> str:
    if val is None:
        return "N/D"
    return f"{val:,.0f} G".replace(",", " ")

def _gal(val) -> str:
    if val is None:
        return "N/D"
    return f"{val:,.3f} gal".replace(",", " ")

def _pct(val) -> str:
    if val is None:
        return "N/D"
    return f"{val:.1f}%"


# ══════════════════════════════════════════════════════════════════════════════
# Construction du prompt
# ══════════════════════════════════════════════════════════════════════════════

def build_audit_prompt(payload: dict, date_debut: date, date_fin: date) -> str:
    stats       = payload["stats"]
    rentab      = payload.get("rentab")
    stocks      = payload.get("stocks", [])
    anomalies   = payload.get("anomalies", [])
    serie_jours = payload.get("serie_jours", {})
    nb_jours    = payload.get("nb_jours", (date_fin - date_debut).days + 1)
    prev_stats  = payload.get("prev_stats", {})

    # ── Ventes ──
    total_montant  = stats.get("total_montant", 0) or 0
    total_quantite = stats.get("total_quantite", 0) or 0
    nb_releves     = stats.get("nb_releves", 0) or 0
    nb_couverts    = stats.get("nb_jours_couverts", 0) or 0

    # ── Évolution vs période précédente ──
    prev_montant = (prev_stats or {}).get("total_montant", 0) or 0
    if prev_montant > 0:
        var = ((total_montant - prev_montant) / prev_montant) * 100
        evolution = (
            f"{'+' if var >= 0 else ''}{var:.1f}% vs période précédente "
            f"(précédent : {_g(prev_montant)})"
        )
    else:
        evolution = "Pas de données sur la période précédente — comparaison impossible."

    # ── Détail par produit ──
    lignes_produit = ""
    for nom, p in stats.get("par_produit", {}).items():
        lignes_produit += (
            f"  • {nom} : {_gal(p.get('quantite', 0))} vendus, "
            f"revenu {_g(p.get('montant', 0))}\n"
        )
    if not lignes_produit:
        lignes_produit = "  Aucun détail disponible.\n"

    # ── Détail par pompe ──
    lignes_pompe = ""
    for nom, p in stats.get("par_pompe", {}).items():
        lignes_pompe += (
            f"  • Pompe {nom} : {_gal(p.get('quantite', 0))}, "
            f"revenu {_g(p.get('montant', 0))}\n"
        )
    if not lignes_pompe:
        lignes_pompe = "  Aucune pompe enregistrée.\n"

    # ── Top 3 jours ──
    top_jours = sorted(
        serie_jours.items(), key=lambda x: x[1].get("montant", 0), reverse=True
    )[:3]
    lignes_top = ""
    for d, v in top_jours:
        lignes_top += f"  • {d} : {_g(v.get('montant', 0))} — {_gal(v.get('quantite', 0))}\n"
    if not lignes_top:
        lignes_top = "  Aucune vente enregistrée sur la période.\n"

    # ── Anomalies ──
    anom_err    = [a for a in anomalies if a.get("type") == "ERREUR"]
    anom_alert  = [a for a in anomalies if a.get("type") == "ALERTE"]
    lignes_anom = ""
    for a in anomalies[:12]:
        lignes_anom += (
            f"  • [{a.get('type', '')}] {a.get('message', '')} "
            f"({a.get('date', '')})\n"
        )
    if not lignes_anom:
        lignes_anom = "  Aucune anomalie détectée sur la période.\n"

    # ── Stock ──
    lignes_stock = ""
    for s in stocks:
        alerte = " ⚠ STOCK BAS" if s.get("alerte_bas") else ""
        lignes_stock += (
            f"  • {s.get('produit_nom', '')}: "
            f"{_gal(s.get('gallons_restants', 0))}{alerte}\n"
        )
    if not lignes_stock:
        lignes_stock = "  Aucun stock enregistré.\n"

    # ── Rentabilité ──
    if rentab and rentab.get("fiable"):
        rentab_txt = (
            f"  • Revenu total   : {_g(rentab.get('revenu_total'))}\n"
            f"  • COGS (coût)    : {_g(rentab.get('cogs_total'))}\n"
            f"  • Bénéfice brut  : {_g(rentab.get('benefice_brut'))}\n"
            f"  • Marge brute    : {_pct(rentab.get('marge_pct'))}\n"
        )
    elif rentab and not rentab.get("fiable"):
        rentab_txt = (
            "  Calcul de rentabilité approximatif "
            "(données de livraison incomplètes — COGS non fiable).\n"
        )
    else:
        rentab_txt = (
            "  Rentabilité non calculable "
            "(aucune livraison enregistrée — COGS absent).\n"
        )

    prompt = f"""Tu es un expert-comptable et auditeur financier senior spécialisé dans les stations-service haïtiennes.

Ta mission : rédiger un **document d'audit professionnel, complet et structuré** destiné à la direction, à partir UNIQUEMENT des données ci-dessous. Zéro chiffre inventé.

══════════════════════════════════════════════════════════
DONNÉES RÉELLES DE LA PÉRIODE
══════════════════════════════════════════════════════════
Période analysée   : {date_debut.strftime("%d/%m/%Y")} au {date_fin.strftime("%d/%m/%Y")} ({nb_jours} jours)
Date d'audit       : {date.today().strftime("%d/%m/%Y")}
Monnaie            : Gourde haïtienne (G)  |  Quantités : gallons (gal)

━━━ VENTES GLOBALES ━━━
  • Volume vendu total      : {_gal(total_quantite)}
  • Revenu brut total       : {_g(total_montant)}
  • Nombre de relevés       : {nb_releves}
  • Jours avec données      : {nb_couverts} / {nb_jours}
  • Évolution               : {evolution}

━━━ RÉPARTITION PAR PRODUIT ━━━
{lignes_produit}
━━━ RÉPARTITION PAR POMPE ━━━
{lignes_pompe}
━━━ TOP 3 MEILLEURES JOURNÉES ━━━
{lignes_top}
━━━ RENTABILITÉ (Coût Moyen Pondéré) ━━━
{rentab_txt}
━━━ STOCK ACTUEL ━━━
{lignes_stock}
━━━ ANOMALIES DÉTECTÉES ━━━
  Erreurs : {len(anom_err)}  |  Alertes : {len(anom_alert)}
{lignes_anom}
══════════════════════════════════════════════════════════

══════════════════════════════════════════════════════════
STRUCTURE REQUISE DU DOCUMENT (à suivre impérativement)
══════════════════════════════════════════════════════════
Utilise EXACTEMENT ces titres Markdown (##, ###) pour que le document soit correctement mis en page.

# RAPPORT D'AUDIT — STATION CARBURANT PÉTROSYNC
## Période : {date_debut.strftime("%d/%m/%Y")} – {date_fin.strftime("%d/%m/%Y")}

### 1. INTRODUCTION ET MANDAT D'AUDIT
[Présente le contexte, la mission, les objectifs de l'audit et les sources de données. 3-5 phrases professionnelles et formelles.]

### 2. ANALYSE COMMERCIALE

#### 2.1 Volume et revenu global
[Analyse le volume total, le revenu, la densité de relevés et la couverture de la période. Interprète les chiffres avec un regard d'expert — au-delà de la simple description.]

#### 2.2 Performance par produit
[Compare les produits entre eux, identifie le(s) plus performant(s) et ceux à surveiller, avec une analyse qualitative.]

#### 2.3 Performance par pompe
[Analyse la contribution de chaque pompe, identifie des écarts ou inefficacités potentielles.]

#### 2.4 Tendances journalières
[Commente les meilleures journées, les creux éventuels, et ce qu'ils révèlent sur les habitudes de la clientèle ou la gestion opérationnelle.]

#### 2.5 Évolution vs période précédente
[Interprète la variation de revenu. Si données absentes, note la limitation et recommande le suivi comparatif.]

### 3. CONTRÔLE INTERNE ET ANOMALIES

[Évalue la qualité du contrôle interne à partir des anomalies détectées. Commente chaque type d'anomalie (quantité négative, régression compteur, etc.), leur cause probable et leur impact sur la fiabilité des données. Si aucune anomalie : note-le comme un indicateur positif de rigueur opérationnelle.]

### 4. GESTION DES STOCKS ET APPROVISIONNEMENT

[Analyse les niveaux de stock actuels, les alertes éventuelles, le risque de rupture et la politique d'approvisionnement implicite. Recommande des actions si applicable.]

### 5. PERFORMANCE FINANCIÈRE ET RENTABILITÉ

[Analyse la marge, le COGS WAC, le bénéfice. Évalue la santé financière. Si les données de livraison sont absentes, explique l'impact sur la fiabilité de l'analyse et l'action corrective à entreprendre.]

### 6. CONCLUSION ET APPRÉCIATION GÉNÉRALE

[Synthèse équilibrée des forces et des points de vigilance. Évaluation globale de la performance sur la période. 4-6 phrases de conclusion professionnelle.]

### 7. RECOMMANDATIONS PRIORITAIRES

[Liste numérotée de 5 à 7 recommandations concrètes, actionnables et priorisées. Chaque recommandation doit être spécifique et réalisable, pas des généralités.]

---
*Document d'audit généré par PétroSync — Intelligence Artificielle*
*{date.today().strftime("%d/%m/%Y")}*
══════════════════════════════════════════════════════════

Rédige maintenant le document complet en français professionnel. Sois analytique et prescriptif — un bon audit explique le POURQUOI et recommande des ACTIONS, il ne se contente pas de décrire des chiffres.
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
                    temperature=0.3,
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

    Returns:
        {"text": str, "payload": dict}
    """
    payload = build_report_payload(db, date_debut, date_fin, produit_id)
    prompt  = build_audit_prompt(payload, date_debut, date_fin)

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

    return {"text": text, "payload": payload}
