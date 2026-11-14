"""Rapport journalier : toutes les dépenses du jour sont sur la feuille ; rien
n'entre dans le rapport par défaut — la direction ajoute avec « + » celles
qui y vont. Une dépense pas dans le rapport reste dans les dépenses, sans
compter dans les totaux du jour."""
H = {"X-API-Key": "smoke-test-key"}
JOUR = "2026-06-21"


def _creer(client, montant, jour=JOUR, **extra):
    r = client.post("/api/depenses", headers=H, json={
        "categorie": "Electricite", "description": f"test rj {montant}", "montant": montant,
        "date_depense": jour, **extra})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _rj(client, jour=JOUR):
    return client.get(f"/api/carburant/rapport-jour?date={jour}", headers=H).json()["depenses"]


def test_rien_dans_le_rapport_par_defaut(client):
    a = _creer(client, 300)
    b = _creer(client, 700)
    d = _rj(client)
    assert not d["liste"] and d["total"] == 0
    assert {a, b} <= {x["id"] for x in d["hors_rapport"]} and d["total_hors_rapport"] == 1000
    liste = client.get(f"/api/depenses?date_debut={JOUR}&date_fin={JOUR}", headers=H).json()["depenses"]
    assert all(x["dans_rapport_journalier"] is False for x in liste)


def test_plus_ajoute_et_retire(client):
    jour = "2026-06-23"
    a = _creer(client, 300, jour)
    b = _creer(client, 700, jour)
    r = client.put(f"/api/depenses/{a}", headers=H, json={"dans_rapport_journalier": True})
    assert r.status_code == 200 and r.json()["dans_rapport_journalier"] is True
    d = _rj(client, jour)
    assert [x["id"] for x in d["liste"]] == [a] and d["total"] == 300
    assert [x["id"] for x in d["hors_rapport"]] == [b]
    client.put(f"/api/depenses/{a}", headers=H, json={"dans_rapport_journalier": False})
    assert _rj(client, jour)["total"] == 0
