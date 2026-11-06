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
    # Un recul non plus n'est pas compté tant que la direction ne l'a pas confirmé
    prod2 = client.post("/api/produits", headers=H, json={"nom": f"RJ2-{time.time_ns()}", "prix_gallon": 500}).json()["id"]
    m2 = client.post(f"/api/produits/{prod2}/pompes", headers=H, json={"nom": "Diesel 9"}).json()["id"]
    ids = []
    for d, per, av, ap in (("2025-06-15", "Apres-midi", 100, 300), ("2025-06-16", "Matin", 250, 260)):
        r = client.post("/api/releves", headers=H, json={"date": d, "periode": per, "pompe_id": m2, "prix_gallon": 500,
                                                         "metter_avant": av, "metter_apres": ap, "motif_ecart_compteur": "changement de compteur"})
        assert r.status_code in (200, 201), r.text
        ids.append(r.json()["id"])
    total = lambda: (client.get("/api/carburant/rapport-jour?date=2025-06-16", headers=H).json()["entrees"]["total"],
                     round(sum(x["montant"] for x in client.get("/api/journal?date_debut=2025-06-16&date_fin=2025-06-16",
                                                                headers=H).json()["resume"]["par_produit"]), 2))
    assert total() == (10000, 10000)                       # saut (5 000) et recul (5 000) non comptés
    rj = client.get("/api/carburant/rapport-jour?date=2025-06-16", headers=H).json()
    nc = {x["releve_id"]: x for x in rj["non_certifies"]["lignes"]}
    assert nc[ids[1]]["type_anomalie"] == "recul_compteur" and nc[ids[1]]["compte"] is False
    # Confirmé (écart justifié) : il compte partout
    r = client.post(f"/api/journal/releves/{ids[1]}/resoudre", headers=H,
                    json={"description": "Compteur changé le matin : nouveau départ vérifié sur place"})
    assert r.status_code == 200, r.text
    assert total() == (15000, 15000)


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


def test_rapport_jour_meme_centimes_que_le_journal(client):
    """Chaque relevé compte au centime près (comme au Journal Meters) : les
    deux totaux sont identiques, sans demi-centime."""
    pid = client.post("/api/produits", headers=H, json={"nom": f"Gazoline C{time.time_ns()}", "prix_gallon": 750}).json()["id"]
    for k, (av, ap) in enumerate(((0, 0.0013), (0.0013, 0.0026), (0.0026, 0.0039))):
        m = client.post(f"/api/produits/{pid}/pompes", headers=H, json={"nom": f"C{k}"}).json()["id"]
        r = client.post("/api/releves", headers=H, json={"date": "2019-02-03", "periode": "Matin", "pompe_id": m,
                                                         "prix_gallon": 750, "metter_avant": 0, "metter_apres": 1.0007})
        assert r.status_code in (200, 201), r.text
    j = client.get("/api/journal?date_debut=2019-02-03&date_fin=2019-02-03", headers=H).json()
    rj = client.get("/api/carburant/rapport-jour?date=2019-02-03", headers=H).json()
    tj = round(sum(x["montant"] for x in j["resume"]["par_produit"]), 2)
    assert rj["entrees"]["total"] == tj and round(tj, 2) == tj and abs(tj - 3 * 750.525) < 0.05


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


def test_meme_total_dans_tous_les_modules(client):
    """Un saut de compteur non résolu n'est compté nulle part : Journal
    Meters, Rapport journalier, Accueil / Tableau de bord (stats, série),
    synthèse du jour et Caisse principale donnent le même total, la caisse
    reste équilibrée et signale le saut."""
    d = "2016-06-02"
    pid = client.post("/api/produits", headers=H, json={"nom": f"Gazoline T{time.time_ns()}", "prix_gallon": 500}).json()["id"]
    m = client.post(f"/api/produits/{pid}/pompes", headers=H, json={"nom": "TT1"}).json()["id"]
    for dd, per, av, ap in (("2016-06-01", "Apres-midi", 0, 100), (d, "Matin", 100, 110), (d, "Apres-midi", 130, 140)):
        r = client.post("/api/releves", headers=H, json={"date": dd, "periode": per, "pompe_id": m, "prix_gallon": 500,
                                                         "metter_avant": av, "metter_apres": ap, "motif_ecart_compteur": "saut test"})
        assert r.status_code in (200, 201), r.text
    attendu = 10 * 500                      # le relevé en saut (130 → 140) n'est pas compté
    j = client.get(f"/api/journal?date_debut={d}&date_fin={d}&pompe_id={m}", headers=H).json()
    assert round(sum(x["montant"] for x in j["resume"]["par_produit"]), 2) == attendu
    tot = lambda u, k: client.get(u, headers=H).json()[k]
    assert tot(f"/api/stats?date_debut={d}&date_fin={d}&pompe_id={m}", "total_montant") == attendu
    cp = client.get(f"/api/caisse-principale/journal?date_debut={d}&date_fin={d}", headers=H).json()
    ventes = [l for l in cp["lignes"] if l["type"] == "vente_carburant" and l.get("produit_id") == pid]
    assert round(sum(l["debit"] for l in ventes), 2) == attendu
    ctl = {c["code"]: c for c in cp["controles"]}
    assert ctl["equilibre"]["niveau"] == "ok"
    assert "sauts_non_comptes" in ctl
    rj = client.get(f"/api/carburant/rapport-jour?date={d}", headers=H).json()
    vj = next(x for x in rj["entrees"]["par_produit"] if x["produit"].startswith("Gazoline T"))
    assert vj["montant"] == attendu
