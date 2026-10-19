"""
Environnement commun aux tests : base SQLite temporaire, clé admin de test,
aucun envoi d'email/WhatsApp. Doit être chargé AVANT tout import de main /
database (qui lisent DATABASE_URL à l'import) — pytest charge conftest.py
en premier.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="konekta-tests-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMP, "tests.db").replace("\\", "/")
os.environ["ADMIN_API_KEY"] = "smoke-test-key"
os.environ["OTP_ENABLED"] = "false"
os.environ["SECURE_COOKIES"] = "false"
os.environ["APP_ENV"] = "test"
for _k in ("EMAIL_HOST_PASSWORD", "TWILIO_AUTH_TOKEN", "GEMINI_API_KEY"):
    os.environ.pop(_k, None)

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="session")
def client():
    import main
    with TestClient(main.app) as c:   # déclenche l'événement startup
        yield c


@pytest.fixture()
def db(client):
    from database import SessionLocal
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
