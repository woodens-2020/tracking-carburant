"""
Livrezon kabiran — application « Konekta Resepsyon » (moun k ap resevwa gaz la).

Circuit (dyagram « Sikwi livrezon kabiran — Konekta v2 ») :
  1. Connexion : code 4 chiffres + 4 lettres (ex. 4827KXMT) + PIN 6 chiffres.
  2. Formulaire de livraison : la personne CHOISIT LA STATION de destination
     et le produit de cette station ; contrôles (champs requis, quantité > 0,
     téléphone +509, bon de livraison sans doublon) → statut VALIDE.
  3. Admin : approuver (AN_WOUT), rejeter (REJTE), modifier, annuler.
  4. Réception à la station : quantité reçue, jauges avant / après →
     une Livraison (stock) est créée DANS LE SCHÉMA DE LA STATION CHOISIE,
     puis contrôle de l'écart envoyé / reçu > tolérance → FEMEN ou EKAT.
  5. Fiche PDF de la livraison ; journal d'audit (chaque changement de
     statut) ; notifications SMS / WhatsApp / Email avec journal, 3 essais
     et canal de repli.

Tout est enregistré dans la base centrale (espace principal), quelle que
soit la station : les requêtes utilisent toujours session_principale().

Préfixes :
  /api/resepsyon/...        compte « Resepsyon Kabiran » (et superviseurs)
  /api/livrezon-admin/...   direction / administration
"""
from __future__ import annotations

import io
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth import hash_code_acces, hash_password, verify_code_acces
from database import get_db, session_espace, session_principale
from models import (
    Livraison, LivrezonKabiran, LivrezonKabiranJounal, LivrezonKabiranNotifikasyon,
    Produit, Role, Utilisateur,
)
from tz_utils import HAITI_TZ, now_haiti, today_haiti

router = APIRouter(tags=["livrezon-kabiran"])
log = logging.getLogger("livrezon")

ROLE_RESEPSYON = "Resepsyon Kabiran"
STATUTS = ("VALIDE", "AN_WOUT", "RESEVWA", "FEMEN", "EKAT", "REJTE", "ANILE")
STATUT_KREYOL = {"VALIDE": "Valide", "AN_WOUT": "An wout", "RESEVWA": "Resevwa", "FEMEN": "Fèmen",
                 "EKAT": "Ekat", "REJTE": "Rejte", "ANILE": "Anile"}
EN_KOU = ("VALIDE", "AN_WOUT")             # pas encore reçue
_TEL_RE = re.compile(r"^\+509[0-9]{8}$")
_CODE_RE = re.compile(r"^[0-9]{4}[A-Z]{4}$")
_PIN_RE = re.compile(r"^[0-9]{6}$")
_LETTRES = "ABCDEFGHJKLMNPQRSTUVWXYZ"     # sans I ni O (confusion avec 1 et 0)
MAX_GALON = 20000                          # garde-fou : un camion-citerne


def tolerans(voye: float) -> float:
    """Écart toléré entre envoyé et reçu (gallons) : le plus grand des deux
    seuils (LIVREZON_TOLERANS_GALON, défaut 5 ; LIVREZON_TOLERANS_PCT, défaut 0,5 %)."""
    try:
        g = float(os.getenv("LIVREZON_TOLERANS_GALON", "5"))
    except ValueError:
        g = 5.0
    try:
        p = float(os.getenv("LIVREZON_TOLERANS_PCT", "0.5"))
    except ValueError:
        p = 0.5
    return round(max(g, voye * p / 100.0), 3)


# ── Rôle et comptes ─────────────────────────────────────────────────────

def est_role_resepsyon(role: Optional[Role]) -> bool:
    return bool(role) and (role.nom or "").strip().lower() == ROLE_RESEPSYON.lower()


def assurer_role_resepsyon(db: Session) -> Role:
    r = next((x for x in db.query(Role).all() if est_role_resepsyon(x)), None)
    if r:
        return r
    r = Role(nom=ROLE_RESEPSYON,
             description="Accès limité à l'application de réception du carburant (/resepsyon)",
             permissions={"finance": "aucun", "bar": "aucun", "cuisine": "aucun", "hotel": "aucun",
                          "employes": "aucun", "carburant": "aucun", "admin": False},
             est_admin=False, est_systeme=True)
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _user(request: Request, db: Session) -> Utilisateur:
    proxy = getattr(request.state, "user", None)
    if not proxy:
        raise HTTPException(401, "Non authentifié")
    u = db.get(Utilisateur, proxy.id)
    if not u or not u.actif:
        raise HTTPException(401, "Compte inactif")
    return u


def _require_superviseur(request: Request, db: Session = Depends(get_db)) -> Utilisateur:
    from pompiste_routes import peut_superviser
    u = _user(request, db)
    if not peut_superviser(u):
        raise HTTPException(403, "Réservé à la direction / administration")
    return u


def _require_resepsyon(request: Request, db: Session = Depends(get_db)) -> Utilisateur:
    """Compte de réception — ou superviseur (pour tester / dépanner)."""
    from pompiste_routes import peut_superviser
    u = _user(request, db)
    if not (est_role_resepsyon(u.role_obj) or peut_superviser(u)):
        raise HTTPException(403, "Espace réservé aux comptes de réception du carburant")
    return u


def _nom(u: Optional[Utilisateur]) -> Optional[str]:
    return (u.nom_complet or u.username) if u else None


def _ip(request: Optional[Request]) -> Optional[str]:
    return request.client.host if request is not None and request.client else None


# ── Stations et produits ────────────────────────────────────────────────

def _schema(station_id: int) -> Optional[str]:
    from espaces_routes import schema_de_station
    try:
        return schema_de_station(int(station_id))
    except (ValueError, TypeError):
        raise HTTPException(404, "Estasyon sa a pa egziste")


def _stations() -> list[dict]:
    from espaces_routes import stations_pompiste
    return stations_pompiste()


def _station(station_id: int) -> dict:
    st = next((s for s in _stations() if s["id"] == int(station_id)), None)
    if not st:
        raise HTTPException(404, "Estasyon sa a pa egziste")
    return st


def _produits_station(station_id: int, avec_stock: bool = False) -> list[dict]:
    from stock_service import stock_restant
    out = []
    with session_espace(_schema(station_id)) as sdb:
        for p in sdb.query(Produit).filter(Produit.actif.is_(True)).order_by(Produit.id).all():
            d = {"id": p.id, "nom": p.nom}
            if avec_stock:
                try:
                    s = stock_restant(sdb, p.id)
                    d.update(stock=s["gallons_restants"], moyenne_jour=s["moyenne_jour"],
                             jours_de_stock=s["jours_de_stock"], alerte_bas=s["alerte_bas"])
                except Exception:
                    log.exception("stock station %s produit %s", station_id, p.id)
                    d.update(stock=None)
            out.append(d)
    return out


def famille_carburant(nom: Optional[str]) -> str:
    """Gazoline 1 / Gazoline 2 → « Gazoline » ; Diesel 1 / Diesel 2 → « Diesel »
    (même règle que le reste du système : main._famille_carburant)."""
    n = (nom or "").lower()
    if "propan" in n:
        return "Propane"
    if "gaz" in n or "essence" in n:
        return "Gazoline"
    if "diesel" in n:
        return "Diesel"
    return (nom or "").strip() or "Inconnu"


