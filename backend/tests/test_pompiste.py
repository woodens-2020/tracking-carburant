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
    """Tant que la direction n'a pas finalisé, les mètres du pompiste sont
    visibles au Journal Meters (« en attente ») mais ne comptent nulle part :
    ni totaux du journal, ni rapport du jour (caisse), ni stock. À la
    finalisation, ils entrent dans le système comme une saisie. Fiche
    employé créée avec le compte, visible dans le filtre ; renvoi = retrait."""
    from tz_utils import today_haiti
    import main as _m
    jour = str(today_haiti())
    pompe = _produit(client, 400)
    c = client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": "Rose", "nom": "Saint-Fleur"}).json()
    assert c["employe_nom"] == "Rose Saint-Fleur"
    emp_id = next(x["id"] for x in client.get("/api/pompistes", headers=H).json() if x["nom"] == "Rose Saint-Fleur")
    p = TestClient(_m.app)
    assert p.post("/api/login/pompiste", json={"username": c["username"], "code_acces": c["code_acces"]}).status_code == 200

    def journal(**f):
        q = "&".join(f"{k}={v}" for k, v in f.items())
        return client.get(f"/api/journal?date_debut={jour}&date_fin={jour}&{q}", headers=H).json()

    def gallons_du_jour():
        d = client.get(f"/api/carburant/rapport-jour?date={jour}", headers=H).json()
        return sum(x["gallons"] for x in d["entrees"]["par_produit"] if any(True for _ in [0]))

    avant = gallons_du_jour()
    r = p.post("/api/pompiste/compteurs", json={"date": jour, "periode": "Matin", "charte_acceptee": True,
              "lignes": [{"pompe_id": pompe, "metter_avant": 10, "metter_apres": 60}]})
    assert r.status_code == 201, r.text
    assert journal(pompe_id=pompe)["entries"] == []                  # rapport pas encore terminé
    rid = p.post("/api/pompiste/terminer", json={"billetterie": [{"coupure": 1000, "billets": 20}]}).json()["id"]
    # Soumis : visible « en attente », compté nulle part
    j = journal(pompe_id=pompe)
    assert len(j["entries"]) == 1 and j["entries"][0]["en_attente"] is True
    assert j["entries"][0]["pompiste_nom"] == "Rose Saint-Fleur" and j["entries"][0]["metter_apres"] == 60
    assert j["resume"]["total_gallons"] == 0 and j["resume"]["nb_en_attente"] == 1 and j["resume"]["gallons_en_attente"] == 50
    assert [e["pompe_id"] for e in journal(pompiste_id=emp_id)["entries"]] == [pompe]
    assert gallons_du_jour() == avant                                 # rapport du jour / caisse inchangés
    # Mètre confirmé (corrigé) par la direction : il entre aussitôt au journal / dans la Saisie
    _sup(client, rid, f"lignes/{pompe}/confirmer", metter_avant=10, metter_apres=58)
    j = journal(pompe_id=pompe)
    assert len(j["entries"]) == 1 and j["entries"][0]["en_attente"] is False and j["entries"][0]["metter_apres"] == 58
    assert abs(gallons_du_jour() - avant - 48) < 1e-6
    # Finalisation : le rapport est clos, les chiffres restent les mêmes
    _sup(client, rid, "billetterie", confirme=True)
    _sup(client, rid, "ecart", confirme=True)
    r = _sup(client, rid, "finaliser", note=9, commentaire="Index relu : 58")
    assert r.status_code == 200, r.text
    j = journal(pompe_id=pompe)
    assert len(j["entries"]) == 1 and j["entries"][0]["en_attente"] is False and j["entries"][0]["rapport_statut"] == "CONFIRME"
    assert j["resume"]["total_gallons"] == 48 and j["resume"]["nb_en_attente"] == 0
    assert abs(gallons_du_jour() - avant - 48) < 1e-6
    lst = client.get(f"/api/pompiste-supervision/rapports?statut=CONFIRME&utilisateur_id={c['id']}", headers=H).json()
    assert [x["id"] for x in lst] == [rid]


def test_renvoi_retire_du_journal_en_attente(client):
    from tz_utils import today_haiti
    import main as _m
    jour = str(today_haiti())
    pompe = _produit(client, 450)
    c = client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": "Ren", "nom": "Voi"}).json()
    p = TestClient(_m.app)
    p.post("/api/login/pompiste", json={"username": c["username"], "code_acces": c["code_acces"]})
    p.post("/api/pompiste/compteurs", json={"date": jour, "periode": "Matin", "charte_acceptee": True,
           "lignes": [{"pompe_id": pompe, "metter_avant": 0, "metter_apres": 30}]})
    rid = p.post("/api/pompiste/terminer", json={"billetterie": [{"coupure": 1000, "billets": 13}]}).json()["id"]
    assert len(client.get(f"/api/journal?date_debut={jour}&date_fin={jour}&pompe_id={pompe}", headers=H).json()["entries"]) == 1
    _sup(client, rid, "rejeter", commentaire="Recomptez s'il vous plaît.")
    assert client.get(f"/api/journal?date_debut={jour}&date_fin={jour}&pompe_id={pompe}", headers=H).json()["entries"] == []

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
    from pompiste_routes import JOURS_EN_ARRIERE
    assert ctx["date_min"] == str(auj - timedelta(days=JOURS_EN_ARRIERE))
    def envoi(d):
        return p.post("/api/pompiste/compteurs", json={"date": str(d), "periode": "Matin", "charte_acceptee": True,
                      "lignes": [{"pompe_id": pompe, "metter_avant": 0, "metter_apres": 5}]})
    r = envoi(auj + timedelta(days=1))
    assert r.status_code == 400 and r.json()["detail"]["code"] == "date_future"
    r = envoi(auj - timedelta(days=JOURS_EN_ARRIERE + 1))
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


