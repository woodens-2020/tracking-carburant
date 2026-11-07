"""
Intelligence de placement des mètres : index écrits sur la mauvaise ligne
(91 sur la ligne du 95, etc.). Le moteur retrouve le bon mètre par la chaîne
des index, place toutes les lignes ensemble, ne mélange jamais gazoline et
diesel sans preuve, applique le placement d'un geste et retient l'erreur.
"""
import time
from datetime import timedelta

from fastapi.testclient import TestClient

H = {"X-API-Key": "smoke-test-key"}


def _produit(client, nom, prix, metres):
    r = client.post("/api/produits", headers=H, json={"nom": nom, "prix_gallon": prix})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    return [client.post(f"/api/produits/{pid}/pompes", headers=H, json={"nom": m}).json()["id"] for m in metres]


def _releve(client, d, per, pompe, av, ap, motif=None):
    body = {"date": d, "periode": per, "pompe_id": pompe, "prix_gallon": 500, "metter_avant": av, "metter_apres": ap}
    if motif:
        body["motif_ecart_compteur"] = motif
    r = client.post("/api/releves", headers=H, json=body)
    assert r.status_code in (200, 201), r.text
    return r.json()


def _sup(client, rid, action, **body):
    return client.post(f"/api/pompiste-supervision/rapports/{rid}/{action}", headers=H, json=body)


def test_unitaires():
    from placement_metres import _meilleur_placement, faute_de_frappe
    assert faute_de_frappe(224503.255, 224503.355) and faute_de_frappe(1234.567, 1243.567)
    assert not faute_de_frappe(1000.0, 1000.0) and not faute_de_frappe(1000.0, 2100.0)
    # Deux lignes veulent le même mètre : la plus sûre l'obtient
    assert _meilleur_placement([[(1, 0), (3, 100)], [(2, 0), (3, 60)]]) == [3, 2]
    # Rotation 1→2→3→1
    assert _meilleur_placement([[(1, 0), (2, 100)], [(2, 0), (3, 100)], [(3, 0), (1, 100)]]) == [2, 3, 1]


def test_rapport_lignes_croisees_91_95(client):
    import main as _m
    from tz_utils import today_haiti
    hier, jour = str(today_haiti() - timedelta(days=3)), str(today_haiti() - timedelta(days=2))
    ns = time.time_ns()
    g95a, g95b, g91a, g91b = _produit(client, f"Gazoline T{ns}", 500, ["G__95", "G_95", "G__91", "G_91"])
    (dsl,) = _produit(client, f"Diesel T{ns}", 600, ["D1"])
    for p, fin in ((g95a, 1000), (g95b, 2000), (g91a, 3000), (g91b, 4000), (dsl, 3000)):
        _releve(client, hier, "Apres-midi", p, 0, fin)
    c = client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": "Mi", "nom": "Reille"}).json()
    p = TestClient(_m.app)
    p.post("/api/login/pompiste", json={"username": c["username"], "code_acces": c["code_acces"]})
    # Les 91 écrits sur les lignes des 95, et inversement
    lignes = [(g95a, 3000, 3200), (g91a, 1000, 1005), (g95b, 4000, 4300), (g91b, 2000, 2002)]
    r = p.post("/api/pompiste/compteurs", json={"date": jour, "periode": "Matin", "charte_acceptee": True, "lignes": [
        {"pompe_id": a, "metter_avant": b, "metter_apres": e, "debut_confirme": True} for a, b, e in lignes]})
    assert r.status_code == 201, r.text
    rap = p.post("/api/pompiste/terminer", json={"billetterie": [{"coupure": 1000, "billets": 260}], "depenses": []}).json()
    rid = rap["id"]
    pl = client.get(f"/api/pompiste-supervision/rapports/{rid}/placement", headers=H).json()
    plan = {x["pompe_id"]: x["vers"] for x in pl["plan"]}
    # 3000 suit aussi le diesel, mais une gazoline reste en gazoline
    assert plan == {g95a: g91a, g91a: g95a, g95b: g91b, g91b: g95b}, pl
    assert all(x["confiance"] == "haute" and x["echange"] and not x["autre_carburant"] for x in pl["propositions"])
    # La liste des rapports porte le placement et la suggestion ligne par ligne
    d = next(x for x in client.get("/api/pompiste-supervision/rapports?statut=SOUMIS", headers=H).json() if x["id"] == rid)
    assert len(d["placement"]["propositions"]) == 4
    assert next(x for x in d["lignes"] if x["pompe_id"] == g95a)["pompe_suggeree"]["pompe_id"] == g91a
    # Appliquer d'un geste
    assert _sup(client, rid, "reclasser", motif="x").status_code == 400
    r = _sup(client, rid, "reclasser", motif="Index écrits sur la mauvaise ligne")
    assert r.status_code == 200, r.text
    L = {x["pompe_id"]: x for x in r.json()["lignes"]}
    assert (L[g91a]["metter_avant"], L[g91a]["metter_apres"]) == (3000, 3200)
    assert (L[g95a]["metter_avant"], L[g95a]["metter_apres"]) == (1000, 1005)
    assert (L[g91b]["metter_avant"], L[g95b]["metter_avant"]) == (4000, 2000)
    assert L[g91a]["pompe_saisie"]["pompe_id"] == g95a and not L[g91a]["confirmee"]
    assert all("ecart_compteur" not in x for x in L.values()) and r.json()["placement"] is None
    # Mémoire : l'erreur est retenue pour ce pompiste
    m = client.get("/api/placement-metres/memoire", headers=H).json()["erreurs"]
    a_lui = [x for x in m if x["pompiste"] == "Mi Reille"]
    assert {(x["saisi_sur"].split(" · ")[1], x["bon_metre"].split(" · ")[1]) for x in a_lui} == \
        {("G__95", "G__91"), ("G__91", "G__95"), ("G_95", "G_91"), ("G_91", "G_95")}
    # Retour en arrière : la mémoire se décompte
    r = _sup(client, rid, "reclasser", motif="Annulation du placement",
             plan=[{"pompe_id": g91a, "vers": g95a}, {"pompe_id": g95a, "vers": g91a}])
    assert r.status_code == 200, r.text
    m = client.get("/api/placement-metres/memoire", headers=H).json()["erreurs"]
    assert not [x for x in m if x["pompiste"] == "Mi Reille" and x["saisi_sur"].endswith("G__95")]
    # Deux lignes sur un même mètre : refusé
    r = _sup(client, rid, "reclasser", motif="mauvais plan", plan=[{"pompe_id": g95a, "vers": g91b}])
    assert r.status_code == 409


