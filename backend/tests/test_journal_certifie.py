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


def test_rapport_jour_signale_non_certifies(client):
    """Rapport journalier : les relevés non certifiés du Journal Meters sont
    signalés, et peuvent être écartés du total."""
    prod = client.post("/api/produits", headers=H, json={"nom": f"RJ-{time.time_ns()}", "prix_gallon": 500}).json()["id"]
    m = client.post(f"/api/produits/{prod}/pompes", headers=H, json={"nom": "Gazoline 1"}).json()["id"]
    rel = lambda d, per, av, ap, motif=None: client.post("/api/releves", headers=H, json={
        "date": d, "periode": per, "pompe_id": m, "prix_gallon": 500, "metter_avant": av, "metter_apres": ap,
        "motif_ecart_compteur": motif}).json()
    rel("2025-06-15", "Apres-midi", 100, 150)
    rel("2025-06-16", "Matin", 150, 170)                                  # continu : certifié
    saut = rel("2025-06-16", "Apres-midi", 190, 200, "écart à vérifier")  # saut : non certifié
    rj = client.get("/api/carburant/rapport-jour?date=2025-06-16", headers=H).json()
    nc = [x for x in rj["non_certifies"]["lignes"] if x["releve_id"] == saut["id"]]
    assert len(nc) == 1 and nc[0]["type_anomalie"] == "saut_compteur" and nc[0]["montant"] == 5000
    sans = client.get("/api/carburant/rapport-jour?date=2025-06-16&sans_non_certifies=true", headers=H).json()
    assert sans["sans_non_certifies"] is True
    assert round(rj["entrees"]["total"] - sans["entrees"]["total"], 2) == rj["non_certifies"]["montant"]
