"""Intelligence de placement des mètres.

Les pompistes se trompent souvent de ligne : l'index du 91 écrit sur la ligne
du 95, Diesel 1 et Diesel 2 croisés, le mètre d'un autre distributeur… Les
index, eux, ne mentent pas : un compteur reprend toujours exactement là où il
s'est arrêté. Le moteur cherche donc, pour chaque ligne d'un rapport (ou
chaque relevé du journal), le mètre dont la chaîne d'index se poursuit, et
propose de remettre les index à leur place au moment de la confirmation.

Indices utilisés, du plus fort au plus faible :
  1. le début saisi reprend exactement la fin connue d'un mètre ;
  2. il la reprend à un chiffre près (faute de frappe) ;
  3. le volume de la période est habituel pour ce mètre (profil appris des
     relevés des 90 derniers jours, recalculé chaque jour) ;
  4. la mémoire des erreurs : ce pompiste (ou la station) a déjà fait cette
     erreur-là, corrigée par la direction (table erreurs_metre_apprises).
Les lignes d'un même rapport sont placées ensemble (échanges et rotations
91 ↔ 95 ↔ …), chaque mètre ne recevant qu'une ligne. Le propane ne se place
jamais sur un mètre de carburant, ni l'inverse ; changer de carburant
(gazoline ↔ diesel) n'est proposé que sur une reprise exacte de l'index.
Rien n'est déplacé sans le geste de la direction.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date as date_type
from datetime import timedelta
from statistics import median
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from models import Employe, ErreurMetreApprise, Pompe, RapportPompiste, RapportPompisteLigne, Releve
from tz_utils import today_haiti

log = logging.getLogger(__name__)
router = APIRouter()

TOL = 0.001
SEUIL = 45          # score minimal d'un autre mètre pour être proposé
MARGE = 20          # avance minimale sur le mètre choisi par le pompiste
ECART_PROCHE = 2.0  # gallons : début presque repris (lecture approximative)
FAMILLES = {"Gazoline", "Diesel", "Propane"}


# ── Outils ─────────────────────────────────────────────────────────────
def famille(p: Pompe) -> str:
    import main
    return main._famille_carburant(p.produit.nom if p.produit else "")


def activite(p: Pompe) -> str:
    return "propane" if famille(p) == "Propane" else "carburant"


def libelle(p: Pompe) -> str:
    """« Gazoline 2 · Gazoline__91 » : le nom du mètre seul est ambigu (les
    deux distributeurs ont les mêmes noms de mètres)."""
    return f"{p.produit.nom} · {p.nom}" if p.produit else p.nom


def _station(p: Pompe) -> str:
    from pompiste_routes import _norm_station
    return _norm_station(getattr(p, "station", None))


def _n(x: float, dec: int = 3) -> str:
    return f"{x:,.{dec}f}".replace(",", " ")


def faute_de_frappe(a: float, b: float) -> bool:
    """Même index à un chiffre près, ou deux chiffres voisins inversés."""
    sa, sb = f"{a:.3f}", f"{b:.3f}"
    if len(sa) != len(sb) or sa == sb:
        return False
    d = [i for i, (x, y) in enumerate(zip(sa, sb)) if x != y]
    if len(d) == 1:
        return True
    return len(d) == 2 and d[1] == d[0] + 1 and sa[d[0]] == sb[d[1]] and sa[d[1]] == sb[d[0]]


def profil(db: Session, pompe_id: int, avant: date_type) -> Optional[dict]:
    """Volume habituel du mètre (gallons par période), appris des relevés des
    90 jours précédents : se met à jour tout seul, jour après jour."""
    rows = (db.query(Releve.metter_avant, Releve.metter_apres)
            .filter(Releve.pompe_id == pompe_id, Releve.date < avant, Releve.date >= avant - timedelta(days=90))
            .order_by(Releve.date.desc()).limit(60).all())
    g = sorted(float(b) - float(a) for a, b in rows if float(b) >= float(a))
    if len(g) < 3:
        return None
    return {"mediane": round(median(g), 3), "max": round(g[-1], 3), "n": len(g)}


def _volume_anormal(g: float, prof: Optional[dict]) -> bool:
    return bool(prof) and g > max(2 * prof["max"], prof["max"] + 150)


# ── Mémoire des erreurs ────────────────────────────────────────────────
def _personne(db: Session, utilisateur_id: Optional[int] = None, employe_id: Optional[int] = None):
    """Compte de connexion et fiche employé d'un même pompiste."""
    uids, eids = set(), set()
    if utilisateur_id:
        uids.add(utilisateur_id)
        eids |= {i for (i,) in db.query(Employe.id).filter(Employe.utilisateur_id == utilisateur_id).all()}
    if employe_id:
        eids.add(employe_id)
        e = db.get(Employe, employe_id)
        if e and e.utilisateur_id:
            uids.add(e.utilisateur_id)
    return uids, eids


def habitudes(db: Session, pompe_saisie_id: int, uids: set, eids: set) -> dict:
    """{bon mètre: (fois pour ce pompiste, fois pour toute la station)}."""
    out: dict = {}
    for x in (db.query(ErreurMetreApprise)
              .filter(ErreurMetreApprise.pompe_saisie_id == pompe_saisie_id, ErreurMetreApprise.nb > 0).all()):
        perso, tous = out.get(x.pompe_correcte_id, (0, 0))
        if x.utilisateur_id in uids or x.employe_id in eids:
            perso += x.nb
        out[x.pompe_correcte_id] = (perso, tous + x.nb)
    return out


