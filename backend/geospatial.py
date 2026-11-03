"""
Carte des stations (Geospatial Station Intelligence Center).

Une « station » est un espace Konekta : la station principale (id 0) ou une
station séparée (registre `espaces`, id ≥ 1). Sa fiche géographique
(StationGeo) et l'historique de ses modifications (StationGeoJournal) sont
tenus dans l'espace principal.

Accès (vérifié ici, jamais d'après ce que le navigateur envoie) :
  propriétaire  → toutes ses stations ;
  gérant / superviseur d'une station → uniquement la station où il est.
Les propriétaires différents ont chacun leur propre installation (base de
données séparée) : un propriétaire ne peut donc jamais voir les stations
d'un autre.

Coordonnées : WGS84 (EPSG:4326), degrés décimaux, contrôlées dans les
limites d'Haïti. « À proximité » : distance exacte (haversine) après un
premier tri par carré de latitude / longitude indexé.
"""
from __future__ import annotations

import math
import re
import threading
import time
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import definir_espace, espace_courant, get_db, retablir_espace, session_espace, session_principale
from models import Espace, StationGeo, StationGeoJournal, Utilisateur
from pompiste_routes import _require_superviseur
from tz_utils import now_haiti, today_haiti

router = APIRouter(prefix="/api/geospatial", tags=["geospatial"])

# Haïti, île de la Tortue, île à Vache, la Gonâve et la Navase comprises
LAT_MIN, LAT_MAX, LON_MIN, LON_MAX = 17.90, 20.20, -74.60, -71.60
DEPARTEMENTS = ["Artibonite", "Centre", "Grand'Anse", "Nippes", "Nord", "Nord-Est", "Nord-Ouest",
                "Ouest", "Sud", "Sud-Est"]
DISTANCE_DOUBLON_M = 50
SOURCES = ("gps", "carte")


# ── Accès ───────────────────────────────────────────────────────────────

def _id_courant() -> int:
    sch = espace_courant()
    return 0 if sch is None else int(sch.split("_")[1])


def _stations_accessibles(request: Request, db: Session) -> list:
    """[(id, nom, adresse, actif, schema)] des stations que l'utilisateur peut voir."""
    from espaces_routes import proprietaire
    with session_principale() as pdb:
        espaces = pdb.query(Espace).order_by(Espace.id).all()
        toutes = [(0, "Station principale", None, True, None)] + [
            (e.id, e.nom, e.adresse, bool(e.actif), e.schema) for e in espaces]
    if proprietaire(request, db):
        return toutes
    ici = _id_courant()
    return [s for s in toutes if s[0] == ici]


def _station(request: Request, db: Session, sid: int):
    s = next((x for x in _stations_accessibles(request, db) if x[0] == sid), None)
    if not s:
        raise HTTPException(404, "Station introuvable.")        # même réponse : on ne révèle pas son existence
    return s


def _nom_utilisateur(u: Utilisateur) -> str:
    return u.nom_complet or u.username or f"#{u.id}"


# ── Fiches ──────────────────────────────────────────────────────────────

CONTACTS = ("responsable", "telephone", "email", "urgence_nom", "urgence_telephone", "horaires", "note")
# Ce qu'une fiche à jour doit avoir (pour le suivi « fiche complète »)
ESSENTIELS = {"adresse": "adresse", "departement": "département", "commune": "commune", "gps": "position GPS",
              "responsable": "responsable", "telephone": "téléphone", "email": "email",
              "urgence_telephone": "numéro d’urgence"}
A_REVOIR_JOURS = 180


def _completude(d: dict) -> dict:
    manquants = [lib for k, lib in ESSENTIELS.items()
                 if not (d.get("latitude") is not None if k == "gps" else d.get(k))]
    return {"pct": round(100 * (len(ESSENTIELS) - len(manquants)) / len(ESSENTIELS)), "manquants": manquants}


def _fiche_dict(f: Optional[StationGeo]) -> dict:
    if not f:
        return dict({"code": None, "pays": "Haïti", "departement": None, "commune": None, "adresse": None,
                     "latitude": None, "longitude": None, "precision_m": None, "source": None,
                     "verification": "absent", "capture_par": None, "capture_le": None,
                     "maj_par": None, "maj_le": None}, **{k: None for k in CONTACTS})
    return {**{k: getattr(f, k) for k in CONTACTS},
            "maj_par": f.maj_par, "maj_le": f.maj_le.isoformat() if f.maj_le else None,
            "code": f.code, "pays": f.pays, "departement": f.departement, "commune": f.commune, "adresse": f.adresse,
            "latitude": float(f.latitude) if f.latitude is not None else None,
            "longitude": float(f.longitude) if f.longitude is not None else None,
            "precision_m": float(f.precision_m) if f.precision_m is not None else None,
            "source": f.source,
            "verification": f.verification if f.latitude is not None else "absent",
            "capture_par": f.capture_par, "capture_le": f.capture_le.isoformat() if f.capture_le else None}


