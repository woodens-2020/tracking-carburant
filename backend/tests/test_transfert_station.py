"""
Transfert d'un rapport pompiste vers une autre station : même jour, même
période ; meeters proposés par le moteur dans la station cible ; l'origine
perd ses relevés et le rapport part à la corbeille, marqué transféré (jamais
restauré) ; la cible le reçoit « à vérifier » ; créneau déjà pris refusé ;
jamais d'autre carburant.
"""
import time
from datetime import timedelta

from fastapi.testclient import TestClient

H = {"X-API-Key": "smoke-test-key"}
PROPRIO = {"email": "admin@konekta.local", "password": "admin123", "code_acces": "123456789"}


def _c(cookie=None):
    import main
    c = TestClient(main.app)
    if cookie:
        c.cookies.set("konekta_espace", cookie)
    return c


def test_transfert_vers_une_autre_station(client):
    from tz_utils import today_haiti
    ns = time.time_ns()
    jour = str(today_haiti() - timedelta(days=5))
    p = _c()
    assert p.post("/api/login", json=PROPRIO).status_code == 200
    st = {"produits": [{"cle": "g", "nom": "Gazoline", "prix": 900}, {"cle": "d", "nom": "Diesel", "prix": 950}],
          "pompes": [{"nom": "Pompe 1", "faces": [{"nom": "A", "metres": [
              {"produit": "g", "nom": "Gazoline 1", "metre_depart": 5000.25}, {"produit": "g", "nom": "Gazoline 2", "metre_depart": 9000.5},
              {"produit": "d", "nom": "Diesel 1", "metre_depart": 5000.25}]}]}]}
    r = p.post("/api/espaces", json={"nom": f"Station TR{ns}", "adresse": "Ailleurs", "station": st,
                                     "gerant": {"nom_complet": "Gérant TR", "email": f"tr{ns}@exemple.ht",
                                                "password": "MotDePasse2026", "pin": "987654321"}})
    assert r.status_code == 201, r.text
    eid = r.json()["id"]
    cible = _c(f"esp_{eid}")
    B = {q["nom"]: q["id"] for x in cible.get("/api/produits", headers=H).json() for q in x["pompes"]}
    # Station principale : le pompiste rapporte (par erreur) l'index du « Gazoline 1 » de l'autre station
    pid = client.post("/api/produits", headers=H, json={"nom": f"Gazoline TRP{ns}", "prix_gallon": 800}).json()["id"]
    (gp,) = [client.post(f"/api/produits/{pid}/pompes", headers=H, json={"nom": "GP"}).json()["id"]]
    c = client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": "Tran", "nom": f"Sfer{ns}"}).json()
    pp = _c()
    assert pp.post("/api/login/pompiste", json={"username": c["username"], "code_acces": c["code_acces"]}).status_code == 200
    r = pp.post("/api/pompiste/compteurs", json={"date": jour, "charte_acceptee": True, "lignes": [
        {"pompe_id": gp, "metter_avant": 5000.25, "metter_apres": 5050.25, "debut_confirme": True}]})
    assert r.status_code == 201, r.text
    rap = pp.post("/api/pompiste/terminer", json={"billetterie": [{"coupure": 1000, "billets": 45}], "depenses": [
        {"description": "Glace", "montant": 500, "autorise_par": "Chef"}]}).json()
    rid, periode = rap["id"], rap["periode"]
    # Aperçu : le moteur propose le « Gazoline 1 » de l'autre station (suite exacte), jamais le diesel
    ap = p.get(f"/api/pompiste-supervision/rapports/{rid}/transfert/apercu?espace_id={eid}")
    assert ap.status_code == 200, ap.text
    l0 = ap.json()["lignes"][0]
    assert l0["proposition"] == B["Gazoline 1"] and all(x["pompe_id"] != B["Diesel 1"] for x in l0["candidats"])
    assert ap.json()["bloquants"] == []
    # Diesel refusé, motif obligatoire
    assert p.post(f"/api/pompiste-supervision/rapports/{rid}/transfert",
                  json={"espace_id": eid, "motif": "Mauvaise station", "lignes": {str(gp): B["Diesel 1"]}}).status_code == 400
    assert p.post(f"/api/pompiste-supervision/rapports/{rid}/transfert",
                  json={"espace_id": eid, "motif": "x", "lignes": {str(gp): B["Gazoline 1"]}}).status_code == 400
    r = p.post(f"/api/pompiste-supervision/rapports/{rid}/transfert",
               json={"espace_id": eid, "motif": "Le pompiste s'est trompé de station", "lignes": {str(gp): B["Gazoline 1"]}})
    assert r.status_code == 200, r.text
    nid = r.json()["rapport_id"]
    # Origine : à la corbeille, marqué transféré, jamais restauré ; plus rien à traiter
    sup = next(x for x in client.get("/api/pompiste-supervision/rapports?statut=SUPPRIME", headers=H).json() if x["id"] == rid)
    assert sup["supprime"]["transfere_vers"]["rapport_id"] == nid
    assert client.post(f"/api/pompiste-supervision/rapports/{rid}/restaurer", headers=H).status_code == 409
    assert p.post(f"/api/pompiste-supervision/rapports/{rid}/transfert",
                  json={"espace_id": eid, "motif": "deuxième fois", "lignes": {str(gp): B["Gazoline 1"]}}).status_code == 409
    # Cible : même jour, même période, à vérifier, au prix de la station cible, dépense à décider
    rb = next(x for x in cible.get("/api/pompiste-supervision/rapports?statut=SOUMIS", headers=H).json() if x["id"] == nid)
    assert (rb["date"], rb["periode"], rb["statut"]) == (jour, periode, "SOUMIS")
    assert rb["lignes"][0]["pompe_id"] == B["Gazoline 1"] and rb["montant_attendu"] == 50 * 900
    assert rb["depenses"][0]["statut"] == "en_attente" and rb["montant_compte"] == 45000
    assert rb["lignes"][0]["rapprochement"]["statut"] == "MATCH_CONFIRMED"
    # Un autre rapport du même créneau ne peut pas aller sur ce meeter déjà rapporté
    c2 = client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": "Deux", "nom": f"Ieme{ns}"}).json()
    (gp2,) = [client.post(f"/api/produits/{pid}/pompes", headers=H, json={"nom": "GP2"}).json()["id"]]
    p2 = _c()
    p2.post("/api/login/pompiste", json={"username": c2["username"], "code_acces": c2["code_acces"]})
    assert p2.post("/api/pompiste/compteurs", json={"date": jour, "periode": periode, "charte_acceptee": True, "lignes": [
        {"pompe_id": gp2, "metter_avant": 5050.25, "metter_apres": 5060.25, "debut_confirme": True}]}).status_code == 201
    rid2 = p2.post("/api/pompiste/terminer", json={"billetterie": [{"coupure": 1000, "billets": 8}], "depenses": []}).json()["id"]
    l = p.get(f"/api/pompiste-supervision/rapports/{rid2}/transfert/apercu?espace_id={eid}").json()["lignes"][0]
    assert next(x for x in l["candidats"] if x["pompe_id"] == B["Gazoline 1"])["prise"] is True
    assert l["proposition"] == B["Gazoline 2"]
    assert p.post(f"/api/pompiste-supervision/rapports/{rid2}/transfert",
                  json={"espace_id": eid, "motif": "Mauvaise station", "lignes": {str(gp2): B["Gazoline 1"]}}).status_code == 409
    # « Transférer quand même » : il arrive en problème, la confirmation est bloquée tant que le conflit existe
    r = p.post(f"/api/pompiste-supervision/rapports/{rid2}/transfert", json={"espace_id": eid, "motif": "Mauvaise station",
               "lignes": {str(gp2): B["Gazoline 1"]}, "forcer": True})
    assert r.status_code == 200, r.text
    nid2 = r.json()["rapport_id"]
    assert r.json()["conflits"] and "Gazoline 1" in r.json()["conflits"][0]
    rb2 = next(x for x in cible.get("/api/pompiste-supervision/rapports?statut=PROBLEME", headers=H).json() if x["id"] == nid2)
    assert rb2["statut"] == "PROBLEME" and rb2["conflits"] and "Transféré" in rb2["probleme_commentaire"]
    rc = cible.post(f"/api/pompiste-supervision/rapports/{nid2}/lignes/{B['Gazoline 1']}/confirmer", headers=H,
                    json={"metter_avant": 5050.25, "metter_apres": 5060.25})
    assert rc.status_code == 409 and "déjà rapporté" in rc.json()["detail"]
    # Réglé là-bas : la ligne passe sur le meeter libre, la confirmation redevient possible
    assert cible.post(f"/api/pompiste-supervision/rapports/{nid2}/lignes/{B['Gazoline 1']}/changer-pompe", headers=H,
                      json={"pompe_id": B["Gazoline 2"], "motif": "Conflit réglé : c'était Gazoline 2"}).status_code == 200
    assert cible.post(f"/api/pompiste-supervision/rapports/{nid2}/lignes/{B['Gazoline 2']}/confirmer", headers=H,
                      json={"metter_avant": 5050.25, "metter_apres": 5060.25}).status_code == 200
    # Le gérant d'une station ne transfère pas (réservé au propriétaire)
    g = _c()
    g.post("/api/login", json={"email": f"tr{ns}@exemple.ht", "password": "MotDePasse2026", "code_acces": "987654321"})
    assert g.get(f"/api/pompiste-supervision/rapports/{nid}/transfert/apercu?espace_id=0").status_code == 403