def _compter(db, utilisateur_id, employe_id, de, vers, pas, d) -> None:
    if not de or not vers or de == vers:
        return
    x = (db.query(ErreurMetreApprise)
         .filter_by(utilisateur_id=utilisateur_id, employe_id=employe_id,
                    pompe_saisie_id=de, pompe_correcte_id=vers).first())
    if x is None:
        if pas < 0:
            return
        x = ErreurMetreApprise(utilisateur_id=utilisateur_id, employe_id=employe_id,
                               pompe_saisie_id=de, pompe_correcte_id=vers, nb=0)
        db.add(x)
    x.nb = max(0, (x.nb or 0) + pas)
    x.derniere_date = d


def apprendre(db: Session, origine: int, ancien: int, nouveau: int, d: date_type,
              utilisateur_id: Optional[int] = None, employe_id: Optional[int] = None) -> None:
    """Des index saisis sur `origine` passent du mètre `ancien` au mètre
    `nouveau` : la correction précédente (origine → ancien) est annulée, la
    nouvelle (origine → nouveau) est retenue."""
    if not (utilisateur_id or employe_id) or ancien == nouveau:
        return
    _compter(db, utilisateur_id, employe_id, origine, ancien, -1, d)
    _compter(db, utilisateur_id, employe_id, origine, nouveau, +1, d)


# ── Évaluation d'un placement ──────────────────────────────────────────
def evaluer(av: float, ap: float, saisie: Pompe, cand: Pompe, prec: Optional[float], src: Optional[str],
            prof: Optional[dict], hab: dict) -> dict:
    """Score de « ces index appartiennent à ce mètre »."""
    score, raisons, exact = 0, [], False
    if prec is None:
        raisons.append("aucun index connu avant ce créneau")
    else:
        d = av - prec
        if abs(d) <= TOL:
            score, exact = 100, True
            raisons.append(f"le début {_n(av, 3)} reprend exactement la fin {_n(prec, 3)}"
                           + (f" ({src})" if src else ""))
        elif faute_de_frappe(av, prec):
            score = 55
            raisons.append(f"le début {_n(av, 3)} est la fin {_n(prec, 3)} à un chiffre près (faute de frappe ?)")
        elif abs(d) <= ECART_PROCHE:
            score = 40
            raisons.append(f"le début est à {'+' if (d) > 0 else ''}{_n(d)} gal de la fin {_n(prec, 3)}")
        else:
            raisons.append(f"le début ne suit pas la fin {_n(prec, 3)} (écart {'+' if (d) > 0 else ''}{_n(d, 3)} gal)")
    g = ap - av
    if _volume_anormal(g, prof):
        score -= 25
        raisons.append(f"{_n(g, 0)} gal : bien plus que d'habitude sur ce mètre (au plus {_n(prof['max'], 0)})")
    elif prof and score > 0:
        score += 5
        raisons.append(f"volume habituel ({_n(g, 1)} gal, d'ordinaire ~{_n(prof['mediane'], 0)})")
    autre_carburant = False
    if cand.id != saisie.id:
        if famille(cand) != famille(saisie) and {famille(cand), famille(saisie)} <= FAMILLES:
            autre_carburant = True
            score -= 40
            raisons.append(f"⚠ autre carburant ({famille(cand)} au lieu de {famille(saisie)})")
        if _station(cand) != _station(saisie):
            score -= 30
            raisons.append("autre station")
        elif cand.distributeur_id and cand.distributeur_id == saisie.distributeur_id:
            score += 5
        perso, tous = hab.get(cand.id, (0, 0))
        if perso and score > 0:
            score += min(25, 8 * perso)
            raisons.append(f"erreur déjà corrigée {perso} fois pour ce pompiste")
        elif tous and score > 0:
            score += min(10, 2 * tous)
            raisons.append(f"erreur déjà vue {tous} fois à la station")
    # Changer de carburant seulement sur une reprise exacte de l'index
    if autre_carburant and not exact:
        score = min(score, 0)
    return {"pompe_id": cand.id, "libelle": libelle(cand), "score": score, "exact": exact,
            "autre_carburant": autre_carburant, "index_precedent": prec, "raisons": raisons}


def _meilleur_placement(options: list) -> list:
    """options[i] = [(pompe_id, score)], la 1re option = rester. Maximise la
    somme des scores, un mètre par ligne ; à égalité, le moins de changements."""
    n = len(options)
    borne = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        borne[i] = borne[i + 1] + max(s for _p, s in options[i])
    meilleur = {"v": None, "plan": None}

    def dfs(i, pris, total, chang, plan):
        if i == n:
            v = (total, -chang)
            if meilleur["v"] is None or v > meilleur["v"]:
                meilleur["v"], meilleur["plan"] = v, list(plan)
            return
        if meilleur["v"] is not None and (total + borne[i], 0) < meilleur["v"]:
            return
        for k, (p, s) in enumerate(options[i]):
            if p in pris:
                continue
            pris.add(p)
            plan.append(p)
            dfs(i + 1, pris, total + s, chang + (k != 0), plan)
            plan.pop()
            pris.discard(p)

    dfs(0, set(), 0, 0, [])
    return meilleur["plan"] or [o[0][0] for o in options]


def _confiance(ev: dict) -> str:
    return "haute" if ev["exact"] and ev["score"] >= 100 and not ev.get("piste") else "moyenne"


# ── Pistes hors créneau (mauvaise date, rapport supprimé) ──────────────
# Quand un rapport est saisi au mauvais jour ou à la mauvaise période, ou que
# le rapport d'avant est dans la corbeille, le « dernier index » du créneau ne
# dit rien. On cherche alors, sur tous les mètres et quelques jours autour,
# l'index qui finit exactement là où la ligne commence (créneau d'avant) ou qui
# commence là où elle finit (créneau d'après) : journal, rapports en attente
# et corbeille.
FENETRE_JOURS = 7
_RANG = {"Matin": 0, "Apres-midi": 1}


