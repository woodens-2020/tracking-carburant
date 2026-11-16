"""
v27 — Piscine, département séparé du Bar ; paiements hôtel en gourdes ou
en dollars, comptés séparément.

Lancer seul : cd backend && python -m pytest tests/test_piscine_dollar.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="konekta-piscine-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMP, "tests.db").replace("\\", "/")
os.environ["ADMIN_API_KEY"] = "test-piscine-key"
os.environ["OTP_ENABLED"] = "false"
os.environ["SECURE_COOKIES"] = "false"

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

H = {"X-API-Key": "test-piscine-key"}


@pytest.fixture(scope="module")
def client():
    import main
    with TestClient(main.app) as c:
        yield c


@pytest.fixture()
def db(client):
    from database import SessionLocal
    from models import Utilisateur
    s = SessionLocal()
    if not s.query(Utilisateur).filter_by(username="admin").first():
        s.add(Utilisateur(username="admin", password_hash="x", nom_complet="Admin", role="admin", actif=True))
        s.commit()
    try:
        yield s
    finally:
        s.close()


_n = [0]


def _num():
    _n[0] += 1
    return _n[0]


def _employe(db):
    from models import Employe
    from tz_utils import today_haiti
    e = Employe(nom="Caisse", prenom=f"P{_num()}", poste="Caissière",
                date_embauche=today_haiti(), salaire_base=Decimal("0"), actif=True)
    db.add(e)
    db.commit()
    return e


def _produit(client, lieu=None):
    nom = f"Article {_num()}"
    r = client.post("/api/pos/produits", headers=H, json={
        "nom": nom, "categorie": "boisson", "prix_initial": 100, "lieu": lieu,
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _stock(client, pid, lieu, qte=50):
    r = client.post(f"/api/pos/produits/{pid}/approvisionnement", headers=H,
                    json={"nb_unites_vrac": qte, "lieu": lieu})
    assert r.status_code == 201, r.text


# ══════════════════════════════════════════════════════════════════
# PISCINE — département séparé
# ══════════════════════════════════════════════════════════════════

def test_vente_de_session_piscine_porte_le_departement(client, db):
    from models import BarVente
    emp = _employe(db)
    pid = _produit(client)
    _stock(client, pid, "PISCINE")
    r = client.post("/api/pos/caisse/sessions/ouvrir?lieu=PISCINE", headers=H, json={"caissier_id": emp.id})
    assert r.status_code == 200, r.text
    assert r.json()["lieu"] == "PISCINE"
    r = client.post("/api/pos/ventes", headers=H, json={
        "caissier_id": emp.id, "lignes": [{"produit_id": pid, "quantite": 2}]})
    assert r.status_code == 201, r.text
    assert r.json()["lieu"] == "PISCINE"
    assert db.get(BarVente, r.json()["vente_id"]).lieu == "PISCINE"


def test_session_existante_fait_foi_sur_l_ecran(client, db):
    """Une caissière déjà en session Bar qui vend depuis l'écran Piscine
    vend quand même pour le Bar (sa session)."""
    emp = _employe(db)
    pid = _produit(client)
    _stock(client, pid, "DEVANT")
    client.post("/api/pos/caisse/sessions/ouvrir", headers=H, json={"caissier_id": emp.id, "lieu": "DEVANT"})
    r = client.post("/api/pos/ventes?lieu=PISCINE", headers=H, json={
        "caissier_id": emp.id, "lignes": [{"produit_id": pid, "quantite": 1}]})
    assert r.status_code == 201, r.text
    assert r.json()["lieu"] == "DEVANT"


def test_vente_sans_session_prend_l_ecran(client, db):
    pid = _produit(client)
    _stock(client, pid, "PISCINE")
    r = client.post("/api/pos/ventes?lieu=PISCINE", headers=H, json={
        "lignes": [{"produit_id": pid, "quantite": 1}]})
    assert r.status_code == 201, r.text
    assert r.json()["lieu"] == "PISCINE"


def test_stock_piscine_ne_sert_pas_le_bar(client, db):
    """Le stock reçu à la Piscine n'est pas vendable par le Bar."""
    pid = _produit(client)
    _stock(client, pid, "PISCINE", qte=5)
    r = client.post("/api/pos/ventes?lieu=DEVANT", headers=H, json={
        "lignes": [{"produit_id": pid, "quantite": 1}]})
    assert r.status_code == 422
    assert "Stock insuffisant" in r.text


