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


# ═══════════════════════════════════════════════════════════════════════════
# Tests : Gestion des comptes employés (require_admin + endpoints CRUD)
# ═══════════════════════════════════════════════════════════════════════════

class TestGestionEmployes:
    """
    Utilise app_client (cookie session admin) et fresh_client() pour les
    scénarios sans privilège.
    """

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _admin_client():
        """Client connecté en tant qu'admin via session cookie."""
        c = fresh_client()
        c.post("/api/login", json={"username": "admin", "password": "admin123"})
        return c

    @staticmethod
    def _create_operateur(client, suffix=""):
        """Crée un compte opérateur et retourne le résultat JSON."""
        return client.post("/api/auth/utilisateurs", json={
            "username":    f"emp_test{suffix}",
            "nom_complet": f"Employé Test{suffix}",
            "password":    "secret123",
            "role":        "operateur",
        })

    # ── 1. Admin (master key via X-API-Key env var) crée un opérateur ────

    def test_master_key_creates_employee(self):
        """L'ADMIN_API_KEY dans X-API-Key doit permettre la création d'un compte."""
        import os
        master = os.getenv("ADMIN_API_KEY", "")
        if not master:
            pytest.skip("ADMIN_API_KEY non défini — test ignoré")
        c = fresh_client()
        res = c.post("/api/auth/utilisateurs",
                     headers={"X-API-Key": master},
                     json={"username": "emp_master", "nom_complet": "Par MasterKey",
                           "password": "pass1234", "role": "operateur"})
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["username"] == "emp_master"
        assert "api_key" in body
        assert body["api_key"].startswith("knt_")
        # Nettoyage
        c.delete(f"/api/auth/utilisateurs/{body['id']}", headers={"X-API-Key": master})

    # ── 2. Admin connecté (session) crée un opérateur ────────────────────

    def test_session_admin_creates_employee(self, app_client):
        app_client.post("/api/login", json={"username": "admin", "password": "admin123"})
        res = self._create_operateur(app_client, "_sess")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["role"] == "operateur"
        assert "api_key" in body
        assert body["api_key"].startswith("knt_")
        # Nettoyage
        app_client.delete(f"/api/auth/utilisateurs/{body['id']}")

    # ── 3. Opérateur tente de créer un compte → 403 ───────────────────────

    def test_operateur_cannot_create(self, app_client):
        app_client.post("/api/login", json={"username": "admin", "password": "admin123"})
        # Crée un opérateur
        cr = self._create_operateur(app_client, "_403")
        assert cr.status_code == 200
        body = cr.json()
        emp_key = body["api_key"]
        emp_id  = body["id"]
        # Tente de créer depuis la clé opérateur
        c2 = fresh_client()
        res = c2.post("/api/auth/utilisateurs",
                      headers={"X-API-Key": emp_key},
                      json={"username": "emp_forbidden", "nom_complet": "X",
                            "password": "pass1234", "role": "operateur"})
        assert res.status_code == 403
        # Nettoyage
        app_client.delete(f"/api/auth/utilisateurs/{emp_id}")

    # ── 4. La liste n'expose jamais api_key_hash ──────────────────────────

    def test_list_never_exposes_hash(self, app_client):
        app_client.post("/api/login", json={"username": "admin", "password": "admin123"})
        res = app_client.get("/api/auth/utilisateurs")
        assert res.status_code == 200
        for u in res.json():
            assert "api_key_hash"  not in u
            assert "password_hash" not in u

    # ── 5. Révocation → clé invalide ─────────────────────────────────────

    def test_revoke_invalidates_key(self, app_client):
        app_client.post("/api/login", json={"username": "admin", "password": "admin123"})
        cr = self._create_operateur(app_client, "_rev")
        body    = cr.json()
        emp_key = body["api_key"]
        emp_id  = body["id"]

        # La clé fonctionne avant révocation
        c2 = fresh_client()
        assert c2.get("/api/me", headers={"X-API-Key": emp_key}).status_code == 200

        # Révocation
        app_client.post(f"/api/auth/utilisateurs/{emp_id}/revoquer")

        # La clé est rejetée
        c3 = fresh_client()
        assert c3.get("/api/me", headers={"X-API-Key": emp_key}).status_code == 401

        # Nettoyage
        app_client.delete(f"/api/auth/utilisateurs/{emp_id}")

    # ── 6. Réactivation → clé refonctionner après régénération ──────────

    def test_reactivate_and_regenerate(self, app_client):
        app_client.post("/api/login", json={"username": "admin", "password": "admin123"})
        cr = self._create_operateur(app_client, "_react")
        body   = cr.json()
        emp_id = body["id"]

        # Révoque
        app_client.post(f"/api/auth/utilisateurs/{emp_id}/revoquer")
        # Réactive
        app_client.post(f"/api/auth/utilisateurs/{emp_id}/reactiver")
        # Régénère la clé
        regen = app_client.post(f"/api/auth/utilisateurs/{emp_id}/regenerer")
        assert regen.status_code == 200
        new_key = regen.json()["api_key"]
        assert new_key.startswith("knt_")

        # Nouvelle clé fonctionne
        c2 = fresh_client()
        assert c2.get("/api/me", headers={"X-API-Key": new_key}).status_code == 200

        # Nettoyage
        app_client.delete(f"/api/auth/utilisateurs/{emp_id}")

    # ── 7. Régénération → ancienne clé invalide, nouvelle valide ─────────

    def test_regenerate_invalidates_old_key(self, app_client):
        app_client.post("/api/login", json={"username": "admin", "password": "admin123"})
        cr      = self._create_operateur(app_client, "_regen")
        body    = cr.json()
        old_key = body["api_key"]
        emp_id  = body["id"]

        regen   = app_client.post(f"/api/auth/utilisateurs/{emp_id}/regenerer")
        new_key = regen.json()["api_key"]

        c2 = fresh_client()
        assert c2.get("/api/me", headers={"X-API-Key": old_key}).status_code == 401
        c3 = fresh_client()
        assert c3.get("/api/me", headers={"X-API-Key": new_key}).status_code == 200

        # Nettoyage
        app_client.delete(f"/api/auth/utilisateurs/{emp_id}")

    # ── 8. Garde-fou : le dernier admin actif ne peut pas se révoquer ────

    def test_last_admin_cannot_revoke_self(self, app_client):
        app_client.post("/api/login", json={"username": "admin", "password": "admin123"})
        # Récupère l'id de l'admin
        users  = app_client.get("/api/auth/utilisateurs").json()
        admins = [u for u in users if u["role"] == "admin" and u["actif"]]
        # Il ne doit y avoir qu'un seul admin actif (base de test fraîche)
        assert len(admins) >= 1
        admin_id = next(u["id"] for u in admins if u["username"] == "admin")
        res = app_client.post(f"/api/auth/utilisateurs/{admin_id}/revoquer")
        assert res.status_code == 409

    def test_last_admin_cannot_delete_self(self, app_client):
        app_client.post("/api/login", json={"username": "admin", "password": "admin123"})
        users    = app_client.get("/api/auth/utilisateurs").json()
        admin_id = next(u["id"] for u in users if u["username"] == "admin")
        res = app_client.delete(f"/api/auth/utilisateurs/{admin_id}")
        assert res.status_code == 409


