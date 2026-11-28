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
        "produits": [{"cle": "g", "nom": "Gazoline 1", "prix": 900}, {"cle": "d", "nom": "Diesel", "prix": 950},
                     {"cle": "g2", "nom": "Gazoline 2", "prix": 900}],
        "pompes": [{"nom": "Pompe 1", "faces": [{"nom": "A", "metres": [
            {"produit": "g", "nom": "G1", "metre_depart": 10}, {"produit": "d", "nom": "D1", "metre_depart": 10},
            {"produit": "g2", "nom": "G2", "metre_depart": 10}]}]}]},
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
    # Seulement Gazoline / Diesel : « Gazoline 1 » et « Gazoline 2 » = un seul type
    assert [x["nom"] for x in ma["produits"]] == ["Gazoline", "Diesel"]
    gaz = ma["produits"][0]
    assert len(gaz["produit_ids"]) == 2
    assert any(s["id"] == 0 for s in st)

    base = {"station_id": eid, "produit_id": diesel_id, "no_bon": "BL-2026-001", "kantite_voye": 3000,
            "pri_galon": 600, "chofe": "Pierre Chofè", "telefon_chofe": "3456 7890", "plak_kamyon": "ab-12345",
            "konpayi": "Sol Haïti", "no_kontre_plen": "4417"}
    # Validation : téléphone, quantité, produit d'une autre station
    r = rc.post("/api/resepsyon/livrezon", json=dict(base, telefon_chofe="12", kantite_voye=0))
    assert r.status_code == 422
    chans = {e["chan"] for e in r.json()["detail"]["erreurs"]}
    assert {"telefon_chofe", "kantite_voye"} <= chans
    autre = max(principal_avant.values()) + 999
    r = rc.post("/api/resepsyon/livrezon", json=dict(base, no_kontre_plen=" "))
    assert r.status_code == 422 and r.json()["detail"]["erreurs"][0]["chan"] == "no_kontre_plen"
    for mal in ("KP-9", "12,5", "-3"):                                   # kontre plen = nonb antye
        r = rc.post("/api/resepsyon/livrezon", json=dict(base, no_kontre_plen=mal))
        assert r.status_code == 422 and r.json()["detail"]["erreurs"][0]["chan"] == "no_kontre_plen", mal
    r = rc.post("/api/resepsyon/livrezon", json=dict(base, produit_id=autre))
    assert r.status_code == 422 and r.json()["detail"]["erreurs"][0]["chan"] == "produit_id"

    r = rc.post("/api/resepsyon/livrezon", json=dict(base, date_livrezon="2026-01-01T08:00"))
    assert r.status_code == 201, r.text
    assert not r.json()["date_livrezon"].startswith("2026-01-01")   # dat & lè otomatik (sèvè a)
    l = r.json()
    assert l["statut"] == "VALIDE" and l["telefon_chofe"] == "+50934567890" and l["plak_kamyon"] == "AB-12345"
    assert l["station_nom"] == "Station Livrezon" and l["numero"].startswith("LVK-")
    # Bon en double (même écrit autrement)
    r = rc.post("/api/resepsyon/livrezon", json=dict(base, no_bon="bl 2026 001"))
    assert r.status_code == 422 and r.json()["detail"]["erreurs"][0]["chan"] == "no_bon"

    # La réception ne peut pas annuler une livraison enregistrée
    assert rc.post(f"/api/livrezon-admin/livrezon/{l['id']}/anile", json={"motif": "x"}).status_code == 403
    assert rc.put(f"/api/livrezon-admin/livrezon/{l['id']}", json={"kantite_voye": 1}).status_code == 403
    # Gazoline : la livraison entre dans le stock du type (1er produit), affichée sous « Gazoline »
    g_avant, g1_id = _stock(sch, "Gazoline 1")
    r = rc.post("/api/resepsyon/livrezon", json=dict(base, produit_id=gaz["produit_ids"][1], no_bon="BL-GAZ-1"))
    assert r.status_code == 201 and r.json()["produit_nom"] == "Gazoline" and r.json()["produit_id"] == g1_id
    assert rc.post(f"/api/resepsyon/livrezon/{r.json()['id']}/resevwa", json={"kantite_resevwa": 3000}).json()["statut"] == "FEMEN"
    assert round(_stock(sch, "Gazoline 1")[0] - g_avant, 3) == 3000
    ma2 = next(s for s in rc.get("/api/resepsyon/stations").json()["stations"] if s["id"] == eid)
    assert ma2["produits"][0]["stock"] == round(gaz["stock"] + 3000, 3)
    assert p.post(f"/api/livrezon-admin/livrezon/{r.json()['id']}/anile", json={"motif": "Test type"}).status_code == 200
    # Application : camion arrivé enregistré en une fois (réception + 2 photos de la fiche obligatoires)
    import base64 as b64
    ico = os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "resepsyon-app", "favicon-64.png")
    png = "data:image/png;base64," + b64.b64encode(open(ico, "rb").read()).decode()
    k = dict(base, no_bon="BL-KONPLE-1", kantite_resevwa=2995, jog_avan=100, jog_apre=3095)
    r = rc.post("/api/resepsyon/livrezon/konple", json=dict(k, fotos=[png], kantite_resevwa=None, chofe=""))
    assert r.status_code == 422
    assert {"fotos", "kantite_resevwa", "chofe"} <= {e["chan"] for e in r.json()["detail"]["erreurs"]}
    assert rc.post("/api/resepsyon/livrezon/konple", json=dict(k, fotos=["data:text/html;base64,PGI+"] * 2)).status_code == 422
    d_avan, _ = _stock(sch, "Diesel")
    r = rc.post("/api/resepsyon/livrezon/konple", json=dict(k, fotos=[png, png], plak_kamyon=None))   # plak opsyonèl
    assert r.status_code == 201, r.text
    kp = r.json()
    assert kp["plak_kamyon"] == "" and kp["statut"] == "FEMEN" and kp["kantite_resevwa"] == 2995 and kp["livraison_id"] and len(kp["fotos"]) == 2
    assert [j["aksyon"] for j in kp["jounal"]] == ["KREYE", "RESEVWA", "FEMEN"]
    assert round(_stock(sch, "Diesel")[0] - d_avan, 3) == 2995
    f = rc.get(f"/api/resepsyon/livrezon/{kp['id']}/foto/{kp['fotos'][0]['id']}")
    assert f.status_code == 200 and f.headers["content-type"] == "image/png" and f.content.startswith(b"\x89PNG")
    assert p.get(f"/api/livrezon-admin/livrezon/{kp['id']}/foto/{kp['fotos'][1]['id']}").status_code == 200
    assert rc.get(f"/api/livrezon-admin/livrezon/{kp['id']}/foto/{kp['fotos'][1]['id']}").status_code == 403
    assert rc.get(f"/api/resepsyon/livrezon/{l['id']}/foto/{kp['fotos'][0]['id']}").status_code == 404
    # Fich konplè : tout sa resepsyon an antre (PDF + mesaj WhatsApp menm kontni), foto yo sou paj 2
    dk = rc.get(f"/api/resepsyon/livrezon/{kp['id']}").json()
    fich = {k: v for s in dk["fich"] for k, v in s["lignes"]}
    assert fich["Kantite resevwa"] == "2 995 gal" and fich["Ekat"] == "5 gal"
    assert fich["Kontre plen"] == "4417" and fich["Foto fèy la"] == "2 foto"
    assert not {"Plak kamyon", "Jòj tank avan", "Jòj tank apre", "Diferans jòj"} & fich.keys()
    r = rc.get(f"/api/resepsyon/livrezon/{kp['id']}/pdf")
    assert r.status_code == 200 and r.content.count(b"/Type /Page\n") + r.content.count(b"/Type /Page\r") + r.content.count(b"/Type /Page ") >= 2
    # Istorik : rechèch, estasyon, paj
    h = rc.get("/api/resepsyon/livrezon", params={"q": "BL-KONPLE"}).json()
    assert h["total"] == 1 and h["livrezons"][0]["id"] == kp["id"]
    assert rc.get("/api/resepsyon/livrezon", params={"station_id": eid, "limit": 1}).json()["limit"] == 1
    assert rc.get("/api/resepsyon/livrezon", params={"station_id": 999999}).json()["total"] == 0
    assert p.post(f"/api/livrezon-admin/livrezon/{kp['id']}/anile", json={"motif": "Test konple"}).status_code == 200
    # Nimewo bon opsyonèl : plizyè livrezon san bon pa bloke youn lòt ; kontre plen « 0012 » → « 12 »
    san = []
    for _ in range(2):
        r = rc.post("/api/resepsyon/livrezon/konple", json=dict(k, no_bon=None, no_kontre_plen="0012", fotos=[png, png]))
        assert r.status_code == 201, r.text
        assert r.json()["no_bon"] == "" and r.json()["no_kontre_plen"] == "12"
        san.append(r.json()["id"])
    fich2 = {k2: v2 for sc in rc.get(f"/api/resepsyon/livrezon/{san[0]}").json()["fich"] for k2, v2 in sc["lignes"]}
    assert fich2["Nimewo bon"] == "—"
    for i in san:
        assert p.post(f"/api/livrezon-admin/livrezon/{i}/anile", json={"motif": "Test san bon"}).status_code == 200
    assert round(_stock(sch, "Diesel")[0] - d_avan, 3) == 0
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
    assert t["station_nom"] == "Station Livrezon" and t["kpi"]["nb_kamyon"] == 2 and t["kpi"]["nb_ekat"] == 1
    assert [a["kantite_resevwa"] for a in t["arrivees"]] == [4900, 2998]          # chak kamyon, dènye an premye
    assert {a["station_id"] for a in t["arrivees"]} == {eid}
    assert {x["nom"] for x in t["stock"]} == {"Gazoline", "Diesel"}
    # Registre du gaz reçu : la station seulement, filtres, totaux par produit, export CSV
    g = p.get(f"/api/livrezon-admin/resepsyon-lis?station_id={eid}").json()
    assert g["station_nom"] == "Station Livrezon" and len(g["lignes"]) == 2
    assert g["totaux"] == [{"produit": "Diesel", "kamyon": 2, "voye": 8000, "resevwa": 7898, "ekat": 102}]
    assert g["produits"] == ["Diesel"] and g["konpayis"] == ["Sol Haïti"]
    assert p.get(f"/api/livrezon-admin/resepsyon-lis?station_id={eid}&produit=Gazoline").json()["lignes"] == []
    assert len(p.get(f"/api/livrezon-admin/resepsyon-lis?station_id={eid}&q=4417").json()["lignes"]) == 2
    r = p.get(f"/api/livrezon-admin/resepsyon-lis.csv?station_id={eid}")
    assert r.status_code == 200 and "Station Livrezon" in r.text and "4900" in r.text
    assert rc.get(f"/api/livrezon-admin/resepsyon-lis?station_id={eid}").status_code == 403
    # Sans station : une seule station, jamais un cumul
    t0 = p.get("/api/livrezon-admin/dashboard").json()
    assert t0["station_id"] == t0["stations"][0]["id"]
    assert all(a["station_id"] == t0["station_id"] for a in t0["arrivees"])

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