def test_rattrapage_et_nettoyage_du_journal(client, db):
    """Démarrage : un rapport finalisé dont un mètre manque au journal est
    rattrapé ; un rapport pas encore finalisé n'a rien au journal officiel
    (écritures anticipées de l'ancien fonctionnement retirées)."""
    from tz_utils import today_haiti
    import main as _m
    import pompiste_routes
    from models import Releve
    jour = str(today_haiti())
    p1 = _produit(client, 700)
    prod = next(p for p in client.get("/api/produits", headers=H).json() if any(q["id"] == p1 for q in p["pompes"]))
    p2 = client.post(f"/api/produits/{prod['id']}/pompes", headers=H, json={"nom": f"{prod['nom']} P2"}).json()["id"]
    c = client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": "Rat", "nom": "Trapage"}).json()
    p = TestClient(_m.app)
    p.post("/api/login/pompiste", json={"username": c["username"], "code_acces": c["code_acces"]})
    r = p.post("/api/pompiste/compteurs", json={"date": jour, "periode": "Apres-midi", "charte_acceptee": True,
              "lignes": [{"pompe_id": p1, "metter_avant": 0, "metter_apres": 40}, {"pompe_id": p2, "metter_avant": 0, "metter_apres": 25}]})
    assert r.status_code == 201, r.text
    rid = p.post("/api/pompiste/terminer", json={"billetterie": [{"coupure": 1000, "billets": 45}]}).json()["id"]
    # Pas encore finalisé : rien au journal officiel ; une écriture anticipée est nettoyée
    assert db.query(Releve).filter(Releve.pompe_id.in_([p1, p2])).count() == 0
    db.add(Releve(date=today_haiti(), periode="Apres-midi", pompe_id=p1, prix_gallon=700, metter_avant=0, metter_apres=40,
                  rapport_pompiste_id=rid))
    db.commit()
    assert pompiste_routes.nettoyer_journal_anticipe(db) == 1
    assert db.query(Releve).filter(Releve.pompe_id.in_([p1, p2])).count() == 0
    # Finalisé, puis mètres absents (rapport d'avant) : rattrapés, toutes les pompes
    for pid in (p1, p2):
        _sup(client, rid, f"lignes/{pid}/confirmer", metter_avant=0, metter_apres=40 if pid == p1 else 25)
    _sup(client, rid, "billetterie", confirme=True)
    _sup(client, rid, "ecart", confirme=True)
    assert _sup(client, rid, "finaliser", note=8).status_code == 200
    db.expire_all()
    db.query(Releve).filter(Releve.rapport_pompiste_id == rid).delete()
    db.commit()
    assert pompiste_routes.rattraper_journal(db) >= 2
    rel = db.query(Releve).filter(Releve.pompe_id.in_([p1, p2])).all()
    assert sorted(float(x.metter_apres) for x in rel) == [25.0, 40.0]
    assert all(x.rapport_pompiste_id == rid and x.pompiste_id for x in rel)
    assert pompiste_routes.rattraper_journal(db) == 0