def _familles_station(station_id: int, avec_stock: bool = False) -> list[dict]:
    """Types de carburant de la station : Gazoline, Diesel (jamais « Gazoline 1 / 2 »).
    id = premier produit du type : c'est là que la livraison entre en stock ;
    le stock affiché est la somme des produits du même type (même cuve)."""
    fam: dict = {}
    for p in _produits_station(station_id, avec_stock):
        f = fam.setdefault(famille_carburant(p["nom"]), {"id": p["id"], "nom": famille_carburant(p["nom"]),
                                                         "produit_ids": []})
        f["produit_ids"].append(p["id"])
        if avec_stock:
            for k in ("stock", "moyenne_jour"):
                f[k] = None if p.get(k) is None or (k in f and f[k] is None) else round(f.get(k, 0) + p[k], 3)
            f["alerte_bas"] = bool(f.get("alerte_bas") or p.get("alerte_bas"))
            f["jours_de_stock"] = (round(f["stock"] / f["moyenne_jour"], 1)
                                   if f.get("stock") is not None and f.get("moyenne_jour") else None)
    ordre = {"Gazoline": 0, "Diesel": 1, "Propane": 2}
    return sorted(fam.values(), key=lambda f: (ordre.get(f["nom"], 9), f["nom"]))


def _dernier_prix(station_id: int, produit_id: int) -> float:
    with session_espace(_schema(station_id)) as sdb:
        lv = (sdb.query(Livraison).filter(Livraison.produit_id == produit_id)
              .order_by(Livraison.date_livraison.desc(), Livraison.id.desc()).first())
        return float(lv.prix_achat_gallon) if lv else 0.0


# ── Sérialisation / journal ────────────────────────────────────────────

def _f(x):
    return float(x) if x is not None else None


def _iso(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(HAITI_TZ).isoformat()


def livrezon_dict(l: LivrezonKabiran, jounal: bool = False, db: Optional[Session] = None) -> dict:
    d = {
        "id": l.id, "numero": l.numero, "station_id": l.station_id, "station_nom": l.station_nom,
        "produit_id": l.produit_id, "produit_nom": l.produit_nom,
        "date_livrezon": _iso(l.date_livrezon), "no_kontre_plen": l.no_kontre_plen, "no_bon": l.no_bon,
        "kantite_voye": _f(l.kantite_voye), "pri_galon": _f(l.pri_galon),
        "chofe": l.chofe, "telefon_chofe": l.telefon_chofe, "plak_kamyon": l.plak_kamyon,
        "konpayi": l.konpayi, "komante": l.komante,
        "statut": l.statut, "statut_label": STATUT_KREYOL.get(l.statut, l.statut),
        "revize_par": _nom(l.revize_par), "revize_at": _iso(l.revize_at), "motif": l.motif,
        "kantite_resevwa": _f(l.kantite_resevwa), "jog_avan": _f(l.jog_avan), "jog_apre": _f(l.jog_apre),
        "ekat": _f(l.ekat), "tolerans": tolerans(float(l.kantite_voye)),
        "resevwa_par": _nom(l.resevwa_par), "resevwa_at": _iso(l.resevwa_at),
        "komante_resepsyon": l.komante_resepsyon, "livraison_id": l.livraison_id,
        "cree_par": _nom(l.cree_par), "cree_par_id": l.cree_par_id,
        "created_at": _iso(l.created_at), "updated_at": _iso(l.updated_at),
    }
    if jounal and db is not None:
        d["jounal"] = [{"aksyon": j.aksyon, "ansyen_statut": j.ansyen_statut, "nouvo_statut": j.nouvo_statut,
                        "detay": j.detay, "itilizate": j.itilizate_non, "at": _iso(j.created_at)}
                       for j in db.query(LivrezonKabiranJounal).filter_by(livrezon_id=l.id)
                       .order_by(LivrezonKabiranJounal.id).all()]
        d["notifikasyon"] = [{"evenman": n.evenman, "kanal": n.kanal, "destinate": n.destinate,
                              "statut": n.statut, "eseye": n.eseye, "ere": n.ere, "ranplasman": n.ranplasman,
                              "at": _iso(n.created_at)}
                             for n in db.query(LivrezonKabiranNotifikasyon).filter_by(livrezon_id=l.id)
                             .order_by(LivrezonKabiranNotifikasyon.id).all()]
    return d


def _jounal(db: Session, l: LivrezonKabiran, aksyon: str, ansyen: Optional[str], u: Optional[Utilisateur],
            request: Optional[Request] = None, detay: Optional[dict] = None) -> None:
    db.add(LivrezonKabiranJounal(livrezon_id=l.id, aksyon=aksyon, ansyen_statut=ansyen, nouvo_statut=l.statut,
                                 detay=detay, itilizate_id=u.id if u else None, itilizate_non=_nom(u),
                                 ip=_ip(request)))


def _utilisateur_central(db: Session, u: Utilisateur) -> Optional[int]:
    """Id du compte dans la base centrale (pour les clés étrangères). Un
    superviseur connecté depuis une station séparée n'y existe pas forcément."""
    from database import espace_courant
    if espace_courant() is None:
        return u.id
    if u.email:
        x = db.query(Utilisateur.id).filter(Utilisateur.email == u.email).first()
        return x[0] if x else None
    return None


class _Acteur:
    """Auteur d'une action, rapporté à la base centrale."""
    def __init__(self, db: Session, u: Utilisateur):
        self.id = _utilisateur_central(db, u)
        self.nom_complet, self.username = u.nom_complet, u.username


def _norm_bon(v: str) -> str:
    return re.sub(r"[^0-9A-Z]", "", (v or "").upper())


def _norm_tel(v: str) -> str:
    t = re.sub(r"[\s\-().]", "", v or "")
    if re.fullmatch(r"[0-9]{8}", t):
        t = "+509" + t
    elif re.fullmatch(r"509[0-9]{8}", t):
        t = "+" + t
    return t


def _prochain_numero(db: Session) -> str:
    an = today_haiti().year
    prefixe = f"LVK-{an}-"
    dernier = (db.query(LivrezonKabiran.numero).filter(LivrezonKabiran.numero.like(prefixe + "%"))
               .order_by(LivrezonKabiran.numero.desc()).first())
    n = int(dernier[0].split("-")[-1]) + 1 if dernier else 1
    return f"{prefixe}{n:05d}"


# ── Validation (« Règ validasyon ») ────────────────────────────────────

class LivrezonIn(BaseModel):
    station_id: int
    produit_id: int
    date_livrezon: Optional[str] = None      # ISO (date + heure) ; vide = maintenant
    no_kontre_plen: Optional[str] = None
    no_bon: str
    kantite_voye: float
    pri_galon: Optional[float] = None
    chofe: str
    telefon_chofe: str
    plak_kamyon: str
    konpayi: str
    komante: Optional[str] = None


class LivrezonPatch(BaseModel):
    station_id: Optional[int] = None
    produit_id: Optional[int] = None
    date_livrezon: Optional[str] = None
    no_kontre_plen: Optional[str] = None
    no_bon: Optional[str] = None
    kantite_voye: Optional[float] = None
    pri_galon: Optional[float] = None
    chofe: Optional[str] = None
    telefon_chofe: Optional[str] = None
    plak_kamyon: Optional[str] = None
    konpayi: Optional[str] = None
    komante: Optional[str] = None
    kantite_resevwa: Optional[float] = None


class ResepsyonIn(BaseModel):
    kantite_resevwa: float
    jog_avan: Optional[float] = None
    jog_apre: Optional[float] = None
    komante: Optional[str] = None


class MotifIn(BaseModel):
    motif: Optional[str] = None


def _parse_date(v: Optional[str]) -> datetime:
    if not v:
        return datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        raise HTTPException(422, {"code": "dat", "chan": "date_livrezon", "message": "Dat la pa bon."})
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=HAITI_TZ)
    if dt > now_haiti() + timedelta(days=30) or dt < now_haiti() - timedelta(days=366):
        raise HTTPException(422, {"code": "dat", "chan": "date_livrezon", "message": "Dat la twò lwen."})
    return dt.astimezone(timezone.utc)


