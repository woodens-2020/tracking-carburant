"""
Stations séparées (« espaces ») — préfixe /api/espaces

Un propriétaire peut avoir plusieurs stations. Une station « séparée » est
un Konekta à part : ses données vivent dans leur propre schéma de base
(voir database.creer_schema_espace) ; une station « fusionnée » reste dans
l'espace où elle a été enregistrée, distinguée par l'adresse de ses pompes.

- Connexion : le compte est cherché dans l'espace principal puis dans
  chaque station séparée ; la requête et les suivantes travaillent dans
  l'espace du compte (cookie konekta_espace, voir main.EspaceMiddleware).
- Le propriétaire (administrateur / PDG de l'espace principal) voit toutes
  ses stations et passe de l'une à l'autre avec son PIN : il est déconnecté
  de la station quittée, une session s'ouvre dans l'autre (sous un compte
  « miroir » qui reprend son email, son mot de passe et son PIN).
- Création d'une station séparée : registre dans l'espace principal, schéma
  vierge, compte du gérant (email, mot de passe, PIN) et miroir du
  propriétaire ; la station (pompes, faces, mètres) y est enregistrée.
"""
from __future__ import annotations

import re
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from auth import create_session, delete_session, hash_code_acces, hash_password, verify_code_acces
from database import (creer_schema_espace, espace_courant, get_db, session_espace,
                      session_principale)
from models import Espace, Utilisateur

router = APIRouter(tags=["espaces"])

ESPACE_COOKIE = "konekta_espace"
_CACHE = {"t": 0.0, "schemas": {}}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PIN_RE = re.compile(r"^[0-9]{9}$")


# ── Registre (espace principal) ─────────────────────────────────────────

def schemas_actifs() -> dict:
    """{schema: nom} des stations séparées actives (cache 30 s)."""
    if time.time() - _CACHE["t"] > 30:
        try:
            with session_principale() as db:
                rows = db.execute(text("SELECT schema, nom FROM espaces WHERE actif = :a"), {"a": True}).fetchall()
            _CACHE["schemas"] = {r[0]: r[1] for r in rows}
        except Exception:
            _CACHE["schemas"] = {}
        _CACHE["t"] = time.time()
    return _CACHE["schemas"]


def invalider_cache():
    _CACHE["t"] = 0.0


def espace_du_compte(email: Optional[str] = None, username: Optional[str] = None) -> Optional[str]:
    """Espace où vit le compte (None = espace principal, qui a la priorité :
    le propriétaire y est, ses comptes miroirs ne comptent pas)."""
    def trouve(db) -> bool:
        # Les comptes d'un pompiste dans les autres stations ne comptent pas
        q = db.query(Utilisateur).filter(Utilisateur.actif.is_(True), Utilisateur.compte_origine.is_(None))
        if email:
            return q.filter(Utilisateur.email == email.strip().lower()).first() is not None
        if username:
            return q.filter(Utilisateur.username.ilike(username.strip().lower())).first() is not None
        return False
    if not (email or username):
        return None
    with session_principale() as db:
        if trouve(db):
            return None
    for sch in schemas_actifs():
        try:
            with session_espace(sch) as db:
                if trouve(db):
                    return sch
        except Exception:
            continue
    return None


def schema_de_station(eid) -> Optional[str]:
    """Station choisie par son numéro (0 = principale) → schéma ; ValueError
    si elle n'existe pas ou a été retirée."""
    if eid in (0, None):
        return None
    sch = f"esp_{int(eid)}"
    if sch not in schemas_actifs():
        invalider_cache()
        if sch not in schemas_actifs():
            raise ValueError("Station inconnue")
    return sch


