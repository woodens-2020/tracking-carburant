"""
Carte des stations : positions GPS / carte validées (Haïti), doublons,
position vérifiée protégée, historique, filtres, et isolation : un gérant
ne voit et ne modifie que sa station.
"""
from fastapi.testclient import TestClient

PROPRIO = {"email": "admin@konekta.local", "password": "admin123", "code_acces": "123456789"}
CAP = (19.7594, -72.2003)          # Cap-Haïtien


def _c():
    import main
    return TestClient(main.app)


def test_carte_des_stations(client):
    p = _c()
    assert p.post("/api/login", json=PROPRIO).status_code == 200
    assert _c().get("/api/geospatial/stations").status_code == 401

    # Station principale : informations et position
    r = p.put("/api/geospatial/stations/0/infos", json={"code": "cpv-01", "departement": "Nord", "commune": "Cap-Haïtien",
                                                        "adresse": "Route nationale 1"})
    assert r.status_code == 200, r.text
    assert r.json()["fiche"]["code"] == "CPV-01"
    assert p.put("/api/geospatial/stations/0/infos", json={"departement": "Paris"}).status_code == 400
    assert p.post("/api/geospatial/stations/0/location", json={"latitude": 48.85, "longitude": 2.35}).status_code == 400
    assert p.post("/api/geospatial/stations/0/location", json={"latitude": 19.7, "longitude": -72.2, "source": "satellite"}).status_code == 400
    r = p.post("/api/geospatial/stations/0/location", json={"latitude": CAP[0], "longitude": CAP[1], "precision_m": 12.5,
                                                            "source": "gps", "verifie": True})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["latitude"] == CAP[0] and d["verification"] == "verifie" and d["fiche"]["precision_m"] == 12.5
    assert d["journal"][0]["action"] == "position" and d["journal"][0]["nouvelles"]["latitude"] == CAP[0]
    # Une position vérifiée ne se remplace pas sans confirmation
    r = p.post("/api/geospatial/stations/0/location", json={"latitude": 19.76, "longitude": -72.21})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "position_verifiee"
    assert p.post("/api/geospatial/stations/0/location", json={"latitude": CAP[0], "longitude": CAP[1], "source": "gps",
                                                               "verifie": True, "confirmer_remplacement": True}).status_code == 200

    # Station séparée enregistrée avec sa localisation ; à 30 m d'une autre : avertie
    r = p.post("/api/espaces", json={"nom": "Station Limbé", "adresse": "Limbé",
               "gerant": {"nom_complet": "Gérant Limbé", "email": "gerant.limbe@exemple.ht", "password": "MotDePasse2026", "pin": "333444555"},
               "geo": {"code": "LMB-01", "departement": "Nord", "commune": "Limbé", "latitude": 19.7034, "longitude": -72.4003,
                       "precision_m": 8, "source": "gps", "verifie": True}})
    assert r.status_code == 201, r.text
    lid = r.json()["id"]
    assert r.json()["avertissement_geo"] is None
    r = p.post(f"/api/geospatial/stations/{lid}/location", json={"latitude": CAP[0] + 0.0002, "longitude": CAP[1], "confirmer_remplacement": True})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "station_proche"
    assert p.put(f"/api/geospatial/stations/{lid}/infos", json={"code": "CPV-01"}).status_code == 409   # code déjà pris

    # Liste, filtres, synthèse, proximité
    tout = p.get("/api/geospatial/stations").json()
    assert {s["id"] for s in tout["stations"]} >= {0, lid}
    assert [s["id"] for s in p.get("/api/geospatial/stations?q=lmb").json()["stations"]] == [lid]
    assert all(s["departement"] == "Nord" for s in p.get("/api/geospatial/stations?departement=Nord").json()["stations"])
    assert all(s["verification"] == "verifie" for s in p.get("/api/geospatial/stations?gps=verifie").json()["stations"])
    o = p.get("/api/geospatial/overview").json()
    assert o["total"] >= 2 and o["gps_verifies"] >= 2 and o["departements_couverts"] >= 1 and o["peut_tout_voir"] is True
    pr = p.get(f"/api/geospatial/stations/nearby?lat={CAP[0]}&lon={CAP[1]}&rayon_km=30").json()["stations"]
    assert [s["id"] for s in pr][:1] == [0] and any(s["id"] == lid and 20000 < s["distance_m"] < 23000 for s in pr)
    dep = p.get("/api/geospatial/departments").json()
    assert len(dep["departements"]) == 10 and "Limbé" in dep["communes"]["Nord"]
    det = p.get(f"/api/geospatial/stations/{lid}").json()
    assert det["activite"]["nb_releves"] == 0 and det["fiche"]["commune"] == "Limbé"

    # Isolation : le gérant de Limbé ne voit que sa station
    g = _c()
    assert g.post("/api/login", json={"email": "gerant.limbe@exemple.ht", "password": "MotDePasse2026",
                                      "code_acces": "333444555"}).status_code == 200
    assert [s["id"] for s in g.get("/api/geospatial/stations").json()["stations"]] == [lid]
    assert g.get("/api/geospatial/stations/0").status_code == 404
    assert g.post("/api/geospatial/stations/0/location", json={"latitude": 19.0, "longitude": -72.5}).status_code == 404
    assert g.put("/api/geospatial/stations/0/infos", json={"code": "PIRATE"}).status_code == 404
    assert g.get("/api/geospatial/overview").json()["total"] == 1
    near = g.get(f"/api/geospatial/stations/nearby?lat={CAP[0]}&lon={CAP[1]}&rayon_km=50").json()["stations"]
    assert [s["id"] for s in near] == [lid]


