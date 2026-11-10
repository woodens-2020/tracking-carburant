"""
Caisse principale : sources configurables (Zo Vincent, Champin, Autres…),
entrées (source, montant, date), totaux par source / jour / période,
annulation avec motif, accès réservé à la direction.
"""
import time
from datetime import timedelta

H = {"X-API-Key": "smoke-test-key"}
P = "/api/caisse-principale"


def test_sources_par_defaut_et_configuration(client):
    r = client.get(f"{P}/sources", headers=H)
    assert r.status_code == 200, r.text
    noms = [s["nom"] for s in r.json()]
    assert noms[:3] == ["Zo Vincent", "Champin", "Autres"]
    nouv = f"Station Test {time.time_ns() % 10**6}"
    r = client.post(f"{P}/sources", headers=H, json={"nom": nouv})
    assert r.status_code == 201
    sid = r.json()["id"]
    assert client.post(f"{P}/sources", headers=H, json={"nom": nouv.upper()}).status_code == 409   # doublon
    assert client.post(f"{P}/sources", headers=H, json={"nom": "x"}).status_code == 400
    r = client.patch(f"{P}/sources/{sid}", headers=H, json={"actif": False})
    assert r.status_code == 200 and r.json()["actif"] is False
    actives = [s["id"] for s in client.get(f"{P}/sources?inclure_inactives=false", headers=H).json()]
    assert sid not in actives
    from tz_utils import today_haiti
    r = client.post(f"{P}/entrees", headers=H, json={"source_id": sid, "montant": 10, "date_entree": str(today_haiti())})
    assert r.status_code == 409                       # source désactivée
    r = client.patch(f"{P}/sources/{sid}", headers=H, json={"actif": True, "nom": nouv + " B"})
    assert r.status_code == 200 and r.json()["nom"] == nouv + " B"


def test_entrees_totaux_et_annulation(client):
    from tz_utils import today_haiti
    auj = today_haiti()
    src = {s["nom"]: s["id"] for s in client.get(f"{P}/sources", headers=H).json()}
    j = str(auj - timedelta(days=400))            # période isolée des autres tests
    for sid, mt in ((src["Zo Vincent"], 506175), (src["Zo Vincent"], 12000.5), (src["Champin"], 230000)):
        r = client.post(f"{P}/entrees", headers=H, json={"source_id": sid, "montant": mt, "date_entree": j,
                                                       "reference": "Dépôt", "note": "test"})
        assert r.status_code == 201, r.text
    assert client.post(f"{P}/entrees", headers=H, json={"source_id": src["Champin"], "montant": 0,
                                                      "date_entree": j}).status_code == 400
    assert client.post(f"{P}/entrees", headers=H, json={"source_id": src["Champin"], "montant": 5,
                                                      "date_entree": str(auj + timedelta(days=1))}).status_code == 400
    d = client.get(f"{P}/entrees?date_debut={j}&date_fin={j}", headers=H).json()
    assert d["total"] == 748175.5 and d["nb"] == 3
    ps = {x["source_nom"]: x for x in d["par_source"]}
    assert ps["Zo Vincent"]["montant"] == 518175.5 and ps["Zo Vincent"]["nb"] == 2
    assert ps["Champin"]["montant"] == 230000
    assert d["par_jour"] == [{"date": j, "montant": 748175.5, "nb": 3}]
    # Filtre par source
    d2 = client.get(f"{P}/entrees?date_debut={j}&date_fin={j}&source_id={src['Champin']}", headers=H).json()
    assert d2["total"] == 230000 and len(d2["entrees"]) == 1
    # Annulation : sort des totaux, reste visible avec inclure_annulees
    eid = d2["entrees"][0]["id"]
    assert client.post(f"{P}/entrees/{eid}/annuler", headers=H, json={"motif": "non"}).status_code == 400
    r = client.post(f"{P}/entrees/{eid}/annuler", headers=H, json={"motif": "Saisie en double"})
    assert r.status_code == 200 and r.json()["annulee"]
    assert client.post(f"{P}/entrees/{eid}/annuler", headers=H, json={"motif": "Saisie en double"}).status_code == 409
    d = client.get(f"{P}/entrees?date_debut={j}&date_fin={j}", headers=H).json()
    assert d["total"] == 518175.5 and d["nb_annulees"] == 1 and all(not e["annulee"] for e in d["entrees"])
    d = client.get(f"{P}/entrees?date_debut={j}&date_fin={j}&inclure_annulees=true", headers=H).json()
    assert any(e["annulee"] and e["motif_annulation"] == "Saisie en double" for e in d["entrees"])


def test_acces_reserve_a_la_direction(client):
    import main
    from fastapi.testclient import TestClient
    c = client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": "Sans", "nom": "Droit"}).json()
    p = TestClient(main.app)
    p.post("/api/login/pompiste", json={"username": c["username"], "code_acces": c["code_acces"]})
    assert p.get(f"{P}/entrees").status_code == 403      # un pompiste n'y a pas accès