def test_deux_lignes_jamais_sur_le_meme_meeter(client):
    """Deux lignes de gazoline proches du même meeter cible : l'aperçu propose
    deux meeters différents, et le transfert passe tel quel."""
    from tz_utils import today_haiti
    ns = time.time_ns()
    jour = str(today_haiti() - timedelta(days=6))
    p = _c()
    assert p.post("/api/login", json=PROPRIO).status_code == 200
    st = {"produits": [{"cle": "g", "nom": "Gazoline", "prix": 900}],
          "pompes": [{"nom": "Pompe 1", "faces": [{"nom": "A", "metres": [
              {"produit": "g", "nom": "Gazoline 1", "metre_depart": 7000.25}, {"produit": "g", "nom": "Gazoline 2", "metre_depart": 9100.5}]}]}]}
    r = p.post("/api/espaces", json={"nom": f"Station DL{ns}", "adresse": "Ailleurs", "station": st,
                                     "gerant": {"nom_complet": "Gérant DL", "email": f"dl{ns}@exemple.ht",
                                                "password": "MotDePasse2026", "pin": "987654321"}})
    assert r.status_code == 201, r.text
    eid = r.json()["id"]
    B = {q["nom"]: q["id"] for x in _c(f"esp_{eid}").get("/api/produits", headers=H).json() for q in x["pompes"]}
    pid = client.post("/api/produits", headers=H, json={"nom": f"Gazoline DLP{ns}", "prix_gallon": 800}).json()["id"]
    g1, g2 = [client.post(f"/api/produits/{pid}/pompes", headers=H, json={"nom": n}).json()["id"] for n in ("A", "B")]
    c = client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": "Deux", "nom": f"Lignes{ns}"}).json()
    pp = _c()
    pp.post("/api/login/pompiste", json={"username": c["username"], "code_acces": c["code_acces"]})
    assert pp.post("/api/pompiste/compteurs", json={"date": jour, "charte_acceptee": True, "lignes": [
        {"pompe_id": g1, "metter_avant": 7000.25, "metter_apres": 7050.25, "debut_confirme": True},
        {"pompe_id": g2, "metter_avant": 7000.75, "metter_apres": 7020.75, "debut_confirme": True}]}).status_code == 201
    rid = pp.post("/api/pompiste/terminer", json={"billetterie": [{"coupure": 1000, "billets": 50}], "depenses": []}).json()["id"]
    ap = p.get(f"/api/pompiste-supervision/rapports/{rid}/transfert/apercu?espace_id={eid}").json()
    props = {l["pompe_id"]: l["proposition"] for l in ap["lignes"]}
    assert props[g1] == B["Gazoline 1"] and props[g2] == B["Gazoline 2"], ap        # suite exacte servie d'abord
    assert p.post(f"/api/pompiste-supervision/rapports/{rid}/transfert", json={"espace_id": eid, "motif": "Mauvaise station",
                  "lignes": {str(g1): B["Gazoline 1"], str(g2): B["Gazoline 1"]}}).status_code == 400
    r = p.post(f"/api/pompiste-supervision/rapports/{rid}/transfert", json={"espace_id": eid, "motif": "Mauvaise station",
               "lignes": {str(k): v for k, v in props.items()}})
    assert r.status_code == 200, r.text
