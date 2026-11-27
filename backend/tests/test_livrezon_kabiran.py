"""
Livrezon kabiran (application « Konekta Resepsyon ») : compte de réception,
choix de la station, validation, réception → stock de LA STATION CHOISIE,
écart → alerte, fiche PDF, tableau de bord, annulation.
"""
import os

from fastapi.testclient import TestClient

os.environ["LIVREZON_NOTIF_SYNC"] = "1"          # notifications dans le même fil (journal vérifiable)

PROPRIO = {"email": "admin@konekta.local", "password": "admin123", "code_acces": "123456789"}


def _client():
    import main
    return TestClient(main.app)


def _stock(schema, nom_produit):
    from database import session_espace
    from models import Produit
    from stock_service import stock_restant
    with session_espace(schema) as db:
        p = db.query(Produit).filter_by(nom=nom_produit).first()
        return stock_restant(db, p.id)["gallons_livres"], p.id


def test_circuit_livrezon(client):
    p = _client()
    assert p.post("/api/login", json=PROPRIO).status_code == 200
    # Une station séparée où livrer
    corps = {"nom": "Station Livrezon", "adresse": "Route test", "station": {
        "produits": [{"cle": "g", "nom": "Gazoline", "prix": 900}, {"cle": "d", "nom": "Diesel", "prix": 950}],
        "pompes": [{"nom": "Pompe 1", "faces": [{"nom": "A", "metres": [
            {"produit": "g", "nom": "G1", "metre_depart": 10}, {"produit": "d", "nom": "D1", "metre_depart": 10}]}]}]},
        "gerant": {"nom_complet": "Gérant Livrezon", "email": "gerant.livrezon@exemple.ht",
                   "password": "MotDePasse2026", "pin": "555666777"}}
    r = p.post("/api/espaces", json=corps)
    assert r.status_code == 201, r.text
    eid = r.json()["id"]
    sch = f"esp_{eid}"
    livre_avant, diesel_id = _stock(sch, "Diesel")
    principal_avant = {x["nom"]: x["id"] for x in p.get("/api/produits").json()}

    # Compte de réception (créé par la direction)
    assert p.post("/api/livrezon-admin/kont", json={"nom_complet": "Jean Resepsyon", "pin": "12"}).status_code == 422
    r = p.post("/api/livrezon-admin/kont", json={"nom_complet": "Jean Resepsyon"})
    assert r.status_code == 201, r.text
    kont = r.json()
    assert len(kont["kod"]) == 8 and kont["kod"][:4].isdigit() and kont["kod"][4:].isalpha() and len(kont["pin"]) == 6

    # Connexion : kòd + PIN
    rc = _client()
    assert rc.post("/api/resepsyon/identifier", json={"username": "0000XXXX"}).status_code == 404
    assert rc.post("/api/resepsyon/identifier", json={"username": kont["kod"]}).json()["prenom"] == "Jean"
    assert rc.post("/api/login/resepsyon", json={"username": kont["kod"], "pin": "000000"}).status_code == 401
    r = rc.post("/api/login/resepsyon", json={"username": kont["kod"].lower(), "pin": kont["pin"]})
    assert r.status_code == 200, r.text
    # Accès limité à l'application
    assert rc.get("/api/produits").status_code == 403
    assert rc.get("/api/livrezon-admin/dashboard").status_code == 403
    assert rc.get("/", follow_redirects=False).headers.get("location") == "/resepsyon"

    st = rc.get("/api/resepsyon/stations").json()["stations"]
    ma = next(s for s in st if s["id"] == eid)
    assert {x["nom"] for x in ma["produits"]} == {"Gazoline", "Diesel"}
    assert any(s["id"] == 0 for s in st)

    base = {"station_id": eid, "produit_id": diesel_id, "no_bon": "BL-2026-001", "kantite_voye": 3000,
            "pri_galon": 600, "chofe": "Pierre Chofè", "telefon_chofe": "3456 7890", "plak_kamyon": "ab-12345",
            "konpayi": "Sol Haïti", "no_kontre_plen": "KP-9"}
    # Validation : téléphone, quantité, produit d'une autre station
    r = rc.post("/api/resepsyon/livrezon", json=dict(base, telefon_chofe="12", kantite_voye=0))
    assert r.status_code == 422
    chans = {e["chan"] for e in r.json()["detail"]["erreurs"]}
    assert {"telefon_chofe", "kantite_voye"} <= chans
    autre = max(principal_avant.values()) + 999
    r = rc.post("/api/resepsyon/livrezon", json=dict(base, produit_id=autre))
    assert r.status_code == 422 and r.json()["detail"]["erreurs"][0]["chan"] == "produit_id"

    r = rc.post("/api/resepsyon/livrezon", json=base)
    assert r.status_code == 201, r.text
    l = r.json()
    assert l["statut"] == "VALIDE" and l["telefon_chofe"] == "+50934567890" and l["plak_kamyon"] == "AB-12345"
    assert l["station_nom"] == "Station Livrezon" and l["numero"].startswith("LVK-")
    # Bon en double (même écrit autrement)
    r = rc.post("/api/resepsyon/livrezon", json=dict(base, no_bon="bl 2026 001"))
    assert r.status_code == 422 and r.json()["detail"]["erreurs"][0]["chan"] == "no_bon"

    # Admin : approuver → en route
    r = p.post(f"/api/livrezon-admin/livrezon/{l['id']}/apwouve", json={})
    assert r.status_code == 200 and r.json()["statut"] == "AN_WOUT"

    # Réception : stock de la station choisie mis à jour, rien dans la principale
    assert rc.post(f"/api/resepsyon/livrezon/{l['id']}/resevwa",
                   json={"kantite_resevwa": 2998, "jog_avan": 500, "jog_apre": 400}).status_code == 422
    r = rc.post(f"/api/resepsyon/livrezon/{l['id']}/resevwa", json={"kantite_resevwa": 2998, "jog_avan": 500, "jog_apre": 3498})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["statut"] == "FEMEN" and d["ekat"] == 2 and d["livraison_id"]
    livre_apres, _ = _stock(sch, "Diesel")
    assert round(livre_apres - livre_avant, 3) == 2998
    assert [j["aksyon"] for j in d["jounal"]] == ["KREYE", "APWOUVE", "RESEVWA", "FEMEN"]
    assert rc.post(f"/api/resepsyon/livrezon/{l['id']}/resevwa", json={"kantite_resevwa": 10}).status_code == 409
    # Notifications : journalisées (non configurées en test)
    assert {n["kanal"] for n in d["notifikasyon"]} >= {"WHATSAPP", "SMS"}
    assert all(n["statut"] == "PA_KONFIGIRE" for n in d["notifikasyon"])

    # Fiche PDF
    r = rc.get(f"/api/resepsyon/livrezon/{l['id']}/pdf")
    assert r.status_code == 200 and r.content[:4] == b"%PDF"

    # Deuxième livraison avec écart → EKAT
    r = rc.post("/api/resepsyon/livrezon", json=dict(base, no_bon="BL-2026-002", kantite_voye=5000))
    l2 = r.json()
    r = rc.post(f"/api/resepsyon/livrezon/{l2['id']}/resevwa", json={"kantite_resevwa": 4900})
    assert r.json()["statut"] == "EKAT" and r.json()["ekat"] == 100

    # Tableau de bord
    t = p.get(f"/api/livrezon-admin/dashboard?station_id={eid}").json()
    assert t["kpi"]["galon_resevwa"] == 7898 and t["kpi"]["nb_ekat"] == 1
    assert t["par_station"][0]["non"] == "Station Livrezon"
    assert sum(t["par_jour"][0]["valeurs"]) == 7898

    # Clôture de l'écart puis annulation : le stock est retiré
    assert p.post(f"/api/livrezon-admin/livrezon/{l2['id']}/femen", json={"motif": ""}).status_code == 422
    assert p.post(f"/api/livrezon-admin/livrezon/{l2['id']}/femen", json={"motif": "Vérifié avec la compagnie"}).json()["statut"] == "FEMEN"
    r = p.post(f"/api/livrezon-admin/livrezon/{l2['id']}/anile", json={"motif": "Erreur de saisie"})
    assert r.status_code == 200 and r.json()["statut"] == "ANILE" and r.json()["livraison_id"] is None
    livre_fin, _ = _stock(sch, "Diesel")
    assert round(livre_fin - livre_avant, 3) == 2998

    # Modification admin d'une livraison reçue : stock corrigé
    r = p.put(f"/api/livrezon-admin/livrezon/{l['id']}", json={"kantite_resevwa": 2990})
    assert r.status_code == 200, r.text
    livre_mod, _ = _stock(sch, "Diesel")
    assert round(livre_mod - livre_avant, 3) == 2990

    # Compte désactivé : plus de connexion
    assert p.put(f"/api/livrezon-admin/kont/{kont['id']}", json={"actif": False}).status_code == 200
    assert rc.get("/api/resepsyon/me").status_code == 401

    # Ménage : la station de test est retirée (les autres tests comptent les stations)
    assert p.post(f"/api/espaces/{eid}/retirer", json={"confirmation": "RETIRER"}).status_code == 200
    from database import session_principale
    from models import LivrezonKabiran
    with session_principale() as db:
        db.query(LivrezonKabiran).delete()
        db.commit()
