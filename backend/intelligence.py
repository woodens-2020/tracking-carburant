"""
KONEKTA Intelligence Center — analyses de gestion.

Aucune donnée n'est saisie ni modifiée ici : tout se calcule à partir des
modules existants.

  Argent    : les lignes du journal de la caisse principale
              (grand_livre.construire_lignes) — mêmes règles que la caisse,
              contrôlées contre la synthèse cash de l'institution.
  Opérations: les relevés de compteurs validés (journal des compteurs) —
              gallons, produits, pompes, pompistes, créneaux.
  Stock     : stock_service (stock restant × coût moyen pondéré).

Vocabulaire (jamais confondu) :
  revenu        = ventes (carburant, Bar, Cuisine, Hôtel)
  encaissements = tout l'argent entré dans la caisse (ventes + sources +
                  renflouements + remboursements de crédit)
  charges       = dépenses, achats, salaires (y compris les dépenses payées
                  par les pompistes, hors caisse)
  résultat      = revenu − charges (base caisse : les achats de stock sont
                  comptés quand ils sont payés)
  trésorerie    = solde de la caisse principale à la date de fin
"""
from __future__ import annotations

import csv
import io
import math
import statistics
import threading
import time
from collections import defaultdict
from datetime import date as date_type, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.orm import Session

from database import definir_espace, espace_courant, get_db, retablir_espace, session_espace
from grand_livre import TYPES, construire_lignes
from models import Utilisateur
from pompiste_routes import _require_superviseur
from tz_utils import today_haiti

router = APIRouter(prefix="/api/intelligence", tags=["intelligence"])

REVENU = {"vente_carburant", "vente_bar", "vente_cuisine", "vente_hotel"}
CHARGES = {"depense", "achat", "achat_bar", "achat_cuisine", "depense_cuisine", "depense_hotel",
           "salaire", "paiement_bar", "depense_pompiste"}
FAMILLES = {
    "vente_carburant": "Ventes", "vente_bar": "Ventes", "vente_cuisine": "Ventes", "vente_hotel": "Ventes",
    "entree_source": "Autres entrées", "renflouement": "Autres entrées", "credit_bar": "Autres entrées",
    "depense": "Dépenses", "depense_cuisine": "Dépenses", "depense_hotel": "Dépenses",
    "depense_pompiste": "Dépenses",
    "achat": "Achats", "achat_bar": "Achats", "achat_cuisine": "Achats",
    "salaire": "Salaires", "paiement_bar": "Salaires",
    "transfert": "Transferts / dépôts",
}
JOURS_SEMAINE = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
MAX_JOURS = 1100          # ~3 ans par requête
_TTL = 60                 # secondes
_cache: dict = {}
_verrou = threading.Lock()


# ── Lecture des données (avec cache court) ──────────────────────────────

def _memo(cle, calcul):
    with _verrou:
        v = _cache.get(cle)
        if v and time.time() - v[0] < _TTL:
            return v[1]
    r = calcul()
    with _verrou:
        if len(_cache) > 200:
            _cache.clear()
        _cache[cle] = (time.time(), r)
    return r


def _montant(l: dict) -> float:
    """Montant positif d'une ligne (la dépense payée par un pompiste est un
    débit négatif dans le journal : c'est une charge de ce montant)."""
    if l["type"] == "depense_pompiste":
        return -l["debit"]
    return l["debit"] if l["sens"] == "D" else l["credit"]


def _lire_station(db: Session, sch, debut: date_type, fin: date_type) -> dict:
    """Lignes d'argent, relevés et solde de caisse d'une station."""
    from models import Releve, RapportPompiste
    lignes = [l for l in construire_lignes(db, debut, fin) if not l["annulee"]]
    solde_fin = round(sum(0.0 if l["annulee"] else l["debit"] - l["credit"]
                          for l in construire_lignes(db, None, fin)), 2)
    noms = {}
    releves = []
    for r in (db.query(Releve).filter(Releve.date >= debut, Releve.date <= fin).all()):
        pompe = r.pompe
        produit = pompe.produit if pompe else None
        pompiste = None
        if r.rapport_pompiste_id:
            if r.rapport_pompiste_id not in noms:
                rap = db.get(RapportPompiste, r.rapport_pompiste_id)
                u = rap.utilisateur if rap else None
                noms[r.rapport_pompiste_id] = (u.nom_complet or u.username) if u else None
            pompiste = noms[r.rapport_pompiste_id]
        releves.append({
            "date": str(r.date), "periode": r.periode,
            "produit_id": produit.id if produit else None, "produit": produit.nom if produit else "—",
            "pompe": pompe.nom if pompe else str(r.pompe_id),
            "gallons": round(float(r.quantite), 3), "montant": round(float(r.montant_vente), 2),
            "pompiste": pompiste,
            "saisi_par": (r.cree_par.nom_complet or r.cree_par.username) if r.cree_par else None,
        })
    return {"lignes": lignes, "releves": releves, "solde_fin": solde_fin}


def _valeur_stock(db: Session) -> Optional[dict]:
    """Stock carburant actuel valorisé au coût moyen pondéré (None si aucun
    coût d'achat connu : on n'invente pas de valeur)."""
    from models import Produit
    from stock_service import cout_moyen_pondere, stock_restant
    total, gallons, inconnus = 0.0, 0.0, []
    for p in db.query(Produit).filter(Produit.actif.is_(True)).all():
        if not p.pompes:
            continue
        g = float(stock_restant(db, p.id)["gallons_restants"] or 0)
        cmp_ = cout_moyen_pondere(db, p.id)
        if cmp_ is None:
            inconnus.append(p.nom)
            continue
        total += max(g, 0.0) * cmp_
        gallons += max(g, 0.0)
    if not total and inconnus:
        return None
    return {"valeur": round(total, 2), "gallons": round(gallons, 1), "sans_cout": inconnus}