def stations_pompiste() -> list:
    """Toutes les stations où un pompiste peut travailler : la principale
    (avec l'adresse de ses pompes) et les stations séparées actives."""
    from models import Distributeur
    with session_principale() as db:
        adresses = []
        for (a,) in db.query(Distributeur.adresse).order_by(Distributeur.ordre, Distributeur.id).all():
            a = " ".join((a or "").split())
            if a and a.casefold() not in [x.casefold() for x in adresses]:
                adresses.append(a)
        out = [{"id": 0, "nom": "Station principale", "adresse": ", ".join(adresses) or None}]
        out += [{"id": e.id, "nom": e.nom, "adresse": e.adresse}
                for e in db.query(Espace).filter(Espace.actif.is_(True)).order_by(Espace.id).all()]
    return out


def espace_du_jeton_reset(jeton: Optional[str]) -> Optional[str]:
    """Station où a été demandé un lien « mot de passe oublié »."""
    import hashlib
    from models import PasswordResetToken
    if not jeton:
        return None
    h = hashlib.sha256(jeton.encode("utf-8")).hexdigest()
    for sch in [None, *schemas_actifs().keys()]:
        try:
            with session_espace(sch) as db:
                if db.query(PasswordResetToken.id).filter_by(token_hash=h).first():
                    return sch
        except Exception:
            continue
    return None


def compte_ailleurs(email: Optional[str] = None, username: Optional[str] = None) -> bool:
    """Un compte avec cet email / cet identifiant existe-t-il dans une AUTRE
    station (principale ou séparée) ? La connexion cherche le compte dans
    toutes les stations : un doublon enverrait la personne au mauvais endroit."""
    if not (email or username):
        return False
    courant = espace_courant()
    for sch in [None, *schemas_actifs().keys()]:
        if sch == courant:
            continue
        try:
            with session_espace(sch) as db:
                q = db.query(Utilisateur.id)
                if email and q.filter(Utilisateur.email == email.strip().lower()).first():
                    return True
                if username and q.filter(Utilisateur.username.ilike(username.strip())).first():
                    return True
        except Exception:
            continue
    return False


def _est_admin(u: Optional[Utilisateur]) -> bool:
    if not u or not u.actif:
        return False
    if u.role in ("admin", "pdg"):
        return True
    r = u.role_obj
    return bool(r and (r.est_admin or (r.permissions or {}).get("admin")))


def _moi(request: Request, db: Session) -> Utilisateur:
    proxy = getattr(request.state, "user", None)
    u = db.get(Utilisateur, proxy.id) if proxy else None
    if not u or not u.actif:
        raise HTTPException(401, "Non authentifié")
    return u


def proprietaire(request: Request, db: Session) -> Optional[Utilisateur]:
    """Le compte propriétaire (administrateur / PDG de l'espace principal)
    correspondant à l'utilisateur connecté, où qu'il soit ; sinon None."""
    moi = _moi(request, db)
    if not moi.email:
        return None
    with session_principale() as pdb:
        p = pdb.query(Utilisateur).filter(Utilisateur.email == moi.email, Utilisateur.actif.is_(True)).first()
        if p and _est_admin(p):
            pdb.expunge(p)
            return p
    return None


def _espace_dict(e: Espace) -> dict:
    return {"id": e.id, "nom": e.nom, "adresse": e.adresse, "schema": e.schema}


@router.get("/api/espaces")
def lister(request: Request, db: Session = Depends(get_db)):
    courant = espace_courant()
    proprio = proprietaire(request, db)
    with session_principale() as pdb:
        espaces = [_espace_dict(e) for e in pdb.query(Espace).filter(Espace.actif.is_(True)).order_by(Espace.id).all()]
    nom_courant = next((e["nom"] for e in espaces if e["schema"] == courant), None) if courant else "Station principale"
    return {"courant": courant, "courant_nom": nom_courant,
            "peut_changer": bool(proprio) and bool(espaces),
            "peut_creer": bool(proprio),
            "espaces": espaces if proprio else [e for e in espaces if e["schema"] == courant]}


class EntrerIn(BaseModel):
    cible_id: int = 0          # 0 = station principale
    pin: str


