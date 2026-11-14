"""Rapport journalier : « + » Autres entrées — argent en plus des ventes
(montant, description, note), compté dans le disponible du jour."""
H = {"X-API-Key": "smoke-test-key"}
JOUR = "2026-06-25"


def _rj(client, jour=JOUR, activite=None):
    q = f"/api/carburant/rapport-jour?date={jour}" + (f"&activite={activite}" if activite else "")
    return client.get(q, headers=H).json()


def test_ajouter_modifier_retirer(client):
    avant = _rj(client)
    assert avant["autres_entrees"] == {"liste": [], "total": 0}
    r = client.post("/api/carburant/autres-entrees", headers=H,
                    json={"date": JOUR, "montant": 1500.5, "description": "  Vente  de bidons ", "note": "payé cash"})
    assert r.status_code == 201, r.text
    e = r.json()
    assert e["description"] == "Vente de bidons" and e["note"] == "payé cash" and e["montant"] == 1500.5
    d = _rj(client)
    assert [x["id"] for x in d["autres_entrees"]["liste"]] == [e["id"]] and d["autres_entrees"]["total"] == 1500.5
    assert round(d["disponible"] - avant["disponible"], 2) == 1500.5

    r = client.put(f"/api/carburant/autres-entrees/{e['id']}", headers=H, json={"montant": 2000, "note": ""})
    assert r.status_code == 200 and r.json()["montant"] == 2000 and r.json()["note"] == ""
    assert _rj(client)["autres_entrees"]["total"] == 2000

    assert client.delete(f"/api/carburant/autres-entrees/{e['id']}", headers=H).status_code == 200
    assert _rj(client)["autres_entrees"]["total"] == 0


def test_refus(client):
    for corps in ({"date": JOUR, "montant": 0, "description": "x zéro"},
                  {"date": JOUR, "montant": -5, "description": "négatif"},
                  {"date": JOUR, "montant": 10, "description": " "}):
        assert client.post("/api/carburant/autres-entrees", headers=H, json=corps).status_code == 400
    assert client.delete("/api/carburant/autres-entrees/999999", headers=H).status_code == 404


def test_propane_separe(client):
    jour = "2026-06-26"
    client.post("/api/carburant/autres-entrees", headers=H, json={"date": jour, "montant": 100, "description": "carburant"})
    client.post("/api/carburant/autres-entrees", headers=H, json={"date": jour, "montant": 40, "description": "propane", "activite": "propane"})
    assert _rj(client, jour, "carburant")["autres_entrees"]["total"] == 100
    assert _rj(client, jour, "propane")["autres_entrees"]["total"] == 40
    assert _rj(client, jour)["autres_entrees"]["total"] == 140
