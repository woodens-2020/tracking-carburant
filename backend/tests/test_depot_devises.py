"""
v28 — Dépôt central (achats/réception, transferts, ventes, caisse) et
argent reçu en dollars dans tous les départements, toujours séparé des
gourdes.

Lancer seul : cd backend && python -m pytest tests/test_depot_devises.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="konekta-depot-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMP, "tests.db").replace("\\", "/")
os.environ["ADMIN_API_KEY"] = "test-depot-key"
os.environ["OTP_ENABLED"] = "false"
os.environ["SECURE_COOKIES"] = "false"

import pytest
from fastapi.testclient import TestClient

H = {"X-API-Key": "test-depot-key"}
_n = [0]


@pytest.fixture(scope="module")
def client():
    import main
    with TestClient(main.app) as c:
        from database import SessionLocal
        from models import Utilisateur
        s = SessionLocal()
        if not s.query(Utilisateur).filter_by(username="admin").first():
            s.add(Utilisateur(username="admin", password_hash="x", nom_complet="Admin", role="admin", actif=True))
            s.commit()
        s.close()
        c.put("/api/devise/taux", headers=H, json={"taux_usd": 130})
        yield c


def _produit(client, prix=100, lieu=None):
    _n[0] += 1
    r = client.post("/api/pos/produits", headers=H, json={
        "nom": f"Prod D{_n[0]}", "categorie": "boisson", "prix_initial": prix, "lieu": lieu})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _achat_recu(client, pid, qte=20, prix=60):
    a = client.post("/api/depot/achats", headers=H, json={
        "fournisseur": "Brana", "numero_facture": "F-1",
        "lignes": [{"produit_id": pid, "quantite": qte, "prix_achat": prix}]}).json()
    r = client.post(f"/api/depot/achats/{a['id']}/reception", headers=H, json={
        "lignes": [{"ligne_id": a["lignes"][0]["id"], "quantite_recue": qte}]})
    assert r.status_code == 200 and r.json()["conforme"], r.text
    return a


def _stock(client, pid):
    return next(p for p in client.get("/api/depot/produits", headers=H).json() if p["id"] == pid)


# ── Dépôt : achats & réception ───────────────────────────────────

def test_achat_total_et_reception_conforme(client):
    pid = _produit(client)
    a = client.post("/api/depot/achats", headers=H, json={
        "fournisseur": "Brana", "lignes": [{"produit_id": pid, "quantite": 24, "prix_achat": 50}]})
    assert a.status_code == 201
    a = a.json()
    assert a["total"] == 1200.0 and a["statut"] == "EN_ATTENTE"
    assert _stock(client, pid)["stock_depot"] == 0          # pas encore reçu
    r = client.post(f"/api/depot/achats/{a['id']}/reception", headers=H, json={
        "lignes": [{"ligne_id": a["lignes"][0]["id"], "quantite_recue": 24}]}).json()
    assert r["conforme"] and r["achat"]["statut"] == "RECEPTIONNE"
    s = _stock(client, pid)
    assert s["stock_depot"] == 24 and s["cout_moyen"] == 50.0


def test_reception_avec_ecart_puis_correction(client):
    pid = _produit(client)
    a = client.post("/api/depot/achats", headers=H, json={
        "fournisseur": "Brana", "lignes": [{"produit_id": pid, "quantite": 10, "prix_achat": 40}]}).json()
    lid = a["lignes"][0]["id"]
    r = client.post(f"/api/depot/achats/{a['id']}/reception", headers=H, json={
        "lignes": [{"ligne_id": lid, "quantite_recue": 8}]}).json()
    assert not r["conforme"] and r["achat"]["statut"] == "ECART" and r["ecarts"]
    assert _stock(client, pid)["stock_depot"] == 0
    # Correction de l'achat (8 facturés) puis réception conforme
    a2 = client.put(f"/api/depot/achats/{a['id']}", headers=H, json={
        "fournisseur": "Brana", "lignes": [{"produit_id": pid, "quantite": 8, "prix_achat": 40}]}).json()
    r = client.post(f"/api/depot/achats/{a['id']}/reception", headers=H, json={
        "lignes": [{"ligne_id": a2["lignes"][0]["id"], "quantite_recue": 8}]}).json()
    assert r["conforme"] and _stock(client, pid)["stock_depot"] == 8


# ── Dépôt : transferts, ventes, caisse ───────────────────────────

def test_transfert_vers_bar_piscine_met_le_stock_a_jour(client):
    pid = _produit(client, prix=150)
    _achat_recu(client, pid, qte=20)
    t = client.post("/api/depot/transactions", headers=H, json={
        "destination": "PISCINE", "lignes": [{"produit_id": pid, "quantite": 6}]})
    assert t.status_code == 201, t.text
    t = t.json()
    assert t["statut"] == "VALIDEE" and t["total"] == 900.0 and t["type"] == "TRANSFERT"
    s = _stock(client, pid)
    assert s["stock_depot"] == 14 and s["stock_piscine"] == 6


def test_stock_insuffisant_refuse(client):
    pid = _produit(client)
    _achat_recu(client, pid, qte=3)
    r = client.post("/api/depot/transactions", headers=H, json={
        "destination": "DEVANT", "lignes": [{"produit_id": pid, "quantite": 5}]})
    assert r.status_code == 422 and "Stock insuffisant" in r.text


def test_vente_client_paiement_partiel_puis_dollars(client):
    pid = _produit(client, prix=200)
    _achat_recu(client, pid, qte=10)
    t = client.post("/api/depot/transactions", headers=H, json={
        "destination": "CLIENT", "client_nom": "Resto X", "lignes": [{"produit_id": pid, "quantite": 5}]}).json()
    assert t["total"] == 1000.0 and t["statut_paiement"] == "NON_PAYEE"
    p1 = client.post(f"/api/depot/transactions/{t['id']}/paiements", headers=H,
                     json={"montant": 350, "devise": "HTG", "methode": "CASH"}).json()
    assert p1["facture"]["statut_paiement"] == "PARTIELLE"
    p2 = client.post(f"/api/depot/transactions/{t['id']}/paiements", headers=H,
                     json={"montant": 5, "devise": "USD", "taux_change": 130, "methode": "ZELLE"})
    assert p2.status_code == 201, p2.text
    f = p2.json()["facture"]
    assert f["statut_paiement"] == "PAYEE" and f["paye_htg"] == 350.0 and f["paye_usd"] == 5.0
    # trop payé : refusé
    assert client.post(f"/api/depot/transactions/{t['id']}/paiements", headers=H,
                       json={"montant": 10, "methode": "CASH"}).status_code == 409

    j = client.get("/api/depot/paiements", headers=H).json()
    assert j["total_usd"] >= 5 and j["total_htg"] >= 350
    caisse = client.get("/api/depot/caisse", headers=H).json()
    assert caisse["balance_usd"] == j["total_usd"] and caisse["balance_htg"] == j["total_htg"]

    inst = client.get("/api/caisse/rapport", headers=H).json()["autres_departements"]
    assert inst["ventes_depot"] >= 350 and inst["dollars"]["depot"] >= 5


def test_annulation_remet_le_stock(client):
    pid = _produit(client)
    _achat_recu(client, pid, qte=10)
    t = client.post("/api/depot/transactions", headers=H, json={
        "destination": "DEVANT", "lignes": [{"produit_id": pid, "quantite": 4}]}).json()
    assert _stock(client, pid)["stock_devant"] == 4
    assert client.post(f"/api/depot/transactions/{t['id']}/annuler", headers=H).status_code == 200
    s = _stock(client, pid)
    assert s["stock_depot"] == 10 and s["stock_devant"] == 0


def test_config_prix_et_rapport(client):
    pid = _produit(client, prix=100)
    r = client.put(f"/api/depot/produits/{pid}/config", headers=H, json={"prix_vente": 175, "seuil_min": 5})
    assert r.json()["prix_vente"] == 175.0
    assert _stock(client, pid)["alerte"] is True
    rap = client.get("/api/depot/rapport", headers=H).json()
    assert "inventaire" in rap and "caisse" in rap and len(rap["ventes_transferts"]["par_destination"]) == 3


# ── Dollars dans les autres départements ─────────────────────────

def test_vente_bar_en_dollars_hors_cash_gourdes(client):
    pid = _produit(client, prix=260)
    client.post(f"/api/pos/produits/{pid}/approvisionnement", headers=H, json={"nb_unites_vrac": 10, "lieu": "DEVANT"})
    avant = client.get("/api/pos/dashboard/bar", headers=H).json()["kpis"]
    v = client.post("/api/pos/ventes?lieu=DEVANT", headers=H, json={
        "lignes": [{"produit_id": pid, "quantite": 1}], "devise": "USD", "taux_change": 130})
    assert v.status_code == 201, v.text
    assert v.json()["devise"] == "USD" and v.json()["montant_devise"] == 2.0
    apres = client.get("/api/pos/dashboard/bar", headers=H).json()["kpis"]
    assert apres["cash_jour"] == avant["cash_jour"]               # gourdes inchangées
    assert round(apres["cash_usd"] - avant.get("cash_usd", 0), 2) == 2.0
    assert client.get("/api/pos/grande-caisse", headers=H).json()["caisse"]["disponible_usd"] >= 2.0


def test_cuisine_patisserie_piscine_en_dollars(client):
    c = client.post("/api/cuisine/ventes", headers=H, json={
        "lignes": [{"nom_plat": "Riz", "quantite": 1, "prix_unitaire": 650}], "devise": "USD", "taux_change": 130})
    assert c.status_code in (200, 201), c.text
    assert c.json()["devise"] == "USD" and c.json()["montant_devise"] == 5.0

    client.put("/api/piscine/config", headers=H, json={"prix_carte": 260})
    p = client.post("/api/piscine/ventes", headers=H, json={"quantite": 1, "devise": "USD"}).json()
    assert p["devise"] == "USD" and p["montant_devise"] == 2.0
    d = client.get("/api/piscine/dashboard", headers=H).json()["kpi"]
    assert d["recu_usd"] >= 2.0

    inst = client.get("/api/caisse/rapport", headers=H).json()["autres_departements"]["dollars"]
    assert inst["cuisine"] >= 5.0 and inst["piscine"] >= 2.0 and inst["bar"] >= 2.0
    assert inst["total"] >= inst["cuisine"] + inst["piscine"] + inst["bar"]


def test_taux_commun(client):
    assert client.put("/api/devise/taux", headers=H, json={"taux_usd": 135}).status_code == 200
    assert client.get("/api/devise/taux", headers=H).json()["taux_usd"] == 135.0
    assert client.get("/api/hotel/config", headers=H).json()["taux_usd"] == 135.0