class _Station:
    def __init__(self, sid: int, nom: str, schema):
        self.id, self.nom, self.schema = sid, nom, schema


def _stations(request: Request, db: Session, demande: Optional[str]) -> list:
    """Stations analysées. Par défaut : celle où l'on se trouve. Plusieurs
    stations : réservé au propriétaire (contrôle serveur)."""
    from espaces_routes import proprietaire, stations_pompiste
    courant = espace_courant()
    toutes = stations_pompiste()
    ici = next((s for s in toutes if (s["id"] == 0 and courant is None) or f"esp_{s['id']}" == courant), None)
    ici_obj = _Station(ici["id"] if ici else 0, ici["nom"] if ici else "Station", courant)
    if not demande:
        return [ici_obj]
    try:
        ids = {int(x) for x in demande.split(",") if x.strip() != ""}
    except ValueError:
        raise HTTPException(400, "Liste de stations invalide.")
    if ids <= {ici_obj.id}:
        return [ici_obj]
    if not proprietaire(request, db):
        raise HTTPException(403, "Seul le propriétaire peut analyser plusieurs stations.")
    out = [_Station(s["id"], s["nom"], None if s["id"] == 0 else f"esp_{s['id']}") for s in toutes if s["id"] in ids]
    if not out:
        raise HTTPException(400, "Aucune station valide.")
    return out


def _donnees(db: Session, stations: list, debut: date_type, fin: date_type, avec_stock: bool = False) -> dict:
    """Données brutes des stations demandées, chaque ligne marquée de sa station."""
    lignes, releves, solde, stock = [], [], 0.0, {"valeur": 0.0, "gallons": 0.0, "sans_cout": []}
    stock_connu = False
    for st in stations:
        def lire(s=st):
            if s.schema == espace_courant():
                d = _lire_station(db, s.schema, debut, fin)
                if avec_stock:
                    d["stock"] = _valeur_stock(db)
                return d
            jeton = definir_espace(s.schema)
            try:
                with session_espace(s.schema) as sdb:
                    d = _lire_station(sdb, s.schema, debut, fin)
                    if avec_stock:
                        d["stock"] = _valeur_stock(sdb)
                    return d
            finally:
                retablir_espace(jeton)
        d = _memo(("donnees", st.schema, debut, fin, avec_stock), lire)
        for l in d["lignes"]:
            lignes.append(dict(l, station=st.nom, station_id=st.id))
        for r in d["releves"]:
            releves.append(dict(r, station=st.nom, station_id=st.id))
        solde += d["solde_fin"]
        if avec_stock and d.get("stock"):
            stock_connu = True
            stock["valeur"] += d["stock"]["valeur"]
            stock["gallons"] += d["stock"]["gallons"]
            stock["sans_cout"] += d["stock"]["sans_cout"]
    return {"lignes": lignes, "releves": releves, "solde_fin": round(solde, 2),
            "stock": stock if stock_connu else None}


# ── Filtres ─────────────────────────────────────────────────────────────

def _liste(v: Optional[str]) -> Optional[set]:
    if not v:
        return None
    s = {x.strip() for x in v.split("|") if x.strip()}
    return s or None


class Filtres:
    def __init__(self, modules=None, types=None, comptes=None, produits=None, employes=None):
        self.modules, self.types, self.comptes = _liste(modules), _liste(types), _liste(comptes)
        self.produits, self.employes = _liste(produits), _liste(employes)

    def actifs(self) -> bool:
        return any([self.modules, self.types, self.comptes, self.produits, self.employes])

    def ligne(self, l: dict) -> bool:
        if self.modules and l["module"] not in self.modules:
            return False
        if self.types and l["type"] not in self.types:
            return False
        if self.comptes and (l["compte"] or "") not in self.comptes:
            return False
        if self.produits and str(l.get("produit_id")) not in self.produits:
            return False
        if self.employes and (l.get("saisi_par") or "") not in self.employes:
            return False
        return True

    def releve(self, r: dict) -> bool:
        if self.modules and "Station" not in self.modules:
            return False
        if self.types and "vente_carburant" not in self.types:
            return False
        if self.produits and str(r["produit_id"]) not in self.produits:
            return False
        if self.employes and (r["pompiste"] or "") not in self.employes and (r["saisi_par"] or "") not in self.employes:
            return False
        return True

    def resume(self) -> dict:
        return {k: sorted(v) for k, v in vars(self).items() if v}


def _periode(debut: Optional[date_type], fin: Optional[date_type]):
    fin = fin or today_haiti()
    debut = debut or (fin - timedelta(days=29))
    if debut > fin:
        raise HTTPException(400, "La date de début est après la date de fin.")
    if (fin - debut).days > MAX_JOURS:
        raise HTTPException(400, f"Période trop longue ({MAX_JOURS} jours au plus).")
    return debut, fin