def _per(p: str) -> str:
    return "Après-midi" if p == "Apres-midi" else p


def creneau_suivant(d: date_type, periode: str) -> tuple:
    return (d, "Apres-midi") if periode == "Matin" else (d + timedelta(days=1), "Matin")


def creneau_precedent(d: date_type, periode: str) -> tuple:
    return (d - timedelta(days=1), "Apres-midi") if periode == "Matin" else (d, "Matin")


def index_autour(db: Session, d: date_type, exclure_rapport_id: Optional[int] = None) -> list:
    """Tous les index connus de d−7 à d+7 : journal, rapports pompistes pas
    encore finalisés et rapports de la corbeille (hors `exclure_rapport_id`)."""
    from pompiste_routes import _EN_ATTENTE, _nom, _valeurs_finales
    d0, d1 = d - timedelta(days=FENETRE_JOURS), d + timedelta(days=FENETRE_JOURS)
    out, vus = [], set()
    for r in db.query(Releve).filter(Releve.date >= d0, Releve.date <= d1).all():
        if exclure_rapport_id and r.rapport_pompiste_id == exclure_rapport_id:
            continue
        av, ap = float(r.metter_avant), float(r.metter_apres)
        vus.add((r.pompe_id, r.date, r.periode, round(av, 3), round(ap, 3)))
        out.append({"pompe_id": r.pompe_id, "date": r.date, "periode": r.periode, "av": av, "ap": ap,
                    "rapport_id": r.rapport_pompiste_id, "supprime": False, "origine": "journal",
                    "source": f"journal — {_per(r.periode)} du {r.date.strftime('%d/%m')}"})
    q = (db.query(RapportPompisteLigne, RapportPompiste)
         .join(RapportPompiste, RapportPompisteLigne.rapport_id == RapportPompiste.id)
         .filter(RapportPompiste.date >= d0, RapportPompiste.date <= d1,
                 RapportPompiste.supprime_at.isnot(None) | RapportPompiste.statut.in_(_EN_ATTENTE)))
    if exclure_rapport_id:
        q = q.filter(RapportPompiste.id != exclure_rapport_id)
    for l, rp in q.all():
        av, ap = _valeurs_finales(l)
        if (l.pompe_id, rp.date, rp.periode, round(av, 3), round(ap, 3)) in vus:
            continue
        sup = rp.supprime_at is not None
        qui = _nom(rp.utilisateur)
        out.append({"pompe_id": l.pompe_id, "date": rp.date, "periode": rp.periode, "av": av, "ap": ap,
                    "rapport_id": rp.id, "supprime": sup, "origine": "corbeille" if sup else "rapport",
                    "source": f"rapport n°{rp.id}{' de ' + qui if qui else ''} ({_per(rp.periode)} du "
                              f"{rp.date.strftime('%d/%m')})" + (" — dans la corbeille" if sup else ", en attente")})
    return out


def _accord(x: float, y: float) -> Optional[str]:
    if abs(x - y) <= TOL:
        return "exact"
    if abs(x - y) <= 0.01 or faute_de_frappe(x, y):
        return "approx"
    return None


def pistes_ligne(av: float, ap: float, idx: list, pompes: dict, activ: str) -> list:
    """Index connus qui s'enchaînent avec cette ligne, n'importe où autour :
    `sens` = "avant" (finit où la ligne commence) ou "apres" (commence où elle
    finit). Les reprises exactes d'abord."""
    out = []
    for x in idx:
        p = pompes.get(x["pompe_id"])
        if p is None or activite(p) != activ:
            continue
        for sens, a, b in (("avant", av, x["ap"]), ("apres", ap, x["av"])):
            acc = _accord(a, b)
            if acc:
                out.append(dict(x, sens=sens, accord=acc, valeur=b, libelle=libelle(p)))
    out.sort(key=lambda x: (x["accord"] != "exact", x["sens"] != "avant", x["supprime"]))
    return out


def _cle(d: date_type, periode: str) -> tuple:
    return (d, _RANG.get(periode, 9))


def diagnostic_dates(rap: RapportPompiste, lignes_pistes: list) -> dict:
    """Ce que les pistes disent de la date du rapport : rapports de la
    corbeille à restaurer, rapports dans le désordre, créneau probable."""
    ici = _cle(rap.date, rap.periode)
    messages, a_restaurer, creneaux = [], {}, []
    for lib, pistes in lignes_pistes:
        # Une reprise exacte quelque part : les « à un chiffre près » ne comptent plus
        if any(p["accord"] == "exact" for p in pistes):
            pistes = [p for p in pistes if p["accord"] == "exact"]
        av_ = next((p for p in pistes if p["sens"] == "avant"), None)
        ap_ = next((p for p in pistes if p["sens"] == "apres"), None)
        for p in (av_, ap_):
            if p and p["supprime"]:
                a_restaurer[p["rapport_id"]] = p["source"]
        if av_:
            c = _cle(av_["date"], av_["periode"])
            if c >= ici:
                messages.append(f"{lib} : le début reprend la fin du {av_['source']}, qui est "
                                + ("au même créneau" if c == ici else "à un créneau plus tard")
                                + " que ce rapport : l'un des deux n'est pas à la bonne date.")
            if not av_["supprime"]:
                creneaux.append(creneau_suivant(av_["date"], av_["periode"]))
        if ap_:
            c = _cle(ap_["date"], ap_["periode"])
            if c <= ici:
                messages.append(f"{lib} : la fin est le début du {ap_['source']}, qui est "
                                + ("au même créneau" if c == ici else "à un créneau plus tôt")
                                + " que ce rapport : l'un des deux n'est pas à la bonne date.")
            if not ap_["supprime"]:
                creneaux.append(creneau_precedent(ap_["date"], ap_["periode"]))
    for rid, src in sorted(a_restaurer.items()):
        messages.append(f"Les index s'enchaînent avec le {src} : restaurez-le (« 🗑 Supprimés ») "
                        "et vérifiez sa date, sinon il manque ses gallons entre deux relevés.")
    probable = None
    if creneaux and len(set(creneaux)) == 1 and creneaux[0] != (rap.date, rap.periode):
        d, p = creneaux[0]
        probable = {"date": str(d), "periode": p,
                    "libelle": f"{_per(p)} du {d.strftime('%d/%m/%Y')}"}
        messages.insert(0, f"Ce rapport est probablement celui du {probable['libelle']} : "
                           "ses index s'y enchaînent.")
    return {"messages": messages, "a_restaurer": sorted(a_restaurer), "creneau_probable": probable}