# ═══════════════════════════════════════════════════════════════════════════
# Tests : OAuth 2.0 (fournisseur simulé — aucun appel réseau réel)
# ═══════════════════════════════════════════════════════════════════════════

class TestOAuth:
    """
    Simule le fournisseur OAuth en mockant httpx.post / httpx.get et les
    variables d'environnement. Aucune connexion réseau réelle.
    """

    FAKE_CLIENT_ID     = "fake-google-client-id.apps.googleusercontent.com"
    FAKE_CLIENT_SECRET = "fake-secret"
    FAKE_EMAIL         = "employe@example.com"
    FAKE_SUB           = "google-sub-12345"
    FAKE_NAME          = "Employé OAuth"

    @pytest.fixture(autouse=True)
    def _set_env(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_CLIENT_ID",     self.FAKE_CLIENT_ID)
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", self.FAKE_CLIENT_SECRET)

    # ── 1. Login redirige vers Google avec un state ───────────────────────

    def test_login_redirects_with_state(self):
        c = fresh_client()
        res = c.get("/api/auth/oauth/google/login", follow_redirects=False)
        assert res.status_code in (302, 307)
        loc = res.headers.get("location", "")
        assert "accounts.google.com" in loc
        assert "state=" in loc
        assert "client_id=" in loc

    # ── 2. Callback avec state invalide → redirige vers /login?oauth_error ─

    def test_callback_invalid_state(self):
        c = fresh_client()
        res = c.get("/api/auth/oauth/google/callback?code=fake&state=INVALID",
                    follow_redirects=False)
        assert res.status_code in (302, 307)
        loc = res.headers.get("location", "")
        assert "oauth_error=invalid_state" in loc

    # ── helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _cleanup(client, *usernames):
        """Supprime les comptes listés s'ils existent (idempotent entre runs)."""
        try:
            users = client.get("/api/auth/utilisateurs").json()
        except Exception:
            return
        for u in users:
            if u["username"] in usernames:
                client.delete(f"/api/auth/utilisateurs/{u['id']}")

    # ── 3. Callback : email connu → session créée ─────────────────────────

    def test_callback_known_email_creates_session(self, app_client, monkeypatch):
        """Un email déjà associé à un compte → connexion réussie."""
        app_client.post("/api/login", json={"username": "admin", "password": "admin123"})
        # Nettoyage préalable (inclut l'ancien nom utilisé avant le renommage)
        self._cleanup(app_client, "emp_oauth_cb", "emp_oauth")
        cr = app_client.post("/api/auth/utilisateurs", json={
            "username": "emp_oauth_cb", "nom_complet": "Employé OAuth CB",
            "password": "unused123", "role": "operateur",
            "email": self.FAKE_EMAIL,
        })
        assert cr.status_code == 200, cr.text
        emp_id = cr.json()["id"]

        try:
            # Démarre le flux OAuth pour récupérer un vrai state
            c2 = fresh_client()
            login_res = c2.get("/api/auth/oauth/google/login", follow_redirects=False)
            from urllib.parse import urlparse, parse_qs
            qs    = parse_qs(urlparse(login_res.headers["location"]).query)
            state = qs["state"][0]

            import httpx as _httpx

            class _FakeResp:
                def __init__(self, data):
                    self._data = data
                def raise_for_status(self): pass
                def json(self): return self._data

            monkeypatch.setattr(_httpx, "post", lambda *a, **kw: _FakeResp({"access_token": "fake_access", "token_type": "Bearer"}))
            monkeypatch.setattr(_httpx, "get",  lambda *a, **kw: _FakeResp({
                "email": self.FAKE_EMAIL, "sub": self.FAKE_SUB,
                "name": self.FAKE_NAME, "email_verified": True,
            }))

            cb = c2.get(f"/api/auth/oauth/google/callback?code=fake_code&state={state}",
                        follow_redirects=False)
            assert cb.status_code in (302, 307), cb.text
            assert cb.headers.get("location", "") == "/"
            assert "session_token" in cb.cookies
        finally:
            app_client.delete(f"/api/auth/utilisateurs/{emp_id}")

    # ── 4. Email inconnu → refus, redirection avec oauth_error ───────────

    def test_callback_unknown_email_refused(self, monkeypatch):
        c = fresh_client()
        login_res = c.get("/api/auth/oauth/google/login", follow_redirects=False)
        from urllib.parse import urlparse, parse_qs
        qs    = parse_qs(urlparse(login_res.headers["location"]).query)
        state = qs["state"][0]

        import httpx as _httpx

        class _FakeResp:
            def __init__(self, data):
                self._data = data
            def raise_for_status(self): pass
            def json(self): return self._data

        monkeypatch.setattr(_httpx, "post", lambda *a, **kw: _FakeResp({"access_token": "t"}))
        monkeypatch.setattr(_httpx, "get",  lambda *a, **kw: _FakeResp({
            "email": "inconnu@nowhere.com", "sub": "other-sub",
            "name": "Inconnu", "email_verified": True,
        }))

        res = c.get(f"/api/auth/oauth/google/callback?code=x&state={state}",
                    follow_redirects=False)
        assert res.status_code in (302, 307)
        assert "account_not_found" in res.headers.get("location", "")

    # ── 5. Session OAuth valide → accès aux routes protégées ─────────────

    def test_oauth_session_grants_access(self, app_client, monkeypatch):
        """Après le flux OAuth, le cookie de session doit ouvrir /api/me."""
        app_client.post("/api/login", json={"username": "admin", "password": "admin123"})
        self._cleanup(app_client, "emp_sess_oauth")
        cr = app_client.post("/api/auth/utilisateurs", json={
            "username": "emp_sess_oauth", "nom_complet": "Session OAuth",
            "password": "unused", "role": "operateur",
            "email": "sess.oauth@test.com",
        })
        assert cr.status_code == 200, cr.text
        emp_id = cr.json()["id"]

        try:
            c2 = fresh_client()
            lr = c2.get("/api/auth/oauth/google/login", follow_redirects=False)
            from urllib.parse import urlparse, parse_qs
            state = parse_qs(urlparse(lr.headers["location"]).query)["state"][0]

            import httpx as _httpx
            class _FR:
                def __init__(self, d): self._d = d
                def raise_for_status(self): pass
                def json(self): return self._d
            monkeypatch.setattr(_httpx, "post", lambda *a, **kw: _FR({"access_token":"t"}))
            monkeypatch.setattr(_httpx, "get",  lambda *a, **kw: _FR({
                "email":"sess.oauth@test.com", "sub":"sub-sess-oauth",
                "name":"Session OAuth", "email_verified":True,
            }))
            cb = c2.get(f"/api/auth/oauth/google/callback?code=c&state={state}",
                        follow_redirects=False)
            assert "session_token" in cb.cookies

            c3 = fresh_client()
            c3.cookies.update({"session_token": cb.cookies["session_token"]})
            me = c3.get("/api/me")
            assert me.status_code == 200
            assert me.json()["username"] == "emp_sess_oauth"
        finally:
            app_client.delete(f"/api/auth/utilisateurs/{emp_id}")

    # ── 6. Rôles respectés : opérateur OAuth ne gère pas les comptes ─────

    def test_oauth_operateur_cannot_manage_accounts(self, app_client, monkeypatch):
        """Un opérateur connecté via OAuth n'accède pas à GET /api/auth/utilisateurs."""
        app_client.post("/api/login", json={"username": "admin", "password": "admin123"})
        self._cleanup(app_client, "emp_role_oauth")
        cr = app_client.post("/api/auth/utilisateurs", json={
            "username": "emp_role_oauth", "nom_complet": "Role OAuth",
            "password": "unused", "role": "operateur",
            "email": "role.oauth@test.com",
        })
        assert cr.status_code == 200, cr.text
        emp_id = cr.json()["id"]

        try:
            c2 = fresh_client()
            lr = c2.get("/api/auth/oauth/google/login", follow_redirects=False)
            from urllib.parse import urlparse, parse_qs
            state = parse_qs(urlparse(lr.headers["location"]).query)["state"][0]

            import httpx as _httpx
            class _FR:
                def __init__(self, d): self._d = d
                def raise_for_status(self): pass
                def json(self): return self._d
            monkeypatch.setattr(_httpx, "post", lambda *a, **kw: _FR({"access_token":"t"}))
            monkeypatch.setattr(_httpx, "get",  lambda *a, **kw: _FR({
                "email":"role.oauth@test.com", "sub":"sub-role-oauth",
                "name":"Role OAuth", "email_verified":True,
            }))
            cb = c2.get(f"/api/auth/oauth/google/callback?code=c&state={state}",
                        follow_redirects=False)

            c3 = fresh_client()
            c3.cookies.update({"session_token": cb.cookies["session_token"]})
            res = c3.get("/api/auth/utilisateurs")
            assert res.status_code == 403
        finally:
            app_client.delete(f"/api/auth/utilisateurs/{emp_id}")