def _comparaison(mode: str, debut: date_type, fin: date_type,
                 cdebut: Optional[date_type], cfin: Optional[date_type]):
    if mode == "aucune":
        return None
    if mode == "annee":
        try:
            return debut.replace(year=debut.year - 1), fin.replace(year=fin.year - 1)
        except ValueError:                                  # 29 février
            return debut - timedelta(days=365), fin - timedelta(days=365)
    if mode == "perso":
        if not (cdebut and cfin) or cdebut > cfin:
            raise HTTPException(400, "Indiquez la période de comparaison (début et fin).")
        return cdebut, cfin
    n = (fin - debut).days + 1                              # « precedent » : même durée juste avant
    return debut - timedelta(days=n), debut - timedelta(days=1)


# ── Calculs ─────────────────────────────────────────────────────────────

def _kpis(lignes: list, releves: list, debut: date_type, fin: date_type) -> dict:
    revenu = sum(_montant(l) for l in lignes if l["type"] in REVENU)
    charges = sum(_montant(l) for l in lignes if l["type"] in CHARGES)
    enc = sum(l["debit"] for l in lignes)
    dec = sum(l["credit"] for l in lignes)
    jours = (fin - debut).days + 1
    jours_ventes = len({l["date"] for l in lignes if l["type"] in REVENU and _montant(l)})
    gallons = sum(r["gallons"] for r in releves)
    return {
        "revenu": round(revenu, 2), "charges": round(charges, 2), "resultat": round(revenu - charges, 2),
        "marge": round((revenu - charges) / revenu * 100, 1) if revenu else None,
        "encaissements": round(enc, 2), "decaissements": round(dec, 2), "flux_net": round(enc - dec, 2),
        "gallons": round(gallons, 1), "nb_releves": len(releves), "nb_operations": len(lignes),
        "revenu_moyen_jour": round(revenu / jours, 2), "jours": jours, "jours_ventes": jours_ventes,
        "prix_moyen_gallon": round(sum(r["montant"] for r in releves) / gallons, 2) if gallons else None,
    }


def _variation(a, b):
    if a is None or b is None or not b:
        return None
    return round((a - b) / abs(b) * 100, 1)


def _granularite(debut: date_type, fin: date_type) -> str:
    n = (fin - debut).days + 1
    return "jour" if n <= 93 else ("semaine" if n <= 400 else "mois")


def _seau(d: date_type, g: str) -> str:
    if g == "jour":
        return str(d)
    if g == "semaine":
        return str(d - timedelta(days=d.weekday()))
    return f"{d.year}-{d.month:02d}"


def _series(lignes: list, debut: date_type, fin: date_type, g: str) -> list:
    seaux: dict = {}
    d = debut
    while d <= fin:
        seaux.setdefault(_seau(d, g), {"cle": _seau(d, g), "revenu": 0.0, "charges": 0.0,
                                       "encaissements": 0.0, "decaissements": 0.0, "nb": 0})
        d += timedelta(days=1)
    for l in lignes:
        s = seaux.get(_seau(date_type.fromisoformat(l["date"]), g))
        if not s:
            continue
        m = _montant(l)
        if l["type"] in REVENU:
            s["revenu"] += m
        if l["type"] in CHARGES:
            s["charges"] += m
        s["encaissements"] += l["debit"]
        s["decaissements"] += l["credit"]
        s["nb"] += 1
    out = list(seaux.values())
    for s in out:
        for k in ("revenu", "charges", "encaissements", "decaissements"):
            s[k] = round(s[k], 2)
        s["resultat"] = round(s["revenu"] - s["charges"], 2)
    return out


def _grouper(items, cle, valeurs) -> list:
    out: dict = {}
    for it in items:
        k = cle(it)
        if k is None:
            continue
        o = out.setdefault(k, {"nom": k, **{v: 0.0 for v in valeurs}, "nb": 0})
        for v, f in valeurs.items():
            o[v] += f(it)
        o["nb"] += 1
    for o in out.values():
        for v in valeurs:
            o[v] = round(o[v], 2)
    return list(out.values())


