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
    # Même total que le Journal Meters : le saut non résolu n'est pas compté
    assert nc[0]["compte"] is False and nc[0]["exclu_journal"] is True
    j = client.get("/api/journal?date_debut=2025-06-16&date_fin=2025-06-16", headers=H).json()
    total_journal = round(sum(x["montant"] for x in j["resume"]["par_produit"]), 2)
    assert rj["entrees"]["total"] == total_journal == 10000
    rec = rj["reconciliation"]
    assert rec["releves"]["montant"] == 15000 and rec["sauts_exclus"]["montant"] == 5000
    assert rec["releves"]["montant"] - rec["sauts_exclus"]["montant"] - rec["autres_exclus"]["montant"] == rj["entrees"]["total"]
    # Un recul est compté (comme au journal), sauf si l'on coche « exclure aussi »
    prod2 = client.post("/api/produits", headers=H, json={"nom": f"RJ2-{time.time_ns()}", "prix_gallon": 500}).json()["id"]
    m2 = client.post(f"/api/produits/{prod2}/pompes", headers=H, json={"nom": "Diesel 9"}).json()["id"]
    for d, per, av, ap in (("2025-06-15", "Apres-midi", 100, 300), ("2025-06-16", "Matin", 250, 260)):
        r = client.post("/api/releves", headers=H, json={"date": d, "periode": per, "pompe_id": m2, "prix_gallon": 500,
                                                         "metter_avant": av, "metter_apres": ap, "motif_ecart_compteur": "changement de compteur"})
        assert r.status_code in (200, 201), r.text
    rj = client.get("/api/carburant/rapport-jour?date=2025-06-16", headers=H).json()
    j = client.get("/api/journal?date_debut=2025-06-16&date_fin=2025-06-16", headers=H).json()
    assert rj["entrees"]["total"] == round(sum(x["montant"] for x in j["resume"]["par_produit"]), 2) == 15000
    sans = client.get("/api/carburant/rapport-jour?date=2025-06-16&sans_non_certifies=true", headers=H).json()
    assert sans["sans_non_certifies"] is True and sans["entrees"]["total"] == 10000
    assert sans["reconciliation"]["autres_exclus"]["montant"] == 5000


def test_gallons_par_carburant_jamais_melanges(client):
    """Gazoline 1 et 2 sont réunies, Diesel 1 et 2 aussi ; essence et diesel
    ne s'additionnent jamais en gallons (journal et rapport du jour)."""
    ns = time.time_ns()
    pompes = {}
    for nom, prix in ((f"Gazoline 1 T{ns}", 500), (f"Gazoline 2 T{ns}", 600), (f"Diesel 1 T{ns}", 700)):
        pid = client.post("/api/produits", headers=H, json={"nom": nom, "prix_gallon": prix}).json()["id"]
        pompes[nom] = (client.post(f"/api/produits/{pid}/pompes", headers=H, json={"nom": f"P {nom}"}).json()["id"], prix)
    for (pm, prix), (av, ap) in zip(pompes.values(), ((0, 10), (0, 20), (0, 5))):
        r = client.post("/api/releves", headers=H, json={"date": "2025-03-10", "periode": "Matin", "pompe_id": pm,
                                                         "prix_gallon": prix, "metter_avant": av, "metter_apres": ap})
        assert r.status_code in (200, 201), r.text
    j = client.get("/api/journal?date_debut=2025-03-10&date_fin=2025-03-10", headers=H).json()
    fam = {x["famille"]: x for x in j["resume"]["par_famille"]}
    assert set(fam) == {"Gazoline", "Diesel"}
    assert fam["Gazoline"]["gallons"] == 30 and fam["Gazoline"]["montant"] == 10 * 500 + 20 * 600
    assert fam["Diesel"]["gallons"] == 5
    rj = client.get("/api/carburant/rapport-jour?date=2025-03-10", headers=H).json()
    assert rj["reconciliation"]["releves"]["gallons_par_carburant"] == {"Gazoline": 30, "Diesel": 5}
    assert rj["entrees"]["total"] == 5000 + 12000 + 3500
