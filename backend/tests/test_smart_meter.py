"""
SMART METER INTELLIGENCE & RECONCILIATION ENGINE — les 17 scénarios de la
mission (docs/smart-meter-audit.md) :
rapprochement pendant la saisie, repli « meeter le plus proche », anomalies,
enregistrement malgré l'anomalie, statuts figés, décision du pompiste,
changement de compteur, doublons, correction par la direction, totaux
officiels non faussés, valeurs d'origine conservées.
"""
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

H = {"X-API-Key": "smoke-test-key"}
REF = {"P1": 125450.317, "P2": 238600.529, "P3": 310200.143, "P4": 175800.761}


def _produit(client, nom, prix, metres):
    pid = client.post("/api/produits", headers=H, json={"nom": nom, "prix_gallon": prix}).json()["id"]
    return [client.post(f"/api/produits/{pid}/pompes", headers=H, json={"nom": m}).json()["id"] for m in metres]


def _releve(client, d, per, pompe, av, ap):
    r = client.post("/api/releves", headers=H, json={"date": d, "periode": per, "pompe_id": pompe, "prix_gallon": 500,
                                                     "metter_avant": av, "metter_apres": ap,
                                                     "motif_ecart_compteur": "Mise en place du test Smart Meter"})
    assert r.status_code in (200, 201), r.text


def _pompiste(client):
    import main
    c = client.post("/api/pompiste-supervision/comptes", headers=H,
                    json={"prenom": "Smart", "nom": f"M{time.time_ns()}"}).json()
    p = TestClient(main.app)
    assert p.post("/api/login/pompiste", json={"username": c["username"], "code_acces": c["code_acces"]}).status_code == 200
    return p


@pytest.fixture(scope="module")
def st(client):
    """Station de test : 4 pistolets de gazoline et 1 de diesel, avec une
    référence fiable (journal, Après-midi de la veille)."""
    from tz_utils import today_haiti
    veille, jour = today_haiti() - timedelta(days=31), today_haiti() - timedelta(days=30)
    ns = time.time_ns()
    g = dict(zip(REF, _produit(client, f"Gazoline SM{ns}", 500, list(REF))))
    (dsl,) = _produit(client, f"Diesel SM{ns}", 600, ["D1"])
    for k, v in REF.items():
        _releve(client, str(veille), "Apres-midi", g[k], round(v - 100, 3), v)
    _releve(client, str(veille), "Apres-midi", dsl, REF["P2"] + 900, REF["P2"] + 1000)
    return {"g": g, "dsl": dsl, "jour": str(jour), "veille": str(veille)}


def _analyser(p, st, pompe, av, ap=None):
    r = p.post("/api/pompiste/analyser-index", json={"date": st["jour"], "pompe_id": pompe, "metter_avant": av,
                                                      "metter_apres": ap})
    assert r.status_code == 200, r.text
    return r.json()


def _envoyer(p, st, pompe, av, ap, **extra):
    return p.post("/api/pompiste/compteurs", json={"date": st["jour"], "charte_acceptee": True, "lignes": [
        {"pompe_id": pompe, "metter_avant": av, "metter_apres": ap, "debut_confirme": True, **extra}]})


# 1. Bon pistolet, bon meter
def test_01_bon_pistolet_bon_meter(client, st):
    p = _pompiste(client)
    a = _analyser(p, st, st["g"]["P1"], REF["P1"], REF["P1"] + 120)
    assert (a["statut_rapprochement"], a["confiance"]) == ("MATCH_CONFIRMED", "HIGH")
    assert a["choisi"]["dernier_meter"] == REF["P1"] and a["choisi"]["ecart"] == 0 and a["recommande"] is None
    assert a["choisi"]["reference_fiable"] and a["choisi"]["reference_statut"] == "validé"
    r = _envoyer(p, st, st["g"]["P1"], REF["P1"], REF["P1"] + 120)
    assert r.status_code == 201, r.text
    rp = r.json()["lignes"][0]["rapprochement"]
    assert rp["statut"] == "MATCH_CONFIRMED" and rp["anomalies"] == [] and rp["decision_pompiste"] == "AUCUNE"
    p.delete(f"/api/pompiste/compteurs/{st['g']['P1']}")