def test_depenses_et_dollars(client):
    """Le pompiste déclare ses dépenses (description, montant, qui a autorisé)
    et ses dollars au taux du jour : l'argent à remettre = ventes − dépenses,
    l'écart en tient compte ; à la finalisation, les dépenses entrent dans
    les dépenses de la station (une seule fois)."""
    from tz_utils import today_haiti
    import main as _m
    jour = str(today_haiti())
    pompe = _produit(client, 500)
    c = client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": "Dep", "nom": "Ense"}).json()
    p = TestClient(_m.app)
    p.post("/api/login/pompiste", json={"username": c["username"], "code_acces": c["code_acces"]})
    r = p.post("/api/pompiste/compteurs", json={"date": jour, "periode": "Matin", "charte_acceptee": True,
              "lignes": [{"pompe_id": pompe, "metter_avant": 0, "metter_apres": 100}]})    # 50 000 G de ventes
    assert r.status_code == 201, r.text
    corps = {"billetterie": [{"coupure": 1000, "billets": 30}], "monnaie": 0,
             "depenses": [{"description": "Glace pour la boutique", "montant": 1500, "autorise_par": "M. Pierre"},
                          {"description": "Transport du gérant", "montant": 2000, "autorise_par": "Directeur"}],
             "devises": [{"devise": "USD", "montant": 100}]}
    # Pas de taux configuré : les dollars sont refusés
    r = p.post("/api/pompiste/terminer", json=corps)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "taux_absent"
    assert client.post("/api/pompiste-supervision/taux", headers=H, json={"taux": 132.5}).status_code == 201
    assert p.get("/api/pompiste/contexte").json()["taux"]["taux"] == 132.5
    # Dépense incomplète refusée
    mauvais = dict(corps, depenses=[{"description": "Glace", "montant": 1500, "autorise_par": ""}])
    assert p.post("/api/pompiste/terminer", json=mauvais).status_code == 400
    r = p.post("/api/pompiste/terminer", json=corps)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["total_depenses"] == 3500 and d["montant_a_remettre"] == 46500
    assert d["devises"][0]["montant_htg"] == 13250 and d["montant_compte"] == 43250     # 30 000 G + 100 $ × 132,5
    assert d["ecart"] == -3250                                                          # 43 250 + 3 500 − 50 000
    rid = d["id"]
    _sup(client, rid, f"lignes/{pompe}/confirmer", metter_avant=0, metter_apres=100)
    _sup(client, rid, "billetterie", confirme=True)
    # L'écart attend la décision sur chaque dépense
    assert _sup(client, rid, "ecart", confirme=True).status_code == 409

    def mes_depenses():
        deps = client.get(f"/api/depenses?date_debut={jour}&date_fin={jour}", headers=H).json()["depenses"]
        return [x for x in deps if (x["reference"] or "").startswith(f"RP{rid}-")]
    # Confirmée : elle entre tout de suite dans les Dépenses, marquée « payée par le pompiste »
    r = _sup(client, rid, "depenses/0/confirmer")
    assert r.status_code == 200 and r.json()["depenses"][0]["statut"] == "confirmee"
    assert [(x["montant"], x["rapport_pompiste_id"]) for x in mes_depenses()] == [(1500, rid)]
    assert "autorisée par M. Pierre" in mes_depenses()[0]["notes"]
    # Refusée (motif obligatoire) : elle ne réduit plus l'argent à remettre
    assert _sup(client, rid, "depenses/1/refuser", motif="").status_code == 400
    r = _sup(client, rid, "depenses/1/refuser", motif="Pas autorisée par la direction")
    assert r.json()["total_depenses_retenues"] == 1500 and r.json()["ecart_final"] == -5250   # 43 250 + 1 500 − 50 000
    assert len(mes_depenses()) == 1
    # Reprendre puis reconfirmer ne crée pas de doublon
    _sup(client, rid, "depenses/0/reprendre"); assert mes_depenses() == []
    _sup(client, rid, "depenses/0/confirmer"); _sup(client, rid, "depenses/0/confirmer")
    assert len(mes_depenses()) == 1
    # Flèche « ➜ Dépenses : Non » à la confirmation : confirmée, mais hors des Dépenses
    _sup(client, rid, "depenses/0/reprendre")
    r = _sup(client, rid, "depenses/0/confirmer", envoyer=False)
    assert r.json()["depenses"][0]["vers_depenses"] is False and r.json()["total_depenses_retenues"] == 1500
    assert mes_depenses() == []
    assert _sup(client, rid, "depenses/1/envoyer", envoyer=True).status_code == 409     # refusée
    # Envoyée plus tard : elle entre dans les Dépenses et le rapport du jour
    r = _sup(client, rid, "depenses/0/envoyer", envoyer=True)
    assert r.status_code == 200 and r.json()["depenses"][0]["vers_depenses"] is True
    assert len(mes_depenses()) == 1
    rj = client.get(f"/api/carburant/rapport-jour?date={jour}", headers=H).json()
    lj = [x for x in rj["depenses"]["liste"] if x["reference"] == f"RP{rid}-1"]
    assert len(lj) == 1 and lj[0]["pompiste"] and lj[0]["source"].startswith("Payée par le pompiste")
    _sup(client, rid, "depenses/0/envoyer", envoyer=False); assert mes_depenses() == []
    rj = client.get(f"/api/carburant/rapport-jour?date={jour}", headers=H).json()
    assert not [x for x in rj["depenses"]["liste"] if x["reference"] == f"RP{rid}-1"]
    _sup(client, rid, "depenses/0/envoyer", envoyer=True); assert len(mes_depenses()) == 1
    assert _sup(client, rid, "ecart", confirme=True).status_code == 200
    assert _sup(client, rid, "finaliser", note=7, commentaire="Il manque 5 250 G").status_code == 200
    assert len(mes_depenses()) == 1
    # Après finalisation, la flèche marche encore (retirer puis renvoyer)
    _sup(client, rid, "depenses/0/envoyer", envoyer=False); assert mes_depenses() == []
    _sup(client, rid, "depenses/0/envoyer", envoyer=True); assert len(mes_depenses()) == 1
    # Journal de caisse : la dépense se déduit des ventes, pas une sortie de la caisse principale
    j = client.get(f"/api/caisse-principale/journal?date_debut={jour}&date_fin={jour}", headers=H).json()
    dpp = [l for l in j["lignes"] if l["type"] == "depense_pompiste"]
    assert [(l["debit"], l["credit"]) for l in dpp] == [(-1500, 0)]
    assert not [l for l in j["lignes"] if l["type"] == "depense" and (l["reference"] or "").startswith(f"RP{rid}-")]
    assert {c["code"]: c for c in j["controles"]}["equilibre"]["niveau"] == "ok"
    # Caisse principale : la direction supprime la dépense (elle ne passe pas), même rapport finalisé
    did = mes_depenses()[0]["id"]
    url = f"/api/caisse-principale/depenses-pompiste/{did}/refuser"
    assert client.post(url, headers=H, json={"motif": "non"}).status_code == 400
    assert client.post(url, headers=H, json={"motif": "Glace jamais livrée"}).status_code == 200
    assert mes_depenses() == []
    j = client.get(f"/api/caisse-principale/journal?date_debut={jour}&date_fin={jour}", headers=H).json()
    assert not [l for l in j["lignes"] if l["type"] == "depense_pompiste"]
    liste = client.get("/api/pompiste-supervision/rapports?statut=CONFIRME,RESOLU", headers=H).json()
    rap = next(x for x in (liste["rapports"] if isinstance(liste, dict) else liste) if x["id"] == rid)
    assert rap["depenses"][0]["statut"] == "refusee" and rap["depenses"][0]["motif"] == "Glace jamais livrée"
    assert rap["total_depenses_retenues"] == 0
    assert client.post(url, headers=H, json={"motif": "Glace jamais livrée"}).status_code == 404


