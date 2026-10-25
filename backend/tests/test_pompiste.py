"""
Espace pompiste : connexion code + PIN (sans email), accès limité, compteurs
envoyés carburant par carburant (rapport EN_COURS), modification depuis le
résumé, « Terminer » avec la billetterie, email aux administrateurs,
vérification par le superviseur (chaque mètre, billetterie, écart), note,
problème → résolu, écriture dans le journal des compteurs.
"""
import re
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
    r = client.post("/api/pompiste-supervision/comptes", headers=H,
                    json={"prenom": "Jean-Éric", "nom": "Pierre"})
    assert r.status_code == 201, r.text
    compte = r.json()
    assert re.fullmatch(r"jeanericpierre\d{4}", compte["username"]), compte["username"]
    assert len(compte["code_acces"]) == 9 and compte["nom_complet"] == "Jean-Éric Pierre"
    import main
    p = TestClient(main.app)     # client séparé : son cookie ne doit pas toucher les autres tests
    return {"pompe": pompe, "diesel": pompe_diesel, "user": compte["username"], "pin": compte["code_acces"],
            "uid": compte["id"], "p": p, "jour": str(today_haiti())}


def _compteurs(env, pompe, avant, apres, periode="Matin", charte=True, confirme=False):
    return env["p"].post("/api/pompiste/compteurs", json={
        "date": env["jour"], "periode": periode, "charte_acceptee": charte,
        "lignes": [{"pompe_id": pompe, "metter_avant": avant, "metter_apres": apres,
                    "debut_confirme": confirme}]})


def _terminer(env, billets):
    return env["p"].post("/api/pompiste/terminer", json={
        "billetterie": [{"coupure": 1000, "liasses": 0, "billets": billets}], "monnaie": 0})


def _sup(client, rid, action, **body):
    return client.post(f"/api/pompiste-supervision/rapports/{rid}/{action}", headers=H, json=body)


def test_codes_uniques(client):
    a = client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": "Marie", "nom": "Joseph"}).json()
    b = client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": "Marie", "nom": "Joseph"}).json()
    assert a["username"] != b["username"] and a["username"].startswith("mariejoseph")
    assert client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": "", "nom": "X"}).status_code == 400


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


def test_contexte_sans_index_et_nouvelle_pompe(client, env):
    """Le dernier index n'est jamais montré au pompiste ; une pompe ajoutée
    dans « Produits & Pompes » apparaît tout de suite dans son espace."""
    d = env["p"].get(f"/api/pompiste/contexte?date={env['jour']}&periode=Matin").json()
    pompes = [q for pr in d["produits"] for q in pr["pompes"]]
    assert pompes and all("index_precedent" not in q for q in pompes)
    pid = next(pr["id"] for pr in d["produits"] if any(q["id"] == env["pompe"] for q in pr["pompes"]))
    r = client.post(f"/api/produits/{pid}/pompes", headers=H, json={"nom": f"Nouvelle {time.time_ns()}"})
    nouvelle = r.json()["id"]
    d = env["p"].get(f"/api/pompiste/contexte?date={env['jour']}&periode=Matin").json()
    assert any(q["id"] == nouvelle for pr in d["produits"] for q in pr["pompes"])
    client.delete(f"/api/pompes/{nouvelle}", headers=H)


def test_controles_des_index(env):
    assert _compteurs(env, env["pompe"], 1000, 1100, charte=False).status_code == 400
    r = _compteurs(env, env["pompe"], 1000, 900)
    assert r.status_code == 422
    assert r.json()["detail"]["erreurs"][0]["pompe_id"] == env["pompe"]
    assert _compteurs(env, env["pompe"], 1000, 1000 + 9000).status_code == 422     # zéro en trop
    assert env["p"].post("/api/pompiste/terminer", json={}).status_code == 409    # rien à terminer