# 2. Mauvais pistolet, meter qui reprend exactement un autre pistolet
def test_02_mauvais_pistolet_meter_d_un_autre(client, st):
    p = _pompiste(client)
    a = _analyser(p, st, st["g"]["P1"], REF["P2"], REF["P2"] + 80)
    assert a["statut_rapprochement"] == "MATCH_REVIEW" and a["confiance"] == "HIGH"
    assert a["recommande"]["pompe_id"] == st["g"]["P2"] and a["recommande"]["ecart"] == 0
    codes = {x["code"] for x in a["anomalies"]}
    assert {"MAUVAIS_METEER", "ECART_POSITIF"} <= codes
    # Le diesel avec un index proche n'est jamais proposé pour de la gazoline
    assert all(c["pompe_id"] != st["dsl"] for c in a["candidats"])


# 3. Plusieurs candidats proches
def test_03_plusieurs_candidats_proches(client, st):
    from tz_utils import today_haiti
    ns = time.time_ns()
    e, f = _produit(client, f"Gazoline SMX{ns}", 500, ["E", "F"])
    _releve(client, st["veille"], "Apres-midi", e, 4000.111, 5000.111)
    _releve(client, st["veille"], "Apres-midi", f, 4001.222, 5001.222)
    p = _pompiste(client)
    a = _analyser(p, st, e, 5000.700)
    assert a["ambigu"] is True and {c["pompe_id"] for c in a["candidats"][:2]} == {e, f}
    assert all(c["niveau"] == "MEDIUM" for c in a["candidats"][:2])


# 4 et 5. Aucune correspondance fiable → meeter numériquement le plus proche
def test_04_05_aucune_correspondance_repli_plus_proche(client, st):
    p = _pompiste(client)
    a = _analyser(p, st, st["g"]["P1"], REF["P2"] + 350)
    assert a["statut_rapprochement"] == "UNMATCHED" and a["confiance"] == "NONE" and a["recommande"] is None
    assert a["plus_proche"]["pompe_id"] == st["g"]["P2"] and a["plus_proche"]["ecart"] == 350
    assert "référence numérique la plus proche" in a["message"] and "vérification" in a["message"]
    # Le pompiste peut quand même enregistrer ; tout est conservé
    r = _envoyer(p, st, st["g"]["P1"], REF["P2"] + 350, REF["P2"] + 400)
    assert r.status_code == 201, r.text
    rp = r.json()["lignes"][0]["rapprochement"]
    assert rp["statut"] == "UNMATCHED" and rp["pompe_proposee"]["pompe_id"] == st["g"]["P2"]
    assert rp["index_reference"] == REF["P1"] and "ECART_POSITIF" in rp["anomalies"]
    p.delete(f"/api/pompiste/compteurs/{st['g']['P1']}")


# 6. Le pompiste confirme le candidat proposé
def test_06_pompiste_confirme_le_candidat(client, st):
    p = _pompiste(client)
    r = _envoyer(p, st, st["g"]["P2"], REF["P2"], REF["P2"] + 75, pompe_choisie_id=st["g"]["P1"], decision="ACCEPTE")
    assert r.status_code == 201, r.text
    l = r.json()["lignes"][0]
    assert l["pompe_id"] == st["g"]["P2"] and l["pompe_saisie"]["pompe_id"] == st["g"]["P1"]
    assert l["rapprochement"]["statut"] == "MATCH_CONFIRMED" and l["rapprochement"]["decision_pompiste"] == "ACCEPTE"
    # L'erreur est apprise pour ce pompiste
    m = client.get("/api/placement-metres/memoire", headers=H).json()["erreurs"]
    assert any(x["saisi_sur"].endswith("· P1") and x["bon_metre"].endswith("· P2") for x in m)
    p.delete(f"/api/pompiste/compteurs/{st['g']['P2']}")


