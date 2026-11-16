"""Fiches papier de la direction prioritaires : un relevé saisi d'après la fiche
(fiche_papier) n'est jamais remplacé par un rapport pompiste, même finalisé ; le
journal reste stable et indique la source « fiche papier »."""
import time

from fastapi.testclient import TestClient

H = {"X-API-Key": "smoke-test-key"}


def _pompe(client):
    nom = f"Fiche-{time.time_ns()}"
    pid = client.post("/api/produits", headers=H, json={"nom": nom, "prix_gallon": 500}).json()["id"]
    return client.post(f"/api/produits/{pid}/pompes", headers=H, json={"nom": f"{nom} P1"}).json()["id"]


def _sup(client, rid, action, **body):
    return client.post(f"/api/pompiste-supervision/rapports/{rid}/{action}", headers=H, json=body)


def _journal(client, d, pompe):
    return client.get(f"/api/journal?date_debut={d}&date_fin={d}&pompe_id={pompe}", headers=H).json()["entries"]


def test_rapport_pompiste_ne_remplace_pas_la_fiche(client):
    import main as _m
    from tz_utils import today_haiti
    d = str(today_haiti())
    pompe = _pompe(client)
    # 1. La direction saisit la fiche papier
    r = client.post("/api/releves", headers=H, json={"date": d, "periode": "Matin", "pompe_id": pompe, "prix_gallon": 500,
                                                     "metter_avant": 0, "metter_apres": 10, "fiche_papier": True})
    assert r.status_code == 200, r.text
    e = _journal(client, d, pompe)
    assert len(e) == 1 and e[0]["fiche_papier"] and e[0]["source"] == "fiche_papier"

    # 2. Un pompiste envoie autre chose pour le même créneau
    c = client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": "Fi", "nom": f"Che{time.time_ns()}"}).json()
    p = TestClient(_m.app)
    p.post("/api/login/pompiste", json={"username": c["username"], "code_acces": c["code_acces"]})
    p.post("/api/pompiste/compteurs", json={"date": d, "periode": "Matin", "charte_acceptee": True, "lignes": [
        {"pompe_id": pompe, "metter_avant": 0, "metter_apres": 12, "debut_confirme": True}]})
    rid = p.post("/api/pompiste/terminer", json={"billetterie": [{"coupure": 1000, "billets": 6}]}).json()["id"]
    e = _journal(client, d, pompe)
    assert len(e) == 1 and float(e[0]["metter_apres"]) == 10 and not e[0]["en_attente"]   # pas de ligne « en attente » en plus

    # 3. Même finalisé, le rapport ne touche pas la fiche
    assert _sup(client, rid, f"lignes/{pompe}/confirmer", metter_avant=0, metter_apres=12).status_code == 200
    _sup(client, rid, "billetterie", confirme=True); _sup(client, rid, "ecart", confirme=True)
    r = _sup(client, rid, "finaliser", note=8, commentaire="Fiche papier prioritaire")
    assert r.status_code == 200, r.text
    e = _journal(client, d, pompe)
    assert len(e) == 1 and float(e[0]["metter_apres"]) == 10 and e[0]["fiche_papier"] and not e[0]["rapport_pompiste_id"]


def test_marquer_fiche_papier(client):
    from tz_utils import today_haiti
    d = str(today_haiti())
    pompe = _pompe(client)
    rid = client.post("/api/releves", headers=H, json={"date": d, "periode": "Apres-midi", "pompe_id": pompe, "prix_gallon": 500,
                                                       "metter_avant": 0, "metter_apres": 5}).json()["id"]
    assert not _journal(client, d, pompe)[0]["fiche_papier"]
    assert client.post("/api/releves/fiche-papier", headers=H, json={"ids": [rid]}).json()["marques"] == 1
    assert _journal(client, d, pompe)[0]["fiche_papier"]
    # Une correction faite par la direction reste possible et garde la marque
    r = client.post("/api/releves", headers=H, json={"date": d, "periode": "Apres-midi", "pompe_id": pompe, "prix_gallon": 500,
                                                     "metter_avant": 0, "metter_apres": 6})
    assert r.status_code == 200 and _journal(client, d, pompe)[0]["fiche_papier"]
    assert client.post("/api/releves/fiche-papier", headers=H, json={"ids": [999999]}).status_code == 404


def test_journal_fiches_seulement(client):
    """Journal Meeters (source=fiches) : seulement les fiches papier, jamais les rapports en attente."""
    import main as _m
    from tz_utils import today_haiti
    d = str(today_haiti())
    a, b = _pompe(client), _pompe(client)
    client.post("/api/releves", headers=H, json={"date": d, "periode": "Matin", "pompe_id": a, "prix_gallon": 500,
                                                 "metter_avant": 0, "metter_apres": 7, "fiche_papier": True})
    client.post("/api/releves", headers=H, json={"date": d, "periode": "Matin", "pompe_id": b, "prix_gallon": 500,
                                                 "metter_avant": 0, "metter_apres": 3})
    c = client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": "So", "nom": f"Urce{time.time_ns()}"}).json()
    p = TestClient(_m.app)
    p.post("/api/login/pompiste", json={"username": c["username"], "code_acces": c["code_acces"]})
    p.post("/api/pompiste/compteurs", json={"date": d, "periode": "Apres-midi", "charte_acceptee": True, "lignes": [
        {"pompe_id": a, "metter_avant": 7, "metter_apres": 9, "debut_confirme": True}]})
    p.post("/api/pompiste/terminer", json={"billetterie": [{"coupure": 1000, "billets": 1}]})
    q = f"/api/journal?date_debut={d}&date_fin={d}&pompe_id={a}&pompe_id={b}"
    tout = client.get(q, headers=H).json()["entries"]
    assert any(e["en_attente"] for e in tout)                                   # sans filtre : comme avant
    fiches = client.get(q + "&source=fiches", headers=H).json()["entries"]
    assert [(e["pompe_id"], e["fiche_papier"]) for e in fiches] == [(a, True)]
    saisies = client.get(q + "&source=saisies", headers=H).json()["entries"]
    assert sorted(e["pompe_id"] for e in saisies) == sorted([a, b]) and not any(e["en_attente"] for e in saisies)
    assert client.get(q + "&source=autre", headers=H).status_code == 400