def test_dollars_rapport_en_retard(client):
    """Rapport daté d'il y a 15 jours, taux fixé aujourd'hui seulement : les
    dollars sont acceptés avec le taux le plus proche (cas du staging)."""
    from datetime import timedelta
    from tz_utils import today_haiti
    import main as _m
    import pompiste_routes
    from database import SessionLocal
    jour = today_haiti() - timedelta(days=15)
    db = SessionLocal()
    try:
        t_avant = pompiste_routes.taux_du_jour(db, jour - timedelta(days=400))
    finally:
        db.close()
    client.post("/api/pompiste-supervision/taux", headers=H, json={"taux": 140})
    pompe = _produit(client, 500)
    c = client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": "Ret", "nom": "Ard"}).json()
    p = TestClient(_m.app)
    p.post("/api/login/pompiste", json={"username": c["username"], "code_acces": c["code_acces"]})
    r = p.post("/api/pompiste/compteurs", json={"date": str(jour), "periode": "Matin", "charte_acceptee": True,
              "lignes": [{"pompe_id": pompe, "metter_avant": 0, "metter_apres": 10}]})
    assert r.status_code == 201, r.text
    ctx = p.get(f"/api/pompiste/contexte?date={jour}&periode=Matin").json()
    assert ctx["taux"] is not None
    r = p.post("/api/pompiste/terminer", json={"billetterie": [], "devises": [{"devise": "USD", "montant": 10}]})
    assert r.status_code == 200, r.text
    assert r.json()["devises"][0]["montant_htg"] == round(10 * ctx["taux"]["taux"], 2)


def test_pompiste_rattache_a_une_station(client):
    """Plusieurs stations : à la création du compte, on choisit l'adresse
    (parmi celles des produits) ; le pompiste ne voit et ne saisit que les
    pompes de sa station."""
    from tz_utils import today_haiti
    import main as _m
    t = time.time_ns()
    a1, a2 = f"Route Nationale 1, Vertières {t}", f"Champin, Cap-Haïtien {t}"
    p1 = client.post("/api/produits", headers=H, json={"nom": f"GazA-{t}", "prix_gallon": 500}).json()["id"]
    p2 = client.post("/api/produits", headers=H, json={"nom": f"GazB-{t}", "prix_gallon": 500}).json()["id"]
    # L'adresse est portée par les pompes physiques
    f1 = client.post("/api/distributeurs", headers=H, json={"nom": f"P-A-{t}", "adresse": a1, "faces": ["A"]}).json()["id"]
    f2 = client.post("/api/distributeurs", headers=H, json={"nom": f"P-B-{t}", "adresse": a2, "faces": ["A"]}).json()["id"]
    dist = {d["id"]: d for d in client.get("/api/distributeurs", headers=H).json()["distributeurs"]}
    q1 = client.post(f"/api/produits/{p1}/pompes", headers=H, json={"nom": "P1", "face_id": dist[f1]["faces"][0]["id"]}).json()["id"]
    q2 = client.post(f"/api/produits/{p2}/pompes", headers=H, json={"nom": "P1", "face_id": dist[f2]["faces"][0]["id"]}).json()["id"]
    st = client.get("/api/pompiste-supervision/stations", headers=H).json()
    assert {a1, a2} <= {s["adresse"] for s in st}
    assert client.post("/api/pompiste-supervision/comptes", headers=H,
                       json={"prenom": "St", "nom": "Inconnue", "station_adresse": "Nulle part"}).status_code == 400
    c = client.post("/api/pompiste-supervision/comptes", headers=H,
                    json={"prenom": "Sta", "nom": "Tion", "station_adresse": "  " + a1.upper() + " "}).json()
    assert c["station_adresse"] == a1                      # rapproché de l'adresse enregistrée
    p = TestClient(_m.app)
    p.post("/api/login/pompiste", json={"username": c["username"], "code_acces": c["code_acces"]})
    ctx = p.get("/api/pompiste/contexte").json()
    ids = {x["id"] for x in ctx["produits"]}
    assert p1 in ids and p2 not in ids and ctx["pompiste"]["station"] == a1
    jour = str(today_haiti())
    r = p.post("/api/pompiste/compteurs", json={"date": jour, "periode": "Matin", "charte_acceptee": True,
              "lignes": [{"pompe_id": q2, "metter_avant": 0, "metter_apres": 5}]})
    assert r.status_code == 422 and r.json()["detail"]["erreurs"][0]["code"] == "autre_station"
    # Changement de station, puis « toutes les stations »
    assert client.patch(f"/api/pompiste-supervision/comptes/{c['id']}", headers=H,
                        json={"station_adresse": a2}).json()["station_adresse"] == a2
    ids = {x["id"] for x in p.get("/api/pompiste/contexte").json()["produits"]}
    assert p2 in ids and p1 not in ids
    assert client.patch(f"/api/pompiste-supervision/comptes/{c['id']}", headers=H,
                        json={"station_adresse": ""}).json()["station_adresse"] is None
    ids = {x["id"] for x in p.get("/api/pompiste/contexte").json()["produits"]}
    assert {p1, p2} <= ids


