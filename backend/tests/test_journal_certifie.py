"""Journal Meters : filtrer les mètres certifiés / non certifiés."""
import time

H = {"X-API-Key": "smoke-test-key"}


def test_filtre_certifie(client):
    prod = client.post("/api/produits", headers=H, json={"nom": f"Cert-{time.time_ns()}", "prix_gallon": 500}).json()["id"]
    m = client.post(f"/api/produits/{prod}/pompes", headers=H, json={"nom": "Gazoline 1"}).json()["id"]
    rel = lambda d, per, av, ap, motif=None: client.post("/api/releves", headers=H, json={
        "date": d, "periode": per, "pompe_id": m, "prix_gallon": 500, "metter_avant": av, "metter_apres": ap,
        "motif_ecart_compteur": motif})
    assert rel("2025-05-05", "Matin", 100, 150).status_code == 200          # première lecture : certifiée
    assert rel("2025-05-05", "Apres-midi", 150, 170).status_code == 200     # continu : certifié
    assert rel("2025-05-06", "Matin", 180, 200, "écart à vérifier").status_code == 200   # saut : non certifié
    url = f"/api/journal?date_debut=2025-05-05&date_fin=2025-05-06&pompe_id={m}"
    tous = client.get(url, headers=H).json()
    assert [e["certifie"] for e in tous["entries"]] == [True, True, False]
    oui = client.get(url + "&certifie=oui", headers=H).json()
    assert len(oui["entries"]) == 2 and all(e["certifie"] for e in oui["entries"])
    assert oui["resume"]["total_entrees"] == 2 and oui["resume"]["total_gallons"] == 70
    non = client.get(url + "&certifie=non", headers=H).json()
    assert [(e["date"], e["periode"]) for e in non["entries"]] == [("2025-05-06", "Matin")]
    assert non["entries"][0]["type_anomalie"] == "saut_compteur"         # la continuité reste calculée sur tout
    assert client.get(url + "&certifie=peut-etre", headers=H).status_code == 400
    assert client.get(url.replace("/api/journal", "/api/journal/pdf") + "&certifie=non", headers=H).status_code == 200


def test_supprimer_un_releve(client):
    """🗑 du Journal Meters : motif obligatoire, la place est libérée."""
    prod = client.post("/api/produits", headers=H, json={"nom": f"Sup-{time.time_ns()}", "prix_gallon": 500}).json()["id"]
    m = client.post(f"/api/produits/{prod}/pompes", headers=H, json={"nom": "Diesel 1"}).json()["id"]
    rid = client.post("/api/releves", headers=H, json={"date": "2025-06-01", "periode": "Matin", "pompe_id": m,
                      "prix_gallon": 500, "metter_avant": 10, "metter_apres": 30}).json()["id"]
    assert client.post("/api/releves/supprimer", headers=H, json={"ids": [rid], "motif": "x"}).status_code == 400
    r = client.post("/api/releves/supprimer", headers=H, json={"ids": [rid], "motif": "index mal tapé"})
    assert r.status_code == 200 and r.json()["supprimes"] == 1
    url = f"/api/journal?date_debut=2025-06-01&date_fin=2025-06-01&pompe_id={m}"
    assert client.get(url, headers=H).json()["entries"] == []
    assert client.post("/api/releves/supprimer", headers=H, json={"ids": [rid], "motif": "encore une fois"}).status_code == 404