# 7. Le pompiste refuse le candidat proposé : enregistré, signalé
def test_07_pompiste_refuse_le_candidat(client, st):
    p = _pompiste(client)
    r = _envoyer(p, st, st["g"]["P1"], REF["P2"], REF["P2"] + 75, decision="REFUSE")
    assert r.status_code == 201, r.text
    rp = r.json()["lignes"][0]["rapprochement"]
    assert rp["statut"] == "MATCH_REVIEW" and rp["decision_pompiste"] == "REFUSE"
    assert rp["pompe_proposee"]["pompe_id"] == st["g"]["P2"] and "MAUVAIS_METEER" in rp["anomalies"]
    p.delete(f"/api/pompiste/compteurs/{st['g']['P1']}")


# 8. Nouveau meter inférieur au précédent
def test_08_recul(client, st):
    p = _pompiste(client)
    a = _analyser(p, st, st["g"]["P4"], REF["P4"] - 10, REF["P4"] + 50)
    assert "RECUL" in {x["code"] for x in a["anomalies"]} and a["choisi"]["ecart"] == -10
    assert a["statut_rapprochement"] == "MATCH_REVIEW" and a["confiance"] == "LOW"


# 9. Écart anormalement élevé (au-dessus de la limite) : enregistré avec anomalie
def test_09_volume_eleve(client, st):
    p = _pompiste(client)
    a = _analyser(p, st, st["g"]["P3"], REF["P3"], REF["P3"] + 6000)
    assert "VOLUME_ELEVE" in {x["code"] for x in a["anomalies"]}
    assert a["statut_rapprochement"] == "MATCH_REVIEW" and a["confiance"] == "MEDIUM"
    assert _envoyer(p, st, st["g"]["P3"], REF["P3"], REF["P3"] + 6000).status_code == 201
    p.delete(f"/api/pompiste/compteurs/{st['g']['P3']}")


# 10. Meter identique (consommation nulle)
def test_10_volume_nul(client, st):
    p = _pompiste(client)
    a = _analyser(p, st, st["g"]["P3"], REF["P3"], REF["P3"])
    assert "VOLUME_NUL" in {x["code"] for x in a["anomalies"]} and a["volume"] == 0


# 11. Pistolet sans historique
def test_11_sans_historique(client, st):
    (h,) = _produit(client, f"Gazoline SMH{time.time_ns()}", 500, ["Neuf"])
    p = _pompiste(client)
    a = _analyser(p, st, h, 98765.432)
    assert a["statut_rapprochement"] == "INITIAL_READING" and a["choisi"]["dernier_meter"] is None
    assert "INITIAL_READING" in {x["code"] for x in a["anomalies"]}


# 12. Compteur remplacé
def test_12_compteur_remplace(client, st):
    from tz_utils import today_haiti
    (c,) = _produit(client, f"Gazoline SMC{time.time_ns()}", 500, ["Change"])
    veille = str(today_haiti() - timedelta(days=11))
    jour = today_haiti() - timedelta(days=10)
    _releve(client, veille, "Apres-midi", c, 88000.5, 88888.5)
    corps = {"pompe_id": c, "date_changement": str(jour), "ancien_numero": "SN-123", "nouveau_numero": "SN-456",
             "ancien_index_final": 88900.5, "nouvel_index_initial": 0, "motif": "Compteur grillé, remplacé",
             "autorise_par": "Directeur gaz"}
    p = _pompiste(client)
    assert p.post("/api/smart-meter/changements-compteur", json=corps).status_code == 403   # pompiste : non
    assert client.post("/api/smart-meter/changements-compteur", headers=H, json=dict(corps, motif="x")).status_code == 400
    r = client.post("/api/smart-meter/changements-compteur", headers=H, json=corps)
    assert r.status_code == 201, r.text
    a = p.post("/api/pompiste/analyser-index", json={"date": str(jour), "pompe_id": c, "metter_avant": 0,
                                                       "metter_apres": 40}).json()
    assert a["statut_rapprochement"] == "MATCH_CONFIRMED" and a["choisi"]["reference_statut"] == "point de départ du meeter"
    assert "COMPTEUR_CHANGE" in {x["code"] for x in a["anomalies"]}
    # Un relevé existe déjà au journal après la date : changement refusé
    assert client.post("/api/smart-meter/changements-compteur", headers=H,
                       json=dict(corps, date_changement=veille)).status_code == 409
    assert any(x["nouveau_numero"] == "SN-456" for x in
               client.get("/api/smart-meter/changements-compteur", headers=H).json())


