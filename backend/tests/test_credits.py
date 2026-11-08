"""Module Crédits : dettes / créances, dépôts et augmentations (règles du
diagramme uml-module-credits.md)."""
import time
from datetime import timedelta

from tz_utils import today_haiti

H = {"X-API-Key": "smoke-test-key"}
J = lambda n=0: str(today_haiti() - timedelta(days=n))


def _credit(client, **kw):
    body = {"sens": "DETTE", "tiers_nom": f"Fournisseur {time.time_ns()}", "montant_initial": 1000,
            "devise": "HTG", "date_credit": J(10)}
    body.update(kw)
    r = client.post("/api/credits", headers=H, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _mvt(client, cid, type_, montant, **kw):
    return client.post(f"/api/credits/{cid}/mouvements", headers=H,
                       json={"type": type_, "montant": montant, **kw})


def test_total_du_reste_et_statut_solde(client):
    c = _credit(client)
    assert c["statut"] == "ACTIF" and c["total_du"] == 1000 and c["reste"] == 1000
    c = _mvt(client, c["id"], "AUGMENTATION", 500, date_mouvement=J(5)).json()
    assert c["total_du"] == 1500 and c["total_augmentations"] == 500 and c["reste"] == 1500
    c = _mvt(client, c["id"], "DEPOT", 600, date_mouvement=J(4), mode_paiement="Cash").json()
    assert c["total_depose"] == 600 and c["reste"] == 900 and c["statut"] == "ACTIF"
    c = _mvt(client, c["id"], "DEPOT", 900).json()
    assert c["reste"] == 0 and c["statut"] == "SOLDE"
    # Une augmentation remet le crédit ACTIF
    c = _mvt(client, c["id"], "AUGMENTATION", 100).json()
    assert c["statut"] == "ACTIF" and c["reste"] == 100
    assert [m["type"] for m in c["mouvements"]] == ["AUGMENTATION", "DEPOT", "DEPOT", "AUGMENTATION"]


def test_depot_jamais_plus_que_le_reste_ni_avant_le_credit(client):
    c = _credit(client, montant_initial=300)
    r = _mvt(client, c["id"], "DEPOT", 300.01)
    assert r.status_code == 409 and "dépasse le reste" in r.json()["detail"]
    r = _mvt(client, c["id"], "DEPOT", 100, date_mouvement=J(11))
    assert r.status_code == 400 and "précéder" in r.json()["detail"]
    assert _mvt(client, c["id"], "DEPOT", 100, date_mouvement=str(today_haiti() + timedelta(days=1))).status_code == 400
    assert _mvt(client, c["id"], "DEPOT", 0).status_code == 400
    assert _mvt(client, c["id"], "DEPOT", 300).json()["statut"] == "SOLDE"
    assert _mvt(client, c["id"], "DEPOT", 1).status_code == 409          # déjà soldé


def test_annule_exclu_des_totaux_et_bloque(client):
    nom = f"Annul {time.time_ns()}"
    c = _credit(client, sens="CREANCE", tiers_nom=nom, montant_initial=777, devise="USD")
    avant = client.get("/api/credits", headers=H).json()["resume"]["CREANCE"]["USD"]
    assert client.post(f"/api/credits/{c['id']}/annuler", headers=H, json={"motif": "x"}).status_code == 400
    r = client.post(f"/api/credits/{c['id']}/annuler", headers=H, json={"motif": "saisi en double"})
    assert r.status_code == 200 and r.json()["statut"] == "ANNULE"
    apres = client.get("/api/credits", headers=H).json()
    assert apres["resume"]["CREANCE"]["USD"]["reste"] == round(avant["reste"] - 777, 2)
    assert all(x["id"] != c["id"] for x in apres["credits"])                      # masqué par défaut
    assert any(x["id"] == c["id"] for x in client.get("/api/credits?statut=ANNULE", headers=H).json()["credits"])
    assert _mvt(client, c["id"], "DEPOT", 10).status_code == 409
    r = client.post(f"/api/credits/{c['id']}/reactiver", headers=H)
    assert r.status_code == 200 and r.json()["statut"] == "ACTIF"


def test_devises_jamais_additionnees(client):
    avant = client.get("/api/credits", headers=H).json()["resume"]["DETTE"]
    _credit(client, montant_initial=100, devise="HTG")
    _credit(client, montant_initial=5, devise="USD")
    apres = client.get("/api/credits", headers=H).json()["resume"]["DETTE"]
    assert apres["HTG"]["reste"] == round(avant["HTG"]["reste"] + 100, 2)
    assert apres["USD"]["reste"] == round(avant["USD"]["reste"] + 5, 2)


def test_en_retard(client):
    c = _credit(client, date_credit=J(30), date_echeance=J(2))
    assert c["en_retard"] is True and c["jours_retard"] == 2
    ids = [x["id"] for x in client.get("/api/credits?statut=EN_RETARD", headers=H).json()["credits"]]
    assert c["id"] in ids
    c = _mvt(client, c["id"], "DEPOT", 1000).json()                       # soldé : plus en retard
    assert c["en_retard"] is False
    assert _credit(client, date_credit=J(1), date_echeance=J(0))["en_retard"] is False


def test_supprimer_credit_en_cascade_et_mouvement(client):
    c = _credit(client, montant_initial=500)
    c = _mvt(client, c["id"], "AUGMENTATION", 200).json()
    c = _mvt(client, c["id"], "DEPOT", 650).json()
    aug = next(m for m in c["mouvements"] if m["type"] == "AUGMENTATION")
    dep = next(m for m in c["mouvements"] if m["type"] == "DEPOT")
    # Sans l'augmentation, 650 déposés > 500 dus : refusé
    r = client.post(f"/api/credits/{c['id']}/mouvements/{aug['id']}/supprimer", headers=H, json={"motif": "erreur"})
    assert r.status_code == 409
    r = client.post(f"/api/credits/{c['id']}/mouvements/{dep['id']}/supprimer", headers=H, json={"motif": "mauvais montant"})
    assert r.status_code == 200 and r.json()["reste"] == 700 and r.json()["nb_mouvements"] == 1
    r = client.post(f"/api/credits/{c['id']}/supprimer", headers=H, json={"motif": "crédit fictif"})
    assert r.status_code == 200
    assert client.get(f"/api/credits/{c['id']}", headers=H).status_code == 404
    from database import SessionLocal
    from models import CreditMouvement
    with SessionLocal() as s:
        assert s.query(CreditMouvement).filter_by(credit_id=c["id"]).count() == 0


def test_modifier_respecte_les_depots(client):
    c = _credit(client, montant_initial=1000)
    c = _mvt(client, c["id"], "DEPOT", 800, date_mouvement=J(3)).json()
    assert client.put(f"/api/credits/{c['id']}", headers=H, json={"montant_initial": 700}).status_code == 409
    assert client.put(f"/api/credits/{c['id']}", headers=H, json={"devise": "USD"}).status_code == 409
    assert client.put(f"/api/credits/{c['id']}", headers=H, json={"date_credit": J(2)}).status_code == 409
    r = client.put(f"/api/credits/{c['id']}", headers=H, json={"montant_initial": 800, "tiers_nom": "Nouveau nom"})
    assert r.status_code == 200 and r.json()["statut"] == "SOLDE" and r.json()["tiers_nom"] == "Nouveau nom"


def test_validations_creation(client):
    for body in ({"sens": "AUTRE"}, {"devise": "EUR"}, {"tiers_nom": "  "}, {"montant_initial": -5},
                 {"date_credit": str(today_haiti() + timedelta(days=1))},
                 {"date_credit": J(5), "date_echeance": J(6)}):
        base = {"sens": "DETTE", "tiers_nom": "X", "montant_initial": 10, "devise": "HTG"}
        base.update(body)
        assert client.post("/api/credits", headers=H, json=base).status_code == 400, body


def test_acces_reserve_a_la_direction(client):
    from database import SessionLocal
    from models import Role, Utilisateur
    from auth import hash_password
    from fastapi.testclient import TestClient
    import main as _m
    ns = time.time_ns()
    with SessionLocal() as s:
        role = Role(nom=f"Caissier test {ns}", permissions={"bar": "operationnel"})
        s.add(role); s.flush()
        s.add(Utilisateur(username=f"caisse{ns}", nom_complet="Caisse Test", role="operateur", role_id=role.id,
                          actif=True, password_hash=hash_password("MotDePasse!2026")))
        s.commit()
    from auth import create_session
    with SessionLocal() as s:
        u = s.query(Utilisateur).filter_by(username=f"caisse{ns}").first()
        token = create_session(s, u.id)
    c = TestClient(_m.app)
    c.cookies.set(_m.SESSION_COOKIE, token)
    assert c.get("/api/credits").status_code == 403
