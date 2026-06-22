"""
Authentification par session (cookie httponly) et clé API (header X-API-Key).

Mots de passe : PBKDF2-HMAC-SHA256, sel aléatoire par utilisateur.
Sessions      : jeton aléatoire opaque stocké en base, avec expiration.
Clés API      : SHA-256 du jeton brut stocké en base (aucun secret en clair).
"""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from models import Utilisateur, SessionToken

SESSION_COOKIE         = "session_token"
SESSION_DURATION_HOURS = 24 * 7   # 7 jours
PBKDF2_ITERATIONS      = 200_000
API_KEY_PREFIX         = "knt_"   # Konekta — identifiable dans les logs


# ── Mots de passe ─────────────────────────────────────────────────

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"{salt.hex()}:{dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, hash_hex = stored.split(":")
    except ValueError:
        return False
    salt = bytes.fromhex(salt_hex)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return hmac.compare_digest(dk.hex(), hash_hex)


# ── Code d'accès 9 chiffres ────────────────────────────────────────

def hash_code_acces(code: str) -> str:
    """PBKDF2 du code numérique — même sécurité que les mots de passe."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", code.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"{salt.hex()}:{dk.hex()}"


def verify_code_acces(code: str, stored: str) -> bool:
    try:
        salt_hex, hash_hex = stored.split(":")
    except ValueError:
        return False
    salt = bytes.fromhex(salt_hex)
    dk = hashlib.pbkdf2_hmac("sha256", code.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return hmac.compare_digest(dk.hex(), hash_hex)


# ── Sessions cookie ────────────────────────────────────────────────

def create_session(db: Session, user_id: int) -> str:
    token   = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=SESSION_DURATION_HOURS)
    db.add(SessionToken(token=token, user_id=user_id, expires_at=expires))
    db.commit()
    return token


def get_session_user(db: Session, token: str):
    if not token:
        return None
    s = db.query(SessionToken).filter_by(token=token).first()
    if not s:
        return None
    expires_at = s.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        db.delete(s)
        db.commit()
        return None
    user = db.query(Utilisateur).get(s.user_id)
    if not user or not user.actif:
        return None
    return user


def delete_session(db: Session, token: str):
    if not token:
        return
    db.query(SessionToken).filter_by(token=token).delete()
    db.commit()


# ── Clés API ───────────────────────────────────────────────────────

def hash_api_key(raw_key: str) -> str:
    """SHA-256 hex digest — seule la valeur stockée en base."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


def make_api_key(db: Session, user_id: int) -> str:
    """Génère une nouvelle clé API, stocke le hash en base et retourne le jeton brut.

    Le jeton brut n'est affiché qu'une seule fois au moment de la génération.
    """
    raw = API_KEY_PREFIX + secrets.token_urlsafe(32)
    key_hash = hash_api_key(raw)
    user = db.query(Utilisateur).get(user_id)
    if not user:
        raise ValueError("Utilisateur introuvable")
    user.api_key_hash = key_hash
    db.commit()
    return raw


def verify_api_key(db: Session, raw_key: str):
    """Retourne l'Utilisateur actif si la clé est valide, sinon None."""
    if not raw_key:
        return None
    key_hash = hash_api_key(raw_key)
    user = db.query(Utilisateur).filter_by(api_key_hash=key_hash).first()
    if not user or not user.actif:
        return None
    return user


def revoke_api_key(db: Session, user_id: int):
    """Supprime la clé API de l'utilisateur (invalide immédiatement)."""
    user = db.query(Utilisateur).get(user_id)
    if user:
        user.api_key_hash = None
        db.commit()