# 13. Rapport soumis deux fois / 14. deux pompistes sur le même pistolet
def test_13_14_doublons(client, st):
    a, b = _pompiste(client), _pompiste(client)
    assert _envoyer(a, st, st["g"]["P4"], REF["P4"], REF["P4"] + 30).status_code == 201
    r = _envoyer(a, st, st["g"]["P4"], REF["P4"], REF["P4"] + 30)        # double soumission
    assert r.status_code == 409 and r.json()["detail"]["code"] == "deja_rapporte"
    r = _envoyer(b, st, st["g"]["P4"], REF["P4"], REF["P4"] + 30)        # autre pompiste, même créneau
    assert r.status_code in (201, 409)
    if r.status_code == 201:                                             # créneau suivant attribué automatiquement
        assert r.json()["periode"] != "Matin"
        b.delete(f"/api/pompiste/compteurs/{st['g']['P4']}")
    a.delete(f"/api/pompiste/compteurs/{st['g']['P4']}")


# 15. La direction corrige une association erronée ; 16. rapport en attente
# jamais compté comme vente validée ; 17. valeurs d'origine conservées
def test_15_16_17_correction_attente_valeurs_origine(client, st):
    p = _pompiste(client)
    r = _envoyer(p, st, st["g"]["P1"], REF["P2"], REF["P2"] + 60, decision="REFUSE")
    assert r.status_code == 201, r.text
    rap = p.post("/api/pompiste/terminer", json={"billetterie": [{"coupure": 1000, "billets": 30}], "depenses": []}).json()
    rid = rap["id"]
    # 16. En attente : rien au journal, rien dans les totaux officiels
    assert not [x for x in client.get(f"/api/releves?date={st['jour']}", headers=H).json()
                if x["pompe_id"] in (st["g"]["P1"], st["g"]["P2"])]
    c = client.get(f"/api/pompistes/classement?date_debut={st['jour']}&date_fin={st['jour']}", headers=H).json()
    assert not [d for x in c["pompistes"] for d in x["releves_detail"]
                if d["date"] == st["jour"] and d["meeter"].split(" · ")[-1] in ("P1", "P2")]
    rec = client.get(f"/api/smart-meter/reconciliation?date_debut={st['jour']}&date_fin={st['jour']}"
                     f"&statut_validation=EN_ATTENTE&anomalie=MAUVAIS_METEER", headers=H).json()
    l0 = next(x for x in rec["lignes"] if x["rapport_id"] == rid)
    assert l0["statut_rapprochement"] == "MATCH_REVIEW" and l0["meeter_propose"].endswith("· P2")
    # 15. Correction de l'association par la direction (outil existant)
    r = client.post(f"/api/pompiste-supervision/rapports/{rid}/lignes/{st['g']['P1']}/changer-pompe", headers=H,
                    json={"pompe_id": st["g"]["P2"], "motif": "Index lus sur le pistolet P2"})
    assert r.status_code == 200, r.text
    # Vérification notée à part, statut de saisie conservé
    assert client.post(f"/api/smart-meter/rapports/{rid}/lignes/{st['g']['P2']}/verifier", headers=H,
                       json={"note": ""}).status_code == 400
    assert client.post(f"/api/smart-meter/rapports/{rid}/lignes/{st['g']['P2']}/verifier", headers=H,
                       json={"note": "Vérifié sur place avec le pompiste"}).status_code == 200
    # 17. Correction des index : les valeurs saisies restent
    r = client.post(f"/api/pompiste-supervision/rapports/{rid}/lignes/{st['g']['P2']}/confirmer", headers=H,
                    json={"metter_avant": REF["P2"], "metter_apres": REF["P2"] + 55})
    assert r.status_code == 200, r.text
    rec = client.get(f"/api/smart-meter/reconciliation?date_debut={st['jour']}&date_fin={st['jour']}", headers=H).json()
    l1 = next(x for x in rec["lignes"] if x["rapport_id"] == rid)
    assert l1["meeter"].endswith("· P2") and l1["meeter_saisi"].endswith("· P1")
    assert l1["metter_apres_saisi"] == REF["P2"] + 60 and l1["metter_apres"] == REF["P2"] + 55
    assert l1["statut_rapprochement"] == "MATCH_REVIEW" and l1["note_verification"].startswith("Vérifié")
    assert l1["statut_validation"] == "VERIFIE"
    hist = client.get(f"/api/smart-meter/rapports/{rid}/historique", headers=H).json()
    assert {"rapprochement_verifie"} <= {h["action"] for h in hist}


