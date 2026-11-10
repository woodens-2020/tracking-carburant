"""
Produits et pompes : renommer, adresse du produit (le système sert à
plusieurs endroits), noms uniques, historique gardé, pompe avec relevés
désactivée plutôt que supprimée.
"""
import time

H = {"X-API-Key": "smoke-test-key"}


def _nouveau(client, adresse=None):
    nom = f"Prod-{time.time_ns()}"
    r = client.post("/api/produits", headers=H, json={"nom": nom, "prix_gallon": 300, "adresse": adresse})
    assert r.status_code == 200, r.text
    return r.json()


def test_renommer_produit_et_adresse(client):
    p = _nouveau(client, "  Route de Frères,   Pétion-Ville ")
    assert p["adresse"] == "Route de Frères, Pétion-Ville"
    pompe = client.post(f"/api/produits/{p['id']}/pompes", headers=H, json={"nom": "P1"}).json()["id"]
    r = client.post("/api/releves", headers=H, json={"date": "2025-03-01", "periode": "Matin", "pompe_id": pompe,
                                                      "prix_gallon": 300, "metter_avant": 0, "metter_apres": 10})
    assert r.status_code in (200, 201), r.text
    nouveau = f"Gazoline Champin {time.time_ns()}"
    r = client.patch(f"/api/produits/{p['id']}", headers=H, json={"nom": nouveau, "adresse": "Champin, Cap-Haïtien"})
    assert r.status_code == 200, r.text
    liste = {x["id"]: x for x in client.get("/api/produits", headers=H).json()}
    assert liste[p["id"]]["nom"] == nouveau and liste[p["id"]]["adresse"] == "Champin, Cap-Haïtien"
    # L'historique suit le produit sous son nouveau nom
    j = client.get(f"/api/journal?date_debut=2025-03-01&date_fin=2025-03-01&pompe_id={pompe}", headers=H).json()
    assert [e["produit_nom"] for e in j["entries"]] == [nouveau]
    # Adresse seule, puis effacée
    assert client.patch(f"/api/produits/{p['id']}", headers=H, json={"adresse": ""}).json()["adresse"] is None


def test_noms_uniques(client):
    a, b = _nouveau(client), _nouveau(client)
    assert client.patch(f"/api/produits/{b['id']}", headers=H, json={"nom": a["nom"].upper()}).status_code == 409
    assert client.post("/api/produits", headers=H, json={"nom": " " + a["nom"] + " "}).status_code == 409
    assert client.patch(f"/api/produits/{b['id']}", headers=H, json={"nom": "x"}).status_code == 400
    q1 = client.post(f"/api/produits/{a['id']}/pompes", headers=H, json={"nom": "P1"}).json()["id"]
    q2 = client.post(f"/api/produits/{a['id']}/pompes", headers=H, json={"nom": "P2"}).json()["id"]
    assert client.patch(f"/api/pompes/{q2}", headers=H, json={"nom": "p1"}).status_code == 409
    r = client.patch(f"/api/pompes/{q2}", headers=H, json={"nom": "Pompe Est"})
    assert r.status_code == 200 and r.json()["nom"] == "Pompe Est"


def test_pompe_avec_releves_desactivee(client):
    p = _nouveau(client)
    q = client.post(f"/api/produits/{p['id']}/pompes", headers=H, json={"nom": "P1"}).json()["id"]
    vide = client.post(f"/api/produits/{p['id']}/pompes", headers=H, json={"nom": "P2"}).json()["id"]
    client.post("/api/releves", headers=H, json={"date": "2025-03-02", "periode": "Matin", "pompe_id": q,
                                                 "prix_gallon": 300, "metter_avant": 0, "metter_apres": 5})
    r = client.delete(f"/api/pompes/{q}", headers=H)
    assert r.status_code == 200 and r.json()["desactivee"] is True
    assert client.delete(f"/api/pompes/{vide}", headers=H).json()["desactivee"] is False
    pompes = next(x for x in client.get("/api/produits", headers=H).json() if x["id"] == p["id"])["pompes"]
    assert pompes == []
    j = client.get(f"/api/journal?date_debut=2025-03-02&date_fin=2025-03-02&pompe_id={q}", headers=H).json()
    assert len(j["entries"]) == 1                     # historique gardé


def test_changer_prix(client):
    """Nouveau prix à partir d'une date : les relevés déjà enregistrés gardent
    leur prix, ceux saisis ensuite prennent le nouveau ; tracé."""
    p = _nouveau(client)
    q = client.post(f"/api/produits/{p['id']}/pompes", headers=H, json={"nom": "P1"}).json()["id"]
    r1 = client.post("/api/releves", headers=H, json={"date": "2025-04-01", "periode": "Matin", "pompe_id": q,
                                                       "prix_gallon": 300, "metter_avant": 0, "metter_apres": 10}).json()
    assert r1["prix_gallon"] == 300
    r = client.post("/api/prix-vente", headers=H, json={"produit_id": p["id"], "prix_vente_gallon": 350,
                                                         "date_effet": "2025-04-02"})
    assert r.status_code == 201, r.text
    assert client.post("/api/prix-vente", headers=H, json={"produit_id": p["id"], "prix_vente_gallon": 0,
                                                           "date_effet": "2025-04-02"}).status_code == 400
    r2 = client.post("/api/releves", headers=H, json={"date": "2025-04-02", "periode": "Matin", "pompe_id": q,
                                                       "prix_gallon": 300, "metter_avant": 10, "metter_apres": 20}).json()
    assert r2["prix_gallon"] == 350                       # le prix configuré s'applique
    j = client.get(f"/api/journal?date_debut=2025-04-01&date_fin=2025-04-02&pompe_id={q}", headers=H).json()
    assert [e["prix_gallon"] for e in j["entries"]] == [300, 350]   # l'ancien relevé garde son prix
    actif = next(x for x in client.get("/api/produits", headers=H).json() if x["id"] == p["id"])["prix_vente_actif"]
    assert actif == 350
