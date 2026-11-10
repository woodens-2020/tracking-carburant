"""
Classement des pompistes : gallons par carburant (jamais additionnés),
montant vendu, dépenses (saisies pour lui ou payées sur ses rapports), net,
rang par montant ; les relevés sans pompiste à part, sans rang.
"""
import time
from datetime import timedelta

H = {"X-API-Key": "smoke-test-key"}


def _produit(client, nom, prix, metres):
    pid = client.post("/api/produits", headers=H, json={"nom": nom, "prix_gallon": prix}).json()["id"]
    return [client.post(f"/api/produits/{pid}/pompes", headers=H, json={"nom": m}).json()["id"] for m in metres], pid


def test_classement(client):
    from tz_utils import today_haiti
    j = today_haiti() - timedelta(days=40)
    d1, d2 = str(j), str(j + timedelta(days=1))
    ns = time.time_ns()
    (g,), gp = _produit(client, f"Gazoline C{ns}", 500, ["G1"])
    (d,), dp = _produit(client, f"Diesel C{ns}", 600, ["D1"])
    emp = lambda p: client.post("/api/employes", headers=H, json={"prenom": p, "nom": f"C{ns}", "poste": "Pompiste",
                                                                  "date_embauche": "2026-01-01", "salaire_base": 0}).json()["id"]
    a, b = emp("Anne"), emp("Bob")
    def rel(dt, per, p, av, ap, qui):
        r = client.post("/api/releves", headers=H, json={"date": dt, "periode": per, "pompe_id": p, "prix_gallon": 500 if p == g else 600,
                                                         "metter_avant": av, "metter_apres": ap, "motif_ecart_compteur": "Début du test classement"})
        assert r.status_code in (200, 201), r.text
        if qui:
            assert client.patch(f"/api/releves/{r.json()['id']}/pompiste", headers=H, json={"pompiste_id": qui}).status_code == 200
    rel(d1, "Matin", g, 1000, 1100, a)        # Anne : 100 gal gazoline = 50 000
    rel(d1, "Matin", d, 2000, 2010, a)        # Anne : 10 gal diesel = 6 000
    rel(d1, "Apres-midi", g, 1100, 1300, b)   # Bob : 200 gal gazoline = 100 000
    rel(d2, "Matin", g, 1300, 1310, None)     # sans pompiste
    r = client.post("/api/depenses", headers=H, json={"categorie": "Divers", "description": "Glace", "montant": 2500,
                                                       "date_depense": d1, "pompiste_id": a})
    assert r.status_code == 201, r.text
    client.post("/api/depenses", headers=H, json={"categorie": "Divers", "description": "Station", "montant": 999, "date_depense": d1})
    c = client.get(f"/api/pompistes/classement?date_debut={d1}&date_fin={d2}", headers=H).json()
    P = {x["pompiste"]: x for x in c["pompistes"]}
    A, B = P[f"Anne C{ns}"], P[f"Bob C{ns}"]
    assert (B["rang"], A["rang"]) == (1, 2) and B["montant"] == 100000 and A["montant"] == 56000
    assert A["par_carburant"]["Gazoline"]["gallons"] == 100 and A["par_carburant"]["Diesel"]["gallons"] == 10
    assert A["depenses"] == 2500 and A["net"] == 53500 and A["liste_depenses"][0]["description"] == "Glace"
    assert c["pompistes"][-1]["pompiste"] == "Non attribué" and c["pompistes"][-1]["rang"] is None
    assert client.get(f"/api/pompistes/classement?date_debut={d2}&date_fin={d1}", headers=H).status_code == 400
    # La dépense garde son pompiste dans la liste des dépenses ; on peut le retirer
    dep = next(x for x in client.get(f"/api/depenses?date_debut={d1}&date_fin={d1}", headers=H).json()["depenses"] if x["description"] == "Glace")
    assert dep["pompiste_nom"] == f"Anne C{ns}"
    assert client.put(f"/api/depenses/{dep['id']}", headers=H, json={"sans_pompiste": True}).status_code == 200
    c = client.get(f"/api/pompistes/classement?date_debut={d1}&date_fin={d2}", headers=H).json()
    assert next(x for x in c["pompistes"] if x["pompiste"] == f"Anne C{ns}")["depenses"] == 0
