"""
Mètres de départ : pour démarrer un mois (novembre), l'index de chaque
mètre à la fin du dernier jour du mois précédent (31/10). La continuité
repart de ce chiffre ; les relevés d'avant restent en historique.
"""
import time

H = {"X-API-Key": "smoke-test-key"}


def _releve(client, d, per, pid, av, ap, motif=None):
    return client.post("/api/releves", headers=H, json={"date": d, "periode": per, "pompe_id": pid, "prix_gallon": 500,
                                                        "metter_avant": av, "metter_apres": ap,
                                                        "motif_ecart_compteur": motif})


def test_metre_de_depart_du_mois(client):
    prod = client.post("/api/produits", headers=H, json={"nom": f"Dep-{time.time_ns()}", "prix_gallon": 500}).json()["id"]
    m = client.post(f"/api/produits/{prod}/pompes", headers=H, json={"nom": "Gazoline 1"}).json()["id"]
    assert _releve(client, "2025-03-30", "Apres-midi", m, 100, 150).status_code == 200
    assert _releve(client, "2025-03-31", "Apres-midi", m, 150, 180).status_code == 200
    # Le 1er avril démarre sur un autre chiffre (nouveau compteur, mètre repris…) : sans départ, écart exigé
    r = _releve(client, "2025-04-01", "Matin", m, 500, 540)
    assert r.status_code == 409
    assert _releve(client, "2025-04-01", "Matin", m, 500, 540, motif="nouveau mois, à vérifier").status_code == 200

    # Espace « Mètres de départ » : proposition = fin du 31/03
    d = client.get("/api/metres-depart?date=2025-03-31", headers=H).json()
    x = next(y for y in d["metres"] if y["pompe_id"] == m)
    assert x["proposition"]["metre"] == 180 and x["proposition"]["date"] == "2025-03-31"
    assert x["fixe"] is False and x["premier_apres"]["metter_avant"] == 500
    r = client.put("/api/metres-depart", headers=H, json={"date": "2025-03-31", "metres": [{"pompe_id": m, "metre": 500}]})
    assert r.status_code == 200, r.text
    assert r.json()["modifies"] == 1 and any("reprend maintenant exactement" in a for a in r.json()["avertissements"])
    x = next(y for y in r.json()["metres"] if y["pompe_id"] == m)
    assert x["fixe"] is True and x["depart"] == {"date": "2025-03-31", "metre": 500.0}

    # Le relevé du 1er avril reprend le départ : plus d'écart, certifié
    rel = next(y for y in client.get("/api/releves?date=2025-04-01", headers=H).json() if y["pompe_id"] == m)
    assert rel["ecart_compteur"] is None and rel["certifie"] is True
    j = client.get(f"/api/journal?date_debut=2025-03-25&date_fin=2025-04-02&pompe_id={m}", headers=H).json()["entries"]
    e = next(y for y in j if y["date"] == "2025-04-01")
    assert e["statut"] == "ok" and e["metter_attendu"] == 500
    j = client.get(f"/api/journal?date_debut=2025-04-01&date_fin=2025-04-02&pompe_id={m}", headers=H).json()["entries"]
    assert j[0]["statut"] == "ok" and j[0]["metter_attendu"] == 500
    # Saisie suivante : préremplie, et la continuité suit normalement
    ip = next(y for y in client.get("/api/releves/index-precedent?date=2025-04-01&periode=Apres-midi", headers=H).json()
              if y["pompe_id"] == m)
    assert ip["index_precedent"] == 540
    assert _releve(client, "2025-04-01", "Apres-midi", m, 540, 560).status_code == 200
    # Le mois d'avant garde sa propre chaîne (corriger le 31/03 ne compare pas au départ)
    assert _releve(client, "2025-03-31", "Apres-midi", m, 150, 185).status_code == 200
    # Retirer le départ : l'écart revient sur le 1er avril
    r = client.put("/api/metres-depart", headers=H, json={"date": "2025-03-31", "metres": [{"pompe_id": m, "metre": None}]})
    assert r.status_code == 200 and r.json()["modifies"] == 1
    j = client.get(f"/api/journal?date_debut=2025-03-25&date_fin=2025-04-02&pompe_id={m}", headers=H).json()["entries"]
    assert next(y for y in j if y["date"] == "2025-04-01" and y["periode"] == "Matin")["type_anomalie"] == "saut_compteur"
    assert client.put("/api/metres-depart", headers=H, json={"date": "2099-01-01", "metres": []}).status_code == 400