def test_config_et_droits(client, st):
    p = _pompiste(client)
    assert p.get("/api/smart-meter/config").status_code == 403
    assert p.get("/api/smart-meter/reconciliation?date_debut=2026-01-01&date_fin=2026-01-02").status_code == 403
    c = client.get("/api/smart-meter/config", headers=H).json()
    assert client.put("/api/smart-meter/config", headers=H, json=dict(c, ecart_medium=100, ecart_low=10)).status_code == 400
    # Seuil faible élargi : l'écart de 350 devient LOW au lieu de « aucune correspondance »
    assert client.put("/api/smart-meter/config", headers=H, json=dict(c, ecart_low=400)).status_code == 200
    a = _analyser(p, st, st["g"]["P1"], REF["P3"] + 350)
    assert a["recommande"]["pompe_id"] == st["g"]["P3"] and a["confiance"] == "LOW"
    # Limite de volume propre à un meeter
    assert client.put("/api/smart-meter/config", headers=H,
                      json=dict(c, volumes_max={str(st["g"]["P3"]): 100})).status_code == 200
    a = _analyser(p, st, st["g"]["P3"], REF["P3"], REF["P3"] + 150)
    assert "VOLUME_ELEVE" in {x["code"] for x in a["anomalies"]} and a["volume_max"] == 100
    assert client.put("/api/smart-meter/config", headers=H, json=c).status_code == 200      # remise à l'état initial


def test_liste_dynamique_des_derniers_meeters(client, st):
    """Liste de la saisie : dernier meeter de chaque pompe, gazoline et diesel séparés."""
    p = _pompiste(client)
    r = p.get(f"/api/pompiste/derniers-meeters?date={st['jour']}")
    assert r.status_code == 200, r.text
    L = {x["pompe_id"]: x for x in r.json()["meeters"]}
    assert L[st["g"]["P3"]]["dernier_meter"] == REF["P3"] and L[st["g"]["P3"]]["famille"] == "Gazoline"
    assert L[st["dsl"]]["famille"] == "Diesel"
    fams = [x["famille"] for x in r.json()["meeters"]]
    assert fams == sorted(fams, key=lambda f: {"Gazoline": 0, "Diesel": 1, "Propane": 2}.get(f, 3))   # classés par carburant
    assert client.get(f"/api/pompiste/derniers-meeters?date={st['jour']}", headers=H).status_code == 403   # pompistes seulement
