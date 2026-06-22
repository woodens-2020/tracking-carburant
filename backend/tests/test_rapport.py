"""
Tests du module de génération de rapports.

Vérifie :
  1. L'endpoint /api/rapport/export renvoie le bon MIME type pour chaque format.
  2. Le nom de fichier est horodaté et porte la bonne extension.
  3. Les données sont cohérentes (pas de chiffre inventé).
  4. Une période sans données produit un rapport valide (pas une erreur) dans les 3 formats.
  5. L'accès est refusé sans authentification.

Lancer depuis backend/ :
    pytest tests/test_rapport.py -v
"""
import os
import sys
import struct
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Base isolée pour ces tests
os.environ["DATABASE_URL"] = "sqlite:///./test_rapport.db"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base, Produit, Pompe, Releve, Utilisateur
from auth import hash_password
from database import init_db

# ── Client d'intégration ──────────────────────────────────────────

@pytest.fixture(scope="module")
def client():
    from main import app
    init_db()
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    # Nettoyage
    try:
        os.remove("test_rapport.db")
    except (FileNotFoundError, PermissionError):
        pass


@pytest.fixture(scope="module")
def auth_client(client):
    """Client authentifié via session cookie."""
    client.post("/api/login", json={"username": "admin", "password": "admin123"})
    return client


@pytest.fixture(scope="module")
def api_key(client):
    """Récupère la clé API de l'admin."""
    res = client.post("/api/login", json={"username": "admin", "password": "admin123"})
    return res.json().get("api_key", "")


# ── Helpers ───────────────────────────────────────────────────────

def _is_pdf(data: bytes) -> bool:
    return data[:4] == b"%PDF"

def _is_zip(data: bytes) -> bool:
    """DOCX et XLSX sont des ZIP."""
    return data[:2] == b"PK"

def _get_content_disposition_filename(response) -> str:
    cd = response.headers.get("content-disposition", "")
    import re
    m = re.search(r'filename="([^"]+)"', cd)
    return m.group(1) if m else ""


# ══════════════════════════════════════════════════════════════════
# 1. TYPES MIME ET EXTENSIONS
# ══════════════════════════════════════════════════════════════════

class TestMimeTypes:
    ENDPOINT = "/api/rapport/export"
    PARAMS_BASE = "?date_debut=2026-01-01&date_fin=2026-01-31"

    def test_pdf_mime(self, auth_client):
        res = auth_client.get(self.ENDPOINT + self.PARAMS_BASE + "&format=pdf")
        assert res.status_code == 200, res.text
        assert "application/pdf" in res.headers["content-type"]
        assert _is_pdf(res.content), "Le contenu ne commence pas par %PDF"

    def test_docx_mime(self, auth_client):
        res = auth_client.get(self.ENDPOINT + self.PARAMS_BASE + "&format=docx")
        assert res.status_code == 200, res.text
        assert "wordprocessingml" in res.headers["content-type"]
        assert _is_zip(res.content), "DOCX doit être un ZIP (PK header)"

    def test_xlsx_mime(self, auth_client):
        res = auth_client.get(self.ENDPOINT + self.PARAMS_BASE + "&format=xlsx")
        assert res.status_code == 200, res.text
        assert "spreadsheetml" in res.headers["content-type"]
        assert _is_zip(res.content), "XLSX doit être un ZIP (PK header)"

    def test_invalid_format_returns_400(self, auth_client):
        res = auth_client.get(self.ENDPOINT + self.PARAMS_BASE + "&format=csv")
        assert res.status_code == 400

    def test_invalid_date_range_returns_400(self, auth_client):
        res = auth_client.get(self.ENDPOINT + "?date_debut=2026-01-31&date_fin=2026-01-01&format=pdf")
        assert res.status_code == 400

    def test_invalid_date_format_returns_400(self, auth_client):
        res = auth_client.get(self.ENDPOINT + "?date_debut=01-01-2026&date_fin=31-01-2026&format=pdf")
        assert res.status_code == 400


# ══════════════════════════════════════════════════════════════════
# 2. NOM DE FICHIER HORODATÉ
# ══════════════════════════════════════════════════════════════════

class TestFilename:
    ENDPOINT = "/api/rapport/export"

    def test_pdf_filename(self, auth_client):
        res = auth_client.get(self.ENDPOINT + "?date_debut=2026-06-01&date_fin=2026-06-21&format=pdf")
        assert res.status_code == 200
        fname = _get_content_disposition_filename(res)
        assert fname.endswith(".pdf"), f"Extension inattendue : {fname}"
        assert "2026-06-01" in fname
        assert "2026-06-21" in fname

    def test_docx_filename(self, auth_client):
        res = auth_client.get(self.ENDPOINT + "?date_debut=2026-06-01&date_fin=2026-06-21&format=docx")
        assert res.status_code == 200
        fname = _get_content_disposition_filename(res)
        assert fname.endswith(".docx"), f"Extension inattendue : {fname}"

    def test_xlsx_filename(self, auth_client):
        res = auth_client.get(self.ENDPOINT + "?date_debut=2026-06-01&date_fin=2026-06-21&format=xlsx")
        assert res.status_code == 200
        fname = _get_content_disposition_filename(res)
        assert fname.endswith(".xlsx"), f"Extension inattendue : {fname}"