# ── Rapport pompiste ───────────────────────────────────────────────────
def _pompes_prises(db: Session, rap: RapportPompiste) -> dict:
    """Mètres déjà rapportés sur ce créneau ailleurs (autre rapport, ou
    saisie du module Saisie) : on ne peut pas y remettre des index."""
    from pompiste_routes import _ACTIFS, _nom
    prises = {}
    for lp, rp in (db.query(RapportPompisteLigne, RapportPompiste)
                   .join(RapportPompiste, RapportPompisteLigne.rapport_id == RapportPompiste.id)
                   .filter(RapportPompiste.date == rap.date, RapportPompiste.periode == rap.periode,
                           RapportPompiste.statut.in_(_ACTIFS), RapportPompiste.supprime_at.is_(None),
                           RapportPompiste.id != rap.id).all()):
        prises[lp.pompe_id] = f"rapport n°{rp.id} ({_nom(rp.utilisateur)})"
    for r in db.query(Releve).filter(Releve.date == rap.date, Releve.periode == rap.periode).all():
        if r.rapport_pompiste_id != rap.id and r.pompe_id not in prises:
            prises[r.pompe_id] = "saisie du module Saisie"
    return prises


def plan_rapport(db: Session, rap: RapportPompiste) -> dict:
    """Place toutes les lignes du rapport ensemble et rend les propositions."""
    from pompiste_routes import _index_precedent, _valeurs_finales
    lignes = list(rap.lignes)
    if not lignes:
        return {"propositions": [], "plan": [], "sans_place": []}
    prises = _pompes_prises(db, rap)
    dans = {l.pompe_id for l in lignes}
    pompes = [p for p in db.query(Pompe).filter(Pompe.actif.is_(True)).all()
              if p.id in dans or p.id not in prises]
    cache_prec, cache_prof = {}, {}

    def prec_de(p):
        if p.id not in cache_prec:
            cache_prec[p.id] = _index_precedent(db, p.id, rap.date, rap.periode, exclure_rapport_id=rap.id)
        return cache_prec[p.id]

    def prof_de(p):
        if p.id not in cache_prof:
            cache_prof[p.id] = profil(db, p.id, rap.date)
        return cache_prof[p.id]

    uids, eids = _personne(db, utilisateur_id=rap.utilisateur_id)
    idx = index_autour(db, rap.date, exclure_rapport_id=rap.id)
    tous = {p.id: p for p in db.query(Pompe).filter(Pompe.actif.is_(True)).all()}
    options, evals, pistes_de = [], [], {}

    def eval_pistes(av, ap, saisie, p, e, pistes, hab):
        """Le créneau ne dit rien (mauvaise date, rapport d'avant dans la
        corbeille) : la fin d'un autre créneau proche sur ce mètre peut le dire."""
        if e["exact"]:
            return e
        for x in pistes:
            if x["sens"] != "avant" or x["pompe_id"] != p.id:
                continue
            alt = evaluer(av, ap, saisie, p, x["valeur"], x["source"], prof_de(p), hab)
            alt["score"] -= 5   # un peu moins sûr que la reprise du créneau même
            alt["piste"] = {k: x[k] for k in ("source", "rapport_id", "supprime", "accord")}
            if alt["score"] > e["score"]:
                e = alt
        return e

    for l in lignes:
        av, ap = _valeurs_finales(l)
        saisie = l.pompe
        hab = habitudes(db, l.pompe_saisie_id or l.pompe_id, uids, eids)
        p0, s0 = prec_de(saisie)
        reste = evaluer(av, ap, saisie, saisie, p0, s0, prof_de(saisie), hab)
        pistes = [] if reste["exact"] else pistes_ligne(av, ap, idx, tous, activite(saisie))
        pistes_de[l.pompe_id] = pistes
        # La comparaison tient compte d'une piste sur le mètre choisi (pour ne
        # pas déplacer des index qui y sont bien), mais la ligne reste signalée.
        cmp = eval_pistes(av, ap, saisie, saisie, reste, pistes, hab)
        ev = {saisie.id: reste}
        opts = [(saisie.id, cmp["score"])]
        if not (cmp["exact"] and not _volume_anormal(ap - av, prof_de(saisie))):
            autres = []
            for p in pompes:
                if p.id == saisie.id or activite(p) != activite(saisie):
                    continue
                pr, src = prec_de(p)
                e = eval_pistes(av, ap, saisie, p, evaluer(av, ap, saisie, p, pr, src, prof_de(p), hab), pistes, hab)
                # Une reprise exacte ailleurs l'emporte sur une reprise
                # approximative (faute de frappe) sur le mètre choisi
                if e["score"] >= SEUIL and (e["score"] >= cmp["score"] + MARGE
                                            or (e["exact"] and not cmp["exact"] and e["score"] > cmp["score"])):
                    autres.append(e)
            autres.sort(key=lambda e: -e["score"])
            for e in autres[:3]:
                ev[e["pompe_id"]] = e
                opts.append((e["pompe_id"], e["score"]))
        options.append(opts)
        evals.append(ev)
    choix = _meilleur_placement(options)
    propositions, plan, sans_place = [], [], []
    vers_de = {l.pompe_id: c for l, c in zip(lignes, choix)}
    for l, c, ev in zip(lignes, choix, evals):
        av, ap = _valeurs_finales(l)
        reste = ev[l.pompe_id]
        if c != l.pompe_id:
            e = ev[c]
            cible = db.get(Pompe, c)
            echange = c in dans and vers_de.get(c) == l.pompe_id
            propositions.append({
                "pompe_id": l.pompe_id, "de": libelle(l.pompe), "vers_pompe_id": c, "vers": libelle(cible),
                "vers_pompe_nom": cible.nom, "confiance": _confiance(e), "score": e["score"],
                "autre_carburant": e["autre_carburant"], "echange": echange,
                "raisons": e["raisons"], "raisons_actuel": reste["raisons"],
                "metter_avant": av, "metter_apres": ap, "piste": e.get("piste"),
            })
            plan.append({"pompe_id": l.pompe_id, "vers": c})
        elif not reste["exact"] and reste["index_precedent"] is not None:
            sans_place.append({"pompe_id": l.pompe_id, "libelle": libelle(l.pompe), "raisons": reste["raisons"],
                               "pistes": [_piste_json(x) for x in pistes_de.get(l.pompe_id, [])[:4]]})
    # La date : ce que les index trouvés ailleurs disent du créneau du rapport
    dates = diagnostic_dates(rap, [(libelle(l.pompe), pistes_de.get(l.pompe_id) or [])
                                   for l, e in zip(lignes, evals) if not e[l.pompe_id]["exact"]])
    return {"propositions": propositions, "plan": plan, "sans_place": sans_place, "dates": dates}


