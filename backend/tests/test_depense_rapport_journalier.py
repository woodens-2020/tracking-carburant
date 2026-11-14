"""Rapport journalier : toutes les dépenses du jour sont sur la feuille ; la
direction choisit avec « + » celles qui entrent dans le rapport. Une dépense
pas dans le rapport reste dans les dépenses, sans compter dans les totaux.
Sans le champ, l'API garde l'ancien comportement (dans le rapport)."""
H = {"X-API-Key": "smoke-test-key"}
JOUR = "2026-06-21"


def _creer(client, montant, dans=True):
    r = client.post("/api/depenses", headers=H, json={
        "categorie": "Electricite", "description": f"test rj {montant}", "montant": montant,
        "date_depense": JOUR, "dans_rapport_journalier": dans})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_depense_hors_rapport_journalier(client):
    a = _creer(client, 300)
    b = _creer(client, 700, dans=False)
    rj = client.get(f"/api/carburant/rapport-jour?date={JOUR}", headers=H).json()
    ids = [x["id"] for x in rj["depenses"]["liste"]]
    hors = [x["id"] for x in rj["depenses"]["hors_rapport"]]
    assert a in ids and b not in ids and b in hors
    assert rj["depenses"]["total"] == 300
    assert rj["depenses"]["total_hors_rapport"] == 700

    # Toujours dans la liste des dépenses, marquée hors rapport
    liste = client.get(f"/api/depenses?date_debut={JOUR}&date_fin={JOUR}", headers=H).json()["depenses"]
    assert next(x for x in liste if x["id"] == b)["dans_rapport_journalier"] is False
    assert next(x for x in liste if x["id"] == a)["dans_rapport_journalier"] is True

    # Remise dans le rapport
    r = client.put(f"/api/depenses/{b}", headers=H, json={"dans_rapport_journalier": True})
    assert r.status_code == 200 and r.json()["dans_rapport_journalier"] is True
    rj = client.get(f"/api/carburant/rapport-jour?date={JOUR}", headers=H).json()
    assert rj["depenses"]["total"] == 1000 and not rj["depenses"]["hors_rapport"]


def test_sans_le_champ_la_depense_est_dans_le_rapport(client):
    r = client.post("/api/depenses", headers=H, json={
        "categorie": "Electricite", "description": "ancien client", "montant": 50, "date_depense": "2026-06-22"})
    rj = client.get("/api/carburant/rapport-jour?date=2026-06-22", headers=H).json()
    assert r.json()["id"] in [x["id"] for x in rj["depenses"]["liste"]]