def test_compteurs_modifier_terminer(client, env, monkeypatch):
    r = _compteurs(env, env["pompe"], 1000, 1110)
    assert r.status_code == 201, r.text
    assert r.json()["statut"] == "EN_COURS" and r.json()["montant_attendu"] == 55000
    r = _compteurs(env, env["diesel"], 0, 10)
    assert len(r.json()["lignes"]) == 2 and r.json()["montant_attendu"] == 61000
    assert _compteurs(env, env["pompe"], 1000, 1100).status_code == 409               # pompe déjà saisie
    assert _compteurs(env, env["pompe"], 1100, 1200, periode="Apres-midi").status_code == 409
    # Modifier depuis le résumé
    r = env["p"].put(f"/api/pompiste/compteurs/{env['pompe']}", json={"metter_avant": 1000, "metter_apres": 1100})
    assert r.status_code == 200 and r.json()["montant_attendu"] == 56000
    assert env["p"].put(f"/api/pompiste/compteurs/{env['pompe']}",
                        json={"metter_avant": 1000, "metter_apres": 900}).status_code == 422
    r = env["p"].delete(f"/api/pompiste/compteurs/{env['diesel']}")
    assert r.status_code == 200 and r.json()["en_cours"]["montant_attendu"] == 50000
    # Terminer : email aux administrateurs
    import pompiste_routes, otp_service
    envoyes = []
    monkeypatch.setattr(pompiste_routes, "_emails_admins", lambda db: ["admin@exemple.ht"])
    monkeypatch.setattr(otp_service, "send_rapport_pompiste_email", lambda **k: envoyes.append(k))

    class ThreadDirect:
        def __init__(self, target, daemon=None): self.t = target
        def start(self): self.t()
    monkeypatch.setattr(pompiste_routes.threading, "Thread", ThreadDirect)
    r = _terminer(env, 49)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["statut"] == "SOUMIS" and d["montant_compte"] == 49000 and d["ecart"] == -1000
    assert envoyes and envoyes[0]["destinataires"] == ["admin@exemple.ht"]
    assert envoyes[0]["pompiste"] == "Jean-Éric Pierre"
    env["r1"] = d["id"]


def test_verification_et_finalisation(client, env):
    rid = env["r1"]
    # Finaliser trop tôt : refusé
    assert _sup(client, rid, "finaliser", note=8).status_code == 409
    # Écart avant les mètres : refusé
    assert _sup(client, rid, "ecart", confirme=True).status_code == 409
    # Mètre corrigé (1100 → 1098) puis billetterie, écart
    r = client.post(f"/api/pompiste-supervision/rapports/{rid}/lignes/{env['pompe']}/confirmer", headers=H,
                    json={"metter_avant": 1000, "metter_apres": 1098})
    assert r.status_code == 200 and r.json()["lignes"][0]["confirmee"]
    assert r.json()["montant_attendu_final"] == 49000 and r.json()["ecart_final"] == 0
    assert _sup(client, rid, "billetterie", confirme=True).json()["billetterie_confirmee"]
    assert _sup(client, rid, "ecart", confirme=True).json()["ecart_confirme"]
    assert _sup(client, rid, "finaliser", note=11).status_code == 400              # note sur 10
    assert _sup(client, rid, "finaliser", note=8).status_code == 400               # correction sans commentaire
    r = _sup(client, rid, "finaliser", note=8, commentaire="Mètre relu sur la pompe : 1098")
    assert r.status_code == 200, r.text
    assert r.json()["statut"] == "CONFIRME" and r.json()["note_superviseur"] == 8
    rel = client.get(f"/api/releves?date={env['jour']}&periode=Matin", headers=H).json()
    mine = [x for x in rel if x["pompe_id"] == env["pompe"]]
    assert len(mine) == 1 and float(mine[0]["metter_apres"]) == 1098
    assert _sup(client, rid, "finaliser", note=8).status_code == 409                # déjà finalisé


def test_probleme_puis_resolu(client, env):
    # Début différent du dernier index (1098) : relire et confirmer
    r = _compteurs(env, env["pompe"], 1100, 1200, periode="Apres-midi")
    assert r.status_code == 422 and r.json()["detail"]["erreurs"][0]["code"] == "debut_different"
    assert "1098" not in r.text                          # la valeur attendue n'est pas révélée
    assert _compteurs(env, env["pompe"], 1098, 1200, periode="Apres-midi").status_code == 201
    rid = _terminer(env, 40).json()["id"]
    assert _sup(client, rid, "probleme", commentaire="non").status_code == 400
    r = _sup(client, rid, "probleme", commentaire="Il manque 11 000 G, à voir avec le pompiste.")
    assert r.status_code == 200 and r.json()["statut"] == "PROBLEME"
    # Pompe d'un rapport en problème : toujours prise sur ce créneau
    r = _compteurs(env, env["pompe"], 1098, 1200, periode="Apres-midi")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "deja_rapporte"
    assert client.get("/api/pompiste-supervision/resume", headers=H).json()["problemes"] >= 1
    client.post(f"/api/pompiste-supervision/rapports/{rid}/lignes/{env['pompe']}/confirmer", headers=H,
                json={"metter_avant": 1098, "metter_apres": 1200})
    _sup(client, rid, "billetterie", confirme=True)
    _sup(client, rid, "ecart", confirme=True)
    assert _sup(client, rid, "finaliser", note=5).status_code == 400               # résolution sans explication
    r = _sup(client, rid, "finaliser", note=5, commentaire="Le pompiste a rapporté l'argent manquant.")
    assert r.status_code == 200 and r.json()["statut"] == "RESOLU"
    mes = env["p"].get("/api/pompiste/rapports").json()
    assert mes[0]["id"] == rid and mes[0]["statut"] == "RESOLU"