def _piste_json(x: dict) -> dict:
    return {"pompe_id": x["pompe_id"], "libelle": x["libelle"], "sens": x["sens"], "accord": x["accord"],
            "valeur": x["valeur"], "source": x["source"], "rapport_id": x["rapport_id"],
            "supprime": x["supprime"], "date": str(x["date"]), "periode": x["periode"]}


def appliquer_plan(db: Session, rap: RapportPompiste, mapping: dict) -> list:
    """Remet les index des lignes sur leurs nouveaux mètres (déplacements,
    échanges, rotations). Les lignes touchées sortent du journal et sont à
    reconfirmer ; la pompe choisie par le pompiste reste mémorisée et
    l'erreur est apprise. Rend la trace des changements."""
    from pompiste_routes import _prix, _retirer_ligne_du_journal
    lignes = {l.pompe_id: l for l in rap.lignes}
    mapping = {int(k): int(v) for k, v in mapping.items() if int(k) != int(v)}
    if not mapping:
        raise HTTPException(400, "Aucun changement de mètre à appliquer.")
    for k in mapping:
        if k not in lignes:
            raise HTTPException(404, f"Mètre {k} absent du rapport.")
    final = [mapping.get(pid, pid) for pid in lignes]
    if len(set(final)) != len(final):
        raise HTTPException(409, "Deux lignes iraient sur le même mètre : déplacez aussi la ligne qui l'occupe.")
    prises = _pompes_prises(db, rap)
    cibles = {}
    for src, dst in mapping.items():
        p = db.get(Pompe, dst)
        if not p or not p.actif:
            raise HTTPException(404, "Mètre introuvable ou inactif.")
        if activite(p) != activite(lignes[src].pompe):
            raise HTTPException(400, "Le propane et le carburant ne se mélangent jamais.")
        if dst not in lignes and dst in prises:
            raise HTTPException(409, f"{libelle(p)} est déjà rapporté sur ce créneau ({prises[dst]}).")
        cibles[dst] = p
    # Valeurs à replacer, avant toute modification
    cles = ("metter_avant", "metter_apres", "metter_avant_corrige", "metter_apres_corrige")
    snap = {src: ({k: getattr(lignes[src], k) for k in cles}, lignes[src].pompe_saisie_id or src)
            for src in mapping}
    trace = [{"de": libelle(lignes[s].pompe), "vers": libelle(cibles[d]),
              "index": [float(snap[s][0]["metter_avant_corrige"] if snap[s][0]["metter_avant_corrige"] is not None
                              else snap[s][0]["metter_avant"]),
                        float(snap[s][0]["metter_apres_corrige"] if snap[s][0]["metter_apres_corrige"] is not None
                              else snap[s][0]["metter_apres"])]}
             for s, d in mapping.items()]
    for src in mapping:
        _retirer_ligne_du_journal(db, rap, lignes[src])
    # Lignes hôtes : celles déjà sur un mètre cible ; les lignes libérées
    # passent sur les mètres cibles qui ne sont pas encore dans le rapport.
    hotes = {dst: lignes[dst] for dst in mapping.values() if dst in lignes}
    liberees = [lignes[src] for src in mapping if src not in mapping.values()]
    neuves = [dst for dst in mapping.values() if dst not in lignes]
    for l, dst in zip(liberees, neuves):
        l.pompe, l.pompe_id = cibles[dst], dst
        l.prix_gallon = _prix(db, cibles[dst], rap.date)
        hotes[dst] = l
    db.flush()
    for src, dst in mapping.items():
        h = hotes[dst]
        vals, origine = snap[src]
        for k in cles:
            setattr(h, k, vals[k])
        h.pompe_saisie_id = None if origine == dst else origine
        h.confirmee, h.confirmee_at = False, None
        apprendre(db, origine, src, dst, rap.date, utilisateur_id=rap.utilisateur_id)
    rap.ecart_confirme = False
    from pompiste_routes import _recalculer_attendu
    db.flush()
    _recalculer_attendu(rap)
    return trace


