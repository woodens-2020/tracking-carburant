"""
Suite de tests pour le système d'authentification (session + clé API).

Prérequis :
    pip install pytest httpx fastapi[all]

Lancer depuis le dossier backend/ :
    pytest tests/test_auth.py -v
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

# Deux bases séparées : une pour les tests unitaires, une pour les tests d'intégration.
# Ainsi init_db() trouve une base vide et crée bien l'admin.
UNIT_DB_URL  = "sqlite:///./test_unit.db"
INTEG_DB_URL = "sqlite:///./test_integ.db"
os.environ["DATABASE_URL"] = INTEG_DB_URL  # utilisé par database.py au premier import

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base, Utilisateur
from auth import hash_password, make_api_key, verify_api_key, revoke_api_key, hash_api_key


# ── Moteur isolé (tests unitaires) ───────────────────────────────

@pytest.fixture(scope="module")
def test_engine():
    eng = create_engine(UNIT_DB_URL, connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    yield eng
    Base.metadata.drop_all(bind=eng)
    eng.dispose()
    try:
        os.remove("test_unit.db")
    except (FileNotFoundError, PermissionError):
        pass


@pytest.fixture(scope="module")
def db_session(test_engine):
    Session = sessionmaker(bind=test_engine)
    db = Session()
    user = Utilisateur(
        username="testuser",
        password_hash=hash_password("pass1234"),
        nom_complet="Test User",
        role="operateur",
        actif=True,
    )
    admin = Utilisateur(
        username="testadmin",
        password_hash=hash_password("adminpass"),
        nom_complet="Test Admin",
        role="admin",
        actif=True,
    )
    db.add_all([user, admin])
    db.commit()
    db.refresh(user)
    db.refresh(admin)
    yield db, user, admin
    db.close()


# ── Tests unitaires auth.py ───────────────────────────────────────

class TestHashApiKey:
    def test_deterministic(self):
        assert hash_api_key("knt_abc") == hash_api_key("knt_abc")

    def test_different_keys_different_hashes(self):
        assert hash_api_key("knt_abc") != hash_api_key("knt_xyz")

    def test_length(self):
        assert len(hash_api_key("any_key")) == 64


class TestMakeAndVerifyApiKey:
    def test_make_returns_prefixed_key(self, db_session):
        db, user, _ = db_session
        raw = make_api_key(db, user.id)
        assert raw.startswith("knt_")

    def test_verify_valid_key(self, db_session):
        db, user, _ = db_session
        raw = make_api_key(db, user.id)
        found = verify_api_key(db, raw)
        assert found is not None
        assert found.id == user.id

    def test_verify_wrong_key_returns_none(self, db_session):
        db, _, _ = db_session
        assert verify_api_key(db, "knt_wrongkey") is None

    def test_verify_empty_returns_none(self, db_session):
        db, _, _ = db_session
        assert verify_api_key(db, "") is None
        assert verify_api_key(db, None) is None

    def test_rotate_invalidates_old_key(self, db_session):
        db, user, _ = db_session
        old_key = make_api_key(db, user.id)
        make_api_key(db, user.id)
        assert verify_api_key(db, old_key) is None

    def test_revoke_removes_key(self, db_session):
        db, user, _ = db_session
        raw = make_api_key(db, user.id)
        revoke_api_key(db, user.id)
        assert verify_api_key(db, raw) is None

    def test_inactive_user_not_authenticated(self, db_session):
        db, user, _ = db_session
        raw = make_api_key(db, user.id)
        user.actif = False
        db.commit()
        assert verify_api_key(db, raw) is None
        user.actif = True
        db.commit()


# ── Helper : client sans cookies ──────────────────────────────────

def fresh_client():
    """Retourne un TestClient vierge (aucun cookie de session)."""
    from fastapi.testclient import TestClient
    from main import app
    return TestClient(app, raise_server_exceptions=True)


# ── Tests d'intégration ───────────────────────────────────────────

@pytest.fixture(scope="module")
def app_client():
    from fastapi.testclient import TestClient
    from main import app
    from database import init_db
    init_db()  # crée l'admin dans INTEG_DB_URL (base vide indépendante)
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    try:
        os.remove("test_integ.db")
    except (FileNotFoundError, PermissionError):
        pass


class TestLoginEndpoint:
    def test_login_success_returns_api_key(self, app_client):
        res = app_client.post("/api/login", json={"username": "admin", "password": "admin123"})
        assert res.status_code == 200
        body = res.json()
        assert "api_key" in body
        assert body["api_key"].startswith("knt_")
        assert body["username"] == "admin"

    def test_login_wrong_password(self):
        c = fresh_client()
        res = c.post("/api/login", json={"username": "admin", "password": "wrong"})
        assert res.status_code == 401

    def test_login_unknown_user(self):
        c = fresh_client()
        res = c.post("/api/login", json={"username": "nobody", "password": "x"})
        assert res.status_code == 401


class TestApiKeyHeader:
    def test_api_key_grants_access(self):
        c = fresh_client()
        login = c.post("/api/login", json={"username": "admin", "password": "admin123"})
        key = login.json()["api_key"]
        # Nouveau client sans cookie
        c2 = fresh_client()
        res = c2.get("/api/me", headers={"X-API-Key": key})
        assert res.status_code == 200
        assert res.json()["username"] == "admin"

    def test_invalid_api_key_rejected(self):
        c = fresh_client()
        res = c.get("/api/me", headers={"X-API-Key": "knt_invalid"})
        assert res.status_code == 401

    def test_no_auth_rejected(self):
        c = fresh_client()
        res = c.get("/api/produits")
        assert res.status_code == 401


class TestAuthMeEndpoint:
    def test_auth_me_with_cookie(self, app_client):
        app_client.post("/api/login", json={"username": "admin", "password": "admin123"})
        res = app_client.get("/api/auth/me")
        assert res.status_code == 200
        body = res.json()
        assert body["username"] == "admin"
        assert body["has_api_key"] is True

    def test_auth_me_with_api_key(self):
        c = fresh_client()
        login = c.post("/api/login", json={"username": "admin", "password": "admin123"})
        key = login.json()["api_key"]
        c2 = fresh_client()
        res = c2.get("/api/auth/me", headers={"X-API-Key": key})
        assert res.status_code == 200
        assert res.json()["has_api_key"] is True


class TestRotateAndRevoke:
    def test_rotate_returns_new_key(self, app_client):
        login = app_client.post("/api/login", json={"username": "admin", "password": "admin123"})
        old_key = login.json()["api_key"]
        rotate = app_client.post("/api/auth/api-key")
        assert rotate.status_code == 200
        new_key = rotate.json()["api_key"]
        assert new_key.startswith("knt_")
        assert new_key != old_key

    def test_old_key_invalid_after_rotate(self, app_client):
        login = app_client.post("/api/login", json={"username": "admin", "password": "admin123"})
        old_key = login.json()["api_key"]
        app_client.post("/api/auth/api-key")  # rotation
        c = fresh_client()
        res = c.get("/api/me", headers={"X-API-Key": old_key})
        assert res.status_code == 401

    def test_revoke_key(self, app_client):
        login = app_client.post("/api/login", json={"username": "admin", "password": "admin123"})
        key = login.json()["api_key"]
        revoke = app_client.delete("/api/auth/api-key")
        assert revoke.status_code == 200
        c = fresh_client()
        res = c.get("/api/me", headers={"X-API-Key": key})
        assert res.status_code == 401


class TestSessionCookieStillWorks:
    def test_cookie_auth_independent_of_api_key(self, app_client):
        app_client.post("/api/login", json={"username": "admin", "password": "admin123"})
        res = app_client.get("/api/me")
        assert res.status_code == 200
        assert res.json()["username"] == "admin"

    def test_logout_clears_session(self):
        c = fresh_client()
        c.post("/api/login", json={"username": "admin", "password": "admin123"})
        c.post("/api/logout")
        c.cookies.clear()
        res = c.get("/api/me")
        assert res.status_code == 401