def _verifier(db: Session, data: dict, exclure_id: Optional[int] = None, kreye: bool = False) -> dict:
    """Règles de validation du diagramme. Lève 422 avec la liste des erreurs
    (chan = champ du formulaire) pour que l'application les montre."""
    erreurs = []

    def err(chan, msg):
        erreurs.append({"chan": chan, "message": msg})

    for chan, label in (("no_bon", "No bon livrezon"), ("chofe", "Chofè"), ("telefon_chofe", "Telefòn chofè"),
                        ("plak_kamyon", "Plak kamyon"), ("konpayi", "Konpayi")):
        v = (data.get(chan) or "").strip()
        if not v:
            err(chan, f"{label} obligatwa.")
        data[chan] = v
    q = data.get("kantite_voye")
    if q is None or q <= 0:
        err("kantite_voye", "Kantite a dwe plis pase 0.")
    elif q > MAX_GALON:
        err("kantite_voye", f"Kantite a twò wo (maks {MAX_GALON} galon).")
    if data.get("pri_galon") is not None and data["pri_galon"] < 0:
        err("pri_galon", "Pri a pa ka negatif.")
    if data.get("telefon_chofe"):
        t = _norm_tel(data["telefon_chofe"])
        if not _TEL_RE.match(t):
            err("telefon_chofe", "Telefòn nan dwe sou fòma +509 ak 8 chif (egz: +50934567890).")
        data["telefon_chofe"] = t
    if data.get("no_bon"):
        n = _norm_bon(data["no_bon"])
        if not n:
            err("no_bon", "No bon livrezon an pa bon.")
        else:
            q2 = db.query(LivrezonKabiran).filter(LivrezonKabiran.no_bon_norm == n)
            if exclure_id:
                q2 = q2.filter(LivrezonKabiran.id != exclure_id)
            dbl = q2.first()
            if dbl:
                err("no_bon", f"Bon livrezon sa a deja anrejistre ({dbl.numero}, {dbl.station_nom}).")
        data["no_bon_norm"] = n
    for chan, n in (("chofe", 150), ("plak_kamyon", 30), ("konpayi", 150), ("no_bon", 60)):
        if len(data.get(chan) or "") > n:
            err(chan, f"Twò long (maks {n} karaktè).")
    if data.get("no_kontre_plen") is not None:
        data["no_kontre_plen"] = data["no_kontre_plen"].strip()[:60] or None
    if kreye and not data.get("no_kontre_plen"):
        err("no_kontre_plen", "Kontre plen obligatwa.")
    if data.get("komante") is not None:
        data["komante"] = data["komante"].strip()[:1000] or None
    if data.get("plak_kamyon"):
        data["plak_kamyon"] = data["plak_kamyon"].upper()
    # Station et produit (le produit doit appartenir à la station choisie)
    if "station_id" in data:
        try:
            st = _station(data["station_id"])
            data["station_nom"] = st["nom"]
            fams = _familles_station(st["id"])
            f = next((f for f in fams if data.get("produit_id") in f["produit_ids"]), None)
            if not f:
                err("produit_id", "Chwazi yon kabiran estasyon sa a.")
            else:
                data["produit_id"], data["produit_nom"] = f["id"], f["nom"]   # Gazoline / Diesel
        except HTTPException:
            err("station_id", "Chwazi yon estasyon.")
    if erreurs:
        raise HTTPException(422, {"code": "validasyon", "message": erreurs[0]["message"], "erreurs": erreurs})
    return data


# ── Stock de la station ─────────────────────────────────────────────────

def _ecrire_stock(l: LivrezonKabiran) -> int:
    """Crée la Livraison (stock) dans le schéma de la station de la livraison."""
    prix = float(l.pri_galon) if l.pri_galon is not None else _dernier_prix(l.station_id, l.produit_id)
    with session_espace(_schema(l.station_id)) as sdb:
        if not sdb.get(Produit, l.produit_id):
            raise HTTPException(409, "Kabiran sa a pa egziste ankò nan estasyon an.")
        dl = l.date_livrezon if l.date_livrezon.tzinfo else l.date_livrezon.replace(tzinfo=timezone.utc)
        d = dl.astimezone(HAITI_TZ).date()
        if l.resevwa_at:
            ra = l.resevwa_at if l.resevwa_at.tzinfo else l.resevwa_at.replace(tzinfo=timezone.utc)
            d = ra.astimezone(HAITI_TZ).date()
        lv = Livraison(produit_id=l.produit_id, date_livraison=d, gallons_recus=l.kantite_resevwa,
                       prix_achat_gallon=prix, fournisseur=l.konpayi[:150],
                       reference_camion=f"{l.plak_kamyon} · Bon {l.no_bon}"[:100],
                       notes=f"{l.numero} — app Resepsyon. Voye {float(l.kantite_voye):g} gal, "
                             f"resevwa {float(l.kantite_resevwa):g} gal. Chofè {l.chofe}."[:500])
        sdb.add(lv)
        sdb.commit()
        return lv.id


def _modifier_stock(l: LivrezonKabiran) -> None:
    if not l.livraison_id:
        return
    with session_espace(_schema(l.station_id)) as sdb:
        lv = sdb.get(Livraison, l.livraison_id)
        if lv:
            lv.gallons_recus = l.kantite_resevwa
            if l.pri_galon is not None:
                lv.prix_achat_gallon = l.pri_galon
            lv.fournisseur = l.konpayi[:150]
            lv.reference_camion = f"{l.plak_kamyon} · Bon {l.no_bon}"[:100]
            sdb.commit()


def _retirer_stock(l: LivrezonKabiran) -> None:
    if not l.livraison_id:
        return
    with session_espace(_schema(l.station_id)) as sdb:
        lv = sdb.get(Livraison, l.livraison_id)
        if lv:
            if lv.terminee or sdb.query(Livraison.id).filter(Livraison.report_vers_livraison_id == lv.id).first():
                raise HTTPException(409, "Kagezon sa a deja fèmen nan Stock › Livraisons : "
                                         "fòk ou korije l la a anvan.")
            sdb.delete(lv)
            sdb.commit()
    l.livraison_id = None


# ── Notifications (SMS / WhatsApp / Email) ─────────────────────────────

def _telefon_destinate(db: Session, l: LivrezonKabiran) -> list[tuple[str, str]]:
    """(rôle, téléphone) : chofè + numéros de la direction / gérants
    (LIVREZON_TELEFON, séparés par des virgules) + administrateurs avec téléphone."""
    out = [("Chofè", l.telefon_chofe)]
    for t in (os.getenv("LIVREZON_TELEFON") or "").split(","):
        t = _norm_tel(t.strip())
        if _TEL_RE.match(t):
            out.append(("Direksyon", t))
    for x in db.query(Utilisateur).filter(Utilisateur.actif.is_(True), Utilisateur.telephone.isnot(None)).all():
        r = x.role_obj
        if x.role in ("admin", "pdg") or (r and (r.est_admin or (r.permissions or {}).get("admin"))):
            out.append(("Pwopriyetè", x.telephone))
    vu, res = set(), []
    for role, t in out:
        if t and t not in vu:
            vu.add(t)
            res.append((role, t))
    return res