def test_creneau_automatique(client):
    """Le pompiste ne choisit plus matin / après-midi : 1er rapport du jour
    d'une pompe = Matin, 2e = Après-midi, 3e refusé."""
    import main
    from tz_utils import today_haiti
    pompe = _produit(client, 700)
    jour = str(today_haiti())

    def pompiste():
        c = client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": "Auto", "nom": "Creneau"}).json()
        t = TestClient(main.app)
        assert t.post("/api/login/pompiste", json={"username": c["username"], "code_acces": c["code_acces"]}).status_code == 200
        return t

    def envoyer(t, av, ap):
        return t.post("/api/pompiste/compteurs", json={"date": jour, "charte_acceptee": True,
                      "lignes": [{"pompe_id": pompe, "metter_avant": av, "metter_apres": ap, "debut_confirme": True}]})

    a, b, c = pompiste(), pompiste(), pompiste()
    ctx = a.get(f"/api/pompiste/contexte?date={jour}").json()
    assert ctx["periode"] is None
    assert all(not q["deja_rapportee"] for x in ctx["produits"] for q in x["pompes"] if q["id"] == pompe)
    r = envoyer(a, 0, 10)
    assert r.status_code == 201, r.text
    assert a.get("/api/pompiste/contexte").json()["en_cours"]["periode"] == "Matin"
    r = envoyer(b, 10, 25)
    assert r.status_code == 201, r.text
    assert b.get("/api/pompiste/contexte").json()["en_cours"]["periode"] == "Apres-midi"
    # Deux rapports ce jour : la pompe apparaît « déjà rapportée » et un 3e envoi est refusé
    ctx = c.get(f"/api/pompiste/contexte?date={jour}").json()
    assert [q["deja_rapportee"] is not None for x in ctx["produits"] for q in x["pompes"] if q["id"] == pompe] == [True]
    r = envoyer(c, 25, 30)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "deja_rapporte"


def test_pompistes_employes_et_rapports_anciens(client):
    """Les pompistes deviennent des employés attribuables (même partis) ;
    l'espace pompiste accepte les rapports des deux derniers mois."""
    import main
    from datetime import timedelta
    from tz_utils import today_haiti
    c = client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": "Ancien", "nom": "Pompiste"}).json()
    r = client.post("/api/pompiste-supervision/recuperer-employes", headers=H)
    assert r.status_code == 200 and r.json()["comptes"] >= 1
    emp = next(p for p in client.get("/api/pompistes", headers=H).json() if p["nom"] == "Ancien Pompiste")
    # Le pompiste part : sa fiche reste attribuable aux relevés anciens
    client.patch(f"/api/pompiste-supervision/comptes/{c['id']}", headers=H, json={"actif": False})
    assert all(p["id"] != emp["id"] for p in client.get("/api/pompistes", headers=H).json())
    assert any(p["id"] == emp["id"] and not p["actif"] for p in client.get("/api/pompistes?inclure_inactifs=true", headers=H).json())
    pompe = _produit(client, 800)
    jour = str(today_haiti() - timedelta(days=60))
    rel = client.post("/api/releves", headers=H, json={"date": jour, "periode": "Matin", "pompe_id": pompe, "prix_gallon": 800,
                                                       "metter_avant": 0, "metter_apres": 10}).json()
    r = client.patch(f"/api/releves/{rel['id']}/pompiste", headers=H, json={"pompiste_id": emp["id"]})
    assert r.status_code == 200 and r.json()["pompiste_id"] == emp["id"]
    # Un employé qui n'est pas pompiste est refusé
    autre = client.post("/api/employes", headers=H, json={"prenom": "Caissier", "nom": "Test", "poste": "Caissier",
                                                         "date_embauche": str(today_haiti()), "salaire_base": 0})
    if autre.status_code in (200, 201):
        assert client.patch(f"/api/releves/{rel['id']}/pompiste", headers=H,
                            json={"pompiste_id": autre.json()["id"]}).status_code == 400
    # Espace pompiste : 60 jours en arrière acceptés, 80 refusés
    c2 = client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": "Deux", "nom": "Mois"}).json()
    t = TestClient(main.app)
    assert t.post("/api/login/pompiste", json={"username": c2["username"], "code_acces": c2["code_acces"]}).status_code == 200
    pompe2 = _produit(client, 810)
    assert t.get(f"/api/pompiste/contexte?date={today_haiti() - timedelta(days=60)}").status_code == 200
    r = t.get(f"/api/pompiste/contexte?date={today_haiti() - timedelta(days=80)}")
    assert r.status_code == 400 and r.json()["detail"]["code"] == "date_ancienne"
    r = t.post("/api/pompiste/compteurs", json={"date": str(today_haiti() - timedelta(days=60)), "charte_acceptee": True,
               "lignes": [{"pompe_id": pompe2, "metter_avant": 0, "metter_apres": 5, "debut_confirme": True}]})
    assert r.status_code == 201, r.text