def _analyse_complete(lignes, releves, prev_l, prev_r, debut, fin, comp) -> dict:
    g = _granularite(debut, fin)
    k = _kpis(lignes, releves, debut, fin)
    kp = _kpis(prev_l, prev_r, comp[0], comp[1]) if comp else None
    variations = {c: _variation(k[c], kp[c]) for c in k if kp is not None and isinstance(k[c], (int, float))} if kp else {}

    rev = lambda l: _montant(l) if l["type"] in REVENU else 0.0
    chg = lambda l: _montant(l) if l["type"] in CHARGES else 0.0
    departements = _grouper(lignes, lambda l: l["module"], {
        "revenu": rev, "charges": chg, "encaissements": lambda l: l["debit"], "decaissements": lambda l: l["credit"]})
    prev_dep = {d["nom"]: d for d in _grouper(prev_l, lambda l: l["module"], {"revenu": rev, "charges": chg})}
    for d in departements:
        d["resultat"] = round(d["revenu"] - d["charges"], 2)
        p = prev_dep.get(d["nom"])
        d["revenu_prec"] = p["revenu"] if p else (0.0 if comp else None)
        d["variation"] = _variation(d["revenu"], d["revenu_prec"])
    departements.sort(key=lambda d: -(d["revenu"] + d["charges"]))

    charges_cat = _grouper([l for l in lignes if l["type"] in CHARGES], lambda l: l["compte"] or "Sans catégorie",
                           {"montant": _montant})
    prev_cat = {c["nom"]: c["montant"] for c in _grouper([l for l in prev_l if l["type"] in CHARGES],
                                                         lambda l: l["compte"] or "Sans catégorie", {"montant": _montant})}
    for c in charges_cat:
        c["prec"] = prev_cat.get(c["nom"], 0.0) if comp else None
        c["variation"] = _variation(c["montant"], c["prec"])
    charges_cat.sort(key=lambda c: -c["montant"])

    # Composition : famille → compte (entrées et sorties)
    compo: dict = {}
    for l in lignes:
        fam = FAMILLES.get(l["type"], TYPES[l["type"]][0])
        sens = "entrees" if l["sens"] == "D" and l["type"] != "depense_pompiste" else "sorties"
        f = compo.setdefault((sens, fam), {"sens": sens, "famille": fam, "montant": 0.0, "comptes": defaultdict(float)})
        f["montant"] += _montant(l)
        f["comptes"][l["compte"] or "—"] += _montant(l)
    composition = [{"sens": f["sens"], "famille": f["famille"], "montant": round(f["montant"], 2),
                    "comptes": sorted(({"nom": n, "montant": round(m, 2)} for n, m in f["comptes"].items()),
                                      key=lambda x: -x["montant"])}
                   for f in compo.values() if round(f["montant"], 2)]
    composition.sort(key=lambda f: (f["sens"], -f["montant"]))

    produits = _grouper(releves, lambda r: r["produit"], {"gallons": lambda r: r["gallons"], "montant": lambda r: r["montant"]})
    prev_prod = {p["nom"]: p for p in _grouper(prev_r, lambda r: r["produit"], {"gallons": lambda r: r["gallons"], "montant": lambda r: r["montant"]})}
    ids = {r["produit"]: r["produit_id"] for r in releves}
    for p in produits:
        p["id"] = ids.get(p["nom"])
        q = prev_prod.get(p["nom"])
        p["montant_prec"] = q["montant"] if q else (0.0 if comp else None)
        p["gallons_prec"] = q["gallons"] if q else (0.0 if comp else None)
        p["variation"] = _variation(p["montant"], p["montant_prec"])
        p["series"] = []
    produits.sort(key=lambda p: -p["montant"])
    par_prod_jour: dict = defaultdict(lambda: defaultdict(float))
    for r in releves:
        par_prod_jour[r["produit"]][_seau(date_type.fromisoformat(r["date"]), g)] += r["gallons"]

    pompistes = _grouper(releves, lambda r: r["pompiste"] or "Saisie au bureau",
                         {"gallons": lambda r: r["gallons"], "montant": lambda r: r["montant"]})
    jours_p = defaultdict(set)
    for r in releves:
        jours_p[r["pompiste"] or "Saisie au bureau"].add(r["date"])
    for p in pompistes:
        p["jours"] = len(jours_p[p["nom"]])
    pompistes.sort(key=lambda p: -p["montant"])
    pompes = _grouper(releves, lambda r: f"{r['pompe']}" + (f" · {r['station']}" if len({x['station'] for x in releves}) > 1 else ""),
                      {"gallons": lambda r: r["gallons"], "montant": lambda r: r["montant"]})
    pompes.sort(key=lambda p: -p["gallons"])
    stations = _grouper(lignes, lambda l: l["station"], {"revenu": rev, "charges": chg,
                                                          "encaissements": lambda l: l["debit"], "decaissements": lambda l: l["credit"]})
    for s in stations:
        s["resultat"] = round(s["revenu"] - s["charges"], 2)
    gal_st = defaultdict(float)
    for r in releves:
        gal_st[r["station"]] += r["gallons"]
    for s in stations:
        s["gallons"] = round(gal_st.get(s["nom"], 0.0), 1)

    # Activité : jour de la semaine × créneau (ventes carburant, en gourdes)
    carte = [[0.0, 0.0] for _ in range(7)]
    nb_jours_semaine = [0] * 7
    d = debut
    while d <= fin:
        nb_jours_semaine[d.weekday()] += 1
        d += timedelta(days=1)
    for r in releves:
        carte[date_type.fromisoformat(r["date"]).weekday()][0 if r["periode"] == "Matin" else 1] += r["montant"]
    activite = [{"jour": JOURS_SEMAINE[i], "matin": round(carte[i][0], 2), "apres_midi": round(carte[i][1], 2),
                 "nb_jours": nb_jours_semaine[i]} for i in range(7)]

    # Calendrier : revenu par jour (365 derniers jours de la période au plus)
    cal = defaultdict(float)
    for l in lignes:
        if l["type"] in REVENU:
            cal[l["date"]] += _montant(l)
    cal_debut = max(debut, fin - timedelta(days=364))
    calendrier = []
    d = cal_debut
    while d <= fin:
        calendrier.append({"date": str(d), "revenu": round(cal.get(str(d), 0.0), 2)})
        d += timedelta(days=1)

    series = _series(lignes, debut, fin, g)
    series_prec = _series(prev_l, comp[0], comp[1], g) if comp else None
    for p in produits:
        p["series"] = [round(par_prod_jour[p["nom"]].get(s["cle"], 0.0), 1) for s in series]

    out = {
        "granularite": g, "kpis": k, "kpis_prec": kp, "variations": variations,
        "series": series, "series_prec": series_prec,
        "departements": departements, "charges_par_categorie": charges_cat, "composition": composition,
        "produits": produits, "pompistes": pompistes, "pompes": pompes[:30], "stations": stations,
        "activite": activite, "calendrier": calendrier,
    }
    out["analyses"] = _insights(out, lignes, releves, debut, fin, comp)
    return out