# ── Journal Meters ─────────────────────────────────────────────────────
def annoter_journal(db: Session, entries: list) -> None:
    """Saut ou recul d'un relevé dont le début reprend la fin d'un AUTRE
    mètre du même créneau : index sans doute écrits sur la mauvaise ligne.
    Ajoute `metre_probable` (et `echange` si les deux mètres sont croisés)."""
    import main
    par_creneau = defaultdict(list)
    for e in entries:
        par_creneau[(e["date"], e["periode"])].append(e)
    memo = defaultdict(int)
    try:
        for x in db.query(ErreurMetreApprise).filter(ErreurMetreApprise.nb > 0).all():
            memo[(x.pompe_saisie_id, x.pompe_correcte_id)] += x.nb
    except Exception:  # table pas encore créée sur une vieille base
        db.rollback()
    fam = lambda e: main._famille_carburant(e.get("produit_nom"))
    act = lambda e: "propane" if fam(e) == "Propane" else "carburant"
    for lst in par_creneau.values():
        for e in lst:
            if e.get("type_anomalie") not in ("saut_compteur", "recul_compteur") or e.get("statut") == "resolu":
                continue
            best = None
            for o in lst:
                if o is e or o.get("metter_attendu") is None or act(o) != act(e):
                    continue
                exact = abs(o["metter_attendu"] - e["metter_avant"]) <= TOL
                if not (exact or faute_de_frappe(e["metter_avant"], o["metter_attendu"])):
                    continue
                autre = fam(o) != fam(e) and {fam(o), fam(e)} <= FAMILLES
                if autre and not exact:
                    continue
                echange = (e.get("metter_attendu") is not None
                           and abs(o["metter_avant"] - e["metter_attendu"]) <= TOL)
                vu = memo.get((e["pompe_id"], o["pompe_id"]), 0)
                score = (100 if exact else 55) + (20 if echange else 0) - (40 if autre else 0) + min(10, 2 * vu)
                if best is None or score > best[0]:
                    best = (score, o, exact, echange, autre, vu)
            if not best:
                continue
            score, o, exact, echange, autre, vu = best
            e["metre_probable"] = {
                "pompe_id": o["pompe_id"], "pompe_nom": o["pompe_nom"], "produit_nom": o["produit_nom"],
                "libelle": f"{o['produit_nom']} · {o['pompe_nom']}", "releve_id": o.get("id"),
                "exact": exact, "echange": echange, "autre_carburant": autre, "deja_vu": vu,
                "confiance": "haute" if exact else "moyenne",
            }
            ou = f"{o['produit_nom']} · {o['pompe_nom']}"
            e["recommandation"] = (
                f"Le début {_n(e['metter_avant'], 3)} reprend {'exactement' if exact else 'à un chiffre près'} "
                f"la fin de {ou} ({_n(o['metter_attendu'], 3)}) : ces index sont sans doute écrits sur la "
                f"mauvaise ligne" + (f", croisés avec {ou}" if echange else "") + ". "
                + ("⚠ Autre carburant : vérifiez la feuille avant de déplacer. " if autre else "")
                + f"Utilisez « ⇄ Mettre sur {ou} »."
            )


class DeplacerReleveIn(BaseModel):
    pompe_id: int
    motif: str


def _ecart_au_creneau(db: Session, r: Releve, motif: str) -> None:
    import main
    prec = main._releve_precedent(db, r.pompe_id, r.date, r.periode)
    base = float(prec.metter_apres) if prec else main.base_depart(r.pompe, r.date)
    if base is None or abs(float(r.metter_avant) - base) <= main.TOLERANCE_ECART_COMPTEUR:
        r.ecart_compteur, r.motif_ecart_compteur = None, None
    else:
        r.ecart_compteur = round(float(r.metter_avant) - base, 4)
        r.motif_ecart_compteur = (r.motif_ecart_compteur or motif)[:255]