# ══════════════════════════════════════════════════════════════════
# 3. COHÉRENCE DES DONNÉES
# ══════════════════════════════════════════════════════════════════

class TestDataCoherence:
    """Teste via rapport_service directement pour vérifier les chiffres."""

    def test_payload_structure(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        engine = create_engine("sqlite:///./test_rapport.db",
                               connect_args={"check_same_thread": False})
        Session = sessionmaker(bind=engine)
        db = Session()

        from datetime import date
        from rapport_service import build_report_payload, build_narrative

        payload = build_report_payload(db, date(2026, 1, 1), date(2026, 1, 31))
        assert "stats" in payload
        assert "rentab" in payload
        assert "stocks" in payload
        assert "anomalies" in payload
        assert "serie_jours" in payload

        # Tous les totaux doivent être >= 0
        assert payload["stats"]["total_montant"] >= 0
        assert payload["stats"]["total_quantite"] >= 0
        assert payload["nb_jours"] == 31

        # Narratif doit exister sans exception
        narr = build_narrative(payload)
        assert "intro_kpis" in narr
        assert "conclusion" in narr
        assert isinstance(narr["recommandations"], list)
        assert len(narr["recommandations"]) > 0

        db.close()

    def test_serie_jours_all_non_negative(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        engine = create_engine("sqlite:///./test_rapport.db",
                               connect_args={"check_same_thread": False})
        Session = sessionmaker(bind=engine)
        db = Session()

        from datetime import date
        from rapport_service import build_report_payload

        payload = build_report_payload(db, date(2026, 1, 1), date(2026, 12, 31))
        for d, v in payload["serie_jours"].items():
            assert v["montant"] >= 0, f"Montant négatif le {d}"
            assert v["quantite"] >= 0, f"Quantité négative le {d}"

        db.close()


# ══════════════════════════════════════════════════════════════════
# 4. PÉRIODE SANS DONNÉES
# ══════════════════════════════════════════════════════════════════

class TestEmptyPeriod:
    """Une période sans relevés doit produire un rapport valide, pas une erreur."""

    ENDPOINT = "/api/rapport/export"
    # Période future sans données
    PARAMS = "?date_debut=2099-01-01&date_fin=2099-01-31"

    def test_pdf_empty_period(self, auth_client):
        res = auth_client.get(self.ENDPOINT + self.PARAMS + "&format=pdf")
        assert res.status_code == 200, res.text
        assert _is_pdf(res.content)
        assert len(res.content) > 1000, "PDF trop court — probablement vide ou erreur"

    def test_docx_empty_period(self, auth_client):
        res = auth_client.get(self.ENDPOINT + self.PARAMS + "&format=docx")
        assert res.status_code == 200, res.text
        assert _is_zip(res.content)

    def test_xlsx_empty_period(self, auth_client):
        res = auth_client.get(self.ENDPOINT + self.PARAMS + "&format=xlsx")
        assert res.status_code == 200, res.text
        assert _is_zip(res.content)

    def test_empty_period_narrative_coherent(self):
        from datetime import date
        from rapport_service import build_report_payload, build_narrative
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        engine = create_engine("sqlite:///./test_rapport.db",
                               connect_args={"check_same_thread": False})
        Session = sessionmaker(bind=engine)
        db = Session()

        payload = build_report_payload(db, date(2099, 1, 1), date(2099, 1, 31))
        assert payload["stats"]["nb_releves"] == 0
        assert payload["stats"]["total_montant"] == 0.0

        narr = build_narrative(payload)
        assert "Aucun relevé" in narr["intro_kpis"]
        assert narr["var_pct"] is None

        db.close()


# ══════════════════════════════════════════════════════════════════
# 5. ACCÈS SANS AUTHENTIFICATION
# ══════════════════════════════════════════════════════════════════

class TestAuthRequired:
    ENDPOINT = "/api/rapport/export"
    PARAMS = "?date_debut=2026-01-01&date_fin=2026-01-31&format=pdf"

    def test_no_auth_returns_401(self, client):
        from fastapi.testclient import TestClient
        from main import app
        fresh = TestClient(app, raise_server_exceptions=True)
        # Client sans cookie ni API key
        res = fresh.get(self.ENDPOINT + self.PARAMS)
        assert res.status_code == 401

    def test_api_key_auth_works(self, api_key):
        from fastapi.testclient import TestClient
        from main import app
        c = TestClient(app, raise_server_exceptions=True)
        res = c.get(self.ENDPOINT + self.PARAMS, headers={"X-API-Key": api_key})
        assert res.status_code == 200
        assert _is_pdf(res.content)

    def test_invalid_api_key_returns_401(self):
        from fastapi.testclient import TestClient
        from main import app
        c = TestClient(app, raise_server_exceptions=True)
        res = c.get(self.ENDPOINT + self.PARAMS, headers={"X-API-Key": "knt_invalid"})
        assert res.status_code == 401
