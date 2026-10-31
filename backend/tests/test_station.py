"""
Réenregistrer une station selon la logique Pompe › Faces › mètres :
« retirer l'existant » archive produits et mètres sans rien effacer, puis
le formulaire crée tout en une fois avec le mètre de départ de chaque mètre.
"""
H = {"X-API-Key": "smoke-test-key"}


def _station(adresse="Route Nationale 1, Vertières"):
    def face(nom, metres):
        return {"nom": nom, "metres": [{"produit": p, "nom": n, "metre_depart": dep} for p, n, dep in metres]}
    return {"adresse": adresse,
            "produits": [{"cle": "g", "nom": "Gazoline", "prix": 925.5}, {"cle": "d", "nom": "Diesel", "prix": 1000}],
            "pompes": [
                {"nom": "Pompe 1", "faces": [face("A", [("g", "Gazoline 1", 1000), ("g", "Gazoline 2", 2000), ("d", "Diesel 1", 500)]),
                                             face("B", [("g", "Gazoline 3", 3000), ("g", "Gazoline 4", 4000), ("d", "Diesel 2", 600)])]},
                {"nom": "Pompe 2", "faces": [face("A", [("g", "Gazoline 5", 0)]), face("B", [("d", "Diesel 3", 0)])]}]}


def test_retirer_puis_enregistrer_la_station(client):
    # Un relevé existant sur l'ancien « Gazoline 1 »
    anciens = {p["nom"]: p for p in client.get("/api/produits", headers=H).json()}
    ancien_metre = anciens["Gazoline"]["pompes"][0]["id"]
    r = client.post("/api/releves", headers=H, json={"date": "2025-05-01", "periode": "Matin", "pompe_id": ancien_metre,
                                                      "prix_gallon": 900, "metter_avant": 0, "metter_apres": 10})
    assert r.status_code in (200, 201), r.text
    # Retirer l'existant : confirmation obligatoire
    assert client.post("/api/stations/retirer-tout", headers=H, json={"confirmation": "oui"}).status_code == 400
    r = client.post("/api/stations/retirer-tout", headers=H, json={"confirmation": "RETIRER"})
    assert r.status_code == 200 and r.json()["produits_archives"] >= 2, r.text
    assert client.get("/api/produits", headers=H).json() == []
    # L'historique reste : le relevé est toujours au journal
    j = client.get(f"/api/journal?date_debut=2025-05-01&date_fin=2025-05-01&pompe_id={ancien_metre}", headers=H).json()
    assert len(j["entries"]) == 1 and "archivé" in j["entries"][0]["produit_nom"]

    # Formulaire : station complète en une fois
    r = client.post("/api/stations", headers=H, json=_station())
    assert r.status_code == 201, r.text
    assert r.json()["metres"] == 8
    prods = {p["nom"]: p for p in client.get("/api/produits", headers=H).json()}
    assert set(prods) == {"Gazoline", "Diesel"}                       # mêmes noms qu'avant : possible
    assert prods["Gazoline"]["prix_vente_actif"] == 925.5
    assert all(q["station"] == "Route Nationale 1, Vertières" for q in prods["Gazoline"]["pompes"])
    dist = client.get("/api/distributeurs", headers=H).json()["distributeurs"]
    assert [d["nom"] for d in dist] == ["Pompe 1", "Pompe 2"]
    assert {d["adresse"] for d in dist} == {"Route Nationale 1, Vertières"}      # l'adresse est sur les pompes
    assert [[m["nom"] for m in f["metres"]] for f in dist[0]["faces"]] == [
        ["Gazoline 1", "Gazoline 2", "Diesel 1"], ["Gazoline 3", "Gazoline 4", "Diesel 2"]]
    # Le mètre de départ sert de « mètre précédent »
    g1 = next(q for q in prods["Gazoline"]["pompes"] if q["nom"] == "Gazoline 1")
    assert g1["metre_depart"] == 1000
    r = client.post("/api/releves", headers=H, json={"date": "2025-05-02", "periode": "Matin", "pompe_id": g1["id"],
                                                      "prix_gallon": 925.5, "metter_avant": 1000, "metter_apres": 1050})
    assert r.status_code in (200, 201), r.text
    e = client.get(f"/api/journal?date_debut=2025-05-02&date_fin=2025-05-02&pompe_id={g1['id']}", headers=H).json()["entries"][0]
    assert e["statut"] == "ok" and e["metter_attendu"] == 1000
    # Début différent du mètre de départ : motif demandé
    g2 = next(q for q in prods["Gazoline"]["pompes"] if q["nom"] == "Gazoline 2")
    r = client.post("/api/releves", headers=H, json={"date": "2025-05-02", "periode": "Matin", "pompe_id": g2["id"],
                                                      "prix_gallon": 925.5, "metter_avant": 2100, "metter_apres": 2150})
    assert r.status_code == 409


def test_formulaire_controle(client):
    st = _station("Champin, Cap-Haïtien")
    st["produits"] = [{"cle": "g", "nom": "Gazoline Champin", "prix": 900}]
    st["pompes"] = [{"nom": "Pompe 9", "faces": [{"nom": "A", "metres": [{"produit": "x", "nom": "G1"}]}]}]
    assert client.post("/api/stations", headers=H, json=st).status_code == 400      # produit inconnu
    st["pompes"][0]["faces"][0]["metres"] = [{"produit": "g", "nom": "G1"}, {"produit": "g", "nom": "g1"}]
    assert client.post("/api/stations", headers=H, json=st).status_code == 409      # mètre en double
    st["adresse"] = ""
    assert client.post("/api/stations", headers=H, json=st).status_code == 400      # adresse obligatoire



def test_deuxieme_station_reprend_les_produits(client):
    """Les produits sont communs : une 2e station reprend Gazoline / Diesel
    (même prix ou nouveau prix), ses pompes portent sa propre adresse, et un
    nom de mètre déjà pris est refusé avec un message clair."""
    avant = {p["nom"]: p["id"] for p in client.get("/api/produits", headers=H).json()}
    st = {"adresse": "Champin, Cap-Haïtien",
          "produits": [{"cle": "g", "nom": "gazoline", "prix": 925.5}],
          "pompes": [{"nom": "Pompe 1", "faces": [{"nom": "A", "metres": [{"produit": "g", "nom": "Gazoline 1"}]}]}]}
    r = client.post("/api/stations", headers=H, json=st)
    if "Gazoline" in avant:
        assert r.status_code == 409 and "existe déjà" in r.json()["detail"]
    st["pompes"][0]["faces"][0]["metres"][0]["nom"] = "Gazoline C1"
    r = client.post("/api/stations", headers=H, json=st)
    assert r.status_code == 201, r.text
    apres = {p["nom"]: p for p in client.get("/api/produits", headers=H).json()}
    assert [n for n in apres if n.casefold() == "gazoline"] == (["Gazoline"] if "Gazoline" in avant else ["gazoline"])
    stations = {s["adresse"] for s in client.get("/api/pompiste-supervision/stations", headers=H).json()}
    assert "Champin, Cap-Haïtien" in stations