def test_listes_et_dashboard_separes(client, db):
    pid = _produit(client)
    _stock(client, pid, "PISCINE")
    _stock(client, pid, "DEVANT")
    vp = client.post("/api/pos/ventes?lieu=PISCINE", headers=H, json={
        "lignes": [{"produit_id": pid, "quantite": 3}]}).json()
    vb = client.post("/api/pos/ventes?lieu=DEVANT", headers=H, json={
        "lignes": [{"produit_id": pid, "quantite": 1}]}).json()

    ids_p = {v["id"] for v in client.get("/api/pos/ventes?limit=500&lieu=PISCINE", headers=H).json()}
    ids_b = {v["id"] for v in client.get("/api/pos/ventes?limit=500&lieu=DEVANT", headers=H).json()}
    ids_t = {v["id"] for v in client.get("/api/pos/ventes?limit=500", headers=H).json()}
    assert vp["vente_id"] in ids_p and vp["vente_id"] not in ids_b
    assert vb["vente_id"] in ids_b and vb["vente_id"] not in ids_p
    assert {vp["vente_id"], vb["vente_id"]} <= ids_t

    dp = client.get("/api/pos/dashboard/bar?lieu=PISCINE", headers=H).json()
    db_ = client.get("/api/pos/dashboard/bar?lieu=DEVANT", headers=H).json()
    tout = client.get("/api/pos/dashboard/bar", headers=H).json()
    assert dp["meta"]["departement"] == "Piscine"
    assert round(dp["kpis"]["ca_jour"] + db_["kpis"]["ca_jour"], 2) == round(tout["kpis"]["ca_jour"], 2)

    sp = client.get("/api/pos/stats?lieu=PISCINE", headers=H).json()
    sb = client.get("/api/pos/stats?lieu=DEVANT", headers=H).json()
    st = client.get("/api/pos/stats", headers=H).json()
    assert round(sp["ca_total"] + sb["ca_total"], 2) == round(st["ca_total"], 2)

    tp = client.get("/api/pos/analyse/tableau?lieu=PISCINE", headers=H)
    assert tp.status_code == 200, tp.text
    ev = client.get("/api/pos/analyse/evolution?lieu=PISCINE", headers=H).json()
    assert ev["total_ca"] == sp["ca_total"]


def test_vente_historique_sans_lieu_reste_au_bar(client, db):
    from models import BarVente
    v = BarVente(numero_ticket=f"HIST{_num():05d}", date_heure=datetime.now(timezone.utc),
                 montant_total=Decimal("77"), mode_paiement="CASH", statut="PAYEE",
                 montant_paye=Decimal("77"), montant_restant=Decimal("0"))
    db.add(v); db.commit()
    ids_b = {x["id"] for x in client.get("/api/pos/ventes?limit=500&lieu=DEVANT", headers=H).json()}
    ids_p = {x["id"] for x in client.get("/api/pos/ventes?limit=500&lieu=PISCINE", headers=H).json()}
    assert v.id in ids_b and v.id not in ids_p


def test_reprise_lieu_depuis_la_session(client, db):
    from database import _reprise_piscine_et_paiements_hotel
    from models import BarVente, BarSessionCaisse
    from tz_utils import today_haiti
    emp = _employe(db)
    s = BarSessionCaisse(caissier_id=emp.id, date_session=today_haiti(), numero_session=1,
                         statut="SOUMIS", lieu="PISCINE")
    db.add(s); db.commit()
    v = BarVente(numero_ticket=f"REP{_num():05d}", caissier_id=emp.id, session_id=s.id,
                 date_heure=datetime.now(timezone.utc), montant_total=Decimal("10"),
                 mode_paiement="CASH", statut="PAYEE", montant_paye=Decimal("10"),
                 montant_restant=Decimal("0"))
    db.add(v); db.commit()
    assert v.lieu is None
    _reprise_piscine_et_paiements_hotel()
    db.expire_all()
    assert db.get(BarVente, v.id).lieu == "PISCINE"