def test_propane_et_diesel_jamais_sans_preuve(client):
    import main as _m
    from tz_utils import today_haiti
    hier, jour = str(today_haiti() - timedelta(days=5)), str(today_haiti() - timedelta(days=4))
    ns = time.time_ns()
    (g1,) = _produit(client, f"Gazoline U{ns}", 500, ["G91"])
    (d1,) = _produit(client, f"Diesel U{ns}", 600, ["D1"])
    (pr,) = _produit(client, f"Propane U{ns}", 300, ["P1"])
    for p, fin in ((g1, 1000), (d1, 5000), (pr, 7000)):
        _releve(client, hier, "Apres-midi", p, 0, fin)
    c = client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": "Pro", "nom": "Pane"}).json()
    p = TestClient(_m.app)
    p.post("/api/login/pompiste", json={"username": c["username"], "code_acces": c["code_acces"]})
    # Début du propane sur la gazoline ; début du diesel à un chiffre près sur la gazoline
    r = p.post("/api/pompiste/compteurs", json={"date": jour, "periode": "Matin", "charte_acceptee": True, "lignes": [
        {"pompe_id": g1, "metter_avant": 7000, "metter_apres": 7010, "debut_confirme": True}]})
    assert r.status_code == 201, r.text
    rap = p.post("/api/pompiste/terminer", json={"billetterie": [{"coupure": 1000, "billets": 5}], "depenses": []}).json()
    pl = client.get(f"/api/pompiste-supervision/rapports/{rap['id']}/placement", headers=H).json()
    assert pl["plan"] == []                                   # propane jamais proposé
    r = _sup(client, rap["id"], "reclasser", motif="essai propane", plan=[{"pompe_id": g1, "vers": pr}])
    assert r.status_code == 400
    r = _sup(client, rap["id"], f"lignes/{g1}/changer-pompe", pompe_id=pr, motif="essai propane")
    assert r.status_code == 400


