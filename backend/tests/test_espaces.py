"""
Stations séparées : le propriétaire crée une station séparée (espace vierge,
gérant, pompes) ; il passe d'une station à l'autre avec son PIN ; chaque
station a ses propres données ; le gérant et les pompistes arrivent
directement dans leur station.
"""
from fastapi.testclient import TestClient

PROPRIO = {"email": "admin@konekta.local", "password": "admin123", "code_acces": "123456789"}


def _client():
    import main
    return TestClient(main.app)


def _station():
    return {"produits": [{"cle": "g", "nom": "Gazoline", "prix": 900}, {"cle": "d", "nom": "Diesel", "prix": 950}],
            "pompes": [{"nom": "Pompe 1", "faces": [
                {"nom": "A", "metres": [{"produit": "g", "nom": "Gazoline 1", "metre_depart": 100}, {"produit": "d", "nom": "Diesel 1", "metre_depart": 50}]},
                {"nom": "B", "metres": [{"produit": "g", "nom": "Gazoline 2", "metre_depart": 200}]}]}]}


def test_station_separee(client):
    p = _client()
    assert p.post("/api/login", json=PROPRIO).status_code == 200
    principal = {x["nom"]: x for x in p.get("/api/produits").json()}
    e = p.get("/api/espaces").json()
    assert e["peut_creer"] is True and e["courant"] is None

    corps = {"nom": "Station Champin", "adresse": "Champin, Cap-Haïtien", "station": _station(),
             "gerant": {"nom_complet": "Gérant Champin", "email": "gerant.champin@exemple.ht",
                        "password": "MotDePasse2026", "pin": "987654321"}}
    assert p.post("/api/espaces", json=dict(corps, gerant=dict(corps["gerant"], pin="12"))).status_code == 400
    r = p.post("/api/espaces", json=corps)
    assert r.status_code == 201, r.text
    eid = r.json()["id"]
    assert p.post("/api/espaces", json=dict(corps, nom="Autre")).status_code == 409      # email du gérant déjà pris
    liste = p.get("/api/espaces").json()
    assert liste["peut_changer"] is True and [x["nom"] for x in liste["espaces"]] == ["Station Champin"]
    # La station principale n'a pas bougé
    assert {x["nom"] for x in p.get("/api/produits").json()} == set(principal)

    # Passer à Champin : PIN obligatoire
    assert p.post("/api/espaces/entrer", json={"cible_id": eid, "pin": "000000000"}).status_code == 400
    r = p.post("/api/espaces/entrer", json={"cible_id": eid, "pin": "123456789"})
    assert r.status_code == 200 and r.json()["courant"] == f"esp_{eid}"
    champin = p.get("/api/produits").json()
    assert {x["nom"] for x in champin} == {"Gazoline", "Diesel"}
    dist = p.get("/api/distributeurs").json()["distributeurs"]
    assert [(d["nom"], d["adresse"]) for d in dist] == [("Pompe 1", "Champin, Cap-Haïtien")]
    assert p.get("/api/espaces").json()["courant_nom"] == "Station Champin"
    # Un relevé à Champin…
    g1 = next(q for x in champin for q in x["pompes"] if q["nom"] == "Gazoline 1")
    r = p.post("/api/releves", json={"date": "2025-06-01", "periode": "Matin", "pompe_id": g1["id"],
                                     "prix_gallon": 900, "metter_avant": 100, "metter_apres": 160})
    assert r.status_code in (200, 201), r.text
    assert len(p.get("/api/journal?date_debut=2025-06-01&date_fin=2025-06-01").json()["entries"]) == 1
    # … n'existe pas dans la station principale
    assert p.post("/api/espaces/entrer", json={"cible_id": 0, "pin": "123456789"}).status_code == 200
    assert p.get("/api/espaces").json()["courant"] is None
    assert p.get("/api/journal?date_debut=2025-06-01&date_fin=2025-06-01").json()["entries"] == []

    # Le gérant se connecte directement dans sa station, sans pouvoir changer
    g = _client()
    r = g.post("/api/login", json={"email": "gerant.champin@exemple.ht", "password": "MotDePasse2026", "code_acces": "987654321"})
    assert r.status_code == 200, r.text
    assert len(g.get("/api/journal?date_debut=2025-06-01&date_fin=2025-06-01").json()["entries"]) == 1
    info = g.get("/api/espaces").json()
    assert info["courant"] == f"esp_{eid}" and info["peut_changer"] is False and info["peut_creer"] is False
    assert g.post("/api/espaces/entrer", json={"cible_id": 0, "pin": "987654321"}).status_code == 403

    # Un pompiste créé par le gérant arrive dans la station de Champin
    c = g.post("/api/pompiste-supervision/comptes", json={"prenom": "Pom", "nom": "Champin"}).json()
    pp = _client()
    assert pp.post("/api/login/pompiste", json={"username": c["username"], "code_acces": c["code_acces"]}).status_code == 200
    noms = {q["nom"] for x in pp.get("/api/pompiste/contexte").json()["produits"] for q in x["pompes"]}
    assert noms == {"Gazoline 1", "Gazoline 2", "Diesel 1"}