def _cascade(lignes_toutes: list, ouverture: float) -> list:
    """Du solde d'ouverture au solde de clôture de la caisse, par famille."""
    etapes = [{"nom": "Solde d'ouverture", "montant": round(ouverture, 2), "type": "total"}]
    fam: dict = defaultdict(float)
    for l in lignes_toutes:
        fam[FAMILLES.get(l["type"], TYPES[l["type"]][0])] += l["debit"] - l["credit"]
    ordre = ["Ventes", "Autres entrées", "Dépenses", "Achats", "Salaires", "Transferts / dépôts"]
    for f in ordre + [x for x in fam if x not in ordre]:
        if round(fam.get(f, 0.0), 2):
            etapes.append({"nom": f, "montant": round(fam[f], 2), "type": "variation"})
    etapes.append({"nom": "Solde de clôture", "montant": round(ouverture + sum(fam.values()), 2), "type": "total"})
    return etapes


# ── Analyses (règles vérifiables, pas d'IA) ─────────────────────────────

def _g(v) -> str:
    return f"{v:,.0f} G".replace(",", " ")


def _insights(a: dict, lignes, releves, debut, fin, comp) -> list:
    out = []
    k, kp, var = a["kpis"], a["kpis_prec"], a["variations"]
    lib_comp = f"du {comp[0].strftime('%d/%m/%Y')} au {comp[1].strftime('%d/%m/%Y')}" if comp else None

    def ajouter(niveau, titre, explication, metrique, preuve, examiner):
        out.append({"niveau": niveau, "titre": titre, "explication": explication, "metrique": metrique,
                    "comparaison": lib_comp, "preuve": preuve, "a_examiner": examiner})

    if kp and var.get("revenu") is not None and abs(var["revenu"]) >= 10:
        hausse = var["revenu"] > 0
        ajouter("positif" if hausse else "attention",
                f"Revenu en {'hausse' if hausse else 'baisse'} de {abs(var['revenu'])} %",
                f"Les ventes de la période atteignent {_g(k['revenu'])}, contre {_g(kp['revenu'])} sur la période de comparaison.",
                "Revenu (ventes)", f"{_g(k['revenu'])} contre {_g(kp['revenu'])}",
                "Les produits et les jours qui expliquent l'écart (onglet Opérations)." if not hausse else
                "Vérifier que la hausse vient des volumes (gallons) et pas seulement des prix.")
    if kp and var.get("charges") is not None and var["charges"] >= 15:
        plus_vite = var.get("revenu") is None or var["charges"] > (var.get("revenu") or 0)
        ajouter("attention", f"Charges en hausse de {var['charges']} %",
                f"Les charges passent de {_g(kp['charges'])} à {_g(k['charges'])}"
                + (", plus vite que le revenu." if plus_vite else "."),
                "Charges", f"{_g(k['charges'])} contre {_g(kp['charges'])}",
                "Les catégories de dépenses en plus forte hausse (onglet Finances).")
    for c in a["charges_par_categorie"]:
        if (c["variation"] is not None and c["variation"] >= 50 and k["charges"]
                and c["montant"] >= 0.05 * k["charges"] and c["montant"] - (c["prec"] or 0) > 0):
            ajouter("attention", f"« {c['nom']} » : +{c['variation']} %",
                    f"Cette catégorie pèse {round(c['montant'] / k['charges'] * 100)} % des charges de la période.",
                    "Charges par catégorie", f"{_g(c['montant'])} contre {_g(c['prec'] or 0)}",
                    "Les dépenses de cette catégorie (explorateur des opérations).")
    for nom, items, cle in (("département", a["departements"], "revenu"), ("produit", a["produits"], "montant")):
        tot = sum(x[cle] for x in items)
        if len([x for x in items if x[cle]]) > 1 and tot:
            top = max(items, key=lambda x: x[cle])
            part = top[cle] / tot * 100
            if part >= 60:
                ajouter("info", f"{top['nom']} : {round(part)} % du revenu",
                        f"Le revenu dépend surtout de ce {nom}.", f"Part du revenu par {nom}",
                        f"{_g(top[cle])} sur {_g(tot)}", "La dépendance à un seul " + nom + " (risque si son activité baisse).")
    for p in a["produits"]:
        if p["variation"] is not None and p["variation"] <= -15 and p["montant_prec"]:
            ajouter("attention", f"{p['nom']} : ventes en baisse de {abs(p['variation'])} %",
                    f"{round(p['gallons'], 1)} gallons vendus contre {round(p['gallons_prec'] or 0, 1)}.",
                    "Ventes par produit", f"{_g(p['montant'])} contre {_g(p['montant_prec'])}",
                    "Prix, approvisionnement (rupture ?) et pompes de ce produit.")

    jours = [c for c in a["calendrier"] if c["date"] >= str(debut)]
    vals = [c["revenu"] for c in jours if c["revenu"]]
    if len(vals) >= 14:
        moy, et = statistics.mean(vals), statistics.pstdev(vals)
        if et:
            hors = sorted((c for c in jours if c["revenu"] and abs(c["revenu"] - moy) > 2 * et),
                          key=lambda c: -abs(c["revenu"] - moy))[:3]
            if hors:
                ajouter("info", f"{len(hors)} jour(s) hors de l'ordinaire",
                        "Revenu du jour à plus de 2 écarts-types de la moyenne des jours de vente de la période.",
                        "Revenu journalier", "; ".join(f"{date_type.fromisoformat(c['date']).strftime('%d/%m')} : {_g(c['revenu'])}" for c in hors)
                        + f" (moyenne {_g(moy)}, écart-type {_g(et)})",
                        "Les relevés et opérations de ces dates (cliquer sur le jour dans le calendrier).")
        sans = [c for c in jours if not c["revenu"]]
        if sans and len(sans) <= len(jours) // 2:
            ajouter("attention", f"{len(sans)} jour(s) sans vente enregistrée",
                    "Des jours de la période n'ont aucune vente validée alors que les autres en ont.",
                    "Jours de vente", ", ".join(date_type.fromisoformat(c["date"]).strftime("%d/%m") for c in sans[:8])
                    + ("…" if len(sans) > 8 else ""),
                    "Rapports pompistes en attente ou relevés manquants (Journal Meters).")
    if (fin - debut).days + 1 >= 14 and releves:
        moy_j = [sum(x["matin"] + x["apres_midi"] for x in a["activite"] if x["jour"] == j["jour"]) / j["nb_jours"]
                 if j["nb_jours"] else 0 for j in a["activite"]]
        m = statistics.mean([v for v in moy_j if v]) if any(moy_j) else 0
        if m:
            i = max(range(7), key=lambda i: moy_j[i])
            if moy_j[i] >= 1.25 * m:
                ajouter("info", f"Le {JOURS_SEMAINE[i].lower()} est le jour le plus actif",
                        f"Ventes carburant moyennes de {_g(moy_j[i])} ce jour-là, contre {_g(m)} en moyenne.",
                        "Ventes par jour de la semaine", f"+{round((moy_j[i] / m - 1) * 100)} % au-dessus de la moyenne",
                        "Le nombre de pompistes et le stock prévus ce jour-là.")
    if k["flux_net"] < 0:
        ajouter("attention", "Plus d'argent sorti qu'entré",
                f"Sur la période, la caisse a reçu {_g(k['encaissements'])} et versé {_g(k['decaissements'])}.",
                "Flux net de caisse", _g(k["flux_net"]), "Les grosses sorties (achats, transferts) de la période.")
    if k["revenu"] and k["marge"] is not None and k["marge"] < 0:
        ajouter("attention", "Charges supérieures au revenu",
                "Le résultat (base caisse) est négatif : les achats de stock payés sur la période peuvent l'expliquer.",
                "Résultat", f"{_g(k['resultat'])} (marge {k['marge']} %)", "Les achats de la période et le stock restant.")
    ordre = {"attention": 0, "positif": 1, "info": 2}
    out.sort(key=lambda x: ordre[x["niveau"]])
    return out


