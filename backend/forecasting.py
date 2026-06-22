"""
Module de prévision des ventes — PétroSync
==========================================
Implémentation en pur Python stdlib (math, statistics) — aucune dépendance externe.

Méthodes statistiques :
  1. Statistiques descriptives : moyenne, médiane, écart-type, CV, skewness, outliers
  2. Régression linéaire OLS  : y = β₀ + β₁·t  (+ intervalle de prédiction Student-t)
  3. Moyenne mobile simple    : SMA(w)
  4. Lissage exponentiel      : SES — α optimisé par minimisation SSE
  5. Holt double lissage      : niveau + tendance (α, β optimisés)
  6. Intervalles de confiance : IC 80 % et IC 95 % sur chaque prévision
  7. Scénarios probabilistes  : optimiste / réaliste / pessimiste
  8. Probabilités seuils      : P(X > seuil) via loi normale N(μ, σ²)
  9. Métriques d'évaluation   : MAE, RMSE, MAPE
 10. Sélection automatique    : meilleur modèle selon RMSE in-sample

Paramètres ajustables (constantes en tête de fichier) :
  MIN_POINTS_FIABLES, MIN_POINTS_HOLT, MIN_POINTS_MA, Z_80, Z_95
"""

import math
from datetime import date as date_type, timedelta
from typing import Optional
from collections import defaultdict
from sqlalchemy.orm import Session
from models import Releve, Pompe


# ══════════════════════════════════════════════════════════════════
# PARAMÈTRES GLOBAUX (ajustables)
# ══════════════════════════════════════════════════════════════════

MIN_POINTS_FIABLES = 14   # sous ce seuil : prévisions peu fiables
MIN_POINTS_HOLT    = 5    # minimum pour la méthode de Holt
MIN_POINTS_MA      = 3    # minimum pour SMA(3)
Z_80 = 1.2816             # z pour IC 80 % (P=0.90 à une queue)
Z_95 = 1.9600             # z pour IC 95 % (P=0.975 à une queue)


# ══════════════════════════════════════════════════════════════════
# 1. UTILITAIRES MATHÉMATIQUES (stdlib uniquement)
# ══════════════════════════════════════════════════════════════════

def _mean(v: list) -> float:
    return sum(v) / len(v) if v else 0.0


def _std(v: list) -> float:
    """Écart-type échantillon (divise par n-1)."""
    if len(v) < 2:
        return 0.0
    m = _mean(v)
    return math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1))


