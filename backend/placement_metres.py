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

from fastapi import APIRouter, Depends, HTTPException, Request
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
    return "haute" if ev["exact"] and ev["score"] >= 100 else "moyenne"


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
    options, evals = [], []
    for l in lignes:
        av, ap = _valeurs_finales(l)
        saisie = l.pompe
        hab = habitudes(db, l.pompe_saisie_id or l.pompe_id, uids, eids)
        p0, s0 = prec_de(saisie)
        reste = evaluer(av, ap, saisie, saisie, p0, s0, prof_de(saisie), hab)
        ev = {saisie.id: reste}
        opts = [(saisie.id, reste["score"])]
        if not (reste["exact"] and not _volume_anormal(ap - av, prof_de(saisie))):
            autres = []
            for p in pompes:
                if p.id == saisie.id or activite(p) != activite(saisie):
                    continue
                pr, src = prec_de(p)
                e = evaluer(av, ap, saisie, p, pr, src, prof_de(p), hab)
                # Une reprise exacte ailleurs l'emporte sur une reprise
                # approximative (faute de frappe) sur le mètre choisi
                if e["score"] >= SEUIL and (e["score"] >= reste["score"] + MARGE
                                            or (e["exact"] and not reste["exact"] and e["score"] > reste["score"])):
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
                "metter_avant": av, "metter_apres": ap,
            })
            plan.append({"pompe_id": l.pompe_id, "vers": c})
        elif not reste["exact"] and reste["index_precedent"] is not None:
            sans_place.append({"pompe_id": l.pompe_id, "libelle": libelle(l.pompe), "raisons": reste["raisons"]})
    return {"propositions": propositions, "plan": plan, "sans_place": sans_place}


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
    if autre:
        for k in ("metter_avant", "metter_apres", "pompiste_id"):
            va, vb = getattr(r, k), getattr(autre, k)
            setattr(r, k, vb)
            setattr(autre, k, va)
    else:
        r.pompe, r.pompe_id = neuve, neuve.id
        pv = main._prix_vente_a_date(db, neuve.produit_id, r.date)
        r.prix_gallon = float(pv) if pv is not None else float(neuve.produit.prix_gallon or 0)
    for x in touches:
        x.nb_modifications = (x.nb_modifications or 0) + 1
        if u is not None:
            x.modifie_par_id = u.id
    db.flush()
    for x in touches:
        _ecart_au_creneau(db, x, motif)
    apprendre(db, ancienne.id, ancienne.id, neuve.id, r.date, employe_id=emp_r)
    if autre:
        apprendre(db, neuve.id, neuve.id, ancienne.id, r.date, employe_id=emp_a)
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
