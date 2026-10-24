"""
Espace pompiste : connexion identifiant + PIN, accès limité, contrôle des
index, rapprochement caisse, confirmation / correction / refus par le
superviseur, écriture dans le journal des compteurs.
"""
import time

import pytest
from fastapi.testclient import TestClient

H = {"X-API-Key": "smoke-test-key"}


@pytest.fixture(scope="module")
def env(client):
    from tz_utils import today_haiti
    nom = f"Pomp-{time.time_ns()}"
    r = client.post("/api/produits", headers=H, json={"nom": nom, "prix_gallon": 500})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    r = client.post(f"/api/produits/{pid}/pompes", headers=H, json={"nom": f"{nom} P1"})
    pompe = r.json()["id"]
    user = f"pomp{time.time_ns() % 10**8}"
    r = client.post("/api/pompiste-supervision/comptes", headers=H,
                    json={"nom_complet": "Jean Pompiste", "username": user})
    assert r.status_code == 201, r.text
    compte = r.json()
    assert len(compte["code_acces"]) == 9
    import main
    p = TestClient(main.app)     # client séparé : son cookie ne doit pas toucher les autres tests
    return {"pompe": pompe, "user": user, "pin": compte["code_acces"], "uid": compte["id"],
            "p": p, "jour": str(today_haiti())}


def _rapport(env, avant, apres, periode="Matin", billets=50, charte=True):
    return env["p"].post("/api/pompiste/rapports", json={
        "date": env["jour"], "periode": periode, "charte_acceptee": charte,
        "lignes": [{"pompe_id": env["pompe"], "metter_avant": avant, "metter_apres": apres}],
        "billetterie": [{"coupure": 1000, "liasses": 0, "billets": billets}], "monnaie": 0})


def test_connexion_et_acces_limite(client, env):
    p = env["p"]
    assert p.post("/api/login/pompiste", json={"username": env["user"], "code_acces": "000000000"}).status_code == 401
    r = p.post("/api/login/pompiste", json={"username": env["user"].upper(), "code_acces": env["pin"]})
    assert r.status_code == 200, r.text
    assert p.get("/api/produits").status_code == 403
    assert p.get("/api/admin/users").status_code == 403
    r = p.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/pompiste"
    assert p.get("/pompiste").status_code == 200
    # Un compte non pompiste n'a pas accès à l'espace pompiste
    assert client.get("/api/pompiste/contexte", headers=H).status_code == 403
    # Le login normal (email + mot de passe) ne connaît pas les pompistes
    assert client.post("/api/login/pompiste", json={"username": "admin", "code_acces": "123456789"}).status_code == 401


def test_contexte(env):
    r = env["p"].get(f"/api/pompiste/contexte?date={env['jour']}&periode=Matin")
    assert r.status_code == 200, r.text
    pompes = {q["id"]: q for pr in r.json()["produits"] for q in pr["pompes"]}
    assert pompes[env["pompe"]]["index_precedent"] is None
    assert 1000 in r.json()["coupures"]


def test_soumission_controles(env):
    assert _rapport(env, 1000, 1100, charte=False).status_code == 400
    r = _rapport(env, 1000, 900)
    assert r.status_code == 422
    assert r.json()["detail"]["erreurs"][0]["pompe_id"] == env["pompe"]
    assert _rapport(env, 1000, 1000 + 9000).status_code == 422     # zéro en trop


def test_soumission_et_rapprochement(env):
    r = _rapport(env, 1000, 1100, billets=49)
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["statut"] == "SOUMIS"
    assert d["montant_attendu"] == 50000 and d["montant_compte"] == 49000 and d["ecart"] == -1000
    env["r1"] = d["id"]
    # Même pompe, même créneau : refusé
    assert _rapport(env, 1000, 1100).status_code == 409
    # Le créneau suivant enchaîne sur le rapport en attente
    r = env["p"].get(f"/api/pompiste/contexte?date={env['jour']}&periode=Apres-midi")
    pompes = {q["id"]: q for pr in r.json()["produits"] for q in pr["pompes"]}
    assert pompes[env["pompe"]]["index_precedent"] == 1100


def test_confirmation_avec_correction(client, env):
    r = client.get("/api/pompiste-supervision/rapports?statut=SOUMIS", headers=H)
    assert any(x["id"] == env["r1"] for x in r.json())
    corr = {"lignes": [{"pompe_id": env["pompe"], "metter_avant": 1000, "metter_apres": 1098}]}
    r = client.post(f"/api/pompiste-supervision/rapports/{env['r1']}/confirmer", headers=H, json=corr)
    assert r.status_code == 400                       # correction sans commentaire
    corr["commentaire"] = "Index relu sur la pompe : 1098"
    r = client.post(f"/api/pompiste-supervision/rapports/{env['r1']}/confirmer", headers=H, json=corr)
    assert r.status_code == 200, r.text
    assert r.json()["statut"] == "CONFIRME"
    rel = client.get(f"/api/releves?date={env['jour']}&periode=Matin", headers=H).json()
    mine = [x for x in rel if x["pompe_id"] == env["pompe"]]
    assert len(mine) == 1 and float(mine[0]["metter_apres"]) == 1098
    # Déjà traité
    r = client.post(f"/api/pompiste-supervision/rapports/{env['r1']}/confirmer", headers=H, json={})
    assert r.status_code == 409


def test_refus_avec_commentaire(client, env):
    # Le début de l'après-midi doit suivre le journal corrigé (1098)
    assert _rapport(env, 1100, 1200, periode="Apres-midi").status_code == 422
    r = _rapport(env, 1098, 1200, periode="Apres-midi", billets=51)
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    assert client.post(f"/api/pompiste-supervision/rapports/{rid}/rejeter", headers=H,
                       json={"commentaire": "non"}).status_code == 400
    r = client.post(f"/api/pompiste-supervision/rapports/{rid}/rejeter", headers=H,
                    json={"commentaire": "Il manque l'argent du client crédit, recomptez."})
    assert r.status_code == 200 and r.json()["statut"] == "REJETE"
    mes = env["p"].get("/api/pompiste/rapports").json()
    assert mes[0]["id"] == rid and "recomptez" in mes[0]["commentaire_superviseur"]
    # Après un refus, le créneau est de nouveau libre
    assert _rapport(env, 1098, 1200, periode="Apres-midi", billets=51).status_code == 201


def test_desactivation_coupe_la_session(client, env):
    r = client.patch(f"/api/pompiste-supervision/comptes/{env['uid']}", headers=H, json={"actif": False})
    assert r.status_code == 200
    assert env["p"].get("/api/pompiste/contexte").status_code == 401
    r = client.patch(f"/api/pompiste-supervision/comptes/{env['uid']}", headers=H,
                     json={"actif": True, "nouveau_pin": True})
    assert r.status_code == 200 and len(r.json()["code_acces"]) == 9
