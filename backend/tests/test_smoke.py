"""
Tests de fumée — barrière avant déploiement (CI GitHub Actions).

Vérifie que l'application démarre sur une base vierge (migrations de
démarrage comprises) et que les routes principales répondent sans erreur
500. Ne dépend d'aucune donnée réelle : base SQLite temporaire.

Lancer depuis backend/ :
    pytest tests/test_smoke.py -v
"""
import pytest
from fastapi.testclient import TestClient

import main  # environnement de test préparé par conftest.py

H = {"X-API-Key": "smoke-test-key"}


# ── Pages et configuration publiques ─────────────────────────────────────────

def test_page_login(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert "<html" in r.text.lower()


def test_branding_public(client):
    r = client.get("/api/config/branding")
    assert r.status_code == 200
    b = r.json()
    assert "modules" in b
    assert b["environnement"] == "test"


def test_api_refuse_sans_authentification(client):
    assert client.get("/api/stock").status_code == 401


def test_cle_api_invalide_refusee(client):
    assert client.get("/api/stock", headers={"X-API-Key": "mauvaise"}).status_code == 401


def test_page_principale(client):
    r = client.get("/", headers=H)
    assert r.status_code == 200
    assert "<html" in r.text.lower()


# ── Routes principales : jamais d'erreur 500 sur une base vide ───────────────

ROUTES = [
    "/api/me",
    "/api/produits",
    "/api/stock",
    "/api/livraisons",
    "/api/prix-vente",
    "/api/releves?date=2026-10-01",
    "/api/rapport?date=2026-10-01",
    "/api/stats?date_debut=2026-10-01&date_fin=2026-10-18",
    "/api/anomalies?date=2026-10-01",
    "/api/depenses",
    "/api/depenses/stats",
    "/api/caisse/rapport?date_debut=2026-10-01&date_fin=2026-10-18",
    "/api/journal?date=2026-10-01",
    "/api/pos/credits",
    "/api/pos/achats/produits-tous",
    "/api/pos/depot/stock",
]


@pytest.mark.parametrize("route", ROUTES)
def test_route_repond(client, route):
    r = client.get(route, headers=H)
    assert r.status_code < 500, f"{route} → {r.status_code} : {r.text[:300]}"
    assert r.status_code not in (401, 403), f"{route} → {r.status_code} avec la clé admin"


# ── Cycle carburant minimal : produit → cargaison → relevé → stock ───────────

def test_cycle_stock_coherent(client):
    r = client.get("/api/produits", headers=H)
    assert r.status_code == 200
    produits = r.json()
    if not produits:
        pytest.skip("aucun produit amorcé sur une base vierge")
    p = produits[0]
    if not p.get("pompes"):
        pytest.skip("le produit amorcé n'a pas de pompe")

    r = client.post("/api/livraisons", headers=H, json={
        "produit_id": p["id"], "date_livraison": "2026-10-01",
        "gallons_recus": 1000, "prix_achat_gallon": 700,
    })
    assert r.status_code < 300, r.text[:300]

    r = client.post("/api/releves", headers=H, json={
        "date": "2026-10-02", "periode": "Matin", "pompe_id": p["pompes"][0]["id"],
        "metter_avant": 100.0, "metter_apres": 350.0, "prix_gallon": 800,
    })
    assert r.status_code < 300, r.text[:300]

    r = client.get("/api/stock", headers=H)
    assert r.status_code == 200
    s = next(x for x in r.json()["stocks"] if x["produit_id"] == p["id"])
    assert s["gallons_livres_cargaison_active"] == 1000
    assert s["gallons_vendus_cargaison_active"] == 250
    # Livré − Vendu = Restant : la règle affichée sur la carte Stock
    assert abs(s["gallons_livres_cargaison_active"] - s["gallons_vendus_cargaison_active"]
               - s["gallons_restants_cargaison_active"]) < 0.01
