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


def test_changer_date_releve_journal(client):
    """Journal Meters : un relevé saisi sur le mauvais jour est déplacé ; la
    chaîne des compteurs se recalcule aux deux créneaux."""
    from database import SessionLocal
    from models import Releve
    pid = client.post("/api/produits", headers=H, json={"nom": f"Gazoline D{time.time_ns()}", "prix_gallon": 500}).json()["id"]
    m = client.post(f"/api/produits/{pid}/pompes", headers=H, json={"nom": "DJ1"}).json()["id"]
    rel = lambda d, per, av, ap: client.post("/api/releves", headers=H, json={
        "date": d, "periode": per, "pompe_id": m, "prix_gallon": 500, "metter_avant": av, "metter_apres": ap,
        "motif_ecart_compteur": "mise en place test"}).json()
    rel("2017-04-01", "Matin", 0, 100)
    j3 = rel("2017-04-03", "Matin", 120, 150)            # écart noté : 100 → 120 manque
    faux = rel("2017-04-05", "Matin", 100, 120)          # ce relevé est celui du 02/04 après-midi
    ap = lambda d, per: client.get(f"/api/releves/{faux['id']}/changer-date/apercu?date={d}&periode={per}", headers=H).json()
    a = ap("2017-04-02", "Apres-midi")
    assert a["bloquants"] == [] and a["lignes"][0]["suit"] and a["lignes"][0]["enchaine"]
    assert ap("2017-04-03", "Matin")["bloquants"]                       # place déjà prise
    assert ap("2099-01-01", "Matin")["bloquants"]                       # futur
    assert client.post(f"/api/releves/{faux['id']}/changer-date", headers=H,
                       json={"date": "2017-04-03", "periode": "Matin", "motif": "essai place prise"}).status_code == 409
    r = client.post(f"/api/releves/{faux['id']}/changer-date", headers=H,
                    json={"date": "2017-04-02", "periode": "Apres-midi", "motif": "Relevé du 02/04 après-midi"})
    assert r.status_code == 200, r.text
    assert any("reprend maintenant exactement" in t for t in r.json()["avertissements"])
    with SessionLocal() as s:
        x = s.get(Releve, faux["id"])
        assert str(x.date) == "2017-04-02" and x.periode == "Apres-midi" and x.ecart_compteur is None
        assert s.get(Releve, j3["id"]).ecart_compteur is None
    E = client.get("/api/journal?date_debut=2017-04-01&date_fin=2017-04-05", headers=H).json()["entries"]
    assert all(e["statut"] == "ok" for e in E if e["pompe_id"] == m and e["type_anomalie"] != "premiere_lecture")