@router.post("/api/releves/{releve_id}/deplacer")
def deplacer_releve(releve_id: int, data: DeplacerReleveIn, request: Request, db: Session = Depends(get_db)):
    """Journal Meters : index écrits sur la mauvaise ligne. Le relevé passe
    sur le bon mètre ; si ce mètre a déjà un relevé sur le créneau, les index
    (et le pompiste qui les a lus) sont échangés. Le prix suit le mètre.
    L'erreur est retenue pour le pompiste du relevé."""
    import main
    from models import ResolutionMetter
    from pompiste_routes import _A_TRAITER, _require_superviseur
    _require_superviseur(request, db)
    motif = " ".join((data.motif or "").split())
    if len(motif) < 5:
        raise HTTPException(400, "Indiquez pourquoi vous changez de mètre (5 caractères minimum).")
    r = db.get(Releve, releve_id)
    if not r:
        raise HTTPException(404, "Relevé introuvable.")
    neuve = db.get(Pompe, data.pompe_id)
    if not neuve or not neuve.actif:
        raise HTTPException(404, "Mètre introuvable ou inactif.")
    if neuve.id == r.pompe_id:
        raise HTTPException(400, "C'est déjà ce mètre.")
    ancienne = r.pompe
    if activite(neuve) != activite(ancienne):
        raise HTTPException(400, "Le propane et le carburant ne se mélangent jamais.")

    def _bloque(x):
        rp = x.rapport_pompiste
        if rp is not None and rp.statut in _A_TRAITER + ("EN_COURS",) and rp.supprime_at is None:
            raise HTTPException(409, f"Relevé du rapport pompiste n°{rp.id} pas encore finalisé : "
                                     "changez le mètre dans Rapports pompistes.")
    _bloque(r)
    autre = db.query(Releve).filter_by(date=r.date, periode=r.periode, pompe_id=neuve.id).first()
    if autre:
        _bloque(autre)
    trace = {"releve_id": r.id, "date": str(r.date), "periode": r.periode, "motif": motif[:300],
             "de": libelle(ancienne), "vers": libelle(neuve), "echange": autre is not None,
             "index": [float(r.metter_avant), float(r.metter_apres)],
             "index_autre": [float(autre.metter_avant), float(autre.metter_apres)] if autre else None}
    u = getattr(request.state, "user", None)
    touches = [r] + ([autre] if autre else [])
    db.query(ResolutionMetter).filter(ResolutionMetter.releve_id.in_([x.id for x in touches])).delete(
        synchronize_session=False)
    emp_r, emp_a = r.pompiste_id, (autre.pompiste_id if autre else None)
    # Ce que le pompiste avait écrit suit les index : mètre d'origine et index
    # d'origine sont conservés (registre des erreurs de mètre)
    cles = ("metter_avant", "metter_apres", "pompiste_id", "metter_avant_saisi", "metter_apres_saisi")
    origine_r = r.pompe_saisie_id or r.pompe_id
    if autre:
        origine_a = autre.pompe_saisie_id or autre.pompe_id
        for k in cles:
            va, vb = getattr(r, k), getattr(autre, k)
            setattr(r, k, vb)
            setattr(autre, k, va)
        r.pompe_saisie_id = None if origine_a == r.pompe_id else origine_a
        autre.pompe_saisie_id = None if origine_r == autre.pompe_id else origine_r
    else:
        r.pompe, r.pompe_id = neuve, neuve.id
        r.pompe_saisie_id = None if origine_r == neuve.id else origine_r
        pv = main._prix_vente_a_date(db, neuve.produit_id, r.date)
        r.prix_gallon = float(pv) if pv is not None else float(neuve.produit.prix_gallon or 0)
    for x in touches:
        x.nb_modifications = (x.nb_modifications or 0) + 1
        if u is not None:
            x.modifie_par_id = u.id
    db.flush()
    for x in touches:
        _ecart_au_creneau(db, x, motif)
    apprendre(db, origine_r, ancienne.id, neuve.id, r.date, employe_id=emp_r)
    if autre:
        apprendre(db, origine_a, neuve.id, ancienne.id, r.date, employe_id=emp_a)
    main._tracer(db, request, "releve_metre_change", trace)
    db.commit()
    avertissements = []
    for pid in {ancienne.id, neuve.id}:
        m = main._reprendre_suivant(db, pid, r.date, r.periode)
        if m:
            avertissements.append(m)
    db.commit()
    log.info("Relevé #%s : %s -> %s", r.id, trace["de"], trace["vers"])
    return {"id": r.id, "echange": autre is not None, "avertissements": avertissements}


@router.get("/api/placement-metres/memoire")
def memoire(request: Request, db: Session = Depends(get_db)):
    """Ce que le système a appris : erreurs de mètre par pompiste."""
    from pompiste_routes import _require_superviseur, _nom
    from models import Utilisateur
    _require_superviseur(request, db)
    out = []
    for x in (db.query(ErreurMetreApprise).filter(ErreurMetreApprise.nb > 0)
              .order_by(ErreurMetreApprise.nb.desc()).limit(300).all()):
        de, vers = db.get(Pompe, x.pompe_saisie_id), db.get(Pompe, x.pompe_correcte_id)
        if not de or not vers:
            continue
        qui = None
        if x.employe_id:
            e = db.get(Employe, x.employe_id)
            qui = f"{e.prenom} {e.nom}" if e else None
        elif x.utilisateur_id:
            qui = _nom(db.get(Utilisateur, x.utilisateur_id))
        out.append({"pompiste": qui, "saisi_sur": libelle(de), "bon_metre": libelle(vers), "fois": x.nb,
                    "derniere_date": str(x.derniere_date) if x.derniere_date else None})
    return {"date": str(today_haiti()), "erreurs": out}


# ── Registre des erreurs de mètre ──────────────────────────────────────
TYPES_ERREUR = {
    "mauvais_metre": "Index sur le mauvais mètre",
    "index_corrige": "Index corrigé",
    "ecart_compteur": "Écart de compteur non expliqué",
}


def _ligne_saisie_rapport(db: Session, e: dict) -> Optional[dict]:
    """Rapports finalisés avant la conservation sur le relevé : la saisie
    d'origine est lue sur la ligne du rapport."""
    rid = e.get("rapport_pompiste_id")
    if not rid or e.get("rapport_statut") not in ("CONFIRME", "RESOLU"):
        return None
    l = db.query(RapportPompisteLigne).filter_by(rapport_id=rid, pompe_id=e["pompe_id"]).first()
    if l is None:
        return None
    corrige = l.metter_avant_corrige is not None or l.metter_apres_corrige is not None
    if not l.pompe_saisie_id and not corrige:
        return None
    p = l.pompe_saisie
    av, ap = float(l.metter_avant), float(l.metter_apres)
    return {"pompe_id": l.pompe_saisie_id, "pompe_nom": p.nom if p else None,
            "produit_nom": p.produit.nom if p and p.produit else None,
            "mauvais_metre": bool(l.pompe_saisie_id), "index_corriges": corrige,
            "metter_avant": av, "metter_apres": ap, "gallons": round(ap - av, 4)}


