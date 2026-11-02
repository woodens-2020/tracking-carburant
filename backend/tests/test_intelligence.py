"""
KONEKTA Intelligence Center : les chiffres viennent des données réelles
(journal de caisse et relevés validés), filtres et comparaison cohérents,
explorateur relié aux opérations, exports, accès réservé.
"""
import time

H = {"X-API-Key": "smoke-test-key"}
J1, J2, J0 = "2024-03-05", "2024-03-06", "2024-03-04"     # J0 : période précédente (même durée)


def _a(client, q):
    r = client.get(f"/api/intelligence/analyse?{q}", headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def _donnees(client):
    nom = f"BI-{time.time_ns()}"
    pid = client.post("/api/produits", headers=H, json={"nom": nom, "prix_gallon": 100}).json()["id"]
    pompe = client.post(f"/api/produits/{pid}/pompes", headers=H, json={"nom": f"{nom} P1"}).json()["id"]
    for d, per, av, ap in ((J0, "Matin", 0, 4), (J1, "Matin", 4, 14), (J1, "Apres-midi", 14, 19), (J2, "Matin", 19, 29)):
        r = client.post("/api/releves", headers=H, json={"date": d, "periode": per, "pompe_id": pompe,
                                                          "prix_gallon": 100, "metter_avant": av, "metter_apres": ap})
        assert r.status_code in (200, 201), r.text
    for cat, m, d in (("Electricite", 800, J2), ("Electricite", 200, J0), ("Dépot_en_Banque", 2000, J2)):
        assert client.post("/api/depenses", headers=H, json={"categorie": cat, "description": "bi", "montant": m,
                                                             "date_depense": d}).status_code in (200, 201)
    return pid, nom


def test_indicateurs_et_comparaison(client):
    pid, nom = _donnees(client)
    a = _a(client, f"debut={J1}&fin={J2}&produits={pid}")
    k = a["kpis"]
    # 25 gallons × 100 G : seules les ventes de ce produit (filtre produit)
    assert k["revenu"] == 2500 and list(k["gallons_par_produit"].values()) == [25] and k["nb_releves"] == 3
    assert a["kpis_prec"]["revenu"] == 400 and a["variations"]["revenu"] == 525.0
    assert a["comparaison"] == {"debut": "2024-03-03", "fin": "2024-03-04"}
    assert [p["nom"] for p in a["produits"]] == [nom] and a["produits"][0]["gallons"] == 25

    # Sans filtre produit : revenu ≠ encaissements ≠ trésorerie ; le transfert n'est pas une charge
    b = _a(client, f"debut={J1}&fin={J2}&comparer=aucune")
    assert b["kpis_prec"] is None and b["variations"] == {} and b["series_prec"] is None
    kb = b["kpis"]
    assert kb["charges"] >= 800 and kb["decaissements"] >= kb["charges"] + 2000 - 0.01
    assert abs(kb["resultat"] - (kb["revenu"] - kb["charges"])) < 0.01
    cat = {c["nom"]: c for c in b["charges_par_categorie"]}
    assert "Electricite" in cat and "Dépot_en_Banque" not in cat
    # Cascade de caisse : ouverture + mouvements = clôture = trésorerie
    cas = b["cascade"]
    assert abs(cas[0]["montant"] + sum(x["montant"] for x in cas[1:-1]) - cas[-1]["montant"]) < 0.01
    assert abs(cas[-1]["montant"] - b["tresorerie"]["solde_fin"]) < 0.01
    # Série journalière : la somme = le KPI
    assert b["granularite"] == "jour" and abs(sum(s["revenu"] for s in b["series"]) - kb["revenu"]) < 0.01
    assert sum(x["montant"] for x in b["activite"]) > 0

    # Filtre département et type
    c = _a(client, f"debut={J1}&fin={J2}&comparer=aucune&types=depense")
    assert c["kpis"]["revenu"] == 0 and c["kpis"]["gallons_par_produit"] == {} and c["kpis"]["charges"] >= 800


def test_explorateur_et_exports(client):
    pid, nom = _donnees(client)
    r = client.get(f"/api/intelligence/operations?debut={J1}&fin={J2}&produits={pid}&taille=10", headers=H)
    assert r.status_code == 200, r.text
    o = r.json()
    assert o["total"] == 3 and o["somme_entrees"] == 2500      # une ligne par jour, période et produit and all(l["produit_id"] == pid for l in o["lignes"])
    r = client.get(f"/api/intelligence/operations?debut={J1}&fin={J2}&q=Electricite&sens=C", headers=H).json()
    assert r["total"] >= 1 and all(l["sens"] == "C" for l in r["lignes"])
    rel = client.get(f"/api/intelligence/releves?debut={J1}&produits={pid}", headers=H).json()
    assert len(rel["releves"]) == 2 and rel["gallons_par_produit"] == {nom: 15}
    x = client.get(f"/api/intelligence/export?quoi=synthese&format=xlsx&debut={J1}&fin={J2}", headers=H)
    assert x.status_code == 200 and x.content[:2] == b"PK"
    c = client.get(f"/api/intelligence/export?quoi=operations&format=csv&debut={J1}&fin={J2}&produits={pid}", headers=H)
    assert c.status_code == 200 and "Ventes carburant" in c.text and c.text.count("\n") >= 3
    f = client.get(f"/api/intelligence/filtres?debut={J1}&fin={J2}", headers=H).json()
    assert any(p["id"] == str(pid) for p in f["produits"]) and "Electricite" in f["comptes"]


def test_validation_et_acces(client):
    assert client.get("/api/intelligence/analyse?debut=2024-03-06&fin=2024-03-05", headers=H).status_code == 400
    assert client.get("/api/intelligence/analyse?debut=2020-01-01&fin=2024-03-05", headers=H).status_code == 400
    assert client.get("/api/intelligence/analyse?comparer=perso", headers=H).status_code == 400
    from fastapi.testclient import TestClient
    import main
    anonyme = TestClient(main.app)
    assert anonyme.get("/api/intelligence/analyse").status_code == 401


def test_plusieurs_stations(client):
    from fastapi.testclient import TestClient
    import main
    p = TestClient(main.app)
    assert p.post("/api/login", json={"email": "admin@konekta.local", "password": "admin123",
                                      "code_acces": "123456789"}).status_code == 200
    r = p.post("/api/espaces", json={"nom": "Station BI", "adresse": "Limbé",
               "gerant": {"nom_complet": "Gérant BI", "email": "gerant.bi@exemple.ht", "password": "MotDePasse2026", "pin": "222333444"},
               "station": {"produits": [{"cle": "g", "nom": "Gazoline", "prix": 200}],
                           "pompes": [{"nom": "Pompe 1", "faces": [{"nom": "A", "metres": [{"produit": "g", "nom": "Gazoline 1", "metre_depart": 0}]}]}]}})
    assert r.status_code == 201, r.text
    eid = r.json()["id"]
    g = TestClient(main.app)
    assert g.post("/api/login", json={"email": "gerant.bi@exemple.ht", "password": "MotDePasse2026",
                                      "code_acces": "222333444"}).status_code == 200
    pompe = g.get("/api/produits").json()[0]["pompes"][0]["id"]
    assert g.post("/api/releves", json={"date": "2024-05-02", "periode": "Matin", "pompe_id": pompe, "prix_gallon": 200,
                                        "metter_avant": 0, "metter_apres": 7}).status_code in (200, 201)
    # Le gérant voit sa station, pas les autres
    assert g.get("/api/intelligence/analyse?debut=2024-05-02&fin=2024-05-02").json()["kpis"]["revenu"] == 1400
    assert g.get("/api/intelligence/analyse?debut=2024-05-02&fin=2024-05-02&stations=0").status_code == 403
    # Le propriétaire analyse les deux stations ensemble
    seule = p.get("/api/intelligence/analyse?debut=2024-05-02&fin=2024-05-02&comparer=aucune").json()["kpis"]["revenu"]
    a = p.get(f"/api/intelligence/analyse?debut=2024-05-02&fin=2024-05-02&comparer=aucune&stations=0,{eid}").json()
    assert abs(a["kpis"]["revenu"] - (seule + 1400)) < 0.01
    assert {s["nom"] for s in a["stations"]} >= {"Station BI"}
    assert p.get("/api/intelligence/filtres").json()["multi_stations"] is True
