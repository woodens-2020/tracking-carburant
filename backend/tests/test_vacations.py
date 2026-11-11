"""
Deux vacations par jour (Matin / Après-midi) : le pompiste choisit sa
vacation ; la liste des rapports, le Rapport journalier et les Ventes par
pompiste se filtrent par vacation.
"""
import time
from datetime import timedelta

from fastapi.testclient import TestClient

H = {"X-API-Key": "smoke-test-key"}


def _pompiste(client, prenom):
    import main
    c = client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": prenom, "nom": "Vacation"}).json()
    p = TestClient(main.app)
    assert p.post("/api/login/pompiste", json={"username": c["username"], "code_acces": c["code_acces"]}).status_code == 200
    return p


def test_deux_vacations_filtrees_partout(client):
    from tz_utils import today_haiti
    jour = str(today_haiti() - timedelta(days=3))
    nom = f"Vac-{time.time_ns()}"
    pid = client.post("/api/produits", headers=H, json={"nom": nom, "prix_gallon": 500}).json()["id"]
    pompe = client.post(f"/api/produits/{pid}/pompes", headers=H, json={"nom": f"{nom} 1"}).json()["id"]

    matin, soir = _pompiste(client, "Matinal"), _pompiste(client, "Soiree")
    for p, per, av, ap, billets in ((matin, "Matin", 100, 110, 5), (soir, "Apres-midi", 110, 130, 10)):
        r = p.post("/api/pompiste/compteurs", json={"date": jour, "periode": per, "charte_acceptee": True,
                   "lignes": [{"pompe_id": pompe, "metter_avant": av, "metter_apres": ap, "debut_confirme": True}]})
        assert r.status_code == 201 and r.json()["periode"] == per, r.text
        assert p.post("/api/pompiste/terminer", json={"billetterie": [{"coupure": 1000, "liasses": 0, "billets": billets}],
                      "monnaie": 0}).status_code == 200

    # Rapports pompistes : filtre par vacation
    def rapports(per=""):
        u = f"/api/pompiste-supervision/rapports?date_debut={jour}&date_fin={jour}" + (f"&periode={per}" if per else "")
        return [x for x in client.get(u, headers=H).json()
                if any(l["pompe_id"] == pompe for l in x["lignes"])]
    assert len(rapports()) == 2
    assert [x["periode"] for x in rapports("Matin")] == ["Matin"]
    assert [x["periode"] for x in rapports("Apres-midi")] == ["Apres-midi"]
    assert client.get("/api/pompiste-supervision/rapports?periode=Soir", headers=H).status_code == 400

    # Finaliser les deux : leurs relevés entrent au journal
    sup = lambda rid, a, **b: client.post(f"/api/pompiste-supervision/rapports/{rid}/{a}", headers=H, json=b)
    for x in sorted(rapports(), key=lambda x: x["periode"] != "Matin"):          # le Matin d'abord
        for l in x["lignes"]:
            assert sup(x["id"], f"lignes/{l['pompe_id']}/confirmer", metter_avant=l["metter_avant"], metter_apres=l["metter_apres"]).status_code == 200
        assert sup(x["id"], "billetterie", confirme=True).status_code == 200
        assert sup(x["id"], "ecart", confirme=True).status_code == 200
        r = sup(x["id"], "finaliser", note=8, commentaire="ok")
        assert r.status_code == 200, r.text

    # Journal Meeters : filtre par vacation
    def journal(per=""):
        j = client.get(f"/api/journal?date_debut={jour}&date_fin={jour}&pompe_id={pompe}" + (f"&periode={per}" if per else ""),
                       headers=H).json()
        return sorted(e["quantite"] for e in j["entries"])
    assert journal() == [10, 20] and journal("Matin") == [10] and journal("Apres-midi") == [20]

    # Rapport journalier : une vacation = ses seules ventes
    def ventes(per=""):
        d = client.get(f"/api/carburant/rapport-jour?date={jour}" + (f"&periode={per}" if per else ""), headers=H).json()
        return round(sum(x["gallons"] for x in d["entrees"]["par_produit"] if x["produit"] == nom), 3)
    assert ventes() == 30 and ventes("Matin") == 10 and ventes("Apres-midi") == 20

    # Ventes par pompiste : une vacation
    def classement(per=""):
        d = client.get(f"/api/pompistes/classement?date_debut={jour}&date_fin={jour}" + (f"&periode={per}" if per else ""),
                       headers=H).json()
        return {x["pompiste"]: round(sum(v["gallons"] for v in x["par_carburant"].values()), 3)
                for x in d["pompistes"] if "Vacation" in (x["pompiste"] or "")}
    tout, m, s = classement(), classement("Matin"), classement("Apres-midi")
    assert sorted(tout.values()) == [10, 20]
    assert list(m.values()) == [10] and list(s.values()) == [20]