def test_renvoi_au_pompiste(client, env):
    # Un nouveau créneau (hier) pour vérifier le renvoi
    from tz_utils import today_haiti
    from datetime import timedelta
    hier = str(today_haiti() - timedelta(days=1))
    r = env["p"].post("/api/pompiste/compteurs", json={
        "date": hier, "periode": "Apres-midi", "charte_acceptee": True,
        "lignes": [{"pompe_id": env["diesel"], "metter_avant": 0, "metter_apres": 5}]})
    assert r.status_code == 201, r.text
    rid = _terminer(env, 3).json()["id"]
    r = _sup(client, rid, "rejeter", commentaire="Recomptez l'argent et refaites le rapport.")
    assert r.status_code == 200 and r.json()["statut"] == "REJETE"


def test_debut_different_confirme_et_annulation(client, env):
    """Début différent du dernier index : refusé tant que le pompiste ne
    confirme pas, accepté ensuite. Un rapport jamais terminé peut être annulé
    par le superviseur (il bloquait ses pompes)."""
    r = client.post("/api/releves", headers=H, json={"date": env["jour"], "periode": "Matin", "pompe_id": env["diesel"],
                                                     "prix_gallon": 600, "metter_avant": 100, "metter_apres": 150})
    assert r.status_code == 200, r.text
    r = _compteurs(env, env["diesel"], 160, 170, periode="Apres-midi")
    assert r.status_code == 422 and r.json()["detail"]["erreurs"][0]["code"] == "debut_different"
    r = _compteurs(env, env["diesel"], 160, 170, periode="Apres-midi", confirme=True)
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    assert client.get("/api/pompiste-supervision/resume", headers=H).json()["non_termines"] >= 1
    lst = client.get("/api/pompiste-supervision/rapports?statut=EN_COURS", headers=H).json()
    assert any(x["id"] == rid for x in lst)
    assert client.delete(f"/api/pompiste-supervision/rapports/{rid}", headers=H).status_code == 200
    assert env["p"].get("/api/pompiste/contexte").json()["en_cours"] is None


def test_journal_comme_le_module_saisie(client):
    """Les mètres du pompiste entrent dans le journal dès le « Terminer »,
    comme une saisie : pompiste attribué (fiche employé créée avec le compte,
    visible dans le filtre), correction du superviseur répercutée, retrait
    si le rapport est renvoyé."""
    from tz_utils import today_haiti
    import main as _m
    jour = str(today_haiti())
    pompe = _produit(client, 400)
    c = client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": "Rose", "nom": "Saint-Fleur"}).json()
    assert c["employe_nom"] == "Rose Saint-Fleur"
    filtre = client.get("/api/pompistes", headers=H).json()
    emp_id = next(x["id"] for x in filtre if x["nom"] == "Rose Saint-Fleur")
    p = TestClient(_m.app)
    assert p.post("/api/login/pompiste", json={"username": c["username"], "code_acces": c["code_acces"]}).status_code == 200
    r = p.post("/api/pompiste/compteurs", json={"date": jour, "periode": "Matin", "charte_acceptee": True,
              "lignes": [{"pompe_id": pompe, "metter_avant": 10, "metter_apres": 60}]})
    assert r.status_code == 201, r.text
    # Pas encore dans le journal : le rapport n'est pas terminé
    j = client.get(f"/api/journal?date_debut={jour}&date_fin={jour}&pompe_id={pompe}", headers=H).json()
    assert j["entries"] == []
    rid = p.post("/api/pompiste/terminer", json={"billetterie": [{"coupure": 1000, "billets": 20}]}).json()["id"]
    j = client.get(f"/api/journal?date_debut={jour}&date_fin={jour}&pompe_id={pompe}", headers=H).json()["entries"]
    assert len(j) == 1 and j[0]["source"] == "pompiste" and j[0]["pompiste_nom"] == "Rose Saint-Fleur"
    assert j[0]["metter_apres"] == 60 and j[0]["rapport_statut"] == "SOUMIS" and j[0]["saisi_par"] == "Rose Saint-Fleur"
    j = client.get(f"/api/journal?date_debut={jour}&date_fin={jour}&pompiste_id={emp_id}", headers=H).json()["entries"]
    assert [e["pompe_id"] for e in j] == [pompe]
    # Correction du superviseur → journal à jour
    client.post(f"/api/pompiste-supervision/rapports/{rid}/lignes/{pompe}/confirmer", headers=H,
                json={"metter_avant": 10, "metter_apres": 58})
    j = client.get(f"/api/journal?date_debut={jour}&date_fin={jour}&pompe_id={pompe}", headers=H).json()["entries"]
    assert j[0]["metter_apres"] == 58
    # Filtre de la liste des rapports par pompiste
    lst = client.get(f"/api/pompiste-supervision/rapports?statut=SOUMIS&utilisateur_id={c['id']}", headers=H).json()
    assert [x["id"] for x in lst] == [rid]
    # Renvoi au pompiste → retiré du journal
    _sup(client, rid, "rejeter", commentaire="Recomptez s'il vous plaît.")
    j = client.get(f"/api/journal?date_debut={jour}&date_fin={jour}&pompe_id={pompe}", headers=H).json()["entries"]
    assert j == []