def _median(v: list) -> float:
    s = sorted(v)
    n = len(s)
    return (s[n // 2 - 1] + s[n // 2]) / 2 if n % 2 == 0 else s[n // 2]


def _mae(actuals, preds) -> float:
    pairs = [(a, p) for a, p in zip(actuals, preds) if a is not None and p is not None]
    return sum(abs(a - p) for a, p in pairs) / len(pairs) if pairs else float("nan")


def _rmse(actuals, preds) -> float:
    pairs = [(a, p) for a, p in zip(actuals, preds) if a is not None and p is not None]
    return math.sqrt(sum((a - p) ** 2 for a, p in pairs) / len(pairs)) if pairs else float("nan")


def _mape(actuals, preds) -> float:
    pairs = [(a, p) for a, p in zip(actuals, preds)
             if a is not None and p is not None and a != 0]
    return sum(abs((a - p) / a) for a, p in pairs) / len(pairs) * 100 if pairs else float("nan")


def _normal_cdf(x: float) -> float:
    """CDF de N(0,1) via la fonction d'erreur (erf disponible dans math depuis Python 3.2)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# Table des quantiles de la loi de Student t_{df} pour α/2 = 0.025 (IC 95 %) et 0.10 (IC 80 %)
_T_TABLE = {
    # df: (t_80%, t_95%)
    1:  (3.078, 12.706),
    2:  (1.886, 4.303),
    3:  (1.638, 3.182),
    4:  (1.533, 2.776),
    5:  (1.476, 2.571),
    6:  (1.440, 2.447),
    7:  (1.415, 2.365),
    8:  (1.397, 2.306),
    9:  (1.383, 2.262),
    10: (1.372, 2.228),
    15: (1.341, 2.131),
    20: (1.325, 2.086),
    25: (1.316, 2.060),
    30: (1.310, 2.042),
}


def _t_quantile(df: int) -> tuple[float, float]:
    """Retourne (t_80, t_95) pour df degrés de liberté."""
    if df <= 0:
        return (Z_80, Z_95)
    if df >= 30:
        return (Z_80, Z_95)
    keys = sorted(_T_TABLE.keys())
    best = min(keys, key=lambda k: abs(k - df))
    return _T_TABLE[best]


def _safe(v) -> float | None:
    """Retourne None si v est nan ou inf, sinon v arrondi à 2 décimales."""
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return None
    return round(float(v), 2)


# ══════════════════════════════════════════════════════════════════
# 2. EXTRACTION ET PRÉPARATION DES DONNÉES
# ══════════════════════════════════════════════════════════════════

def get_daily_series(
    db: Session,
    metric: str = "montant",
    produit_id: Optional[int] = None,
    pompe_id_filter: Optional[int] = None,
) -> list[dict]:
    """
    Extrait la série temporelle journalière depuis la base PétroSync.

    Traitement :
      - Agrégation de toutes les séances (Matin + Après-midi) par date
      - Remplissage des jours sans saisie avec valeur=0 (série continue)
      - Détection des jours « manquants » (flag manquant=True)

    metric : "montant" (chiffre d'affaires) ou "quantite" (gallons)
    """
    q = db.query(Releve)
    if pompe_id_filter:
        q = q.filter(Releve.pompe_id == pompe_id_filter)
    releves = q.all()

    if produit_id:
        # Filtre via la relation Pompe → produit_id
        pompes_prod = {
            p.id for p in db.query(Pompe).filter(Pompe.produit_id == produit_id).all()
        }
        releves = [r for r in releves if r.pompe_id in pompes_prod]

    if not releves:
        return []

    par_date: dict = defaultdict(lambda: {"montant": 0.0, "quantite": 0.0, "nb": 0})
    for r in releves:
        d = str(r.date)
        par_date[d]["montant"]  += float(r.montant_vente)
        par_date[d]["quantite"] += float(r.quantite)
        par_date[d]["nb"]       += 1

    dates_sorted = sorted(par_date.keys())
    d_min = date_type.fromisoformat(dates_sorted[0])
    d_max = date_type.fromisoformat(dates_sorted[-1])

    series = []
    cur = d_min
    while cur <= d_max:
        ds = str(cur)
        val = par_date.get(ds)
        series.append({
            "date":       ds,
            "valeur":     val[metric] if val else 0.0,
            "nb_releves": val["nb"] if val else 0,
            "manquant":   val is None,
        })
        cur += timedelta(days=1)

    return series


# ══════════════════════════════════════════════════════════════════
# 3. STATISTIQUES DESCRIPTIVES
# ══════════════════════════════════════════════════════════════════

def descriptive_stats(values: list[float]) -> dict:
    """
    Analyse descriptive complète de la série.

    Formules utilisées :
      Moyenne  : μ = (1/n) Σ yᵢ
      Variance : s² = (1/(n-1)) Σ (yᵢ - μ)²
      CV       : s / μ × 100
      Skewness : (n / ((n-1)(n-2))) × Σ ((yᵢ - μ)/s)³   [coefficient de Fisher]
      Outliers : méthode IQR — seuils à Q1 - 1.5·IQR et Q3 + 1.5·IQR
    """
    if not values:
        return {}

    n   = len(values)
    mu  = _mean(values)
    sig = _std(values)
    med = _median(values)
    sv  = sorted(values)

    q1  = sv[max(0, n // 4)]
    q3  = sv[min(n - 1, 3 * n // 4)]
    iqr = q3 - q1

    cv = (sig / mu * 100) if mu != 0 else None

    # Skewness de Fisher (non biaisé)
    if sig > 0 and n >= 3:
        skew = (n / ((n - 1) * (n - 2))) * sum(((x - mu) / sig) ** 3 for x in values)
    else:
        skew = 0.0

    fence_low  = q1 - 1.5 * iqr
    fence_high = q3 + 1.5 * iqr
    outliers   = [x for x in values if x < fence_low or x > fence_high]

    return {
        "n":          n,
        "moyenne":    round(mu, 2),
        "mediane":    round(med, 2),
        "ecart_type": round(sig, 2),
        "variance":   round(sig ** 2, 2),
        "cv":         round(cv, 1) if cv is not None else None,
        "min":        round(min(values), 2),
        "max":        round(max(values), 2),
        "q1":         round(q1, 2),
        "q3":         round(q3, 2),
        "iqr":        round(iqr, 2),
        "skewness":   round(skew, 3),
        "nb_outliers":len(outliers),
        "outliers":   [round(o, 2) for o in outliers],
    }


# ══════════════════════════════════════════════════════════════════
# 4. RÉGRESSION LINÉAIRE OLS
# ══════════════════════════════════════════════════════════════════

def linear_regression(values: list[float]) -> dict:
    """
    Régression linéaire par moindres carrés ordinaires (OLS).

    Modèle    : yₜ = β₀ + β₁·t + εₜ,   t ∈ {0, 1, …, n-1}
    Estimateurs OLS :
      β₁ = Σ(tᵢ - t̄)(yᵢ - ȳ) / Σ(tᵢ - t̄)²
      β₀ = ȳ - β₁·t̄
    Erreur standard résiduelle :
      sₑ = √( SSE / (n-2) )
    Intervalle de prédiction à l'horizon h (quantile Student, df=n-2) :
      ŷ ± t_{α/2} · sₑ · √(1 + 1/n + (t_new - t̄)² / Sₜₜ)
    R² = 1 - SSE/SST
    """
    n = len(values)
    if n < 2:
        return {"statut": "insuffisant", "n": n, "min_requis": 2}

    ts = list(range(n))
    t_bar = _mean(ts)
    y_bar = _mean(values)

    Stt = sum((ti - t_bar) ** 2 for ti in ts)
    Sty = sum((ti - t_bar) * (yi - y_bar) for ti, yi in zip(ts, values))

    beta1 = Sty / Stt if Stt != 0 else 0.0
    beta0 = y_bar - beta1 * t_bar

    y_pred = [beta0 + beta1 * ti for ti in ts]
    sse    = sum((yi - yp) ** 2 for yi, yp in zip(values, y_pred))
    sst    = sum((yi - y_bar) ** 2 for yi in values)
    r2     = 1 - sse / sst if sst > 0 else 0.0
    se     = math.sqrt(sse / max(1, n - 2))

    t80, t95 = _t_quantile(max(1, n - 2))

    def predict(h: int):
        t_new = n - 1 + h
        yhat  = beta0 + beta1 * t_new
        fact  = math.sqrt(1 + 1 / n + (t_new - t_bar) ** 2 / (Stt or 1))
        m80   = t80 * se * fact
        m95   = t95 * se * fact
        return {
            "valeur":    max(0.0, round(yhat, 2)),
            "ic80_bas":  max(0.0, round(yhat - m80, 2)),
            "ic80_haut": max(0.0, round(yhat + m80, 2)),
            "ic95_bas":  max(0.0, round(yhat - m95, 2)),
            "ic95_haut": max(0.0, round(yhat + m95, 2)),
        }

    return {
        "statut":      "ok",
        "n":           n,
        "beta0":       round(beta0, 4),
        "beta1":       round(beta1, 4),
        "r2":          round(r2, 4),
        "se":          round(se, 4),
        "mae":         round(_mae(values, y_pred), 2),
        "rmse":        round(_rmse(values, y_pred), 2),
        "mape":        _safe(_mape(values, y_pred)),
        "y_pred_full": [round(y, 2) for y in y_pred],
        "predict":     predict,
        "tendance":    "hausse" if beta1 > 0 else ("baisse" if beta1 < 0 else "stable"),
        "description": (
            f"y = {beta0:.0f} + {beta1:+.0f}·t  "
            f"(R²={r2:.3f}, tendance {'+' if beta1>=0 else ''}{beta1:.0f}/jour)"
        ),
    }


# ══════════════════════════════════════════════════════════════════
# 5. MOYENNE MOBILE SIMPLE (SMA)
# ══════════════════════════════════════════════════════════════════

def simple_ma(values: list[float], window: int) -> dict:
    """
    Moyenne mobile simple (SMA) sur fenêtre de w périodes.

    Formule : SMA_t = (1/w) · Σ_{i=t-w+1}^{t} yᵢ
    Prévision : ŷ_{t+h} = SMA_t  (prévision plate — pas de tendance)
    Incertitude : IC calculé depuis sₑ = std(résidus) × √(1 + 1/w)
    """
    n = len(values)
    if n < window:
        return {"statut": "insuffisant", "n": n, "window": window, "min_requis": window}

    y_pred = [None] * window
    for i in range(window, n):
        y_pred.append(_mean(values[i - window:i]))

    act = values[window:]
    prd = [p for p in y_pred[window:] if p is not None]
    res = [a - p for a, p in zip(act, prd)]
    se  = _std(res) if len(res) >= 2 else (_mae(act, prd) or 0.0)

    last_val = _mean(values[-window:])

    def predict(h: int):
        yhat = last_val
        fact = math.sqrt(1 + 1 / window)
        return {
            "valeur":    max(0.0, round(yhat, 2)),
            "ic80_bas":  max(0.0, round(yhat - Z_80 * se * fact, 2)),
            "ic80_haut": max(0.0, round(yhat + Z_80 * se * fact, 2)),
            "ic95_bas":  max(0.0, round(yhat - Z_95 * se * fact, 2)),
            "ic95_haut": max(0.0, round(yhat + Z_95 * se * fact, 2)),
        }

    return {
        "statut":      "ok",
        "n":           n,
        "window":      window,
        "mae":         round(_mae(act, prd), 2),
        "rmse":        round(_rmse(act, prd), 2),
        "mape":        _safe(_mape(act, prd)),
        "se":          round(se, 2),
        "y_pred_full": [round(y, 2) if y is not None else None for y in y_pred],
        "predict":     predict,
        "description": f"SMA({window}) — prévision plate = moyenne des {window} derniers jours",
    }


# ══════════════════════════════════════════════════════════════════
# 6. LISSAGE EXPONENTIEL SIMPLE (SES)
# ══════════════════════════════════════════════════════════════════

def ses_smooth(values: list[float], alpha: Optional[float] = None) -> dict:
    """
    Lissage exponentiel simple (SES — Exponential Smoothing, niveau seul).

    Formules (Holt 1957) :
      Initialisation : l₀ = y₀
      Lissage        : lₜ = α·yₜ + (1-α)·lₜ₋₁
      Prévision      : ŷ_{t+h} = lₜ  (prévision plate)

    Si alpha=None : α optimal trouvé par grid-search [0.01, 0.99] (pas 0.01)
    minimisant SSE = Σ (yₜ - ŷₜ)².

    Variance de la prévision à h pas : Var(h) = σ²_ε · h  (random-walk)
    → IC croissants avec l'horizon.

    Hypothèse : série sans tendance notable.
    """
    n = len(values)
    if n < 2:
        return {"statut": "insuffisant", "n": n, "min_requis": 2}

    def _fit(a: float):
        level = values[0]
        preds = [values[0]]
        for i in range(1, n):
            level = a * values[i] + (1 - a) * level
            preds.append(level)
        sse = sum((values[i] - preds[i]) ** 2 for i in range(1, n))
        return preds, sse, level

    if alpha is None:
        best_a, best_sse = 0.3, float("inf")
        for ai in range(1, 100):
            a = ai / 100.0
            _, sse, _ = _fit(a)
            if sse < best_sse:
                best_sse, best_a = sse, a
        alpha = best_a

    y_pred, _, level = _fit(alpha)
    res = [values[i] - y_pred[i] for i in range(1, n)]
    se  = _std(res) if len(res) >= 2 else 0.0

    def predict(h: int):
        yhat = level
        # Variance cumulée : random-walk → marge × √h
        m80 = Z_80 * se * math.sqrt(max(1, h))
        m95 = Z_95 * se * math.sqrt(max(1, h))
        return {
            "valeur":    max(0.0, round(yhat, 2)),
            "ic80_bas":  max(0.0, round(yhat - m80, 2)),
            "ic80_haut": max(0.0, round(yhat + m80, 2)),
            "ic95_bas":  max(0.0, round(yhat - m95, 2)),
            "ic95_haut": max(0.0, round(yhat + m95, 2)),
        }

    return {
        "statut":      "ok",
        "n":           n,
        "alpha":       round(alpha, 4),
        "level":       round(level, 2),
        "mae":         round(_mae(values[1:], y_pred[1:]), 2),
        "rmse":        round(_rmse(values[1:], y_pred[1:]), 2),
        "mape":        _safe(_mape(values[1:], y_pred[1:])),
        "se":          round(se, 2),
        "y_pred_full": [round(y, 2) for y in y_pred],
        "predict":     predict,
        "description": (
            f"SES(α={alpha:.2f}) — niveau={level:.0f}  "
            f"(α={alpha:.2f} : pondération récente {'forte' if alpha>0.5 else 'douce'})"
        ),
    }


# ══════════════════════════════════════════════════════════════════
# 7. MÉTHODE DE HOLT (double lissage — niveau + tendance)
# ══════════════════════════════════════════════════════════════════

def holt_smooth(values: list[float]) -> dict:
    """
    Lissage exponentiel double de Holt (Holt 1957).

    Formules :
      Niveau  : lₜ = α·yₜ + (1-α)·(lₜ₋₁ + bₜ₋₁)
      Tendance: bₜ = β·(lₜ - lₜ₋₁) + (1-β)·bₜ₋₁
      Prévision h pas : ŷ_{t+h} = lₜ + h·bₜ

    Initialisation : l₀ = y₀,  b₀ = y₁ - y₀
    Optimisation   : grid-search 6×5 sur (α, β) ∈ {0.1,0.2,…,0.9} × {0.05,0.1,0.2,0.3,0.5}
                     minimisant SSE.
    IC : basé sur sₑ = std(résidus) × √h (erreur de prévision croissante).

    Hypothèse : série avec tendance linéaire.
    Nécessite au moins MIN_POINTS_HOLT=5 observations.
    """
    n = len(values)
    if n < MIN_POINTS_HOLT:
        return {"statut": "insuffisant", "n": n, "min_requis": MIN_POINTS_HOLT}

    def _fit(a: float, b: float):
        level = values[0]
        trend = values[1] - values[0]
        preds = [values[0]]
        for i in range(1, n):
            l_prev, b_prev = level, trend
            level = a * values[i] + (1 - a) * (l_prev + b_prev)
            trend = b * (level - l_prev) + (1 - b) * b_prev
            preds.append(level)
        sse = sum((values[i] - preds[i]) ** 2 for i in range(1, n))
        return preds, sse, level, trend

    best_a, best_b, best_sse = 0.3, 0.1, float("inf")
    for ai in [0.1, 0.2, 0.3, 0.5, 0.7, 0.9]:
        for bi in [0.05, 0.1, 0.2, 0.3, 0.5]:
            _, sse, _, _ = _fit(ai, bi)
            if sse < best_sse:
                best_sse, best_a, best_b = sse, ai, bi

    y_pred, _, level, trend = _fit(best_a, best_b)
    res = [values[i] - y_pred[i] for i in range(1, n)]
    se  = _std(res) if len(res) >= 2 else 0.0

    def predict(h: int):
        yhat = level + h * trend
        m80  = Z_80 * se * math.sqrt(max(1, h))
        m95  = Z_95 * se * math.sqrt(max(1, h))
        return {
            "valeur":    max(0.0, round(yhat, 2)),
            "ic80_bas":  max(0.0, round(yhat - m80, 2)),
            "ic80_haut": max(0.0, round(yhat + m80, 2)),
            "ic95_bas":  max(0.0, round(yhat - m95, 2)),
            "ic95_haut": max(0.0, round(yhat + m95, 2)),
        }

    tendance_txt = f"{'+' if trend >= 0 else ''}{trend:.0f}/jour"
    return {
        "statut":      "ok",
        "n":           n,
        "alpha":       round(best_a, 4),
        "beta":        round(best_b, 4),
        "level":       round(level, 2),
        "trend":       round(trend, 2),
        "mae":         round(_mae(values[1:], y_pred[1:]), 2),
        "rmse":        round(_rmse(values[1:], y_pred[1:]), 2),
        "mape":        _safe(_mape(values[1:], y_pred[1:])),
        "se":          round(se, 2),
        "y_pred_full": [round(y, 2) for y in y_pred],
        "predict":     predict,
        "description": (
            f"Holt(α={best_a:.2f}, β={best_b:.2f}) — "
            f"niveau={level:.0f}, tendance {tendance_txt}"
        ),
    }


# ══════════════════════════════════════════════════════════════════
# 8. SÉLECTION DU MEILLEUR MODÈLE
# ══════════════════════════════════════════════════════════════════

def select_best(modeles: dict) -> str:
    """
    Sélectionne le meilleur modèle selon RMSE in-sample minimum.
    Tie-break : MAE, puis MAPE.
    Exclut les modèles avec statut != "ok" ou RMSE manquant.
    """
    candidates = {
        k: v for k, v in modeles.items()
        if v.get("statut") == "ok"
        and v.get("rmse") is not None
        and not math.isnan(v.get("rmse", float("nan")))
    }
    if not candidates:
        return next(iter(modeles), "aucun")
    return min(candidates, key=lambda k: (
        candidates[k].get("rmse", float("inf")),
        candidates[k].get("mae",  float("inf")),
        candidates[k].get("mape", float("inf")) or float("inf"),
    ))


# ══════════════════════════════════════════════════════════════════
# 9. PROBABILITÉS
# ══════════════════════════════════════════════════════════════════

def calc_probabilities(values: list[float], seuils: list[float], objectif: Optional[float] = None) -> dict:
    """
    Calcule P(X > seuil) sous hypothèse X ~ N(μ, σ²).

    Formule : P(X > s) = 1 - Φ((s - μ) / σ)
    où Φ est la CDF de N(0,1).

    Hypothèse de normalité à vérifier — valide si n ≥ 30 (Théorème Central Limite).
    Pour n < 30, les probabilités sont indicatives uniquement.
    """
    if not values or len(values) < 2:
        return {}

    mu  = _mean(values)
    sig = _std(values)

    result = {}
    all_seuils = list(seuils)
    if objectif and objectif > 0:
        all_seuils.append(objectif)

    for s in all_seuils:
        if s <= 0:
            continue
        if sig > 0:
            z    = (s - mu) / sig
            prob = (1 - _normal_cdf(z)) * 100
        else:
            prob = 100.0 if mu >= s else 0.0
        result[int(round(s))] = round(prob, 1)

    return {
        "mu":    round(mu, 2),
        "sigma": round(sig, 2),
        "seuils": result,
        "note": (
            "Hypothèse : X ~ N(μ, σ²). "
            f"Fiabilité {'faible (n<14)' if len(values)<14 else 'raisonnable'}."
        ),
    }


# ══════════════════════════════════════════════════════════════════
# 10. ORCHESTRATEUR PRINCIPAL
# ══════════════════════════════════════════════════════════════════

def run_forecast(
    db: Session,
    horizon:    int             = 14,
    metric:     str             = "montant",
    produit_id: Optional[int]   = None,
    pompe_id:   Optional[int]   = None,
    objectif:   Optional[float] = None,
) -> dict:
    """
    Point d'entrée principal du module de prévision.

    Étapes :
      1. Extraction de la série journalière
      2. Évaluation de la fiabilité (n_actifs, taux de couverture)
      3. Statistiques descriptives
      4. Entraînement des modèles (régression, SMA, SES, Holt)
      5. Sélection du meilleur modèle (RMSE minimum)
      6. Génération des prévisions horizon h avec IC 80 %/95 %
      7. Scénarios : optimiste / réaliste / pessimiste
      8. Probabilités d'atteindre des seuils

    horizon   : nombre de jours à prévoir (1–90)
    metric    : "montant" (CA en Gourdes) ou "quantite" (gallons)
    objectif  : seuil journalier pour calculer P(X > objectif)
    """
    # ── 1. Extraction ──────────────────────────────────────────────
    series = get_daily_series(db, metric, produit_id, pompe_id)
    if not series:
        return {"erreur": "Aucune donnée disponible", "statut": "vide"}

    values_all    = [s["valeur"] for s in series]
    values_actifs = [s["valeur"] for s in series if not s["manquant"]]
    n_total  = len(values_all)
    n_actifs = len(values_actifs)
    taux_cov = round(n_actifs / n_total * 100, 1) if n_total else 0.0

    # ── 2. Fiabilité et avertissements ─────────────────────────────
    if n_actifs >= 30:
        fiabilite = "élevée"
    elif n_actifs >= MIN_POINTS_FIABLES:
        fiabilite = "modérée"
    elif n_actifs >= 3:
        fiabilite = "faible"
    else:
        fiabilite = "très faible"

    warns = []
    if n_actifs < 3:
        warns.append(
            f"Seulement {n_actifs} jour(s) avec des données. "
            "Minimum recommandé : 14 jours pour une prévision indicative. "
            "Les chiffres affichés sont à titre illustratif uniquement."
        )
    elif n_actifs < MIN_POINTS_FIABLES:
        warns.append(
            f"Historique court ({n_actifs} jours actifs sur {n_total} jours calendaires). "
            f"Les prévisions ont une forte incertitude. "
            f"Continuez à saisir les données pour atteindre {MIN_POINTS_FIABLES} jours."
        )
    if taux_cov < 50:
        warns.append(
            f"Taux de couverture bas ({taux_cov}%) : "
            "beaucoup de jours sans saisie — les zéros remplissent les trous "
            "et peuvent fausser les modèles."
        )

    # ── 3. Statistiques descriptives ───────────────────────────────
    stats = descriptive_stats(values_actifs) if values_actifs else {}

    # ── 4. Entraînement des modèles ────────────────────────────────
    _models_raw = {}

    reg = linear_regression(values_all)
    _models_raw["regression"] = reg

    ma3 = simple_ma(values_all, MIN_POINTS_MA)
    _models_raw["ma3"] = ma3

    if n_total >= 7:
        ma7 = simple_ma(values_all, 7)
        _models_raw["ma7"] = ma7

    ses_r = ses_smooth(values_all)
    _models_raw["ses"] = ses_r

    holt_r = holt_smooth(values_all)
    _models_raw["holt"] = holt_r

    # ── 5. Sélection ───────────────────────────────────────────────
    MODEL_NOMS = {
        "regression": "Régression linéaire",
        "ma3":        "Moyenne mobile (3 j)",
        "ma7":        "Moyenne mobile (7 j)",
        "ses":        "Lissage exponentiel (SES)",
        "holt":       "Holt double lissage",
    }
    # On prépare un dict sans les callables pour le sélecteur
    _sel_map = {
        k: {**v, "nom": MODEL_NOMS.get(k, k)}
        for k, v in _models_raw.items()
    }
    meilleur = select_best(_sel_map)

    # ── 6. Prévisions ──────────────────────────────────────────────
    last_date = date_type.fromisoformat(series[-1]["date"])
    mu_actifs = _mean(values_actifs) if values_actifs else 0.0
    sd_actifs = _std(values_actifs)  if len(values_actifs) >= 2 else 0.0
    predict_fn = _models_raw[meilleur].get("predict")

    previsions = []
    for h in range(1, horizon + 1):
        date_prev = last_date + timedelta(days=h)
        if predict_fn:
            p = predict_fn(h)
        else:
            p = {
                "valeur":    mu_actifs,
                "ic80_bas":  max(0.0, mu_actifs - Z_80 * sd_actifs),
                "ic80_haut": mu_actifs + Z_80 * sd_actifs,
                "ic95_bas":  max(0.0, mu_actifs - Z_95 * sd_actifs),
                "ic95_haut": mu_actifs + Z_95 * sd_actifs,
            }
        previsions.append({
            "date":               str(date_prev),
            "h":                  h,
            "valeur":             p["valeur"],
            "ic80_bas":           p.get("ic80_bas",  0.0),
            "ic80_haut":          p.get("ic80_haut", p["valeur"] * 2),
            "ic95_bas":           p.get("ic95_bas",  0.0),
            "ic95_haut":          p.get("ic95_haut", p["valeur"] * 2.5),
            "scenario_optimiste":  max(0.0, round(p["valeur"] + sd_actifs, 2)),
            "scenario_realiste":   p["valeur"],
            "scenario_pessimiste": max(0.0, round(p["valeur"] - sd_actifs, 2)),
        })

    # ── 7. Prévisions in-sample (pour graphique) ───────────────────
    in_sample = _models_raw[meilleur].get("y_pred_full", [])

    # ── 8. Probabilités ────────────────────────────────────────────
    proba = {}
    if len(values_actifs) >= 2:
        seuils_auto = [
            mu_actifs * 0.50,
            mu_actifs * 0.75,
            mu_actifs,
            mu_actifs * 1.25,
            mu_actifs * 1.50,
        ]
        if objectif and objectif > 0:
            seuils_auto.append(objectif)
        proba = calc_probabilities(values_actifs, seuils_auto, objectif)

    # ── 9. Tableau comparatif (sans callables) ─────────────────────
    modeles_out = {}
    for k, v in _models_raw.items():
        modeles_out[k] = {
            "nom":         MODEL_NOMS.get(k, k),
            "statut":      v.get("statut", "ok"),
            "mae":         _safe(v.get("mae")),
            "rmse":        _safe(v.get("rmse")),
            "mape":        _safe(v.get("mape")),
            "r2":          _safe(v.get("r2")),
            "alpha":       _safe(v.get("alpha")),
            "beta":        _safe(v.get("beta")),
            "se":          _safe(v.get("se")),
            "description": v.get("description", ""),
            "meilleur":    k == meilleur,
        }

    return {
        "meta": {
            "metric":          metric,
            "n_total":         n_total,
            "n_actifs":        n_actifs,
            "taux_couverture": taux_cov,
            "fiabilite":       fiabilite,
            "date_min":        series[0]["date"],
            "date_max":        series[-1]["date"],
            "horizon":         horizon,
            "avertissements":  warns,
            "meilleur_modele": meilleur,
            "meilleur_nom":    MODEL_NOMS.get(meilleur, meilleur),
        },
        "stats":      stats,
        "historique": series,
        "in_sample":  [round(v, 2) if v is not None else None for v in in_sample],
        "previsions": previsions,
        "modeles":    modeles_out,
        "probabilites": proba,
    }