def _message(l: LivrezonKabiran, evenman: str) -> str:
    base = (f"Konekta — Livrezon {l.numero} ({l.station_nom}) : {l.produit_nom} "
            f"{float(l.kantite_voye):g} gal, kamyon {l.plak_kamyon}, chofè {l.chofe}.")
    return base + {
        "KREYE": " Livrezon anrejistre.",
        "APWOUVE": " Livrezon apwouve : an wout.",
        "REJTE": f" Livrezon rejte. {l.motif or ''}",
        "RESEVWA": f" Resevwa : {float(l.kantite_resevwa or 0):g} gal.",
        "EKAT": f" ALÈT EKAT : resevwa {float(l.kantite_resevwa or 0):g} gal, ekat {float(l.ekat or 0):g} gal.",
        "FEMEN": " Livrezon fèmen.",
        "ANILE": f" Livrezon anile. {l.motif or ''}",
    }.get(evenman, "")


def _voye_twilio(kanal: str, tel: str, corps: str) -> None:
    import requests
    import otp_service as o
    de = o.TWILIO_WHATSAPP_FROM if kanal == "WHATSAPP" else o.TWILIO_FROM_NUMBER
    if not (o.TWILIO_ACCOUNT_SID and o.TWILIO_AUTH_TOKEN and de):
        raise LookupError("pa konfigire")
    pre = "whatsapp:" if kanal == "WHATSAPP" else ""
    r = requests.post(o.TWILIO_API_URL.format(sid=o.TWILIO_ACCOUNT_SID),
                      auth=(o.TWILIO_ACCOUNT_SID, o.TWILIO_AUTH_TOKEN),
                      data={"To": pre + tel, "From": pre + de, "Body": corps[:1500]}, timeout=10)
    if r.status_code >= 300:
        raise RuntimeError(f"Twilio {r.status_code}")


def _voye_email(dest: list[str], sujet: str, corps: str) -> None:
    from email.mime.text import MIMEText
    import otp_service as o
    if not (o.EMAIL_USER and o.EMAIL_PASSWORD):
        raise LookupError("pa konfigire")
    msg = MIMEText(corps, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = sujet, f"{o.EMAIL_FROM_NAME} <{o.EMAIL_USER}>", o.EMAIL_USER
    with o._smtp_connect() as smtp:
        smtp.sendmail(o.EMAIL_USER, dest, msg.as_string())


def _essayer(fn, *args) -> tuple[str, int, Optional[str]]:
    """Jusqu'à 3 essais (1 s, 3 s entre deux). LookupError = canal non configuré."""
    err = None
    for n in range(1, 4):
        try:
            fn(*args)
            return "VOYE", n, None
        except LookupError as e:
            return "PA_KONFIGIRE", 0, str(e)
        except Exception as e:                      # réseau, Twilio, SMTP
            err = str(e)[:300]
            if n < 3:
                time.sleep(1 if n == 1 else 3)
    return "ECHWE", 3, err


def _notifier_maintenant(livrezon_id: int, evenman: str) -> None:
    from pompiste_routes import _emails_admins
    with session_principale() as db:
        l = db.get(LivrezonKabiran, livrezon_id)
        if not l:
            return
        corps = _message(l, evenman)
        lignes = []
        for _role, tel in _telefon_destinate(db, l):
            st, n, err = _essayer(_voye_twilio, "WHATSAPP", tel, corps)
            ranp = None
            if st != "VOYE":                     # canal de repli : SMS
                st2, n2, err2 = _essayer(_voye_twilio, "SMS", tel, corps)
                if st2 == "VOYE":
                    ranp = "SMS"
                lignes.append(LivrezonKabiranNotifikasyon(livrezon_id=l.id, evenman=evenman, kanal="SMS",
                                                          destinate=tel, statut=st2, eseye=n2, ere=err2))
            lignes.append(LivrezonKabiranNotifikasyon(livrezon_id=l.id, evenman=evenman, kanal="WHATSAPP",
                                                      destinate=tel, statut=st, eseye=n, ere=err, ranplasman=ranp))
        emails = _emails_admins(db)
        if emails:
            st, n, err = _essayer(_voye_email, emails, f"[KONEKTA] {corps[:120]}", corps)
            lignes.append(LivrezonKabiranNotifikasyon(livrezon_id=l.id, evenman=evenman, kanal="EMAIL",
                                                      destinate=", ".join(emails)[:254], statut=st, eseye=n, ere=err))
        db.add_all(lignes)
        db.commit()


def notifier(db: Session, l: LivrezonKabiran, evenman: str) -> None:
    """Notification dans Konekta (cloche) tout de suite ; SMS / WhatsApp /
    Email en arrière-plan (jamais bloquant pour la personne qui saisit)."""
    try:
        from notifications_service import creer_notification
        creer_notification(db, "carburant", "livrezon", f"Livrezon {l.numero} — {STATUT_KREYOL.get(l.statut)}",
                           _message(l, evenman), "livrezon-kabiran", dedupe_minutes=None)
        db.commit()
    except Exception:
        db.rollback()
        log.exception("notification interne livrezon %s", l.id)
    if os.getenv("LIVREZON_NOTIF_SYNC") == "1":          # tests
        _notifier_maintenant(l.id, evenman)
        return
    threading.Thread(target=_safe_notifier, args=(l.id, evenman), daemon=True).start()


def _safe_notifier(lid: int, evenman: str) -> None:
    try:
        _notifier_maintenant(lid, evenman)
    except Exception:
        log.exception("notifications livrezon %s", lid)


# ── Connexion ───────────────────────────────────────────────────────────

class IdentifierIn(BaseModel):
    username: str


class LoginIn(BaseModel):
    username: str
    pin: str


def _compte(db: Session, username: str) -> Optional[Utilisateur]:
    u = (db.query(Utilisateur).filter(Utilisateur.username.ilike(username.strip()),
                                      Utilisateur.actif.is_(True)).first())
    return u if u and est_role_resepsyon(u.role_obj) else None


@router.post("/api/resepsyon/identifier")
def identifier(data: IdentifierIn, request: Request):
    import main as m
    cle = f"{_ip(request)}|resepsyon-code"
    m._check_login_rate(cle, maximum=30)
    with session_principale() as db:
        u = _compte(db, data.username) if data.username.strip() else None
        if not u:
            m._record_login_failure(cle)
            raise HTTPException(404, {"code": "code_inconnu", "message": "Kòd sa a pa egziste."})
        return {"prenom": (u.nom_complet or "").split(" ")[0]}


@router.post("/api/login/resepsyon")
def login(data: LoginIn, request: Request, response: Response):
    import main as m
    from activity_log import LOGIN_FAILED, LOGIN_SUCCESS, log_event
    from auth import SESSION_COOKIE, create_session
    ip = _ip(request)
    username = data.username.strip().lower()
    cle = f"{ip}|resepsyon|{username}"
    m._check_login_rate(cle)
    with session_principale() as db:
        u = _compte(db, username)
        if not u or not u.code_acces_hash or not verify_code_acces(data.pin.strip(), u.code_acces_hash):
            m._record_login_failure(cle)
            log_event(db, LOGIN_FAILED, user_id=u.id if u else None, ip_address=ip,
                      details={"username": username, "espace": "resepsyon"})
            raise HTTPException(401, "Kòd oswa PIN pa bon")
        m._clear_login_failures(cle)
        token = create_session(db, u.id, ip_address=ip, user_agent=request.headers.get("user-agent", ""))
        log_event(db, LOGIN_SUCCESS, user_id=u.id, ip_address=ip, details={"espace": "resepsyon"})
        response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", secure=m._SECURE_COOKIES,
                            max_age=12 * 3600, path="/")
        return {"id": u.id, "username": u.username.upper(), "nom_complet": u.nom_complet}


# ── API de l'application ───────────────────────────────────────────────

@router.get("/api/resepsyon/me")
def me(u: Utilisateur = Depends(_require_resepsyon)):
    return {"id": u.id, "username": (u.username or "").upper(), "nom_complet": u.nom_complet,
            "resepsyon": est_role_resepsyon(u.role_obj), "tolerans_galon": tolerans(0)}


