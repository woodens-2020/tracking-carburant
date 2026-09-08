"""Listes de référence partagées : catégories de dépense et postes.

Deux besoins identiques traités au même endroit :
  - ajout d'une valeur « à la volée » pendant la saisie (dépense / employé),
    réservé aux managers et administrateurs ;
  - déduplication : une valeur ne peut jamais être enregistrée deux fois,
    même avec une casse ou des espaces différents (`nom_norm` unique).

Importé par main.py, patisserie_routes.py, cuisine_routes.py, hotel_routes.py
et zelle_routes.py — la logique n'est donc dupliquée nulle part.
"""
from __future__ import annotations

import re
from typing import Optional

from fastapi import HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import CategorieDepense, CategorieAchat, Poste, Utilisateur  # noqa: F401 (ré-exportés)

_ESPACES = re.compile(r"\s+")


def normaliser(valeur: Optional[str]) -> str:
    """Forme canonique servant à la comparaison / contrainte d'unicité :
    espaces réduits, sans espace de bord, casse repliée."""
    return _ESPACES.sub(" ", (valeur or "").strip()).casefold()


def _maxlen(modele) -> int:
    return modele.__table__.c.nom.type.length or 100


def peut_gerer(user) -> bool:
    """Seuls le manager et l'administrateur/PDG peuvent créer une entrée."""
    if not user:
        return False
    if getattr(user, "role", None) in ("admin", "pdg"):
        return True
    role_obj = getattr(user, "role_obj", None)
    if role_obj and (role_obj.permissions or {}).get("admin", False):
        return True
    nom = ((role_obj.nom if role_obj else None) or getattr(user, "poste", None) or "").strip().lower()
    return nom == "manager"


def utilisateur_courant(request: Request, db: Session) -> Optional[Utilisateur]:
    """`request.state.user` est un proxy léger sans role_obj — on recharge la
    ligne complète pour pouvoir évaluer les permissions."""
    u = getattr(getattr(request, "state", None), "user", None)
    if u is None:
        return None
    return db.get(Utilisateur, u.id) or u


def lister(db: Session, modele, *, inclure_inactifs: bool = False):
    q = db.query(modele)
    if not inclure_inactifs:
        q = q.filter(modele.actif.is_(True))
    return q.order_by(modele.nom).all()


def resoudre(db: Session, modele, valeur: Optional[str], *, request: Request,
             defaut: Optional[str] = None, label: str = "valeur") -> Optional[str]:
    """Renvoie le `nom` canonique de l'entrée correspondant à `valeur`.

    - vide / None  → `defaut` (créé au besoin), sinon None ;
    - déjà connue  → le `nom` déjà stocké (peu importe casse / espaces) ;
    - nouvelle     → créée si l'utilisateur est manager/admin, sinon HTTP 400.

    La création passe par un SAVEPOINT : en cas de course, on récupère la
    ligne gagnante sans casser la transaction de l'appelant.
    """
    brut = (valeur or "").strip()
    if not brut:
        if defaut is None:
            return None
        brut = defaut.strip()
    norm = normaliser(brut)
    if not norm:
        return None

    existante = db.query(modele).filter(modele.nom_norm == norm).first()
    if existante:
        return existante.nom

    est_le_defaut = defaut is not None and normaliser(defaut) == norm
    user = utilisateur_courant(request, db)
    if not est_le_defaut and not peut_gerer(user):
        raise HTTPException(
            400,
            f"{label.capitalize()} « {brut} » inconnue — seul un manager ou un "
            "administrateur peut créer une nouvelle valeur.",
        )

    mx = _maxlen(modele)
    entree = modele(nom=brut[:mx], nom_norm=norm[:mx], actif=True,
                    cree_par_id=user.id if user else None)
    try:
        with db.begin_nested():
            db.add(entree)
        return entree.nom
    except IntegrityError:
        gagnante = db.query(modele).filter(modele.nom_norm == norm).first()
        if gagnante:
            return gagnante.nom
        raise
