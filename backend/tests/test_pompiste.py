"""
Espace pompiste : connexion identifiant + PIN, accès limité, compteurs
envoyés carburant par carburant (rapport EN_COURS), « Terminer » avec la
billetterie de toute la période, confirmation / correction / refus par le
superviseur, écriture dans le journal des compteurs.
"""
import time

import pytest
from fastapi.testclient import TestClient

H = {"X-API-Key": "smoke-test-key"}


def _produit(client, prix):
    nom = f"Pomp-{time.time_ns()}"
    r = client.post("/api/produits", headers=H, json={"nom": nom, "prix_gallon": prix})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    r = client.post(f"/api/produits/{pid}/pompes", headers=H, json={"nom": f"{nom} P1"})
    return r.json()["id"]


@pytest.fixture(scope="module")
def env(client):
    from tz_utils import today_haiti
    pompe = _produit(client, 500)
    pompe_diesel = _produit(client, 600)
    user = f"pomp{time.time_ns() % 10**8}"
    r = client.post("/api/pompiste-supervision/comptes", headers=H,
                    json={"nom_complet": "Jean Pompiste", "username": user})
    assert r.status_code == 201, r.text
    compte = r.json()
    assert len(compte["code_acces"]) == 9
    import main
    p = TestClient(main.app)     # client séparé : son cookie ne doit pas toucher les autres tests
    return {"pompe": pompe, "diesel": pompe_diesel, "user": user, "pin": compte["code_acces"],
            "uid": compte["id"], "p": p, "jour": str(today_haiti())}


def _compteurs(env, pompe, avant, apres, periode="Matin", charte=True):
    return env["p"].post("/api/pompiste/compteurs", json={
        "date": env["jour"], "periode": periode, "charte_acceptee": charte,
        "lignes": [{"pompe_id": pompe, "metter_avant": avant, "metter_apres": apres}]})


def _terminer(env, billets):
    return env["p"].post("/api/pompiste/terminer", json={
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
    assert client.get("/api/pompiste/contexte", headers=H).status_code == 403
    assert client.post("/api/login/pompiste", json={"username": "admin", "code_acces": "123456789"}).status_code == 401


def test_contexte(env):
    r = env["p"].get(f"/api/pompiste/contexte?date={env['jour']}&periode=Matin")
    assert r.status_code == 200, r.text
    d = r.json()
    pompes = {q["id"]: q for pr in d["produits"] for q in pr["pompes"]}
    assert pompes[env["pompe"]]["index_precedent"] is None
    assert d["en_cours"] is None
    assert 1000 in d["coupures"]


def test_controles_des_index(env):
    assert _compteurs(env, env["pompe"], 1000, 1100, charte=False).status_code == 400
    r = _compteurs(env, env["pompe"], 1000, 900)
    assert r.status_code == 422
    assert r.json()["detail"]["erreurs"][0]["pompe_id"] == env["pompe"]
    assert _compteurs(env, env["pompe"], 1000, 1000 + 9000).status_code == 422     # zéro en trop
    assert env["p"].post("/api/pompiste/terminer", json={}).status_code == 409    # rien à terminer


def test_compteurs_puis_terminer(client, env):
    # Gazoline puis Diesel : un seul rapport en cours
    r = _compteurs(env, env["pompe"], 1000, 1100)
    assert r.status_code == 201, r.text
    assert r.json()["statut"] == "EN_COURS" and r.json()["montant_attendu"] == 50000
    r = _compteurs(env, env["diesel"], 0, 10)
    assert r.status_code == 201
    assert len(r.json()["lignes"]) == 2 and r.json()["montant_attendu"] == 56000
    # Même pompe deux fois : refusé
    assert _compteurs(env, env["pompe"], 1000, 1100).status_code == 409
    # Un autre créneau tant que celui-ci n'est pas terminé : refusé
    assert _compteurs(env, env["pompe"], 1100, 1200, periode="Apres-midi").status_code == 409
    # Le contexte reprend le rapport ouvert
    d = env["p"].get("/api/pompiste/contexte").json()
    assert d["en_cours"]["id"] == r.json()["id"] and d["periode"] == "Matin"
    # Retirer une pompe saisie par erreur
    r = env["p"].delete(f"/api/pompiste/compteurs/{env['diesel']}")
    assert r.status_code == 200 and r.json()["en_cours"]["montant_attendu"] == 50000
    # Pas visible du superviseur tant que ce n'est pas terminé
    lst = client.get("/api/pompiste-supervision/rapports?statut=SOUMIS", headers=H).json()
    assert all(x["pompiste_nom"] != "Jean Pompiste" for x in lst)
    # Terminer : billetterie et rapprochement
    r = _terminer(env, 49)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["statut"] == "SOUMIS" and d["montant_compte"] == 49000 and d["ecart"] == -1000
    env["r1"] = d["id"]
    # Le créneau suivant enchaîne sur ce rapport
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
    r = client.post(f"/api/pompiste-supervision/rapports/{env['r1']}/confirmer", headers=H, json={})
    assert r.status_code == 409


def test_refus_avec_commentaire(client, env):
    # Le début de l'après-midi doit suivre le journal corrigé (1098)
    assert _compteurs(env, env["pompe"], 1100, 1200, periode="Apres-midi").status_code == 422
    assert _compteurs(env, env["pompe"], 1098, 1200, periode="Apres-midi").status_code == 201
    r = _terminer(env, 51)
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    assert client.post(f"/api/pompiste-supervision/rapports/{rid}/rejeter", headers=H,
                       json={"commentaire": "non"}).status_code == 400
    r = client.post(f"/api/pompiste-supervision/rapports/{rid}/rejeter", headers=H,
                    json={"commentaire": "Il manque l'argent du client crédit, recomptez."})
    assert r.status_code == 200 and r.json()["statut"] == "REJETE"
    mes = env["p"].get("/api/pompiste/rapports").json()
    assert mes[0]["id"] == rid and "recomptez" in mes[0]["commentaire_superviseur"]
    # Après un refus, le créneau est de nouveau libre
    assert _compteurs(env, env["pompe"], 1098, 1200, periode="Apres-midi").status_code == 201
    assert _terminer(env, 51).status_code == 200


def test_desactivation_coupe_la_session(client, env):
    r = client.patch(f"/api/pompiste-supervision/comptes/{env['uid']}", headers=H, json={"actif": False})
    assert r.status_code == 200
    assert env["p"].get("/api/pompiste/contexte").status_code == 401
    r = client.patch(f"/api/pompiste-supervision/comptes/{env['uid']}", headers=H,
                     json={"actif": True, "nouveau_pin": True})
    assert r.status_code == 200 and len(r.json()["code_acces"]) == 9