@router.get("/api/resepsyon/stations")
def stations(_u: Utilisateur = Depends(_require_resepsyon)):
    out = []
    for st in _stations():
        try:
            prods = _familles_station(st["id"], avec_stock=True)
        except Exception:
            log.exception("produits station %s", st["id"])
            prods = []
        out.append(dict(st, produits=prods))
    return {"stations": out}


@router.post("/api/resepsyon/livrezon", status_code=201)
def kreye(data: LivrezonIn, request: Request, u: Utilisateur = Depends(_require_resepsyon),
          udb: Session = Depends(get_db)):
    with session_principale() as db:
        acteur = _Acteur(udb, u)
        v = _verifier(db, data.model_dump(), kreye=True)
        l = LivrezonKabiran(
            numero=_prochain_numero(db), station_id=v["station_id"], station_nom=v["station_nom"],
            produit_id=v["produit_id"], produit_nom=v["produit_nom"], date_livrezon=datetime.now(timezone.utc),  # dat & lè otomatik
            no_kontre_plen=v["no_kontre_plen"], no_bon=v["no_bon"], no_bon_norm=v["no_bon_norm"],
            kantite_voye=v["kantite_voye"], pri_galon=v["pri_galon"], chofe=v["chofe"],
            telefon_chofe=v["telefon_chofe"], plak_kamyon=v["plak_kamyon"], konpayi=v["konpayi"],
            komante=v["komante"], statut="VALIDE", cree_par_id=acteur.id)
        db.add(l)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            raise HTTPException(422, {"code": "validasyon", "message": "Bon livrezon sa a deja anrejistre.",
                                      "erreurs": [{"chan": "no_bon", "message": "Bon livrezon sa a deja anrejistre."}]})
        _jounal(db, l, "KREYE", None, acteur, request)
        db.commit()
        db.refresh(l)
        notifier(db, l, "KREYE")
        return livrezon_dict(l)


@router.get("/api/resepsyon/livrezon")
def mes_livrezons(statut: Optional[str] = None, u: Utilisateur = Depends(_require_resepsyon),
                  udb: Session = Depends(get_db)):
    from pompiste_routes import peut_superviser
    with session_principale() as db:
        q = db.query(LivrezonKabiran)
        if not peut_superviser(u):
            # Ses livraisons + toutes celles qui attendent une réception
            q = q.filter((LivrezonKabiran.cree_par_id == u.id) | (LivrezonKabiran.statut.in_(EN_KOU)))
        if statut:
            q = q.filter(LivrezonKabiran.statut.in_([s for s in statut.split(",") if s in STATUTS]))
        rows = q.order_by(LivrezonKabiran.date_livrezon.desc(), LivrezonKabiran.id.desc()).limit(100).all()
        return {"livrezons": [livrezon_dict(l) for l in rows]}


def _charger(db: Session, lid: int) -> LivrezonKabiran:
    l = db.get(LivrezonKabiran, lid)
    if not l:
        raise HTTPException(404, "Livrezon sa a pa egziste")
    return l


@router.get("/api/resepsyon/livrezon/{lid}")
def detay(lid: int, _u: Utilisateur = Depends(_require_resepsyon)):
    with session_principale() as db:
        return livrezon_dict(_charger(db, lid), jounal=True, db=db)


def _fini_resepsyon(db: Session, l: LivrezonKabiran, acteur, request: Request) -> None:
    """Après la réception : écart > tolérance → EKAT (alerte), sinon FEMEN."""
    l.ekat = round(float(l.kantite_voye) - float(l.kantite_resevwa), 3)
    ansyen = l.statut
    l.statut = "EKAT" if abs(float(l.ekat)) > tolerans(float(l.kantite_voye)) else "FEMEN"
    _jounal(db, l, "EKAT" if l.statut == "EKAT" else "FEMEN", ansyen, acteur, request,
            {"ekat": float(l.ekat), "tolerans": tolerans(float(l.kantite_voye))})


@router.post("/api/resepsyon/livrezon/{lid}/resevwa")
def resevwa(lid: int, data: ResepsyonIn, request: Request, u: Utilisateur = Depends(_require_resepsyon),
            udb: Session = Depends(get_db)):
    if data.kantite_resevwa is None or data.kantite_resevwa <= 0:
        raise HTTPException(422, {"code": "validasyon", "message": "Kantite resevwa a dwe plis pase 0.",
                                  "erreurs": [{"chan": "kantite_resevwa", "message": "Kantite resevwa a dwe plis pase 0."}]})
    if data.kantite_resevwa > MAX_GALON:
        raise HTTPException(422, {"code": "validasyon", "message": "Kantite a twò wo."})
    for v in (data.jog_avan, data.jog_apre):
        if v is not None and v < 0:
            raise HTTPException(422, {"code": "validasyon", "message": "Jòj tank lan pa ka negatif."})
    if data.jog_avan is not None and data.jog_apre is not None and data.jog_apre < data.jog_avan:
        raise HTTPException(422, {"code": "validasyon", "message": "Jòj apre a dwe pi wo pase jòj avan an."})
    with session_principale() as db:
        acteur = _Acteur(udb, u)
        l = db.query(LivrezonKabiran).filter_by(id=lid).with_for_update().first() if db.bind.dialect.name == "postgresql" \
            else _charger(db, lid)
        if not l:
            raise HTTPException(404, "Livrezon sa a pa egziste")
        if l.statut not in EN_KOU:
            raise HTTPException(409, f"Livrezon sa a deja {STATUT_KREYOL.get(l.statut, l.statut).lower()}.")
        ansyen = l.statut
        l.kantite_resevwa, l.jog_avan, l.jog_apre = data.kantite_resevwa, data.jog_avan, data.jog_apre
        l.komante_resepsyon = (data.komante or "").strip()[:1000] or None
        l.resevwa_par_id, l.resevwa_at, l.statut = acteur.id, datetime.now(timezone.utc), "RESEVWA"
        l.livraison_id = _ecrire_stock(l)             # stock de la station choisie
        try:
            _jounal(db, l, "RESEVWA", ansyen, acteur, request,
                    {"kantite_resevwa": data.kantite_resevwa, "jog_avan": data.jog_avan, "jog_apre": data.jog_apre,
                     "livraison_id": l.livraison_id})
            _fini_resepsyon(db, l, acteur, request)
            l.updated_at = datetime.now(timezone.utc)
            db.commit()
        except Exception:
            db.rollback()
            # Ne pas laisser de stock orphelin dans la station
            try:
                with session_espace(_schema(l.station_id)) as sdb:
                    lv = sdb.get(Livraison, l.livraison_id)
                    if lv:
                        sdb.delete(lv)
                        sdb.commit()
            except Exception:
                log.exception("nettoyage livraison orpheline")
            raise
        db.refresh(l)
        notifier(db, l, "EKAT" if l.statut == "EKAT" else "RESEVWA")
        return livrezon_dict(l, jounal=True, db=db)


# ── Fiche PDF ──────────────────────────────────────────────────────────

def _fiche_station(station_id: int) -> dict:
    try:
        from models import StationGeo
        with session_principale() as pdb:
            f = pdb.query(StationGeo).filter_by(station_id=station_id).first()
            return {"adresse": f.adresse, "telephone": f.telephone} if f else {}
    except Exception:
        return {}


