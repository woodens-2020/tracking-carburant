"""
Reprise des comptes pompistes depuis l'ancien système (staging).

Un pompiste dont le compte n'existe pas encore ici tape son code : s'il
existe dans l'ancien système, l'application lui demande le PIN qu'il avait
déjà. Ce PIN est vérifié AUPRÈS DE L'ANCIEN SYSTÈME (qui seul le connaît,
chiffré) ; s'il est bon, le compte est créé ici avec le même code et le même
PIN (chiffré). Personne ne voit jamais le PIN et personne ne peut choisir
le PIN d'un autre. Les comptes de test ne sont pas repris ; la reprise
s'arrête d'elle-même à la date de fin.

Source : variable REPRISE_POMPISTES_URL, sinon l'ancien système associé au
domaine (konekta-cpv.com → staging Vertières). Jamais sur le staging lui-même.
"""
from __future__ import annotations

import logging
import os
import secrets
from datetime import date
from typing import Optional

from tz_utils import today_haiti

log = logging.getLogger(__name__)

SOURCES_PAR_DOMAINE = {
    "konekta-cpv.com": "https://vertieres-app-staging.up.railway.app",
    "www.konekta-cpv.com": "https://vertieres-app-staging.up.railway.app",
}
COMPTES_EXCLUS = {"pompiste.test", "pompisteessai8974"}      # comptes de test
FIN_REPRISE = date(2026, 12, 31)
DELAI_S = 8


def source(request) -> Optional[str]:
    if today_haiti() > FIN_REPRISE:
        return None
    if (os.environ.get("APP_ENV") or "").strip().lower() == "staging":
        return None
    url = (os.environ.get("REPRISE_POMPISTES_URL") or "").strip()
    if not url:
        hote = (request.headers.get("host") or "").split(":")[0].lower()
        url = SOURCES_PAR_DOMAINE.get(hote, "")
    return url.rstrip("/") or None


def _appel(url: str, chemin: str, corps: dict, session=None):
    """POST JSON vers l'ancien système ; (statut, json) ou (None, None)."""
    import requests
    try:
        r = (session or requests).post(url + chemin, json=corps, timeout=DELAI_S,
                                       headers={"User-Agent": "konekta-reprise-pompistes"})
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, None
    except Exception as exc:
        log.warning("Reprise pompistes : ancien système injoignable (%s)", type(exc).__name__)
        return None, None


def prenom_ailleurs(request, username: str) -> Optional[str]:
    """Le code existe-t-il dans l'ancien système ? Retourne le prénom."""
    url = source(request)
    if not url or username in COMPTES_EXCLUS:
        return None
    st, d = _appel(url, "/api/pompiste/identifier", {"username": username})
    if st == 200 and isinstance(d, dict):
        return d.get("prenom") or ""
    return None


def verifier_ailleurs(request, username: str, pin: str) -> Optional[str]:
    """Code + PIN vérifiés par l'ancien système ; retourne le nom complet."""
    import requests
    url = source(request)
    if not url or username in COMPTES_EXCLUS:
        return None
    s = requests.Session()
    st, d = _appel(url, "/api/login/pompiste", {"username": username, "code_acces": pin}, session=s)
    if st != 200 or not isinstance(d, dict):
        return None
    _appel(url, "/api/logout", {}, session=s)                   # ne pas laisser de session ouverte là-bas
    return (d.get("nom_complet") or username).strip()


def creer_compte_repris(username: str, nom_complet: str, pin: str):
    """Crée le compte pompiste ici (espace principal), même code, même PIN."""
    from auth import hash_code_acces, hash_password
    from database import session_principale
    from models import Utilisateur
    from pompiste_routes import assurer_role_pompiste, employe_du_compte
    with session_principale() as db:
        if db.query(Utilisateur).filter(Utilisateur.username.ilike(username)).first():
            return
        role = assurer_role_pompiste(db)
        u = Utilisateur(username=username, nom_complet=nom_complet[:150],
                        password_hash=hash_password(secrets.token_urlsafe(24)),
                        code_acces_hash=hash_code_acces(pin), role="operateur", role_id=role.id,
                        poste=role.nom, actif=True)
        db.add(u)
        db.flush()
        prenom, _, nom = nom_complet.partition(" ")
        employe_du_compte(db, u, prenom or nom_complet, nom or prenom)
        db.commit()
        log.info("Reprise pompistes : compte %s repris de l'ancien système", username)