def registre_erreurs(db: Session, d_debut: date_type, d_fin: date_type, pompiste_ids: Optional[list] = None) -> dict:
    """Toutes les erreurs de mètre de la période, sans rien perdre de ce que
    le pompiste avait écrit : corrections faites (mauvais mètre, index
    corrigés) et écarts encore non expliqués, avec leur poids en gallons et
    en gourdes ; statistiques par pompiste."""
    import main
    entries = main._build_journal_entries(db, d_debut, d_fin, inclure_en_attente=True,
                                          pompiste_id_filter=pompiste_ids or None)
    lignes, stats = [], {}

    def st(nom):
        return stats.setdefault(nom or "Non attribué", {
            "pompiste": nom or "Non attribué", "releves": 0, "gallons": {}, "erreurs": 0,
            "mauvais_metre": 0, "index_corrige": 0, "ecart_compteur": 0, "ecart_gallons": {}})

    def ajoute(dct, fam, g):      # gallons toujours séparés par carburant
        dct[fam] = dct.get(fam, 0.0) + g

    for e in entries:
        s_p = st(e.get("pompiste_nom"))
        s_p["releves"] += 1
        ajoute(s_p["gallons"], main._famille_carburant(e["produit_nom"]), e.get("quantite") or 0)
        retenu = {"libelle": f"{e['produit_nom']} · {e['pompe_nom']}", "metter_avant": e["metter_avant"],
                  "metter_apres": e["metter_apres"], "gallons": e["quantite"]}
        base = {"releve_id": e["id"] if isinstance(e["id"], int) else None, "date": e["date"],
                "periode": e["periode"], "pompiste": e.get("pompiste_nom") or "Non attribué",
                "pompiste_id": e.get("pompiste_id"), "produit_nom": e["produit_nom"],
                "famille": main._famille_carburant(e["produit_nom"]), "en_attente": e.get("en_attente", False),
                "rapport_pompiste_id": e.get("rapport_pompiste_id"), "retenu": retenu}
        sp = None if e.get("en_attente") else (e.get("saisie_pompiste") or _ligne_saisie_rapport(db, e))
        if sp:
            types = (["mauvais_metre"] if sp["mauvais_metre"] else []) + (["index_corrige"] if sp["index_corriges"] else [])
            lib = (f"{sp['produit_nom']} · {sp['pompe_nom']}" if sp.get("pompe_nom") else retenu["libelle"])
            d_gal = round((e["quantite"] or 0) - sp["gallons"], 4)
            autre_carb = bool(sp.get("produit_nom")) and main._famille_carburant(sp["produit_nom"]) != base["famille"]
            lignes.append(dict(base, types=types, statut="corrige",
                               saisi={"libelle": lib, "metter_avant": sp["metter_avant"],
                                      "metter_apres": sp["metter_apres"], "gallons": sp["gallons"]},
                               ecart_gallons=d_gal, ecart_gourdes=round(d_gal * e["prix_gallon"], 2),
                               autre_carburant=autre_carb))
            s_p["erreurs"] += 1
            for t in types:
                s_p[t] += 1
            ajoute(s_p["ecart_gallons"], base["famille"], abs(d_gal))
        if e.get("type_anomalie") in ("saut_compteur", "recul_compteur") and e.get("statut") != "resolu":
            g = abs(e.get("decalage") or 0)
            lignes.append(dict(base, types=["ecart_compteur"], statut="a_verifier",
                               saisi={"libelle": retenu["libelle"], "metter_avant": e["metter_avant"],
                                      "metter_apres": e["metter_apres"], "gallons": e["quantite"]},
                               metter_attendu=e.get("metter_attendu"), decalage=e.get("decalage"),
                               ecart_gallons=round(g, 4), ecart_gourdes=round(g * e["prix_gallon"], 2),
                               metre_probable=e.get("metre_probable"), autre_carburant=False))
            s_p["erreurs"] += 1
            s_p["ecart_compteur"] += 1
            ajoute(s_p["ecart_gallons"], base["famille"], g)
    for x in stats.values():
        x["taux_erreur"] = round(100 * x["erreurs"] / x["releves"], 1) if x["releves"] else 0.0
        x["gallons"] = {k: round(v, 3) for k, v in x["gallons"].items()}
        x["ecart_gallons"] = {k: round(v, 3) for k, v in x["ecart_gallons"].items() if v}
    lignes.sort(key=lambda x: (x["date"], 0 if x["periode"] == "Matin" else 1, x["retenu"]["libelle"]))
    return {"date_debut": str(d_debut), "date_fin": str(d_fin), "types": TYPES_ERREUR,
            "total": {"erreurs": len(lignes), "corrigees": sum(x["statut"] == "corrige" for x in lignes),
                      "a_verifier": sum(x["statut"] == "a_verifier" for x in lignes)},
            "par_pompiste": sorted(stats.values(), key=lambda x: (-x["erreurs"], x["pompiste"])),
            "erreurs": lignes}


@router.get("/api/erreurs-metre")
def erreurs_metre(request: Request, date_debut: date_type, date_fin: date_type,
                  pompiste_id: list[int] = Query(default=[]), db: Session = Depends(get_db)):
    from pompiste_routes import _require_superviseur
    _require_superviseur(request, db)
    if date_fin < date_debut or (date_fin - date_debut).days > 93:
        raise HTTPException(400, "Période de 3 mois au plus.")
    return registre_erreurs(db, date_debut, date_fin, pompiste_id)