def test_fiche_station_contacts(client):
    """Contacts de la station : numéros d'Haïti normalisés, email contrôlé,
    seuls les champs envoyés changent, fiche complète suivie."""
    p = _c()
    assert p.post("/api/login", json=PROPRIO).status_code == 200
    r = p.put("/api/geospatial/stations/0/infos", json={
        "adresse": "Champin", "departement": "Nord", "commune": "Cap-Haïtien", "responsable": "Marie Gérante",
        "telephone": "3712 3456", "email": "Station.Champin@Exemple.ht", "urgence_nom": "Pompiers",
        "urgence_telephone": "+509 4455-6677", "horaires": "6 h – 22 h", "note": "Accès camions par l’arrière"})
    assert r.status_code == 200, r.text
    f = r.json()["fiche"]
    assert f["telephone"] == "+509 3712-3456" and f["urgence_telephone"] == "+509 4455-6677"
    assert f["email"] == "station.champin@exemple.ht" and f["maj_par"] and f["maj_le"]
    assert r.json()["journal"][0]["action"] == "informations"
    # Un seul champ envoyé : le reste ne bouge pas
    r = p.put("/api/geospatial/stations/0/infos", json={"telephone": "50937123457"})
    assert r.json()["fiche"]["telephone"] == "+509 3712-3457" and r.json()["fiche"]["email"] == "station.champin@exemple.ht"
    assert p.put("/api/geospatial/stations/0/infos", json={"telephone": "+1 305 555 0101"}).json()["fiche"]["telephone"] == "+13055550101"
    for mauvais in ({"telephone": "abc"}, {"telephone": "123"}, {"urgence_telephone": "+509 12"}, {"email": "pas-un-email"}):
        assert p.put("/api/geospatial/stations/0/infos", json=mauvais).status_code == 400, mauvais
    # Effacer un champ
    assert p.put("/api/geospatial/stations/0/infos", json={"email": ""}).json()["fiche"]["email"] is None
    s = next(x for x in p.get("/api/geospatial/stations").json()["stations"] if x["id"] == 0)
    assert "email" in s["completude"]["manquants"] and s["completude"]["pct"] < 100 and s["a_revoir"] is False
    assert s["telephone"] == "+13055550101" and s["responsable"] == "Marie Gérante"
