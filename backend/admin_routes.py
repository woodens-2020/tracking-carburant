"""Routes d'administration : gestion des rôles et des comptes utilisateurs."""
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth import hash_code_acces, hash_password, make_api_key
from database import get_db
from models import Role, Utilisateur

router = APIRouter(prefix="/api/admin", tags=["admin"])

_EMAIL_RE    = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9._\-]{3,60}$")
_CODE_RE     = re.compile(r"^\d{9}$")

DOMAINES = ["finance", "bar", "cuisine", "hotel", "employes", "carburant"]
NIVEAUX  = ["aucun", "lecture", "complet"]

_PERMS_VIDES: dict = {d: "aucun" for d in DOMAINES}
_PERMS_VIDES["admin"] = False


def _require_admin(request: Request, db: Session = Depends(get_db)) -> Utilisateur:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(403, "Non authentifié")
    if user.role == "admin":
        return user
    if user.role_id:
        u = db.get(Utilisateur, user.id)
        if u and u.role_obj and u.role_obj.permissions.get("admin", False):
            return u
    raise HTTPException(403, "Accès réservé aux administrateurs")


def _validate_permissions(perms: dict) -> dict:
    out = dict(_PERMS_VIDES)
    for d in DOMAINES:
        v = perms.get(d, "aucun")
        if v not in NIVEAUX:
            raise HTTPException(400, f"Niveau invalide '{v}' pour '{d}'. Valeurs: {NIVEAUX}")
        out[d] = v
    out["admin"] = bool(perms.get("admin", False))
    return out


def _role_public(r: Role) -> dict:
    return {
        "id":             r.id,
        "nom":            r.nom,
        "description":    r.description,
        "permissions":    r.permissions,
        "est_admin":      r.est_admin,
        "est_systeme":    r.est_systeme,
        "date_creation":  r.date_creation.isoformat() if r.date_creation else None,
        "nb_utilisateurs": len(r.utilisateurs),
    }


def _user_public(u: Utilisateur) -> dict:
    return {
        "id":             u.id,
        "username":       u.username,
        "nom_complet":    u.nom_complet,
        "email":          u.email,
        "role":           u.role,
        "poste":          u.poste,
        "role_id":        u.role_id,
        "role_nom":       u.role_obj.nom if u.role_obj else u.poste,
        "actif":          u.actif,
        "created_at":     u.created_at.isoformat() if u.created_at else None,
        "oauth_provider": u.oauth_provider,
    }


# ═══════════════════════════════════════
# RÔLES — CRUD
# ═══════════════════════════════════════

class RoleIn(BaseModel):
    nom:         str
    description: Optional[str] = None
    permissions: dict = {}


@router.get("/roles")
def lister_roles(
    _admin: Utilisateur = Depends(_require_admin),
    db: Session = Depends(get_db),
):
    return [_role_public(r) for r in db.query(Role).order_by(Role.id).all()]