def test_recuperation_garde_les_comptes(client, db):
    """« Récupérer les pompistes » ne prend que le nom : le compte reste intact
    et le pompiste se connecte toujours avec son code et son PIN."""
    import main
    from models import Employe, Utilisateur
    c = client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": "Garde", "nom": "Compte"}).json()
    db.expire_all()
    u = db.get(Utilisateur, c["id"])
    avant = (u.username, u.code_acces_hash, u.password_hash, u.actif, u.role_id, u.nom_complet)
    for _ in range(2):                                         # plusieurs fois : sans effet de bord
        assert client.post("/api/pompiste-supervision/recuperer-employes", headers=H).status_code == 200
    db.expire_all()
    u = db.get(Utilisateur, c["id"])
    assert (u.username, u.code_acces_hash, u.password_hash, u.actif, u.role_id, u.nom_complet) == avant
    fiches = db.query(Employe).filter_by(utilisateur_id=u.id).all()
    assert len(fiches) == 1 and (fiches[0].prenom, fiches[0].nom, fiches[0].poste) == ("Garde", "Compte", "Pompiste")
    assert "seul le nom est repris" in fiches[0].notes
    t = TestClient(main.app)
    assert t.post("/api/login/pompiste", json={"username": c["username"], "code_acces": c["code_acces"]}).status_code == 200


def test_reprise_compte_avec_code_impose(client):
    import main
    r = client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": "Repris", "nom": "Compte", "username": "reprisCompte1234"})
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["username"] == "repriscompte1234" and len(c["code_acces"]) == 9
    assert client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": "A", "nom": "B", "username": "repriscompte1234"}).status_code == 409
    assert client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": "A", "nom": "B", "username": "x y"}).status_code == 400
    t = TestClient(main.app)
    assert t.post("/api/login/pompiste", json={"username": c["username"], "code_acces": c["code_acces"]}).status_code == 200


def test_feuille_bons_credits_et_credit_en_caisse(client):
    """Feuille de la station : C = bons et chèques et D = crédits justifient des
    ventes ; F = crédit en caisse (fonds reçu de la caisse) est retiré du cash.
    Short / Over = (E − F) + C + D + dépenses − ventes."""
    from tz_utils import today_haiti
    import main as _m
    jour = str(today_haiti())
    pompe, diesel = _produit(client, 500), _produit(client, 600)
    c = client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": "Feu", "nom": "Ille"}).json()
    p = TestClient(_m.app)
    p.post("/api/login/pompiste", json={"username": c["username"], "code_acces": c["code_acces"]})
    # Les deux carburants envoyés ensemble, comme sur la feuille : 50 000 + 12 000 G
    r = p.post("/api/pompiste/compteurs", json={"date": jour, "charte_acceptee": True, "lignes": [
        {"pompe_id": pompe, "metter_avant": 0, "metter_apres": 100}, {"pompe_id": diesel, "metter_avant": 0, "metter_apres": 20}]})
    assert r.status_code == 201, r.text
    assert "station" in p.get("/api/pompiste/contexte").json()
    corps = {"billetterie": [{"coupure": 1000, "billets": 45}], "monnaie": 250, "heures": "06:00 – 14:00",
             "bons": [{"reference": "Chèque BNC 0042", "montant": 10000}],
             "credits": [{"nom": "Transport Saint-Michel", "montant": 4000}],
             "fonds_caisse": {"nom": "Caissière Rose", "montant": 2000},
             "depenses": [{"description": "Glace", "montant": 500, "autorise_par": "Gérant"}]}
    for mauvais in ({"credits": [{"nom": "", "montant": 10}]}, {"bons": [{"reference": "x", "montant": -5}]},
                    {"fonds_caisse": {"nom": "", "montant": 100}}):
        assert p.post("/api/pompiste/terminer", json=dict(corps, **mauvais)).status_code == 400, mauvais
    r = p.post("/api/pompiste/terminer", json=corps)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["montant_attendu"] == 62000 and d["montant_compte"] == 45250
    assert d["totaux_feuille"] == {"bons": 10000, "credits": 4000, "fonds_caisse": 2000, "net": 12000}
    # (45 250 − 2 000) + 10 000 + 4 000 + 500 − 62 000 = −4 250 (short)
    assert d["ecart"] == -4250 and d["ecart_final"] == -4250
    assert d["feuille"]["heures"] == "06:00 – 14:00" and d["feuille"]["credits"][0]["nom"] == "Transport Saint-Michel"
    sup = client.get("/api/pompiste-supervision/rapports", headers=H).json()
    lst = sup["rapports"] if isinstance(sup, dict) else sup
    assert any(x["id"] == d["id"] and x["totaux_feuille"]["net"] == 12000 for x in lst)


def test_doublon_inactif_cache_de_la_saisie(client):
    """Deux comptes pour la même personne : le doublon désactivé et jamais
    utilisé n'apparaît pas ; un pompiste parti sans doublon reste proposé."""
    a = client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": "Rose", "nom": "Doublon"}).json()
    b = client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": "rose", "nom": "doublon"}).json()
    assert client.patch(f"/api/pompiste-supervision/comptes/{b['id']}", headers=H, json={"actif": False}).status_code == 200
    seul = client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": "Parti", "nom": "Sansdoublon"}).json()
    client.patch(f"/api/pompiste-supervision/comptes/{seul['id']}", headers=H, json={"actif": False})
    noms = [p["nom"].lower() for p in client.get("/api/pompistes?inclure_inactifs=true", headers=H).json()]
    assert noms.count("rose doublon") == 1 and "parti sansdoublon" in noms