def test_depenses_et_caisse_par_departement(client, db):
    r = client.post("/api/pos/depenses?lieu=PISCINE", headers=H,
                    json={"description": "Chlore", "montant": 1500, "categorie": "Entretien"})
    assert r.status_code == 201, r.text
    assert r.json()["lieu"] == "PISCINE"
    r2 = client.post("/api/pos/depenses?lieu=DEVANT", headers=H,
                     json={"description": "Glace", "montant": 400})
    assert r2.status_code == 201
    # Sans département → refus
    assert client.post("/api/pos/depenses", headers=H, json={"description": "x", "montant": 1}).status_code == 422

    lp = client.get("/api/pos/depenses?lieu=PISCINE", headers=H).json()
    assert any(d["id"] == r.json()["id"] for d in lp["depenses"])
    assert all(d["lieu"] == "PISCINE" for d in lp["depenses"])

    gp = client.get("/api/pos/grande-caisse?lieu=PISCINE", headers=H).json()
    gb = client.get("/api/pos/grande-caisse?lieu=DEVANT", headers=H).json()
    assert gp["caisse"]["depenses"] >= 1500 and gb["caisse"]["depenses"] >= 400
    assert gp["departement"] == "Piscine"

    rr = client.post("/api/pos/renflouements?lieu=PISCINE", headers=H, json={"montant": 2000, "source": "Direction"})
    assert rr.status_code == 201, rr.text
    lr = client.get("/api/pos/renflouements?lieu=PISCINE", headers=H).json()["renflouements"]
    assert any(x["id"] == rr.json()["id"] for x in lr)
    lb = client.get("/api/pos/renflouements?lieu=DEVANT", headers=H).json()["renflouements"]
    assert not any(x["id"] == rr.json()["id"] for x in lb)

    assert client.delete(f"/api/pos/depenses/{r2.json()['id']}", headers=H).status_code == 200


def test_achat_et_paie_du_departement(client, db):
    pid = _produit(client)
    a = client.post("/api/pos/achats?lieu=PISCINE", headers=H, json={
        "produit_id": pid, "quantite": 10, "prix_achat_unitaire": 50})
    assert a.status_code == 201, a.text
    ach_p = client.get("/api/pos/achats?lieu=PISCINE", headers=H).json()
    ach_b = client.get("/api/pos/achats?lieu=DEVANT", headers=H).json()
    assert any(x["id"] == a.json()["achat_id"] and x["lieu"] == "PISCINE" for x in ach_p)
    assert not any(x["id"] == a.json()["achat_id"] for x in ach_b)

    # Produit exclusif au Bar : achat Piscine refusé
    pid_bar = _produit(client, lieu="DEVANT")
    r = client.post("/api/pos/achats?lieu=PISCINE", headers=H, json={
        "produit_id": pid_bar, "quantite": 1, "prix_achat_unitaire": 5})
    assert r.status_code == 422

    emp = _employe(db)
    p = client.post("/api/pos/paiements-employes?lieu=PISCINE", headers=H, json={
        "employe_id": emp.id, "montant": 3000})
    assert p.status_code == 201 and p.json()["lieu"] == "PISCINE"
    assert any(x["id"] == p.json()["id"] for x in client.get("/api/pos/paiements-employes?lieu=PISCINE", headers=H).json())
    assert not any(x["id"] == p.json()["id"] for x in client.get("/api/pos/paiements-employes?lieu=DEVANT", headers=H).json())


def test_rapport_caisse_institution_separe_piscine(client, db):
    d = client.get("/api/caisse/rapport", headers=H).json()
    ad = d["autres_departements"]
    for k in ("ventes_piscine", "achats_piscine", "depenses_piscine", "payroll_piscine", "depenses_bar", "hotel_usd"):
        assert k in ad
    assert ad["ventes_piscine"] > 0


def test_lieu_invalide_refuse(client):
    assert client.get("/api/pos/ventes?lieu=CUISINE", headers=H).status_code == 422


# ══════════════════════════════════════════════════════════════════
# HÔTEL — paiements en gourdes ou en dollars
# ══════════════════════════════════════════════════════════════════

def _chambre(client, prix=5000):
    r = client.post("/api/hotel/chambres", headers=H, json={"numero": f"C{_num()}", "prix_nuit": prix})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _sejour(client, chambre_id, **kw):
    body = {"chambre_id": chambre_id, "client_nom": "Client Test", "client_id_piece": "NIF-1",
            "type_sejour": "NUIT", "date_arrivee": datetime.now(timezone.utc).isoformat(), "nb_nuits": 2}
    body.update(kw)
    return client.post("/api/hotel/reservations", headers=H, json=body)