def _fiches(pdb: Session) -> dict:
    return {f.station_id: f for f in pdb.query(StationGeo).all()}


def _ligne_carte(s, f: Optional[StationGeo]) -> dict:
    """Données légères pour la carte et la liste."""
    sid, nom, adresse, actif, _sch = s
    d = _fiche_dict(f)
    d["adresse"] = d["adresse"] or adresse
    maj = f.maj_le if f and f.maj_le else (f.capture_le if f else None)
    return {"id": sid, "nom": nom, "code": d["code"], "departement": d["departement"], "commune": d["commune"],
            "adresse": d["adresse"], "statut": "active" if actif else "inactive",
            "latitude": d["latitude"], "longitude": d["longitude"], "verification": d["verification"],
            **{k: d[k] for k in CONTACTS},
            "completude": _completude(d), "maj_le": maj.isoformat() if maj else None,
            "a_revoir": bool(maj and (today_haiti() - maj.date()).days > A_REVOIR_JOURS),
            "ici": sid == _id_courant()}


def _filtrer(lignes: list, q, departement, commune, statut, gps) -> list:
    if q:
        qq = q.casefold().strip()
        lignes = [l for l in lignes if any(qq in (l[c] or "").casefold() for c in ("nom", "code", "commune", "adresse", "departement"))]
    if departement:
        lignes = [l for l in lignes if l["departement"] == departement]
    if commune:
        lignes = [l for l in lignes if (l["commune"] or "").casefold() == commune.casefold()]
    if statut in ("active", "inactive"):
        lignes = [l for l in lignes if l["statut"] == statut]
    if gps in ("verifie", "non_verifie", "absent"):
        lignes = [l for l in lignes if l["verification"] == gps]
    return lignes