def _miroir(db: Session, p: Utilisateur) -> Utilisateur:
    """Compte du propriétaire dans un espace (créé au besoin, à jour)."""
    u = db.query(Utilisateur).filter(Utilisateur.email == p.email).first()
    if not u:
        base = re.sub(r"[^a-z0-9._-]", "", (p.username or "proprietaire").lower())[:60] or "proprietaire"
        nom, i = base, 2
        while db.query(Utilisateur).filter(Utilisateur.username == nom).first():
            nom = f"{base}{i}"; i += 1
        u = Utilisateur(username=nom, email=p.email)
        db.add(u)
    u.password_hash, u.code_acces_hash = p.password_hash, p.code_acces_hash
    u.nom_complet, u.role, u.actif = p.nom_complet, (p.role if p.role in ("admin", "pdg") else "admin"), True
    db.flush()
    return u


def _cookie_espace(response: Response, schema: Optional[str], secure: bool):
    if schema:
        response.set_cookie(ESPACE_COOKIE, schema, httponly=True, samesite="lax", secure=secure, max_age=30 * 24 * 3600, path="/")
    else:
        response.delete_cookie(ESPACE_COOKIE, path="/")


@router.post("/api/espaces/entrer")
def entrer(data: EntrerIn, request: Request, response: Response, db: Session = Depends(get_db)):
    """Passer à une autre station : PIN du propriétaire ; déconnexion de la
    station quittée, session ouverte dans l'autre."""
    import main
    proprio = proprietaire(request, db)
    if not proprio:
        raise HTTPException(403, "Seul le propriétaire peut passer d'une station à l'autre.")
    ip = request.client.host if request.client else None
    cle = f"{ip}|espace|{proprio.id}"
    main._check_login_rate(cle)
    if not proprio.code_acces_hash or not verify_code_acces((data.pin or "").strip(), proprio.code_acces_hash):
        main._record_login_failure(cle)
        raise HTTPException(400, "PIN incorrect.")      # pas 401 : l'interface y lirait une session expirée
    main._clear_login_failures(cle)
    cible = None
    if data.cible_id:
        with session_principale() as pdb:
            e = pdb.get(Espace, data.cible_id)
            if not e or not e.actif:
                raise HTTPException(404, "Station introuvable.")
            cible = e.schema
    with session_espace(cible) as tdb:
        u = _miroir(tdb, proprio) if cible else tdb.get(Utilisateur, proprio.id)
        tdb.commit()
        token = create_session(tdb, u.id, ip_address=ip, user_agent=request.headers.get("user-agent", ""))
    ancien = request.cookies.get(main.SESSION_COOKIE)
    if ancien:
        try:
            delete_session(db, ancien)            # déconnecté de la station quittée
        except Exception:
            db.rollback()
    response.set_cookie(main.SESSION_COOKIE, token, httponly=True, samesite="lax",
                        secure=main._SECURE_COOKIES, max_age=12 * 3600, path="/")
    _cookie_espace(response, cible, main._SECURE_COOKIES)
    return {"ok": True, "courant": cible}


class GerantIn(BaseModel):
    nom_complet: str
    email: str
    password: str
    pin: str


class EspaceIn(BaseModel):
    nom: str
    adresse: str
    gerant: GerantIn
    station: Optional[dict] = None       # même contenu que POST /api/stations


def _email_libre(email: str) -> None:
    if espace_du_compte(email=email) is not None:
        raise HTTPException(409, "Cet email est déjà utilisé par un compte d'une autre station.")
    with session_principale() as pdb:
        if pdb.query(Utilisateur).filter(Utilisateur.email == email).first():
            raise HTTPException(409, "Cet email est déjà utilisé par un compte de la station principale.")