def test_supprimer_releves_et_rapport_mal_saisis(client):
    """Un relevé (ou tout un rapport) mal saisi se supprime avec un motif :
    la place est libérée et peut être ressaisie ; un rapport pompiste, même
    finalisé, se supprime avec ses relevés et ses dépenses."""
    from tz_utils import today_haiti
    import main as _m
    jour = str(today_haiti())
    pompe = _produit(client, 500)
    r = client.post("/api/releves", headers=H, json={"date": jour, "periode": "Matin", "pompe_id": pompe, "prix_gallon": 500,
                                                     "metter_avant": 0, "metter_apres": 999})
    rid = r.json()["id"]
    assert client.post("/api/releves/supprimer", headers=H, json={"ids": [rid], "motif": ""}).status_code == 400
    assert client.post("/api/releves/supprimer", headers=H, json={"ids": [rid], "motif": "Index faux tapé"}).json()["supprimes"] == 1
    assert client.post("/api/releves/supprimer", headers=H, json={"ids": [rid], "motif": "Index faux tapé"}).status_code == 404
    # La place est libre : nouvelle saisie possible
    r = client.post("/api/releves", headers=H, json={"date": jour, "periode": "Matin", "pompe_id": pompe, "prix_gallon": 500,
                                                     "metter_avant": 0, "metter_apres": 10})
    assert r.status_code in (200, 201), r.text
    assert client.post("/api/releves/supprimer", headers=H, json={"ids": [r.json()["id"]], "motif": "nettoyage test"}).status_code == 200
    # Rapport pompiste envoyé (avec une dépense) puis supprimé
    c = client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": "Sup", "nom": "Prime"}).json()
    p = TestClient(_m.app)
    p.post("/api/login/pompiste", json={"username": c["username"], "code_acces": c["code_acces"]})
    assert p.post("/api/pompiste/compteurs", json={"date": jour, "charte_acceptee": True,
              "lignes": [{"pompe_id": pompe, "metter_avant": 0, "metter_apres": 20, "debut_confirme": True}]}).status_code == 201
    rap = p.post("/api/pompiste/terminer", json={"billetterie": [{"coupure": 1000, "billets": 10}],
                 "depenses": [{"description": "Glace test", "montant": 100, "autorise_par": "Gérant"}]}).json()
    # Ses relevés au journal ne se suppriment pas un par un
    from database import SessionLocal
    from models import Releve, RapportPompiste
    with SessionLocal() as s:
        lies = [x.id for x in s.query(Releve).filter_by(rapport_pompiste_id=rap["id"]).all()]
    if lies:
        assert client.post("/api/releves/supprimer", headers=H, json={"ids": lies, "motif": "essai direct"}).status_code == 409
    assert client.post(f"/api/pompiste-supervision/rapports/{rap['id']}/supprimer", headers=H, json={"motif": "x"}).status_code == 400
    r = client.post(f"/api/pompiste-supervision/rapports/{rap['id']}/supprimer", headers=H, json={"motif": "Rapport mal rempli"})
    assert r.status_code == 200, r.text
    with SessionLocal() as s:
        gardé = s.get(RapportPompiste, rap["id"])
        assert gardé is not None and gardé.supprime_at is not None          # corbeille, pas effacé
        assert not s.query(Releve).filter_by(rapport_pompiste_id=rap["id"]).count()
    liste = lambda st: [x["id"] for x in client.get(f"/api/pompiste-supervision/rapports?statut={st}", headers=H).json()]
    assert rap["id"] not in liste("SOUMIS,PROBLEME,CONFIRME,RESOLU,REJETE") and rap["id"] in liste("SUPPRIME")
    sup = next(x for x in client.get("/api/pompiste-supervision/rapports?statut=SUPPRIME", headers=H).json() if x["id"] == rap["id"])
    assert sup["supprime"]["motif"] == "Rapport mal rempli" and sup["supprime"]["statut_avant"] == "SOUMIS"
    # Supprimé : plus de décision possible, plus en attente au journal
    assert client.post(f"/api/pompiste-supervision/rapports/{rap['id']}/depenses/0/confirmer", headers=H, json={}).status_code == 409
    j = client.get(f"/api/journal?date_debut={jour}&date_fin={jour}", headers=H).json()
    assert not [e for e in j["entries"] if e.get("rapport_pompiste_id") == rap["id"]]
    # Restaurer : il revient « à vérifier », tout est à reconfirmer
    r = client.post(f"/api/pompiste-supervision/rapports/{rap['id']}/restaurer", headers=H)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["statut"] == "SOUMIS" and d["supprime"] is None and not any(l["confirmee"] for l in d["lignes"])
    assert d["depenses"][0]["statut"] == "en_attente"
    assert client.post(f"/api/pompiste-supervision/rapports/{rap['id']}/restaurer", headers=H).status_code == 404
    assert _sup(client, rap["id"], f"lignes/{pompe}/confirmer", metter_avant=0, metter_apres=20).status_code == 200
    assert client.post(f"/api/pompiste-supervision/rapports/{rap['id']}/supprimer", headers=H, json={"motif": "Rapport mal rempli"}).status_code == 200
    # Les pompes sont libérées : le pompiste peut refaire son rapport
    assert p.post("/api/pompiste/compteurs", json={"date": jour, "charte_acceptee": True,
              "lignes": [{"pompe_id": pompe, "metter_avant": 0, "metter_apres": 20, "debut_confirme": True}]}).status_code == 201
    # … et l'ancien ne peut plus revenir sur ce créneau sans retirer le nouveau
    r = client.post(f"/api/pompiste-supervision/rapports/{rap['id']}/restaurer", headers=H)
    assert r.status_code == 409 and "déjà rapporté" in r.json()["detail"]