@router.get("/stations")
def stations(request: Request, q: Optional[str] = None, departement: Optional[str] = None,
             commune: Optional[str] = None, statut: Optional[str] = None, gps: Optional[str] = None,
             page: int = Query(1, ge=1), taille: int = Query(500, ge=1, le=2000),
             _s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    """Stations accessibles, filtrées côté serveur (données légères pour la carte)."""
    acc = _stations_accessibles(request, db)
    with session_principale() as pdb:
        fiches = _fiches(pdb)
    lignes = _filtrer([_ligne_carte(s, fiches.get(s[0])) for s in acc], q, departement, commune, statut, gps)
    total = len(lignes)
    return {"total": total, "page": page, "taille": taille,
            "stations": lignes[(page - 1) * taille: page * taille]}


@router.get("/overview")
def overview(request: Request, _s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    from espaces_routes import proprietaire
    acc = _stations_accessibles(request, db)
    with session_principale() as pdb:
        fiches = _fiches(pdb)
    lignes = [_ligne_carte(s, fiches.get(s[0])) for s in acc]
    p = proprietaire(request, db)
    return {
        "proprietaire": _nom_utilisateur(p) if p else None,
        "total": len(lignes),
        "actives": sum(1 for l in lignes if l["statut"] == "active"),
        "inactives": sum(1 for l in lignes if l["statut"] == "inactive"),
        "departements_couverts": len({l["departement"] for l in lignes if l["departement"]}),
        "gps_verifies": sum(1 for l in lignes if l["verification"] == "verifie"),
        "gps_non_verifies": sum(1 for l in lignes if l["verification"] == "non_verifie"),
        "sans_gps": sum(1 for l in lignes if l["verification"] == "absent"),
        "peut_tout_voir": bool(p),
    }


@router.get("/departments")
def departements(request: Request, _s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    """Les 10 départements d'Haïti et les communes déjà utilisées par les stations accessibles."""
    ids = {s[0] for s in _stations_accessibles(request, db)}
    with session_principale() as pdb:
        fiches = [f for f in pdb.query(StationGeo).all() if f.station_id in ids]
    communes: dict = {}
    for f in fiches:
        if f.departement and f.commune:
            communes.setdefault(f.departement, set()).add(f.commune)
    return {"departements": DEPARTEMENTS, "communes": {d: sorted(c) for d, c in communes.items()}}


def _activite(sch) -> dict:
    """Activité réelle de la station : relevés validés des 30 derniers jours."""
    from models import Releve
    fin = today_haiti()
    debut = fin - timedelta(days=29)
    jeton = definir_espace(sch)
    try:
        with session_espace(sch) as sdb:
            rs = sdb.query(Releve).filter(Releve.date >= debut, Releve.date <= fin).all()
            dernier = sdb.query(Releve.date).order_by(Releve.date.desc()).first()
            gal: dict = {}
            for r in rs:
                nom = r.pompe.produit.nom if r.pompe and r.pompe.produit else "—"
                gal[nom] = round(gal.get(nom, 0.0) + float(r.quantite), 3)   # jamais additionnés entre produits
            return {"periode": {"debut": str(debut), "fin": str(fin)},
                    "ventes": round(sum(float(r.montant_vente) for r in rs), 2),
                    "nb_releves": len(rs), "jours_actifs": len({r.date for r in rs}),
                    "gallons_par_produit": dict(sorted(gal.items())),
                    "dernier_releve": str(dernier[0]) if dernier else None}
    except Exception:
        return None
    finally:
        retablir_espace(jeton)


@router.get("/stations/nearby")
def proches(request: Request, lat: float, lon: float, rayon_km: float = Query(10, gt=0, le=300),
            _s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    """Stations accessibles à moins de rayon_km du point (distance réelle sur la Terre)."""
    _verifier_point(lat, lon)
    ids = {s[0]: s for s in _stations_accessibles(request, db)}
    dlat = rayon_km / 111.0
    dlon = rayon_km / (111.0 * max(0.1, math.cos(math.radians(lat))))
    with session_principale() as pdb:
        candidates = (pdb.query(StationGeo)
                      .filter(StationGeo.latitude.between(lat - dlat, lat + dlat),
                              StationGeo.longitude.between(lon - dlon, lon + dlon)).all())
        out = []
        for f in candidates:
            if f.station_id in ids:
                d = _distance_m(lat, lon, float(f.latitude), float(f.longitude))
                if d <= rayon_km * 1000:
                    out.append(dict(_ligne_carte(ids[f.station_id], f), distance_m=round(d, 1)))
    return {"stations": sorted(out, key=lambda x: x["distance_m"])}


@router.get("/stations/{sid}")
def detail(sid: int, request: Request, _s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    from espaces_routes import proprietaire
    s = _station(request, db, sid)
    with session_principale() as pdb:
        f = pdb.query(StationGeo).filter_by(station_id=sid).first()
        journal = [{"date": j.created_at.isoformat() if j.created_at else None, "utilisateur": j.utilisateur,
                    "action": j.action, "anciennes": j.anciennes, "nouvelles": j.nouvelles}
                   for j in pdb.query(StationGeoJournal).filter_by(station_id=sid)
                   .order_by(StationGeoJournal.id.desc()).limit(30).all()]
        enregistree = None
        if sid:
            e = pdb.get(Espace, sid)
            enregistree = e.created_at.isoformat() if e and e.created_at else None
    p = proprietaire(request, db)
    return dict(_ligne_carte(s, f), **{
        "fiche": _fiche_dict(f), "proprietaire": _nom_utilisateur(p) if p else None,
        "enregistree_le": enregistree, "activite": _activite(s[4]) if s[3] else None,
        "journal": journal,
    })


# ── Modifications ───────────────────────────────────────────────────────

def _distance_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def _verifier_point(lat: float, lon: float) -> None:
    if lat is None or lon is None or any(map(math.isnan, (lat, lon))):
        raise HTTPException(400, "Latitude et longitude sont obligatoires.")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(400, "Coordonnées impossibles (latitude entre −90 et 90, longitude entre −180 et 180).")
    if not (LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX):
        raise HTTPException(400, "Ce point est hors d'Haïti : vérifiez la latitude (≈ 18 à 20) et la longitude (≈ −74 à −72).")


def _journaliser(pdb: Session, sid: int, qui: str, action: str, avant: dict, apres: dict) -> None:
    pdb.add(StationGeoJournal(station_id=sid, utilisateur=qui, action=action, anciennes=avant, nouvelles=apres))


def _fiche_pour(pdb: Session, sid: int) -> StationGeo:
    f = pdb.query(StationGeo).filter_by(station_id=sid).first()
    if not f:
        f = StationGeo(station_id=sid, pays="Haïti", verification="non_verifie")
        pdb.add(f)
        pdb.flush()
    return f


class InfosIn(BaseModel):
    """Seuls les champs envoyés sont modifiés ; un champ envoyé vide est effacé."""
    code: Optional[str] = None
    departement: Optional[str] = None
    commune: Optional[str] = None
    adresse: Optional[str] = None
    responsable: Optional[str] = None
    telephone: Optional[str] = None
    email: Optional[str] = None
    urgence_nom: Optional[str] = None
    urgence_telephone: Optional[str] = None
    horaires: Optional[str] = None
    note: Optional[str] = None


_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[a-z]{2,24}$", re.I)
_TEL_INVALIDE = "{} : numéro invalide (ex. 3712 3456 ou +509 3712 3456)."


def normaliser_telephone(v: Optional[str], libelle: str) -> Optional[str]:
    """Numéro d'Haïti (8 chiffres, avec ou sans +509) → « +509 3712-3456 » ;
    numéro étranger gardé avec son indicatif. Refuse ce qui n'est pas un numéro."""
    v = (v or "").strip()
    if not v:
        return None
    if re.search(r"[^\d\s+().-]", v) or v.count("+") > 1 or ("+" in v and not v.startswith("+")):
        raise HTTPException(400, _TEL_INVALIDE.format(libelle))
    chiffres = re.sub(r"\D", "", v)
    if chiffres.startswith("509") and len(chiffres) == 11:
        chiffres = chiffres[3:]
    elif v.startswith("+"):
        if 8 <= len(chiffres) <= 15:
            return "+" + chiffres
        raise HTTPException(400, _TEL_INVALIDE.format(libelle))
    if len(chiffres) != 8:
        raise HTTPException(400, _TEL_INVALIDE.format(libelle))
    return f"+509 {chiffres[:4]}-{chiffres[4:]}"


def appliquer_infos(pdb: Session, sid: int, data: InfosIn, qui: str) -> StationGeo:
    nettoie = lambda v, n: (" ".join((v or "").split())[:n] or None)
    envoyes = data.model_fields_set
    val = {}
    if "code" in envoyes:
        code = nettoie(data.code, 30)
        if code:
            code = code.upper()
            autre = pdb.query(StationGeo).filter(StationGeo.code == code, StationGeo.station_id != sid).first()
            if autre:
                raise HTTPException(409, f"Le code {code} est déjà utilisé par une autre station.")
        val["code"] = code
    if "departement" in envoyes:
        if data.departement and data.departement not in DEPARTEMENTS:
            raise HTTPException(400, "Département inconnu.")
        val["departement"] = data.departement or None
    for k, n in (("commune", 100), ("adresse", 255), ("responsable", 150), ("urgence_nom", 150), ("horaires", 120)):
        if k in envoyes:
            val[k] = nettoie(getattr(data, k), n)
    if "note" in envoyes:
        val["note"] = (data.note or "").strip()[:500] or None
    if "email" in envoyes:
        e = (data.email or "").strip().lower() or None
        if e and (len(e) > 254 or not _EMAIL_RE.match(e)):
            raise HTTPException(400, "Email de la station invalide (ex. station@exemple.com).")
        val["email"] = e
    for k, lib in (("telephone", "Téléphone"), ("urgence_telephone", "Numéro d’urgence")):
        if k in envoyes:
            val[k] = normaliser_telephone(getattr(data, k), lib)
    f = _fiche_pour(pdb, sid)
    avant = {k: getattr(f, k) for k in val}
    for k, v in val.items():
        setattr(f, k, v)
    if any(avant[k] != val[k] for k in val):
        f.maj_par, f.maj_le = qui, now_haiti()
        _journaliser(pdb, sid, qui, "informations", {k: v for k, v in avant.items() if v != val[k]},
                     {k: v for k, v in val.items() if v != avant[k]})
    return f


@router.put("/stations/{sid}/infos")
def modifier_infos(sid: int, data: InfosIn, request: Request, s: Utilisateur = Depends(_require_superviseur),
                   db: Session = Depends(get_db)):
    _station(request, db, sid)
    with session_principale() as pdb:
        appliquer_infos(pdb, sid, data, _nom_utilisateur(s))
        pdb.commit()
    return detail(sid, request, s, db)


class LocationIn(BaseModel):
    latitude: float
    longitude: float
    precision_m: Optional[float] = None
    source: str = "carte"
    verifie: bool = False                    # position confirmée sur place / sur la carte
    forcer: bool = False                     # une autre station est à moins de 50 m : c'est voulu
    confirmer_remplacement: bool = False     # remplacer une position déjà vérifiée


def appliquer_location(pdb: Session, sid: int, data: LocationIn, qui: str) -> StationGeo:
    _verifier_point(data.latitude, data.longitude)
    if data.source not in SOURCES:
        raise HTTPException(400, "Source de position inconnue (gps ou carte).")
    if data.precision_m is not None and not (0 <= data.precision_m <= 100000):
        raise HTTPException(400, "Précision GPS invalide.")
    if not data.forcer:
        for o in pdb.query(StationGeo).filter(StationGeo.station_id != sid, StationGeo.latitude.isnot(None)).all():
            d = _distance_m(data.latitude, data.longitude, float(o.latitude), float(o.longitude))
            if d < DISTANCE_DOUBLON_M:
                raise HTTPException(409, {"code": "station_proche", "distance_m": round(d, 1),
                                          "message": f"Une autre station est déjà enregistrée à {round(d)} m de ce point. "
                                                     "S'il s'agit bien d'une autre station, confirmez."})
    f = _fiche_pour(pdb, sid)
    if (f.verification == "verifie" and f.latitude is not None and not data.confirmer_remplacement
            and _distance_m(float(f.latitude), float(f.longitude), data.latitude, data.longitude) > 1):
        raise HTTPException(409, {"code": "position_verifiee",
                                  "message": "Cette station a déjà une position vérifiée. Confirmez pour la remplacer."})
    avant = _fiche_dict(f)
    avant = {k: avant[k] for k in ("latitude", "longitude", "precision_m", "source", "verification")}
    f.latitude, f.longitude = round(data.latitude, 6), round(data.longitude, 6)
    f.precision_m = round(data.precision_m, 1) if data.precision_m is not None else None
    f.source, f.verification = data.source, "verifie" if data.verifie else "non_verifie"
    f.capture_par, f.capture_le = qui, now_haiti()
    f.maj_par, f.maj_le = qui, f.capture_le
    pdb.flush()
    apres = {"latitude": float(f.latitude), "longitude": float(f.longitude),
             "precision_m": float(f.precision_m) if f.precision_m is not None else None,
             "source": f.source, "verification": f.verification}
    _journaliser(pdb, sid, qui, "position", avant, apres)
    return f


@router.post("/stations/{sid}/location")
def enregistrer_location(sid: int, data: LocationIn, request: Request, s: Utilisateur = Depends(_require_superviseur),
                         db: Session = Depends(get_db)):
    _station(request, db, sid)
    with session_principale() as pdb:
        appliquer_location(pdb, sid, data, _nom_utilisateur(s))
        pdb.commit()
    return detail(sid, request, s, db)


# ── Recherche d'un lieu (OpenStreetMap Nominatim, via le serveur) ───────

_rech_cache: dict = {}
_rech_verrou = threading.Lock()
_rech_dernier = [0.0]


@router.get("/recherche")
def rechercher_lieu(q: str = Query(..., min_length=2, max_length=120), _s: Utilisateur = Depends(_require_superviseur)):
    """Cherche une ville, un quartier ou une route en Haïti pour centrer la
    carte. Respecte la règle d'usage de Nominatim (1 requête / s, cache)."""
    import requests
    cle = q.strip().casefold()
    if cle in _rech_cache:
        return _rech_cache[cle]
    with _rech_verrou:
        attente = 1.05 - (time.time() - _rech_dernier[0])
        if attente > 0:
            time.sleep(attente)
        try:
            r = requests.get("https://nominatim.openstreetmap.org/search",
                             params={"q": q, "format": "jsonv2", "countrycodes": "ht", "limit": 6},
                             headers={"User-Agent": "KONEKTA-station-map/1.0", "Accept-Language": "fr"}, timeout=6)
            r.raise_for_status()
            res = r.json()
        except Exception:
            raise HTTPException(502, "La recherche de lieu est indisponible pour le moment : placez la station directement sur la carte.")
        finally:
            _rech_dernier[0] = time.time()
    out = {"resultats": [{"nom": x.get("display_name"), "latitude": float(x["lat"]), "longitude": float(x["lon"])}
                         for x in res if LAT_MIN <= float(x["lat"]) <= LAT_MAX and LON_MIN <= float(x["lon"]) <= LON_MAX],
           "attribution": "© OpenStreetMap contributors"}
    if len(_rech_cache) > 500:
        _rech_cache.clear()
    _rech_cache[cle] = out
    return out
