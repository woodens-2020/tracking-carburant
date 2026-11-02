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
    assert pp.get("/api/pompiste/contexte").json()["pompiste"]["station_nom"] == "Station Champin"
    # Le gérant ne peut pas créer de compte dans une autre station
    assert g.get("/api/pompiste-supervision/comptes").json()["autres_stations"] == []
    assert g.post("/api/pompiste-supervision/comptes", json={"prenom": "X", "nom": "Y", "espace_id": 0}).status_code == 403

    # Depuis la station principale, le propriétaire crée un pompiste pour Champin
    lst = p.get("/api/pompiste-supervision/comptes").json()
    assert lst["station_courante"] == "Station principale"
    assert lst["autres_stations"] == [{"id": eid, "nom": "Station Champin"}]
    r = p.post("/api/pompiste-supervision/comptes", json={"prenom": "Jean", "nom": "Champin", "espace_id": eid})
    assert r.status_code == 201, r.text
    c2 = r.json()
    assert c2["station_nom"] == "Station Champin"
    assert all(x["username"] != c2["username"] for x in p.get("/api/pompiste-supervision/comptes").json()["comptes"])
    assert any(x["username"] == c2["username"] for x in g.get("/api/pompiste-supervision/comptes").json()["comptes"])
    p2 = _client()
    assert p2.post("/api/login/pompiste", json={"username": c2["username"], "code_acces": c2["code_acces"]}).status_code == 200
    ctx = p2.get("/api/pompiste/contexte").json()
    assert ctx["pompiste"]["station_nom"] == "Station Champin"
    assert {q["nom"] for x in ctx["produits"] for q in x["pompes"]} == {"Gazoline 1", "Gazoline 2", "Diesel 1"}


def test_comptes_uniques_et_mot_de_passe_oublie(client, monkeypatch):
    """Un email déjà pris dans une station est refusé dans une autre ; le
    lien « mot de passe oublié » du gérant est retrouvé dans sa station."""
    import password_reset
    p = _client()
    assert p.post("/api/login", json=PROPRIO).status_code == 200
    r = p.post("/api/espaces", json={"nom": "Station Limonade", "adresse": "Limonade",
               "gerant": {"nom_complet": "Gérant Limonade", "email": "gerant.limonade@exemple.ht",
                          "password": "MotDePasse2026", "pin": "555666777"}})
    assert r.status_code == 201, r.text
    # Dans la station principale, l'email du gérant de Limonade est refusé
    roles = p.get("/api/admin/roles").json()
    roles = roles if isinstance(roles, list) else roles.get("roles", [])
    assert roles, "liste des rôles vide"
    if True:
        r = p.post("/api/admin/users", json={"username": "doublon", "email": "gerant.limonade@exemple.ht",
                   "nom_complet": "Doublon", "password": "MotDePasse2026", "code_acces": "111222333",
                   "role_id": roles[0]["id"]})
        assert r.status_code == 409, r.text
    # Mot de passe oublié : le jeton est créé dans la station de Limonade
    captures = []
    monkeypatch.setattr(password_reset, "_send_reset_email", lambda user, jeton, base: captures.append(jeton))
    anonyme = _client()
    assert anonyme.post("/api/auth/forgot-password", json={"identifier": "gerant.limonade@exemple.ht"}).status_code == 200
    assert len(captures) == 1
    assert _client().get(f"/api/auth/reset-password/verify?token={captures[0]}").status_code == 200
    r = _client().post("/api/auth/reset-password", json={"token": captures[0], "new_password": "NouveauMdp2026!"})
    assert r.status_code == 200, r.text
    g = _client()
    assert g.post("/api/login", json={"email": "gerant.limonade@exemple.ht", "password": "NouveauMdp2026!",
                                      "code_acces": "555666777"}).status_code == 200


def test_retirer_station_separee(client):
    p = _client()
    assert p.post("/api/login", json=PROPRIO).status_code == 200
    eid = p.post("/api/espaces", json={"nom": "Station à retirer", "adresse": "Nulle part",
                 "gerant": {"nom_complet": "G R", "email": "gerant.retirer@exemple.ht",
                            "password": "MotDePasse2026", "pin": "444555666"}}).json()["id"]
    assert p.post(f"/api/espaces/{eid}/retirer", json={"confirmation": "non"}).status_code == 400
    assert p.post(f"/api/espaces/{eid}/retirer", json={"confirmation": "RETIRER"}).status_code == 200
    assert all(x["id"] != eid for x in p.get("/api/espaces").json()["espaces"])
    assert p.post("/api/espaces/entrer", json={"cible_id": eid, "pin": "123456789"}).status_code == 404
    # Son gérant ne peut plus entrer
    g = _client()
    assert g.post("/api/login", json={"email": "gerant.retirer@exemple.ht", "password": "MotDePasse2026",
                                      "code_acces": "444555666"}).status_code == 401
