"""
Journal Meters : affichage jour par jour, et écart de compteur marqué
« résolu » par la direction avec une justification (≥ 20 caractères) —
il sort alors des problèmes du journal.
"""
import time

H = {"X-API-Key": "smoke-test-key"}


def _pompes(client, n=2):
    nom = f"Res-{time.time_ns()}"
    pid = client.post("/api/produits", headers=H, json={"nom": nom, "prix_gallon": 100}).json()["id"]
    return [client.post(f"/api/produits/{pid}/pompes", headers=H, json={"nom": f"{nom} P{i + 1}"}).json()["id"]
            for i in range(n)]


def _releve(client, jour, periode, pompe, av, ap):
    r = client.post("/api/releves", headers=H, json={"date": jour, "periode": periode, "pompe_id": pompe,
                                                      "prix_gallon": 100, "metter_avant": av, "metter_apres": ap,
                                                      "motif_ecart_compteur": "Test de saut"})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _journal(client, debut, fin, pompes):
    q = "".join(f"&pompe_id={p}" for p in pompes)
    return client.get(f"/api/journal?date_debut={debut}&date_fin={fin}{q}", headers=H).json()


def test_journal_jour_par_jour(client):
    p1, p2 = _pompes(client)
    for jour, base in (("2026-03-10", 0), ("2026-03-11", 100)):
        _releve(client, jour, "Matin", p1, base, base + 50)
        _releve(client, jour, "Apres-midi", p1, base + 50, base + 100)
        _releve(client, jour, "Matin", p2, base, base + 50)
        _releve(client, jour, "Apres-midi", p2, base + 50, base + 100)
    e = _journal(client, "2026-03-10", "2026-03-11", [p1, p2])["entries"]
    assert [(x["date"], x["periode"], x["pompe_id"]) for x in e] == [
        ("2026-03-10", "Matin", p1), ("2026-03-10", "Matin", p2),
        ("2026-03-10", "Apres-midi", p1), ("2026-03-10", "Apres-midi", p2),
        ("2026-03-11", "Matin", p1), ("2026-03-11", "Matin", p2),
        ("2026-03-11", "Apres-midi", p1), ("2026-03-11", "Apres-midi", p2),
    ]
    assert all(x["statut"] == "ok" for x in e)      # la continuité reste analysée pompe par pompe


def test_ecart_resolu_avec_description(client):
    (p,) = _pompes(client, 1)
    _releve(client, "2026-04-01", "Matin", p, 0, 40)
    rid = _releve(client, "2026-04-01", "Apres-midi", p, 55, 80)     # saut de 15
    j = _journal(client, "2026-04-01", "2026-04-01", [p])
    e = next(x for x in j["entries"] if x["id"] == rid)
    assert e["statut"] == "alerte" and e["type_anomalie"] == "saut_compteur" and e["resolution"] is None
    assert j["resume"]["nb_alertes"] == 1 and j["resume"]["total_gallons"] == 40

    url = f"/api/journal/releves/{rid}/resoudre"
    r = client.post(url, headers=H, json={"description": "Compteur déréglé"})
    assert r.status_code == 400 and "20 caractères" in r.json()["detail"]
    r = client.post(url, headers=H, json={"description": "   court    mais    espacé   "})
    assert r.status_code == 400                        # les espaces ne comptent pas
    desc = "Compteur déréglé, réglé par le technicien ; écart confirmé avec le pompiste."
    assert client.post(url, headers=H, json={"description": desc}).status_code == 200

    j = _journal(client, "2026-04-01", "2026-04-01", [p])
    e = next(x for x in j["entries"] if x["id"] == rid)
    assert e["statut"] == "resolu" and e["resolution"]["description"] == desc
    assert j["resume"]["nb_alertes"] == 0 and j["resume"]["nb_resolus"] == 1
    assert j["resume"]["total_gallons"] == 65           # le saut justifié n'est plus exclu

    # Un relevé sans écart ne se « résout » pas
    ok_id = next(x["id"] for x in j["entries"] if x["id"] != rid)
    assert client.post(f"/api/journal/releves/{ok_id}/resoudre", headers=H, json={"description": desc}).status_code == 400

    # Rouvrir : l'alerte revient
    assert client.delete(url, headers=H).status_code == 200
    assert next(x for x in _journal(client, "2026-04-01", "2026-04-01", [p])["entries"] if x["id"] == rid)["statut"] == "alerte"


def test_resolution_caduque_si_les_valeurs_changent(client):
    (p,) = _pompes(client, 1)
    _releve(client, "2026-04-05", "Matin", p, 0, 40)
    rid = _releve(client, "2026-04-05", "Apres-midi", p, 50, 90)
    assert client.post(f"/api/journal/releves/{rid}/resoudre", headers=H,
                       json={"description": "Écart de 10 gallons vérifié avec le pompiste."}).status_code == 200
    # Le relevé du matin est corrigé : l'écart n'est plus le même → l'anomalie réapparaît
    r = client.post("/api/releves", headers=H, json={"date": "2026-04-05", "periode": "Matin", "pompe_id": p,
                                                      "prix_gallon": 100, "metter_avant": 0, "metter_apres": 45})
    assert r.status_code in (200, 201), r.text
    e = next(x for x in _journal(client, "2026-04-05", "2026-04-05", [p])["entries"] if x["id"] == rid)
    assert e["statut"] == "alerte" and e["decalage"] == 5


def test_ecart_apres_un_rapport_pompiste_en_attente(client):
    """Cas du staging : le relevé précédent est un rapport pompiste pas
    encore finalisé. L'écart affiché au journal doit pouvoir être résolu, et
    rester résolu quelles que soient les dates choisies."""
    from datetime import timedelta
    from fastapi.testclient import TestClient
    from tz_utils import today_haiti
    import main as _m
    (p,) = _pompes(client, 1)
    j1, j2 = str(today_haiti() - timedelta(days=2)), str(today_haiti() - timedelta(days=1))
    c = client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": "Att", "nom": "Ente"}).json()
    pp = TestClient(_m.app)
    pp.post("/api/login/pompiste", json={"username": c["username"], "code_acces": c["code_acces"]})
    r = pp.post("/api/pompiste/compteurs", json={"date": j1, "periode": "Matin", "charte_acceptee": True,
                "lignes": [{"pompe_id": p, "metter_avant": 0, "metter_apres": 40, "debut_confirme": True}]})
    assert r.status_code == 201, r.text
    pp.post("/api/pompiste/terminer", json={"billetterie": [{"coupure": 1000, "billets": 4}]})
    rid = _releve(client, j2, "Matin", p, 50, 90)                      # saut de 10 après le rapport en attente

    for debut in (j1, j2):                                             # même écart, quelle que soit la période
        e = next(x for x in _journal(client, debut, j2, [p])["entries"] if x["id"] == rid)
        assert e["statut"] == "alerte" and e["decalage"] == 10, (debut, e)
    r = client.post(f"/api/journal/releves/{rid}/resoudre", headers=H,
                    json={"description": "Index du matin relu avec le pompiste, écart accepté."})
    assert r.status_code == 200, r.text
    for debut in (j1, j2):
        assert next(x for x in _journal(client, debut, j2, [p])["entries"] if x["id"] == rid)["statut"] == "resolu"
