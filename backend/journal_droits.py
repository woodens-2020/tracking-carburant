"""Droits du Journal Meters (bpccp).

Sur cpv, ces contrôles viennent de pompiste_routes (espace pompiste) ;
bpccp n'a pas d'espace pompiste, d'où ce module minimal : mêmes règles
(direction, administration, carburant « complet », rôles de supervision).
"""
from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from models import Utilisateur


def _user(request: Request, db: Session) -> Utilisateur:
    proxy = getattr(request.state, "user", None)
    if not proxy:
        raise HTTPException(401, "Non authentifié")
    u = db.get(Utilisateur, proxy.id)
    if not u or not u.actif:
        raise HTTPException(401, "Compte inactif")
    return u


def peut_superviser(u: Utilisateur) -> bool:
    if u.role in ("admin", "pdg"):
        return True
    r = u.role_obj
    if not r:
        return False
    perms = r.permissions or {}
    if r.est_admin or perms.get("admin") or perms.get("carburant") == "complet":
        return True
    nom = (r.nom or "").lower()
    return any(m in nom for m in ("directeur", "superviseur", "manager", "gérant", "gerant"))


def _require_superviseur(request: Request, db: Session) -> Utilisateur:
    u = _user(request, db)
    if not peut_superviser(u):
        raise HTTPException(403, "Réservé à la direction ou à l'administration.")
    return u