def pdf_livrezon(d: dict) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=1.6 * cm, rightMargin=1.6 * cm,
                            topMargin=1.4 * cm, bottomMargin=1.4 * cm,
                            title=f"Livrezon {d['numero']}", author="Konekta")
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Title"], fontSize=17, spaceAfter=2, alignment=0)
    pt = ParagraphStyle("pt", parent=ss["Normal"], fontSize=9, textColor=colors.HexColor("#475569"))
    nor = ParagraphStyle("n", parent=ss["Normal"], fontSize=10, leading=13)
    h2 = ParagraphStyle("h2", parent=ss["Heading3"], fontSize=11.5, spaceBefore=10, spaceAfter=4,
                        textColor=colors.HexColor("#0f172a"))

    def fmt_dt(s):
        if not s:
            return "—"
        try:
            return datetime.fromisoformat(s).strftime("%d/%m/%Y %H:%M")
        except ValueError:
            return s

    def g(x):
        return "—" if x is None else f"{x:,.3f}".replace(",", " ").rstrip("0").rstrip(".") + " gal"

    nom_site = os.getenv("BRANDING_NOM", "").strip() or "Konekta"
    lignes = [
        ("Estasyon", d["station_nom"]),
        ("Tip kabiran", d["produit_nom"]),
        ("Dat & lè", fmt_dt(d["resevwa_at"] or d["date_livrezon"])),
        ("Kontre plen", d["no_kontre_plen"]),
        ("Konpayi", d["konpayi"]),
        ("Kantite resevwa", g(d["kantite_resevwa"])),
    ]
    big = ParagraphStyle("big", parent=nor, fontSize=13, leading=17)
    t = Table([[Paragraph(f"<b>{k}</b>", big), Paragraph(str(v if v not in (None, "") else "—"), big)] for k, v in lignes],
              colWidths=(6 * cm, 11.4 * cm))
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                           ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f0fdfa")),
                           ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                           ("TOPPADDING", (0, 0), (-1, -1), 9), ("BOTTOMPADDING", (0, 0), (-1, -1), 9)]))
    el = [Paragraph("Fich livrezon kabiran", h1), Paragraph(nom_site, pt), Spacer(1, 14), t]
    doc.build(el)
    return buf.getvalue()


def _pdf_reponse(lid: int) -> Response:
    with session_principale() as db:
        d = livrezon_dict(_charger(db, lid), jounal=True, db=db)
    return Response(pdf_livrezon(d), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="livrezon-{d["numero"]}.pdf"',
                             "Cache-Control": "no-store"})


@router.get("/api/resepsyon/livrezon/{lid}/pdf")
def pdf_resepsyon(lid: int, _u: Utilisateur = Depends(_require_resepsyon)):
    return _pdf_reponse(lid)


@router.get("/api/livrezon-admin/livrezon/{lid}/pdf")
def pdf_admin(lid: int, _u: Utilisateur = Depends(_require_superviseur)):
    return _pdf_reponse(lid)


# ── Administration ─────────────────────────────────────────────────────

def _periode(debut: Optional[str], fin: Optional[str]) -> tuple:
    from datetime import date as date_type
    try:
        f = date_type.fromisoformat(fin) if fin else today_haiti()
        d = date_type.fromisoformat(debut) if debut else f - timedelta(days=29)
    except ValueError:
        raise HTTPException(400, "Dat pa bon (YYYY-MM-DD)")
    if d > f:
        d, f = f, d
    a = datetime(d.year, d.month, d.day, tzinfo=HAITI_TZ)
    b = datetime(f.year, f.month, f.day, tzinfo=HAITI_TZ) + timedelta(days=1)
    return d, f, a.astimezone(timezone.utc), b.astimezone(timezone.utc)


@router.get("/api/livrezon-admin/livrezon")
def admin_lister(statut: Optional[str] = None, station_id: Optional[int] = None, debut: Optional[str] = None,
                 fin: Optional[str] = None, q: Optional[str] = None,
                 _u: Utilisateur = Depends(_require_superviseur)):
    with session_principale() as db:
        x = db.query(LivrezonKabiran)
        if debut or fin:
            _d, _f, a, b = _periode(debut, fin)
            x = x.filter(LivrezonKabiran.date_livrezon >= a, LivrezonKabiran.date_livrezon < b)
        if statut:
            x = x.filter(LivrezonKabiran.statut.in_([s for s in statut.split(",") if s in STATUTS]))
        if station_id is not None:
            x = x.filter(LivrezonKabiran.station_id == station_id)
        if q:
            t = f"%{q.strip()}%"
            x = x.filter(LivrezonKabiran.numero.ilike(t) | LivrezonKabiran.no_bon.ilike(t)
                         | LivrezonKabiran.chofe.ilike(t) | LivrezonKabiran.plak_kamyon.ilike(t)
                         | LivrezonKabiran.konpayi.ilike(t))
        rows = x.order_by(LivrezonKabiran.date_livrezon.desc(), LivrezonKabiran.id.desc()).limit(500).all()
        return {"livrezons": [livrezon_dict(l) for l in rows]}


# ── Registre du gaz reçu (une station à la fois) ────────────────────────

RECUS = ("RESEVWA", "FEMEN", "EKAT")


def _registre(station_id: Optional[int], debut: Optional[str], fin: Optional[str], produit: Optional[str],
              konpayi: Optional[str], q: Optional[str]) -> dict:
    toutes = _stations()
    if station_id is None or not any(s["id"] == station_id for s in toutes):
        station_id = toutes[0]["id"] if toutes else 0
    st_nom = next((s["nom"] for s in toutes if s["id"] == station_id), "")
    d, f, a, b = _periode(debut, fin)
    with session_principale() as db:
        base = db.query(LivrezonKabiran).filter(LivrezonKabiran.station_id == station_id,
                                                LivrezonKabiran.statut.in_(RECUS),
                                                LivrezonKabiran.resevwa_at >= a, LivrezonKabiran.resevwa_at < b)
        # Choix possibles des filtres (sur la période, avant les filtres produit / compagnie)
        produits = sorted({r[0] for r in base.with_entities(LivrezonKabiran.produit_nom).distinct()})
        konpayis = sorted({r[0] for r in base.with_entities(LivrezonKabiran.konpayi).distinct()})
        x = base
        if produit:
            x = x.filter(LivrezonKabiran.produit_nom == produit)
        if konpayi:
            x = x.filter(LivrezonKabiran.konpayi == konpayi)
        if q and q.strip():
            t = f"%{q.strip()}%"
            x = x.filter(LivrezonKabiran.numero.ilike(t) | LivrezonKabiran.no_bon.ilike(t)
                         | LivrezonKabiran.no_kontre_plen.ilike(t) | LivrezonKabiran.chofe.ilike(t)
                         | LivrezonKabiran.plak_kamyon.ilike(t) | LivrezonKabiran.konpayi.ilike(t))
        rows = x.order_by(LivrezonKabiran.resevwa_at.desc(), LivrezonKabiran.id.desc()).limit(5000).all()
        lignes = [livrezon_dict(l) for l in rows]
    tot = {}
    for l in lignes:
        e = tot.setdefault(l["produit_nom"], {"produit": l["produit_nom"], "kamyon": 0, "voye": 0.0, "resevwa": 0.0})
        e["kamyon"] += 1
        e["voye"] += l["kantite_voye"] or 0
        e["resevwa"] += l["kantite_resevwa"] or 0
    totaux = [{**e, "voye": round(e["voye"], 3), "resevwa": round(e["resevwa"], 3),
               "ekat": round(e["voye"] - e["resevwa"], 3)} for e in sorted(tot.values(), key=lambda e: e["produit"])]
    return {"debut": d.isoformat(), "fin": f.isoformat(), "station_id": station_id, "station_nom": st_nom,
            "lignes": lignes, "totaux": totaux, "produits": produits, "konpayis": konpayis,
            "stations": [{"id": s["id"], "nom": s["nom"]} for s in toutes]}