def test_metre_confirme_entre_dans_la_saisie(client):
    """Dès que la direction confirme un mètre d'un rapport pompiste, il est
    dans la Saisie (journal), attribué au pompiste ; « reprendre » l'en retire ;
    le nettoyage du démarrage le garde."""
    from tz_utils import today_haiti
    import main as _m
    jour = str(today_haiti())
    pompe = _produit(client, 500)
    c = client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": "Con", "nom": "Firme"}).json()
    p = TestClient(_m.app)
    p.post("/api/login/pompiste", json={"username": c["username"], "code_acces": c["code_acces"]})
    assert p.post("/api/pompiste/compteurs", json={"date": jour, "charte_acceptee": True,
              "lignes": [{"pompe_id": pompe, "metter_avant": 0, "metter_apres": 40, "debut_confirme": True}]}).status_code == 201
    rap = p.post("/api/pompiste/terminer", json={"billetterie": [{"coupure": 1000, "billets": 20}]}).json()
    dans_saisie = lambda: [x for x in client.get(f"/api/releves?date={jour}", headers=H).json() if x["pompe_id"] == pompe]
    assert dans_saisie() == []                                     # soumis, pas encore confirmé
    r = client.post(f"/api/pompiste-supervision/rapports/{rap['id']}/lignes/{pompe}/confirmer", headers=H,
                    json={"metter_avant": 0, "metter_apres": 40})
    assert r.status_code == 200 and not any("non écrit" in a for a in r.json()["avertissements"]), r.text
    x = dans_saisie()
    assert len(x) == 1 and float(x[0]["metter_apres"]) == 40 and x[0]["pompiste_nom"] == "Con Firme" \
        and x[0]["rapport_pompiste_id"] == rap["id"]
    from database import SessionLocal
    from pompiste_routes import nettoyer_journal_anticipe
    with SessionLocal() as s:
        nettoyer_journal_anticipe(s)
    assert len(dans_saisie()) == 1                                 # gardé au redémarrage
    # Correction puis reprise
    client.post(f"/api/pompiste-supervision/rapports/{rap['id']}/lignes/{pompe}/confirmer", headers=H,
                json={"metter_avant": 0, "metter_apres": 38})
    assert float(dans_saisie()[0]["metter_apres"]) == 38
    assert client.post(f"/api/pompiste-supervision/rapports/{rap['id']}/lignes/{pompe}/reprendre", headers=H).status_code == 200
    assert dans_saisie() == []


def test_pompe_dans_un_rapport_en_attente_indisponible_en_saisie(client):
    """Rapport pompiste envoyé : ses pompes sont prises pour ce créneau — la
    Saisie les refuse (et les montre « en attente ») jusqu'à la confirmation
    du mètre ou la suppression du rapport."""
    from tz_utils import today_haiti
    import main as _m
    jour = str(today_haiti())
    pompe = _produit(client, 500)
    c = client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": "Pla", "nom": "Ceprise"}).json()
    p = TestClient(_m.app)
    p.post("/api/login/pompiste", json={"username": c["username"], "code_acces": c["code_acces"]})
    assert p.post("/api/pompiste/compteurs", json={"date": jour, "charte_acceptee": True,
              "lignes": [{"pompe_id": pompe, "metter_avant": 0, "metter_apres": 40, "debut_confirme": True}]}).status_code == 201
    rap = p.post("/api/pompiste/terminer", json={"billetterie": [{"coupure": 1000, "billets": 20}]}).json()
    att = client.get(f"/api/releves/en-attente?date={jour}", headers=H).json()
    assert any(x["pompe_id"] == pompe and x["rapport_id"] == rap["id"] and x["pompiste"] == "Pla Ceprise" for x in att)
    saisie = lambda: client.post("/api/releves", headers=H, json={"date": jour, "periode": rap["periode"], "pompe_id": pompe,
                                                                  "prix_gallon": 500, "metter_avant": 0, "metter_apres": 41})
    r = saisie()
    assert r.status_code == 409 and r.json()["detail"]["code"] == "dans_un_rapport", r.text
    # Mètre confirmé : il est au journal, plus « en attente » ; la Saisie peut le modifier
    assert client.post(f"/api/pompiste-supervision/rapports/{rap['id']}/lignes/{pompe}/confirmer", headers=H,
                       json={"metter_avant": 0, "metter_apres": 40}).status_code == 200
    assert not any(x["pompe_id"] == pompe for x in client.get(f"/api/releves/en-attente?date={jour}", headers=H).json())
    assert saisie().status_code in (200, 201)
    # Rapport supprimé : place libre
    assert client.post(f"/api/pompiste-supervision/rapports/{rap['id']}/supprimer", headers=H, json={"motif": "test place"}).status_code == 200
    assert not any(x["pompe_id"] == pompe for x in client.get(f"/api/releves/en-attente?date={jour}", headers=H).json())
