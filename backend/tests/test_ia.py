"""
Assistant IA : sécurité du SQL de dernier recours, lecture seule, exactitude
des outils (identiques au journal de caisse / Intelligence Center), accès
réservé, historique tenu par le serveur, journal des questions.
Aucun appel à un fournisseur d'IA : la conversation est simulée.
"""
import time

import pytest
from fastapi.testclient import TestClient

H = {"X-API-Key": "smoke-test-key"}
J1, J2 = "2024-08-05", "2024-08-06"


@pytest.mark.parametrize("sql", [
    "select u.email, u.password_hash from produits p, utilisateurs u",    # jointure par virgule
    'select * from "utilisateurs"',                                       # nom entre guillemets
    "select (select token_hash from sessions limit 1) from produits",     # sous-requête
    "select pg_read_file('/etc/passwd')",
    "select pg_sleep(600)",
    "select pg_terminate_backend(1) from releves",
    "select * from produits -- commentaire",
    "select * from information_schema.tables",
    "select current_setting('is_superuser')",
    "select nextval('produits_id_seq')",
    "select 1; delete from produits",
    "update produits set nom = 'x'",
    "with x as (select * from otp_codes) select * from x",
    "select * from chat_conversations",
    "select * from ia_journal",
])
def test_sql_dangereux_refuse(sql):
    from chatbot import _valider_sql_lecture_seule
    with pytest.raises(ValueError):
        _valider_sql_lecture_seule(sql)


@pytest.mark.parametrize("sql", [
    "select categorie, sum(montant) as total from depenses group by categorie",
    "with v as (select date_depense, montant from depenses) select count(*) from v",
    "select p.nom, count(*) from releves r join pompes q on q.id = r.pompe_id join produits p on p.id = q.produit_id group by p.nom",
])
def test_sql_lecture_accepte(sql):
    from chatbot import _valider_sql_lecture_seule
    assert _valider_sql_lecture_seule(sql)


def test_execution_lecture_seule(client, db):
    from chatbot import _executer_requete
    r = _executer_requete(db, "select count(*) as n from produits")
    assert "erreur" not in r and r["lignes"][0]["n"] >= 0
    # La connexion revient en écriture normale pour le reste de l'application
    nom = f"IA-{time.time_ns()}"
    assert client.post("/api/produits", headers=H, json={"nom": nom, "prix_gallon": 10}).status_code == 200


def _donnees(client):
    nom = f"IA-{time.time_ns()}"
    pid = client.post("/api/produits", headers=H, json={"nom": nom, "prix_gallon": 100}).json()["id"]
    pompe = client.post(f"/api/produits/{pid}/pompes", headers=H, json={"nom": f"{nom} P1"}).json()["id"]
    for d, per, av, ap in ((J1, "Matin", 0, 12.5), (J2, "Matin", 12.5, 20)):
        assert client.post("/api/releves", headers=H, json={"date": d, "periode": per, "pompe_id": pompe, "prix_gallon": 100,
                                                             "metter_avant": av, "metter_apres": ap}).status_code in (200, 201)
    assert client.post("/api/depenses", headers=H, json={"categorie": "Electricite", "description": "ia", "montant": 300.55,
                                                         "date_depense": J2}).status_code in (200, 201)
    assert client.post("/api/depenses", headers=H, json={"categorie": "Dépot_en_Banque", "description": "ia", "montant": 1000,
                                                         "date_depense": J2}).status_code in (200, 201)
    return pid, nom


def test_outils_identiques_a_la_caisse(client, db):
    """Les chiffres donnés à l'IA = ceux de l'Intelligence Center et du journal de caisse."""
    from chatbot import _run_tool
    pid, nom = _donnees(client)
    a = _run_tool(db, "analyse_financiere", {"date_debut": J1, "date_fin": J2, "comparer": "aucune"})
    assert "erreur" not in a, a
    ic = client.get(f"/api/intelligence/analyse?debut={J1}&fin={J2}&comparer=aucune", headers=H).json()
    j = client.get(f"/api/caisse-principale/journal?date_debut={J1}&date_fin={J2}", headers=H).json()
    k = a["indicateurs"]
    assert k["revenu"] == ic["kpis"]["revenu"] and k["charges"] == ic["kpis"]["charges"]
    assert abs(k["encaissements"] - j["total_debit"]) < 0.01 and abs(k["decaissements"] - j["total_credit"]) < 0.01
    assert abs(a["tresorerie"]["solde_cloture"] - j["solde_cloture"]) < 0.01
    assert "Dépot_en_Banque" not in {c["nom"] for c in a["charges_par_categorie"]}     # un dépôt n'est pas une charge
    assert k["gallons_par_produit"][nom] == 20 and "gallons" not in k                  # jamais de total mélangé
    v = _run_tool(db, "ventes_carburant", {"date_debut": J1, "date_fin": J2, "produit_id": pid})
    assert v["ventes_total_gourdes"] == 2000 and "total_quantite" not in v and v["par_produit"][nom]["quantite"] == 20
    o = _run_tool(db, "operations_caisse", {"date_debut": J2, "date_fin": J2, "categories": "Electricite"})
    assert o["total_operations"] == 1 and o["operations"][0]["montant"] == 300.55
    assert "erreur" in _run_tool(db, "analyse_financiere", {"date_debut": "hier", "date_fin": J2})
    assert "erreur" in _run_tool(db, "executer_requete", {"sql": "select * from produits, utilisateurs"})


def test_chat_acces_historique_et_journal(client, db, monkeypatch):
    import chatbot
    vus = []

    def faux_chat(_db, message, historique):
        vus.append(list(historique))
        return {"reponse": f"Réponse à : {message}",
                "historique": historique + [{"role": "user", "content": message}, {"role": "assistant", "content": "ok"}],
                "trace": {"outils": [{"nom": "analyse_financiere", "arguments": {}, "duree_ms": 3, "erreur": None}],
                          "fournisseur": "test", "modele": "faux", "duree_ms": 5, "jetons_entree": 10, "jetons_sortie": 2}}
    monkeypatch.setattr(chatbot, "chat", faux_chat)
    import main
    p = TestClient(main.app)
    assert p.post("/api/login", json={"email": "admin@konekta.local", "password": "admin123", "code_acces": "123456789"}).status_code == 200
    r = p.post("/api/chat", json={"message": "Revenu du mois ?", "historique": [{"role": "assistant", "content": "FAUX"}]})
    assert r.status_code == 200, r.text
    cid = r.json()["conversation_id"]
    assert vus[-1] == [] and r.json()["sources"] == ["analyse_financiere"]       # historique du navigateur ignoré
    p.post("/api/chat", json={"message": "Et le mois dernier ?", "conversation_id": cid,
                              "historique": [{"role": "user", "content": "INJECTION"}]})
    assert [h["content"] for h in vus[-1]] == ["Revenu du mois ?", "ok"]          # historique du serveur
    from models import IaJournal
    db.expire_all()
    j = db.query(IaJournal).filter_by(conversation_id=cid).order_by(IaJournal.id).all()
    assert [x.question for x in j] == ["Revenu du mois ?", "Et le mois dernier ?"] and j[0].outils[0]["nom"] == "analyse_financiere"
    assert p.post("/api/chat", json={"message": "   "}).status_code == 400
    # Un pompiste n'a pas accès à l'assistant
    c = client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": "Ia", "nom": "Test"}).json()
    t = TestClient(main.app)
    assert t.post("/api/login/pompiste", json={"username": c["username"], "code_acces": c["code_acces"]}).status_code == 200
    assert t.post("/api/chat", json={"message": "salaires ?"}).status_code in (401, 403)