def test_journal_index_croises_et_deplacement(client):
    from tz_utils import today_haiti
    hier, jour = str(today_haiti() - timedelta(days=8)), str(today_haiti() - timedelta(days=7))
    ns = time.time_ns()
    g95, g91 = _produit(client, f"Gazoline J{ns}", 500, ["J95", "J91"])
    _releve(client, hier, "Apres-midi", g95, 0, 1000)
    _releve(client, hier, "Apres-midi", g91, 0, 3000)
    a = _releve(client, jour, "Matin", g95, 3000, 3150, motif="feuille du pompiste")
    b = _releve(client, jour, "Matin", g91, 1000, 1002, motif="feuille du pompiste")
    j = client.get(f"/api/journal?date_debut={jour}&date_fin={jour}", headers=H).json()["entries"]
    e = next(x for x in j if x["id"] == a["id"])
    assert e["metre_probable"]["pompe_id"] == g91 and e["metre_probable"]["echange"], e
    assert "mauvaise ligne" in e["recommandation"]
    assert client.post(f"/api/releves/{a['id']}/deplacer", headers=H, json={"pompe_id": g91, "motif": "x"}).status_code == 400
    r = client.post(f"/api/releves/{a['id']}/deplacer", headers=H, json={"pompe_id": g91, "motif": "Lignes 91 et 95 inversées"})
    assert r.status_code == 200 and r.json()["echange"], r.text
    j = client.get(f"/api/journal?date_debut={jour}&date_fin={jour}", headers=H).json()["entries"]
    mine = {x["pompe_id"]: x for x in j if x["pompe_id"] in (g95, g91)}
    assert (mine[g91]["metter_avant"], mine[g91]["metter_apres"]) == (3000, 3150)
    assert (mine[g95]["metter_avant"], mine[g95]["metter_apres"]) == (1000, 1002)
    assert all(x["type_anomalie"] == "ok" and "metre_probable" not in x for x in mine.values())
    # Ce que le pompiste avait écrit reste visible
    assert mine[g91]["saisie_pompiste"]["pompe_id"] == g95 and mine[g95]["saisie_pompiste"]["pompe_id"] == g91


def test_saisie_d_origine_conservee_et_registre(client):
    """Corriger n'efface jamais ce que le pompiste a écrit : le journal et le
    registre des erreurs montrent la saisie d'origine à côté du retenu."""
    import main as _m
    from tz_utils import today_haiti
    hier, jour = str(today_haiti() - timedelta(days=12)), str(today_haiti() - timedelta(days=11))
    ns = time.time_ns()
    g95, g91, g2 = _produit(client, f"Gazoline R{ns}", 500, ["R95", "R91", "R91b"])
    for p, fin in ((g95, 1000), (g91, 3000), (g2, 6000)):
        _releve(client, hier, "Apres-midi", p, 0, fin)
    # 1. Index corrigé dans la Saisie : l'ancien est gardé
    a = _releve(client, jour, "Matin", g2, 6000, 6400)
    _releve(client, jour, "Matin", g2, 6000, 6040)
    # 2. Rapport pompiste : 91 écrit sur le 95, remis en place puis finalisé
    c = client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": "Reg", "nom": "Istre"}).json()
    p = TestClient(_m.app)
    p.post("/api/login/pompiste", json={"username": c["username"], "code_acces": c["code_acces"]})
    assert p.post("/api/pompiste/compteurs", json={"date": jour, "periode": "Matin", "charte_acceptee": True, "lignes": [
        {"pompe_id": g95, "metter_avant": 3000, "metter_apres": 3100, "debut_confirme": True}]}).status_code == 201
    rap = p.post("/api/pompiste/terminer", json={"billetterie": [{"coupure": 1000, "billets": 50}], "depenses": []}).json()
    r = _sup(client, rap["id"], "reclasser", motif="91 écrit sur la ligne du 95")
    assert r.status_code == 200, r.text
    assert _sup(client, rap["id"], f"lignes/{g91}/confirmer", metter_avant=3000, metter_apres=3100).status_code == 200
    _sup(client, rap["id"], "billetterie", confirme=True); _sup(client, rap["id"], "ecart", confirme=True)
    r = _sup(client, rap["id"], "finaliser", note=7, commentaire="Ligne 95 au lieu de 91")
    assert r.status_code == 200, r.text
    j = {x["pompe_id"]: x for x in client.get(f"/api/journal?date_debut={jour}&date_fin={jour}", headers=H).json()["entries"]
         if x["pompe_id"] in (g95, g91, g2)}
    assert j[g2]["saisie_pompiste"]["index_corriges"] and j[g2]["saisie_pompiste"]["metter_apres"] == 6400
    assert j[g91]["saisie_pompiste"]["mauvais_metre"] and j[g91]["saisie_pompiste"]["pompe_id"] == g95
    assert g95 not in j                                           # plus rien sur le 95
    reg = client.get(f"/api/erreurs-metre?date_debut={jour}&date_fin={jour}", headers=H).json()
    mine = [x for x in reg["erreurs"] if x["retenu"]["libelle"].startswith(f"Gazoline R{ns}")]
    par = {x["retenu"]["libelle"].split(" · ")[1]: x for x in mine}
    assert par["R91b"]["types"] == ["index_corrige"] and par["R91b"]["ecart_gallons"] == -360
    assert par["R91"]["types"] == ["mauvais_metre"] and par["R91"]["saisi"]["libelle"].endswith("R95")
    assert par["R91"]["pompiste"] == "Reg Istre" and par["R91"]["statut"] == "corrige"
    st = next(x for x in reg["par_pompiste"] if x["pompiste"] == "Reg Istre")
    assert st["mauvais_metre"] == 1 and st["releves"] == 1 and st["taux_erreur"] == 100.0