@router.get("/api/livrezon-admin/resepsyon-lis")
def registre(station_id: Optional[int] = None, debut: Optional[str] = None, fin: Optional[str] = None,
             produit: Optional[str] = None, konpayi: Optional[str] = None, q: Optional[str] = None,
             _u: Utilisateur = Depends(_require_superviseur)):
    return _registre(station_id, debut, fin, produit, konpayi, q)


@router.get("/api/livrezon-admin/resepsyon-lis.csv")
def registre_csv(station_id: Optional[int] = None, debut: Optional[str] = None, fin: Optional[str] = None,
                 produit: Optional[str] = None, konpayi: Optional[str] = None, q: Optional[str] = None,
                 _u: Utilisateur = Depends(_require_superviseur)):
    import csv
    r = _registre(station_id, debut, fin, produit, konpayi, q)

    def dh(s):
        if not s:
            return ""
        dt = datetime.fromisoformat(s)
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(HAITI_TZ).strftime("%d/%m/%Y %H:%M")

    def n(x):
        return "" if x is None else f"{x:.3f}".rstrip("0").rstrip(".").replace(".", ",")

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow([f"Gaz reçu — {r['station_nom']}", f"du {r['debut']} au {r['fin']}"])
    w.writerow(["Date & heure", "N°", "Produit", "Kontre plen", "Compagnie", "N° bon", "Camion", "Chauffeur",
                "Envoyé (gal)", "Reçu (gal)", "Écart (gal)", "Statut", "Reçu par"])
    for l in r["lignes"]:
        w.writerow([dh(l["resevwa_at"]), l["numero"], l["produit_nom"], l["no_kontre_plen"] or "", l["konpayi"],
                    l["no_bon"], l["plak_kamyon"], l["chofe"], n(l["kantite_voye"]), n(l["kantite_resevwa"]),
                    n(l["ekat"]), l["statut_label"], l["resevwa_par"] or ""])
    w.writerow([])
    for t in r["totaux"]:
        w.writerow([f"Total {t['produit']}", f"{t['kamyon']} camion(s)", "", "", "", "", "", "",
                    n(t["voye"]), n(t["resevwa"]), n(t["ekat"])])
    nom = re.sub(r"[^A-Za-z0-9]+", "-", r["station_nom"]).strip("-").lower() or "station"
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="gaz-recu-{nom}-{r["debut"]}-{r["fin"]}.csv"'})

@router.get("/api/livrezon-admin/livrezon/{lid}")
def admin_detay(lid: int, _u: Utilisateur = Depends(_require_superviseur)):
    with session_principale() as db:
        return livrezon_dict(_charger(db, lid), jounal=True, db=db)


def _changer_statut(lid: int, request: Request, u: Utilisateur, udb: Session, permis: tuple, nouvo: str,
                    aksyon: str, motif: Optional[str] = None, motif_requis: bool = False) -> dict:
    motif = (motif or "").strip()
    if motif_requis and len(motif) < 3:
        raise HTTPException(422, {"code": "validasyon", "message": "Ekri rezon an."})
    with session_principale() as db:
        acteur = _Acteur(udb, u)
        l = _charger(db, lid)
        if l.statut not in permis:
            raise HTTPException(409, f"Livrezon sa a {STATUT_KREYOL.get(l.statut, l.statut).lower()} : aksyon sa a pa posib.")
        ansyen = l.statut
        if nouvo == "ANILE" and l.livraison_id:
            _retirer_stock(l)                       # le stock de la station est remis comme avant
        l.statut = nouvo
        l.revize_par_id, l.revize_at = acteur.id, datetime.now(timezone.utc)
        if motif:
            l.motif = motif[:500]
        l.updated_at = datetime.now(timezone.utc)
        _jounal(db, l, aksyon, ansyen, acteur, request, {"motif": motif} if motif else None)
        db.commit()
        db.refresh(l)
        notifier(db, l, aksyon)
        return livrezon_dict(l, jounal=True, db=db)


@router.post("/api/livrezon-admin/livrezon/{lid}/apwouve")
def apwouve(lid: int, request: Request, u: Utilisateur = Depends(_require_superviseur), udb: Session = Depends(get_db)):
    return _changer_statut(lid, request, u, udb, ("VALIDE",), "AN_WOUT", "APWOUVE")


@router.post("/api/livrezon-admin/livrezon/{lid}/rejte")
def rejte(lid: int, data: MotifIn, request: Request, u: Utilisateur = Depends(_require_superviseur),
          udb: Session = Depends(get_db)):
    return _changer_statut(lid, request, u, udb, EN_KOU, "REJTE", "REJTE", data.motif, True)


@router.post("/api/livrezon-admin/livrezon/{lid}/femen")
def femen(lid: int, data: MotifIn, request: Request, u: Utilisateur = Depends(_require_superviseur),
          udb: Session = Depends(get_db)):
    """Clôture d'une livraison en écart, après vérification par la direction."""
    return _changer_statut(lid, request, u, udb, ("EKAT",), "FEMEN", "FEMEN", data.motif, True)


@router.post("/api/livrezon-admin/livrezon/{lid}/anile")
def anile(lid: int, data: MotifIn, request: Request, u: Utilisateur = Depends(_require_superviseur),
          udb: Session = Depends(get_db)):
    return _changer_statut(lid, request, u, udb, ("VALIDE", "AN_WOUT", "FEMEN", "EKAT"), "ANILE", "ANILE",
                           data.motif, True)


@router.put("/api/livrezon-admin/livrezon/{lid}")
def modifye(lid: int, data: LivrezonPatch, request: Request, u: Utilisateur = Depends(_require_superviseur),
            udb: Session = Depends(get_db)):
    chan = {k: v for k, v in data.model_dump().items() if v is not None}
    with session_principale() as db:
        acteur = _Acteur(udb, u)
        l = _charger(db, lid)
        if l.statut in ("REJTE", "ANILE"):
            raise HTTPException(409, "Livrezon sa a fèmen nèt : li pa ka modifye.")
        resevwa_deja = l.livraison_id is not None
        if resevwa_deja and ({"station_id", "produit_id"} & chan.keys()):
            if chan.get("station_id", l.station_id) != l.station_id or chan.get("produit_id", l.produit_id) != l.produit_id:
                raise HTTPException(409, "Livrezon an deja nan stock estasyon an : anile l epi refè l pou chanje estasyon.")
        if "kantite_resevwa" in chan and not resevwa_deja:
            raise HTTPException(409, "Livrezon an poko resevwa.")
        avan = livrezon_dict(l)
        v = {c: getattr(l, c) for c in ("station_id", "produit_id", "no_kontre_plen", "no_bon", "chofe",
                                         "telefon_chofe", "plak_kamyon", "konpayi", "komante", "pri_galon")}
        v["kantite_voye"] = float(l.kantite_voye)
        v["pri_galon"] = _f(v["pri_galon"])
        v.update({k: c for k, c in chan.items() if k not in ("date_livrezon", "kantite_resevwa")})
        v = _verifier(db, v, exclure_id=l.id)
        for c in ("station_id", "station_nom", "produit_id", "produit_nom", "no_kontre_plen", "no_bon", "no_bon_norm",
                  "kantite_voye", "pri_galon", "chofe", "telefon_chofe", "plak_kamyon", "konpayi", "komante"):
            setattr(l, c, v[c])
        if "date_livrezon" in chan:
            l.date_livrezon = _parse_date(chan["date_livrezon"])
        if "kantite_resevwa" in chan:
            if chan["kantite_resevwa"] <= 0:
                raise HTTPException(422, {"code": "validasyon", "message": "Kantite resevwa a dwe plis pase 0."})
            l.kantite_resevwa = chan["kantite_resevwa"]
        if resevwa_deja:
            _fini_resepsyon(db, l, acteur, request) if l.statut in ("FEMEN", "EKAT") else None
            _modifier_stock(l)
        l.updated_at = datetime.now(timezone.utc)
        apre = livrezon_dict(l)
        chanje = {k: [avan[k], apre[k]] for k in apre if avan.get(k) != apre.get(k)
                  and k not in ("updated_at", "statut_label", "tolerans")}
        _jounal(db, l, "MODIFYE", avan["statut"], acteur, request, chanje)
        db.commit()
        db.refresh(l)
        return livrezon_dict(l, jounal=True, db=db)