def test_desactivation_coupe_la_session(client, env):
    r = client.patch(f"/api/pompiste-supervision/comptes/{env['uid']}", headers=H, json={"actif": False})
    assert r.status_code == 200
    assert env["p"].get("/api/pompiste/contexte").status_code == 401
    r = client.patch(f"/api/pompiste-supervision/comptes/{env['uid']}", headers=H,
                     json={"actif": True, "nouveau_pin": True})
    assert r.status_code == 200 and len(r.json()["code_acces"]) == 9



def test_limite_connexion_par_identifiant(client):
    """Les tablettes d'une station partagent une IP : les erreurs d'un pompiste
    ne doivent pas bloquer la connexion des autres."""
    for _ in range(10):
        assert client.post("/api/login/pompiste", json={"username": "inconnu.a", "code_acces": "111111111"}).status_code == 401
    assert client.post("/api/login/pompiste", json={"username": "inconnu.a", "code_acces": "111111111"}).status_code == 429
    assert client.post("/api/login/pompiste", json={"username": "inconnu.b", "code_acces": "111111111"}).status_code == 401


def test_date_choisie_dans_le_calendrier(client):
    """Le pompiste choisit la date : jamais dans le futur, au plus 31 jours en arrière."""
    from datetime import timedelta
    from tz_utils import today_haiti
    import main as _m
    auj = today_haiti()
    pompe = _produit(client, 300)
    c = client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": "Cal", "nom": "Endrier"}).json()
    p = TestClient(_m.app)
    p.post("/api/login/pompiste", json={"username": c["username"], "code_acces": c["code_acces"]})
    ctx = p.get("/api/pompiste/contexte").json()
    assert ctx["date_min"] == str(auj - timedelta(days=31))
    def envoi(d):
        return p.post("/api/pompiste/compteurs", json={"date": str(d), "periode": "Matin", "charte_acceptee": True,
                      "lignes": [{"pompe_id": pompe, "metter_avant": 0, "metter_apres": 5}]})
    r = envoi(auj + timedelta(days=1))
    assert r.status_code == 400 and r.json()["detail"]["code"] == "date_future"
    r = envoi(auj - timedelta(days=32))
    assert r.status_code == 400 and r.json()["detail"]["code"] == "date_ancienne"
    assert p.get(f"/api/pompiste/contexte?date={auj - timedelta(days=10)}&periode=Matin").status_code == 200
    r = envoi(auj - timedelta(days=10))
    assert r.status_code == 201, r.text
    assert r.json()["date"] == str(auj - timedelta(days=10))


def test_application_installable(client):
    """L'espace pompiste s'installe comme une application (PWA), sans connexion préalable."""
    from fastapi.testclient import TestClient
    import main as _m
    anonyme = TestClient(_m.app)
    r = anonyme.get("/pompiste/manifest.webmanifest")
    assert r.status_code == 200 and "manifest+json" in r.headers["content-type"]
    m = r.json()
    assert m["start_url"] == "/pompiste" and m["display"] == "standalone"
    assert {i["sizes"] for i in m["icons"]} >= {"192x192", "512x512"} and any(i["purpose"] == "maskable" for i in m["icons"])
    for i in m["icons"]:
        assert anonyme.get(i["src"]).status_code == 200
    r = anonyme.get("/pompiste-sw.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert "worker-src 'self'" in r.headers["content-security-policy"]
    r = anonyme.get("/pompiste/qr.png")
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    assert anonyme.get("/pompiste/installer").status_code == 200