# ── Points d'accès ──────────────────────────────────────────────────────

@router.get("/filtres")
def filtres(request: Request, stations: Optional[str] = None, debut: Optional[date_type] = None,
            fin: Optional[date_type] = None, _s: Utilisateur = Depends(_require_superviseur),
            db: Session = Depends(get_db)):
    """Valeurs proposées dans les filtres (tirées des données réelles)."""
    from espaces_routes import proprietaire, stations_pompiste
    debut, fin = _periode(debut, fin)
    st = _stations(request, db, stations)
    d = _donnees(db, st, debut - timedelta(days=365), fin)
    proprio = bool(proprietaire(request, db))
    lignes = d["lignes"]
    return {
        "stations": stations_pompiste() if proprio else [{"id": s.id, "nom": s.nom} for s in st],
        "station_courante": st[0].id if len(st) == 1 else None,
        "multi_stations": proprio,
        "modules": sorted({l["module"] for l in lignes}),
        "types": [{"id": t, "nom": lib} for t, (lib, _s) in TYPES.items() if any(l["type"] == t for l in lignes)],
        "comptes": sorted({l["compte"] for l in lignes if l["compte"] and l["type"] in CHARGES}),
        "produits": sorted(({"id": str(i), "nom": n} for i, n in {(r["produit_id"], r["produit"]) for r in d["releves"] if r["produit_id"]}),
                           key=lambda x: x["nom"]),
        "employes": sorted({r["pompiste"] for r in d["releves"] if r["pompiste"]}
                           | {l["saisi_par"] for l in lignes if l.get("saisi_par")}),
    }


def _params(debut, fin, comparer, cdebut, cfin):
    debut, fin = _periode(debut, fin)
    if comparer not in ("precedent", "annee", "perso", "aucune"):
        raise HTTPException(400, "Comparaison inconnue.")
    return debut, fin, _comparaison(comparer, debut, fin, cdebut, cfin)