def test_paiement_en_dollars_a_l_enregistrement(client, db):
    ch = _chambre(client, prix=5000)          # 2 nuits = 10 000 G
    r = _sejour(client, ch, montant_paye=50, devise_paiement="USD", taux_change=130)
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["paye_usd"] == 50.0
    assert d["paye_htg"] == 0.0
    assert d["montant_paye"] == 6500.0        # 50 $ × 130
    assert d["solde"] == 3500.0
    assert d["paiements"][0]["devise"] == "USD" and d["paiements"][0]["taux_change"] == 130.0

    # Complément en gourdes
    r2 = client.post(f"/api/hotel/reservations/{d['id']}/paiement", headers=H,
                     json={"montant": 3500, "devise": "HTG", "mode_paiement": "CASH"})
    assert r2.status_code == 200, r2.text
    d2 = r2.json()
    assert d2["paye_htg"] == 3500.0 and d2["paye_usd"] == 50.0
    assert d2["solde"] == 0.0


def test_dollars_trop_eleves_refuses(client, db):
    ch = _chambre(client, prix=1000)          # 2 000 G
    r = _sejour(client, ch, montant_paye=100, devise_paiement="USD", taux_change=130)
    assert r.status_code == 422


def test_paiement_gourdes_inchange(client, db):
    ch = _chambre(client, prix=1000)
    r = _sejour(client, ch, montant_paye=1500)
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["paye_htg"] == 1500.0 and d["paye_usd"] == 0.0 and d["solde"] == 500.0


def test_taux_par_defaut_et_config(client, db):
    assert client.put("/api/hotel/config", headers=H, json={"taux_usd": 132.5}).status_code == 200
    assert client.get("/api/hotel/config", headers=H).json()["taux_usd"] == 132.5
    ch = _chambre(client, prix=5000)
    d = _sejour(client, ch, montant_paye=10, devise_paiement="USD").json()
    assert d["paiements"][0]["taux_change"] == 132.5
    assert d["montant_paye"] == 1325.0


def test_totaux_separes_dashboard_rapport_journal_et_caisse(client, db):
    dash = client.get("/api/hotel/dashboard", headers=H).json()["kpi"]
    assert dash["encaisse_usd"] >= 50
    assert dash["caisse_usd"] == dash["encaisse_usd"]
    # La caisse gourdes ne contient pas les dollars
    assert round(dash["caisse_disponible"] - dash["renflouements_periode"] + dash["depenses_periode"], 2) == dash["encaisse_htg"]

    rap = client.get("/api/hotel/rapport", headers=H).json()["kpis"]
    assert rap["encaisse_usd"] == dash["encaisse_usd"]
    assert rap["encaisse_htg"] == dash["encaisse_htg"]

    j = client.get("/api/hotel/paiements", headers=H).json()
    assert j["total_usd"] >= 60 and j["total_htg"] > 0
    assert all(p["devise"] in ("HTG", "USD") for p in j["paiements"])

    caisse = client.get("/api/caisse/rapport", headers=H).json()["autres_departements"]
    assert caisse["hotel_usd"] == dash["encaisse_usd"]
    assert caisse["ventes_hotel"] == dash["encaisse_htg"]

    assert client.get("/api/hotel/rapport/pdf", headers=H).status_code == 200
    assert client.get("/api/hotel/rapport/xlsx", headers=H).status_code == 200


def test_reprise_paiements_historiques(client, db):
    from database import _reprise_piscine_et_paiements_hotel
    from models import HotelReservation, HotelPaiement
    ch = _chambre(client, prix=800)
    r = HotelReservation(chambre_id=ch, client_nom="Ancien", type_sejour="NUIT",
                         date_arrivee=datetime.now(timezone.utc) - timedelta(days=3),
                         date_depart_prevue=datetime.now(timezone.utc) - timedelta(days=2),
                         nb_nuits=1, prix_unitaire=Decimal("800"), montant_total=Decimal("800"),
                         montant_paye=Decimal("800"), solde=Decimal("0"), statut="TERMINEE")
    db.add(r); db.commit()
    _reprise_piscine_et_paiements_hotel()
    _reprise_piscine_et_paiements_hotel()   # idempotent
    lignes = db.query(HotelPaiement).filter_by(reservation_id=r.id).all()
    assert len(lignes) == 1
    assert lignes[0].devise == "HTG" and lignes[0].origine == "HISTORIQUE" and float(lignes[0].montant) == 800.0


def test_fiche_pdf_avec_dollars(client, db):
    ch = _chambre(client, prix=5000)
    d = _sejour(client, ch, montant_paye=20, devise_paiement="USD", taux_change=130).json()
    assert client.get(f"/api/hotel/reservations/{d['id']}/fiche.pdf", headers=H).status_code == 200