@router.post("/roles", status_code=201)
def creer_role(
    data: RoleIn,
    _admin: Utilisateur = Depends(_require_admin),
    db: Session = Depends(get_db),
):
    nom = data.nom.strip()
    if not nom:
        raise HTTPException(400, "Le nom du rôle est requis")
    if db.query(Role).filter_by(nom=nom).first():
        raise HTTPException(409, f"Un rôle nommé '{nom}' existe déjà")
    perms = _validate_permissions(data.permissions)
    r = Role(
        nom=nom,
        description=data.description.strip() if data.description else None,
        permissions=perms,
        est_admin=bool(perms.get("admin", False)),
        est_systeme=False,
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return _role_public(r)


@router.put("/roles/{role_id}")
def modifier_role(
    role_id: int,
    data: RoleIn,
    _admin: Utilisateur = Depends(_require_admin),
    db: Session = Depends(get_db),
):
    r = db.get(Role, role_id)
    if not r:
        raise HTTPException(404, "Rôle introuvable")
    nom = data.nom.strip()
    if not nom:
        raise HTTPException(400, "Le nom du rôle est requis")
    if db.query(Role).filter(Role.nom == nom, Role.id != role_id).first():
        raise HTTPException(409, f"Un rôle nommé '{nom}' existe déjà")
    perms = _validate_permissions(data.permissions)
    r.nom         = nom
    r.description = data.description.strip() if data.description else None
    r.permissions = perms
    r.est_admin   = bool(perms.get("admin", False))
    db.commit()
    db.refresh(r)
    return _role_public(r)


@router.delete("/roles/{role_id}")
def supprimer_role(
    role_id: int,
    _admin: Utilisateur = Depends(_require_admin),
    db: Session = Depends(get_db),
):
    r = db.get(Role, role_id)
    if not r:
        raise HTTPException(404, "Rôle introuvable")
    if r.est_systeme:
        raise HTTPException(409, "Les rôles système ne peuvent pas être supprimés")
    if db.query(Utilisateur).filter_by(role_id=role_id).count() > 0:
        raise HTTPException(409, "Ce rôle est attribué à des utilisateurs — réassignez-les d'abord")
    db.delete(r)
    db.commit()
    return {"ok": True, "id": role_id}


# ═══════════════════════════════════════
# UTILISATEURS — CRUD
# ═══════════════════════════════════════

class CreateUserIn(BaseModel):
    nom_complet: str
    email:       str
    username:    str
    password:    str
    code_acces:  str
    role_id:     int


class UpdateUserIn(BaseModel):
    nom_complet: Optional[str] = None
    email:       Optional[str] = None
    role_id:     Optional[int] = None
    actif:       Optional[bool] = None


class ResetPasswordIn(BaseModel):
    nouveau_mot_de_passe: str


class ResetPinIn(BaseModel):
    nouveau_code: str


@router.get("/users")
def lister_users(
    _admin: Utilisateur = Depends(_require_admin),
    db: Session = Depends(get_db),
):
    return [_user_public(u) for u in db.query(Utilisateur).order_by(Utilisateur.id).all()]


@router.post("/users", status_code=201)
def creer_user(
    data: CreateUserIn,
    _admin: Utilisateur = Depends(_require_admin),
    db: Session = Depends(get_db),
):
    role = db.get(Role, data.role_id)
    if not role:
        raise HTTPException(404, "Rôle introuvable")
    email = data.email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(400, "Adresse email invalide")
    username = data.username.strip()
    if not _USERNAME_RE.match(username):
        raise HTTPException(400, "Identifiant invalide (3-60 chars, lettres/chiffres/._-)")
    if not data.nom_complet.strip():
        raise HTTPException(400, "Le nom complet est requis")
    if len(data.password) < 6:
        raise HTTPException(400, "Le mot de passe doit contenir au moins 6 caractères")
    if not _CODE_RE.match(data.code_acces):
        raise HTTPException(400, "Le code PIN doit contenir exactement 9 chiffres")
    if db.query(Utilisateur).filter_by(username=username).first():
        raise HTTPException(409, f"L'identifiant '{username}' est déjà utilisé")
    if db.query(Utilisateur).filter_by(email=email).first():
        raise HTTPException(409, "Cet email est déjà associé à un compte")

    u = Utilisateur(
        username=username,
        password_hash=hash_password(data.password),
        code_acces_hash=hash_code_acces(data.code_acces),
        nom_complet=data.nom_complet.strip(),
        email=email,
        role="admin" if role.est_admin else "operateur",
        role_id=role.id,
        poste=role.nom,
        actif=True,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    raw_key = make_api_key(db, u.id)
    result = _user_public(u)
    result["api_key"] = raw_key
    return result


@router.put("/users/{uid}")
def modifier_user(
    uid: int,
    data: UpdateUserIn,
    _admin: Utilisateur = Depends(_require_admin),
    db: Session = Depends(get_db),
):
    u = db.get(Utilisateur, uid)
    if not u:
        raise HTTPException(404, "Utilisateur introuvable")
    if data.nom_complet is not None:
        if not data.nom_complet.strip():
            raise HTTPException(400, "Le nom complet est requis")
        u.nom_complet = data.nom_complet.strip()
    if data.email is not None:
        email = data.email.strip().lower()
        if not _EMAIL_RE.match(email):
            raise HTTPException(400, "Adresse email invalide")
        if db.query(Utilisateur).filter(Utilisateur.email == email, Utilisateur.id != uid).first():
            raise HTTPException(409, "Cet email est déjà associé à un autre compte")
        u.email = email
    if data.role_id is not None:
        role = db.get(Role, data.role_id)
        if not role:
            raise HTTPException(404, "Rôle introuvable")
        u.role_id = role.id
        u.poste   = role.nom
        u.role    = "admin" if role.est_admin else "operateur"
    if data.actif is not None:
        if not data.actif and u.role == "admin":
            nb_admins = db.query(Utilisateur).filter_by(role="admin", actif=True).count()
            if nb_admins <= 1:
                raise HTTPException(409, "Impossible : dernier administrateur actif")
        u.actif = data.actif
    db.commit()
    db.refresh(u)
    return _user_public(u)


@router.post("/users/{uid}/reset-password")
def reset_password(
    uid: int,
    data: ResetPasswordIn,
    _admin: Utilisateur = Depends(_require_admin),
    db: Session = Depends(get_db),
):
    u = db.get(Utilisateur, uid)
    if not u:
        raise HTTPException(404, "Utilisateur introuvable")
    if len(data.nouveau_mot_de_passe) < 6:
        raise HTTPException(400, "Le mot de passe doit contenir au moins 6 caractères")
    u.password_hash = hash_password(data.nouveau_mot_de_passe)
    db.commit()
    return {"ok": True}


@router.post("/users/{uid}/reset-pin")
def reset_pin(
    uid: int,
    data: ResetPinIn,
    _admin: Utilisateur = Depends(_require_admin),
    db: Session = Depends(get_db),
):
    u = db.get(Utilisateur, uid)
    if not u:
        raise HTTPException(404, "Utilisateur introuvable")
    if not _CODE_RE.match(data.nouveau_code):
        raise HTTPException(400, "Le code PIN doit contenir exactement 9 chiffres")
    u.code_acces_hash = hash_code_acces(data.nouveau_code)
    db.commit()
    return {"ok": True}