@router.get("/analyse")
def analyse(request: Request, debut: Optional[date_type] = None, fin: Optional[date_type] = None,
            comparer: str = "precedent", cdebut: Optional[date_type] = None, cfin: Optional[date_type] = None,
            stations: Optional[str] = None, modules: Optional[str] = None, types: Optional[str] = None,
            comptes: Optional[str] = None, produits: Optional[str] = None, employes: Optional[str] = None,
            _s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    debut, fin, comp = _params(debut, fin, comparer, cdebut, cfin)
    st = _stations(request, db, stations)
    f = Filtres(modules, types, comptes, produits, employes)
    d = _donnees(db, st, debut, fin, avec_stock=True)
    lignes = [l for l in d["lignes"] if f.ligne(l)]
    releves = [r for r in d["releves"] if f.releve(r)]
    prev_l, prev_r = [], []
    if comp:
        p = _donnees(db, st, comp[0], comp[1])
        prev_l = [l for l in p["lignes"] if f.ligne(l)]
        prev_r = [r for r in p["releves"] if f.releve(r)]
    out = _analyse_complete(lignes, releves, prev_l, prev_r, debut, fin, comp)
    ouverture = d["solde_fin"] - sum(l["debit"] - l["credit"] for l in d["lignes"])
    out.update({
        "periode": {"debut": str(debut), "fin": str(fin)},
        "comparaison": {"debut": str(comp[0]), "fin": str(comp[1])} if comp else None,
        "stations_analysees": [{"id": s.id, "nom": s.nom} for s in st],
        "filtres": f.resume(),
        "tresorerie": {"solde_fin": d["solde_fin"], "ouverture": round(ouverture, 2),
                       "note": "Solde de la caisse principale à la date de fin (non filtré)."},
        "stock": d["stock"] if not f.actifs() or (f.modules is None or "Station" in f.modules) else None,
        "cascade": _cascade(d["lignes"], ouverture),
        "calcule_le": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    return out


_TRI = {"date": lambda l: (l["date"], l["cle"]), "montant": lambda l: _montant(l),
        "compte": lambda l: (l["compte"] or "").casefold(), "type": lambda l: l["type_lib"]}


def _operations(request, db, debut, fin, stations, modules, types, comptes, produits, employes, q, sens):
    st = _stations(request, db, stations)
    f = Filtres(modules, types, comptes, produits, employes)
    d = _donnees(db, st, debut, fin)
    lignes = [l for l in d["lignes"] if f.ligne(l)]
    if sens in ("D", "C"):
        lignes = [l for l in lignes if l["sens"] == sens and l["type"] != "depense_pompiste"
                  or (sens == "C" and l["type"] == "depense_pompiste")]
    if q:
        qq = q.casefold().strip()
        lignes = [l for l in lignes if any(qq in str(l.get(c) or "").casefold()
                                           for c in ("description", "tiers", "compte", "reference", "type_lib",
                                                     "saisi_par", "module", "station"))]
    return lignes, f


@router.get("/operations")
def operations(request: Request, debut: Optional[date_type] = None, fin: Optional[date_type] = None,
               stations: Optional[str] = None, modules: Optional[str] = None, types: Optional[str] = None,
               comptes: Optional[str] = None, produits: Optional[str] = None, employes: Optional[str] = None,
               q: Optional[str] = None, sens: Optional[str] = None, tri: str = "date", ordre: str = "desc",
               page: int = Query(1, ge=1), taille: int = Query(50, ge=10, le=200),
               _s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    """Explorateur : les opérations derrière chaque chiffre (lignes du journal de caisse)."""
    debut, fin = _periode(debut, fin)
    lignes, _f = _operations(request, db, debut, fin, stations, modules, types, comptes, produits, employes, q, sens)
    lignes.sort(key=_TRI.get(tri, _TRI["date"]), reverse=(ordre != "asc"))
    total = len(lignes)
    pages = max(1, math.ceil(total / taille))
    page = min(page, pages)
    morceau = lignes[(page - 1) * taille: page * taille]
    return {
        "total": total, "page": page, "pages": pages, "taille": taille,
        "somme_entrees": round(sum(l["debit"] for l in lignes if l["type"] != "depense_pompiste"), 2),
        "somme_sorties": round(sum(_montant(l) for l in lignes if l["sens"] == "C" or l["type"] == "depense_pompiste"), 2),
        "lignes": [{c: l.get(c) for c in ("cle", "date", "type", "type_lib", "sens", "compte", "description", "tiers",
                                          "reference", "module", "station", "saisi_par", "debit", "credit", "produit_id", "lien")}
                   | {"montant": _montant(l)} for l in morceau],
    }


@router.get("/releves")
def releves_du_jour(request: Request, debut: date_type, fin: Optional[date_type] = None,
                    stations: Optional[str] = None, produits: Optional[str] = None, employes: Optional[str] = None,
                    _s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    """Relevés de compteurs validés d'une date ou d'une période (détail d'un jour, d'un produit)."""
    debut, fin = _periode(debut, fin or debut)
    st = _stations(request, db, stations)
    f = Filtres(produits=produits, employes=employes)
    d = _donnees(db, st, debut, fin)
    rs = sorted((r for r in d["releves"] if f.releve(r)), key=lambda r: (r["date"], r["periode"] != "Matin", r["pompe"]))
    return {"releves": rs, "gallons": round(sum(r["gallons"] for r in rs), 3),
            "montant": round(sum(r["montant"] for r in rs), 2)}


def _xlsx(feuilles: list) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    wb.remove(wb.active)
    for titre, lignes in feuilles:
        ws = wb.create_sheet(titre[:31])
        for i, row in enumerate(lignes):
            ws.append(row)
            if i == 0 or (row and isinstance(row[0], str) and row[0].isupper() and len(row) == 1):
                for c in ws[ws.max_row]:
                    c.font = Font(bold=True)
                    c.fill = PatternFill("solid", fgColor="E8EEF8")
        for col in ws.columns:
            larg = max((len(str(c.value)) for c in col if c.value is not None), default=8)
            ws.column_dimensions[col[0].column_letter].width = min(max(10, larg + 2), 60)
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


@router.get("/export")
def exporter(request: Request, quoi: str = "synthese", format: str = "xlsx",
             debut: Optional[date_type] = None, fin: Optional[date_type] = None,
             comparer: str = "precedent", cdebut: Optional[date_type] = None, cfin: Optional[date_type] = None,
             stations: Optional[str] = None, modules: Optional[str] = None, types: Optional[str] = None,
             comptes: Optional[str] = None, produits: Optional[str] = None, employes: Optional[str] = None,
             q: Optional[str] = None, sens: Optional[str] = None,
             s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    """Exports (Excel / CSV) — toujours avec les filtres en cours ; tracé dans le journal d'activité."""
    import main
    if format not in ("xlsx", "csv") or quoi not in ("synthese", "operations"):
        raise HTTPException(400, "Export inconnu.")
    debut, fin, comp = _params(debut, fin, comparer, cdebut, cfin)
    entete = [["KONEKTA INTELLIGENCE CENTER"], ["Généré le", time.strftime("%d/%m/%Y %H:%M")],
              ["Période", f"{debut.strftime('%d/%m/%Y')} au {fin.strftime('%d/%m/%Y')}"]]
    feuilles = []
    if quoi == "operations":
        lignes, f = _operations(request, db, debut, fin, stations, modules, types, comptes, produits, employes, q, sens)
        lignes.sort(key=_TRI["date"])
        filtres_txt = "; ".join(f"{k} : {', '.join(v)}" for k, v in f.resume().items()) or "aucun"
        rows = [["Date", "Station", "Département", "Type", "Compte / catégorie", "Description", "Tiers",
                 "Référence", "Saisi par", "Entrée (G)", "Sortie (G)"]]
        for l in lignes:
            ent = l["debit"] if l["type"] != "depense_pompiste" else 0.0
            sor = l["credit"] if l["type"] != "depense_pompiste" else _montant(l)
            rows.append([l["date"], l["station"], l["module"], l["type_lib"], l["compte"], l["description"], l["tiers"],
                         l["reference"], l["saisi_par"], ent, sor])
        feuilles = [("Opérations", rows), ("Paramètres", entete + [["Filtres", filtres_txt], ["Recherche", q or ""]])]
    else:
        a = analyse(request, debut, fin, comparer, cdebut, cfin, stations, modules, types, comptes, produits, employes, s, db)
        filtres_txt = "; ".join(f"{k} : {', '.join(v)}" for k, v in a["filtres"].items()) or "aucun"
        k, kp = a["kpis"], a["kpis_prec"] or {}
        libs = [("revenu", "Revenu (ventes)"), ("charges", "Charges"), ("resultat", "Résultat (revenu − charges)"),
                ("marge", "Marge (%)"), ("encaissements", "Encaissements"), ("decaissements", "Décaissements"),
                ("flux_net", "Flux net de caisse"), ("gallons", "Gallons vendus"), ("nb_releves", "Relevés de compteurs"),
                ("nb_operations", "Opérations"), ("revenu_moyen_jour", "Revenu moyen par jour")]
        res = entete + [["Comparaison", f"{a['comparaison']['debut']} au {a['comparaison']['fin']}" if a["comparaison"] else "aucune"],
                        ["Stations", ", ".join(x["nom"] for x in a["stations_analysees"])], ["Filtres", filtres_txt], [],
                        ["Indicateur", "Valeur", "Période de comparaison", "Variation (%)"]]
        for c, lib in libs:
            res.append([lib, k[c], kp.get(c), a["variations"].get(c)])
        res.append(["Trésorerie (solde caisse à la date de fin)", a["tresorerie"]["solde_fin"]])
        if a["stock"]:
            res.append(["Valeur du stock carburant (coût moyen)", a["stock"]["valeur"]])
        feuilles = [
            ("Synthèse", res),
            ("Évolution", [["Période", "Revenu", "Charges", "Résultat", "Encaissements", "Décaissements", "Opérations"]]
             + [[x["cle"], x["revenu"], x["charges"], x["resultat"], x["encaissements"], x["decaissements"], x["nb"]] for x in a["series"]]),
            ("Départements", [["Département", "Revenu", "Charges", "Résultat", "Encaissements", "Décaissements", "Revenu période préc.", "Variation (%)"]]
             + [[x["nom"], x["revenu"], x["charges"], x["resultat"], x["encaissements"], x["decaissements"], x["revenu_prec"], x["variation"]] for x in a["departements"]]),
            ("Charges", [["Catégorie", "Montant", "Période préc.", "Variation (%)", "Opérations"]]
             + [[x["nom"], x["montant"], x["prec"], x["variation"], x["nb"]] for x in a["charges_par_categorie"]]),
            ("Produits", [["Produit", "Gallons", "Montant", "Montant préc.", "Variation (%)", "Relevés"]]
             + [[x["nom"], x["gallons"], x["montant"], x["montant_prec"], x["variation"], x["nb"]] for x in a["produits"]]),
            ("Pompistes", [["Pompiste", "Gallons", "Montant", "Relevés", "Jours"]]
             + [[x["nom"], x["gallons"], x["montant"], x["nb"], x["jours"]] for x in a["pompistes"]]),
            ("Analyses", [["Niveau", "Constat", "Explication", "Preuve", "À examiner"]]
             + [[x["niveau"], x["titre"], x["explication"], x["preuve"], x["a_examiner"]] for x in a["analyses"]]),
        ]
    main._tracer(db, request, "export_intelligence", {"quoi": quoi, "format": format, "debut": str(debut), "fin": str(fin)},
                 user_id=s.id)
    db.commit()
    nom = f"konekta-{quoi}-{debut}-{fin}"
    if format == "csv":
        b = io.StringIO()
        w = csv.writer(b, delimiter=";")
        for titre, rows in feuilles:
            if len(feuilles) > 1:
                w.writerow([titre.upper()])
            w.writerows(rows)
            w.writerow([])
        return Response("﻿" + b.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{nom}.csv"'})
    return StreamingResponse(io.BytesIO(_xlsx(feuilles)),
                             media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="{nom}.xlsx"'})