@router.get("/api/livrezon-admin/dashboard")
def dashboard(debut: Optional[str] = None, fin: Optional[str] = None, station_id: Optional[int] = None,
              _u: Utilisateur = Depends(_require_superviseur)):
    """Tableau de bord d'UNE station (jamais de cumul entre stations) :
    chaque arrivée de camion, commandes en cours, écarts, stock de la station."""
    toutes = _stations()
    if station_id is None or not any(s["id"] == station_id for s in toutes):
        station_id = toutes[0]["id"] if toutes else 0
    st_nom = next((s["nom"] for s in toutes if s["id"] == station_id), "")
    d, f, a, b = _periode(debut, fin)
    with session_principale() as db:
        base = db.query(LivrezonKabiran).filter(LivrezonKabiran.station_id == station_id)
        rows = base.filter(LivrezonKabiran.date_livrezon >= a, LivrezonKabiran.date_livrezon < b).all()
        en_kou = base.filter(LivrezonKabiran.statut.in_(EN_KOU)).order_by(LivrezonKabiran.date_livrezon).all()
        recus = [l for l in rows if l.statut in ("RESEVWA", "FEMEN", "EKAT")]
        recus.sort(key=lambda l: (l.resevwa_at or l.date_livrezon).replace(tzinfo=None), reverse=True)
        statuts = {s: 0 for s in STATUTS}
        for l in rows:
            statuts[l.statut] += 1
        ekat_rows = [l for l in recus if l.statut == "EKAT"]
        arrivees = [livrezon_dict(l) for l in recus]
        kpi = {
            "nb_kamyon": len(recus),
            "dernye_arive": arrivees[0]["resevwa_at"] if arrivees else None,
            "nb_en_kou": len(en_kou),
            "nb_ekat": len(ekat_rows),
        }
        en_kou_d = [livrezon_dict(l) for l in en_kou]
        ekat_d = [livrezon_dict(l) for l in ekat_rows]
    try:
        stock = _familles_station(station_id, True)
    except Exception:
        log.exception("stock station %s", station_id)
        stock = []
    return {
        "debut": d.isoformat(), "fin": f.isoformat(), "station_id": station_id, "station_nom": st_nom,
        "kpi": kpi, "statuts": statuts, "arrivees": arrivees, "en_kou": en_kou_d, "ekat": ekat_d,
        "stock": stock, "stations": [{"id": s["id"], "nom": s["nom"]} for s in toutes],
    }


@router.get("/api/livrezon-admin/notifikasyon")
def notifikasyon(_u: Utilisateur = Depends(_require_superviseur)):
    with session_principale() as db:
        rows = (db.query(LivrezonKabiranNotifikasyon, LivrezonKabiran.numero)
                .join(LivrezonKabiran, LivrezonKabiran.id == LivrezonKabiranNotifikasyon.livrezon_id)
                .order_by(LivrezonKabiranNotifikasyon.id.desc()).limit(200).all())
        return {"notifikasyon": [{"numero": num, "evenman": n.evenman, "kanal": n.kanal, "destinate": n.destinate,
                                  "statut": n.statut, "eseye": n.eseye, "ere": n.ere, "ranplasman": n.ranplasman,
                                  "at": _iso(n.created_at)} for n, num in rows]}


# ── Comptes de réception ──────────────────────────────────────────────

class KontIn(BaseModel):
    nom_complet: str
    pin: Optional[str] = None          # 6 chiffres ; vide = généré


class KontPatch(BaseModel):
    nom_complet: Optional[str] = None
    actif: Optional[bool] = None
    nouvo_pin: Optional[bool] = None   # True = générer un nouveau PIN
    pin: Optional[str] = None


def _nouveau_code(db: Session) -> str:
    for _ in range(200):
        c = "".join(secrets.choice("0123456789") for _ in range(4)) + "".join(secrets.choice(_LETTRES) for _ in range(4))
        if not db.query(Utilisateur.id).filter(Utilisateur.username.ilike(c)).first():
            return c
    raise HTTPException(500, "Impossible de générer un code")


def _pin(pin: Optional[str]) -> str:
    if pin:
        pin = pin.strip()
        if not _PIN_RE.match(pin):
            raise HTTPException(422, {"code": "validasyon", "message": "PIN nan dwe gen 6 chif."})
        return pin
    return "".join(secrets.choice("0123456789") for _ in range(6))


def _kont_dict(u: Utilisateur, db: Session) -> dict:
    nb = db.query(func.count(LivrezonKabiran.id)).filter(LivrezonKabiran.cree_par_id == u.id).scalar()
    return {"id": u.id, "kod": (u.username or "").upper(), "nom_complet": u.nom_complet, "actif": u.actif,
            "created_at": _iso(u.created_at), "nb_livrezon": nb}


@router.get("/api/livrezon-admin/kont")
def kont_lister(_u: Utilisateur = Depends(_require_superviseur)):
    with session_principale() as db:
        r = assurer_role_resepsyon(db)
        return {"kont": [_kont_dict(u, db) for u in db.query(Utilisateur).filter(Utilisateur.role_id == r.id)
                         .order_by(Utilisateur.actif.desc(), Utilisateur.nom_complet).all()]}


@router.post("/api/livrezon-admin/kont", status_code=201)
def kont_kreye(data: KontIn, _u: Utilisateur = Depends(_require_superviseur)):
    nom = " ".join((data.nom_complet or "").split())
    if len(nom) < 3:
        raise HTTPException(422, {"code": "validasyon", "message": "Ekri non moun nan."})
    pin = _pin(data.pin)
    with session_principale() as db:
        r = assurer_role_resepsyon(db)
        code = _nouveau_code(db)
        u = Utilisateur(username=code.lower(), nom_complet=nom[:150], password_hash=hash_password(secrets.token_urlsafe(24)),
                        role="operateur", role_id=r.id, poste=r.nom, actif=True, code_acces_hash=hash_code_acces(pin))
        db.add(u)
        db.commit()
        db.refresh(u)
        return dict(_kont_dict(u, db), pin=pin)


@router.put("/api/livrezon-admin/kont/{uid}")
def kont_modifye(uid: int, data: KontPatch, _u: Utilisateur = Depends(_require_superviseur)):
    with session_principale() as db:
        u = db.get(Utilisateur, uid)
        if not u or not est_role_resepsyon(u.role_obj):
            raise HTTPException(404, "Kont sa a pa egziste")
        pin = None
        if data.nom_complet is not None:
            nom = " ".join(data.nom_complet.split())
            if len(nom) < 3:
                raise HTTPException(422, {"code": "validasyon", "message": "Ekri non moun nan."})
            u.nom_complet = nom[:150]
        if data.actif is not None:
            u.actif = data.actif
            if not data.actif:
                from models import SessionToken
                db.query(SessionToken).filter(SessionToken.user_id == u.id).delete()
        if data.nouvo_pin or data.pin:
            pin = _pin(data.pin)
            u.code_acces_hash = hash_code_acces(pin)
        db.commit()
        out = _kont_dict(u, db)
        if pin:
            out["pin"] = pin
        return out