@router.post("/api/espaces", status_code=201)
def creer(data: EspaceIn, request: Request, db: Session = Depends(get_db)):
    """Station séparée : espace vierge avec toutes les fonctions de Konekta,
    compte du gérant, et la station (pompes, faces, mètres) enregistrée dedans."""
    import main
    proprio = proprietaire(request, db)
    if not proprio:
        raise HTTPException(403, "Seul le propriétaire peut créer une station séparée.")
    nom = " ".join((data.nom or "").split())[:150]
    adresse = " ".join((data.adresse or "").split())[:255]
    g = data.gerant
    email = (g.email or "").strip().lower()
    if len(nom) < 2 or len(adresse) < 2:
        raise HTTPException(400, "Indiquez le nom et l'adresse de la station.")
    if not _EMAIL_RE.match(email):
        raise HTTPException(400, "Email du gérant invalide.")
    if len(g.password or "") < 8:
        raise HTTPException(400, "Le mot de passe du gérant doit faire au moins 8 caractères.")
    if not _PIN_RE.match((g.pin or "").strip()):
        raise HTTPException(400, "Le PIN du gérant doit contenir exactement 9 chiffres.")
    if len(" ".join((g.nom_complet or "").split())) < 2:
        raise HTTPException(400, "Indiquez le nom du gérant.")
    _email_libre(email)
    station = None
    if data.station:
        try:
            station = main.StationIn(**dict(data.station, adresse=adresse))
        except Exception as exc:
            raise HTTPException(400, f"Station : données invalides ({exc}).")
    # 1. Registre (espace principal) → schéma esp_<id>
    with session_principale() as pdb:
        e = Espace(nom=nom, adresse=adresse, schema=f"tmp_{int(time.time() * 1000)}", actif=True)
        pdb.add(e); pdb.flush()
        e.schema = f"esp_{e.id}"
        pdb.commit()
        eid, schema = e.id, e.schema
    try:
        creer_schema_espace(schema)
        # 2. Comptes et station dans le nouvel espace
        with session_espace(schema) as edb:
            base = re.sub(r"[^a-z0-9._-]", "", email.split("@")[0])[:60] or "gerant"
            gerant = Utilisateur(username=base, email=email, nom_complet=" ".join(g.nom_complet.split())[:150],
                                 password_hash=hash_password(g.password), code_acces_hash=hash_code_acces(g.pin.strip()),
                                 role="admin", actif=True)
            edb.add(gerant)
            miroir = _miroir(edb, proprio)
            if miroir.username == gerant.username:
                miroir.username = f"{miroir.username}-proprio"
            edb.flush()
            if station is not None:
                main._creer_station(edb, station, request, user_id=miroir.id)
            edb.commit()
    except HTTPException:
        _desactiver(eid)
        raise
    except Exception as exc:
        _desactiver(eid)
        raise HTTPException(500, f"La station n'a pas pu être créée : {exc}")
    invalider_cache()
    main._tracer(db, request, "station_separee_creee", {"espace_id": eid, "nom": nom, "adresse": adresse, "gerant": email})
    db.commit()
    return {"id": eid, "nom": nom, "adresse": adresse, "schema": schema, "gerant": email}


def _desactiver(eid: int) -> None:
    with session_principale() as pdb:
        e = pdb.get(Espace, eid)
        if e:
            e.actif = False
            pdb.commit()
    invalider_cache()


class RetirerEspaceIn(BaseModel):
    confirmation: str


@router.post("/api/espaces/{eid}/retirer")
def retirer(eid: int, data: RetirerEspaceIn, request: Request, response: Response, db: Session = Depends(get_db)):
    """Retire une station séparée de la liste (ses données sont gardées dans
    son schéma ; elle n'est plus proposée et personne ne peut plus y entrer)."""
    import main
    if not proprietaire(request, db):
        raise HTTPException(403, "Seul le propriétaire peut retirer une station.")
    if (data.confirmation or "").strip().upper() != "RETIRER":
        raise HTTPException(400, "Tapez RETIRER pour confirmer.")
    with session_principale() as pdb:
        e = pdb.get(Espace, eid)
        if not e or not e.actif:
            raise HTTPException(404, "Station introuvable.")
        schema, nom = e.schema, e.nom
        e.actif = False
        pdb.commit()
    invalider_cache()
    if espace_courant() == schema:                # on était dedans : retour à la station principale
        _cookie_espace(response, None, main._SECURE_COOKIES)
    return {"ok": True, "nom": nom}
