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
from datetime import date, timedelta
from fastapi.testclient import TestClient

# Période large (hier → demain) : la base SQLite de test stocke l'heure UTC
# sans fuseau ; autour de minuit, « aujourd'hui » diffère entre UTC et Haïti.
PER = f"date_debut={date.today() - timedelta(days=1)}&date_fin={date.today() + timedelta(days=1)}"

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

    gp = client.get(f"/api/pos/grande-caisse?lieu=PISCINE&{PER}", headers=H).json()
    gb = client.get(f"/api/pos/grande-caisse?lieu=DEVANT&{PER}", headers=H).json()
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
    for k in ("ventes_piscine", "depenses_piscine", "depenses_bar", "hotel_usd"):
        assert k in ad
    # Les ventes du comptoir « Bar Piscine » restent dans le Bar
    assert ad["ventes_bar"] > 0


# ══════════════════════════════════════════════════════════════════
# PISCINE — vente de cartes
# ══════════════════════════════════════════════════════════════════

def test_vente_cartes_sans_prix_refusee(client, db):
    from models import PiscineConfig
    if db.get(PiscineConfig, 1) is None:
        r = client.post("/api/piscine/ventes", headers=H, json={"quantite": 2})
        assert r.status_code == 422


def test_vente_cartes_calculs_et_rabais(client, db):
    assert client.put("/api/piscine/config", headers=H, json={"prix_carte": 250}).status_code == 200
    assert client.get("/api/piscine/config", headers=H).json()["prix_carte"] == 250.0

    r = client.post("/api/piscine/ventes", headers=H, json={"quantite": 4, "lieu_vente": "DEVANT"})
    assert r.status_code == 201, r.text
    v = r.json()
    assert v["montant_brut"] == 1000.0 and v["rabais_montant"] == 0 and v["montant_net"] == 1000.0
    assert v["lieu_libelle"] == "Devant"

    r = client.post("/api/piscine/ventes", headers=H, json={
        "quantite": 2, "lieu_vente": "DERRIERE", "rabais_type": "POURCENT", "rabais_valeur": 10})
    v = r.json()
    assert v["montant_brut"] == 500.0 and v["rabais_montant"] == 50.0 and v["montant_net"] == 450.0

    r = client.post("/api/piscine/ventes", headers=H, json={
        "quantite": 1, "rabais_type": "MONTANT", "rabais_valeur": 100})
    v = r.json()
    assert v["lieu_vente"] is None and v["montant_net"] == 150.0

    # Rabais supérieur au montant / pourcentage > 100 : refusés
    assert client.post("/api/piscine/ventes", headers=H, json={
        "quantite": 1, "rabais_type": "MONTANT", "rabais_valeur": 300}).status_code == 422
    assert client.post("/api/piscine/ventes", headers=H, json={
        "quantite": 1, "rabais_type": "POURCENT", "rabais_valeur": 120}).status_code == 422
    assert client.post("/api/piscine/ventes", headers=H, json={
        "quantite": 1, "lieu_vente": "COTE"}).status_code == 422


def test_dashboard_piscine_et_depenses(client, db):
    client.put("/api/piscine/config", headers=H, json={"prix_carte": 200})
    v = client.post("/api/piscine/ventes", headers=H, json={"quantite": 3, "lieu_vente": "DEVANT"}).json()
    dep = client.post("/api/piscine/depenses", headers=H, json={"description": "Chlore", "montant": 300})
    assert dep.status_code == 201, dep.text

    d = client.get(f"/api/piscine/dashboard?{PER}", headers=H).json()
    k = d["kpi"]
    assert k["nb_cartes"] >= 3
    assert k["depenses"] >= 300
    assert round(k["montant_net"] - k["depenses"], 2) == k["solde"]
    assert round(k["montant_brut"] - k["rabais"], 2) == k["montant_net"]
    assert d["par_lieu"]["DEVANT"]["nb_cartes"] >= 3
    assert sum(j["nb_cartes"] for j in d["evolution"]) == k["nb_cartes"]

    # Annulation : la vente sort des totaux
    avant = k["nb_cartes"]
    assert client.post(f"/api/piscine/ventes/{v['id']}/annuler", headers=H).status_code == 200
    apres = client.get(f"/api/piscine/dashboard?{PER}", headers=H).json()["kpi"]["nb_cartes"]
    assert apres == avant - 3

    lst = client.get(f"/api/piscine/depenses?{PER}", headers=H).json()
    assert any(x["id"] == dep.json()["id"] for x in lst["depenses"])
    assert client.delete(f"/api/piscine/depenses/{dep.json()['id']}", headers=H).status_code == 200

    caisse = client.get("/api/caisse/rapport", headers=H).json()["autres_departements"]
    assert caisse["ventes_piscine"] == client.get(f"/api/piscine/dashboard?{PER}", headers=H).json()["kpi"]["montant_net"]


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
    dash = client.get(f"/api/hotel/dashboard?{PER}", headers=H).json()["kpi"]
    assert dash["encaisse_usd"] >= 50
    assert dash["caisse_usd"] == dash["encaisse_usd"]
    # La caisse gourdes ne contient pas les dollars
    assert round(dash["caisse_disponible"] - dash["renflouements_periode"] + dash["depenses_periode"], 2) == dash["encaisse_htg"]

    rap = client.get(f"/api/hotel/rapport?{PER}", headers=H).json()["kpis"]
    assert rap["encaisse_usd"] == dash["encaisse_usd"]
    assert rap["encaisse_htg"] == dash["encaisse_htg"]

    j = client.get(f"/api/hotel/paiements?{PER}", headers=H).json()
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


# ══════════════════════════════════════════════════════════════════
# HÔTEL — prix des chambres en dollars, tarif choisi par le client
# ══════════════════════════════════════════════════════════════════

def test_prix_chambre_en_dollars(client, db):
    r = client.post("/api/hotel/chambres", headers=H, json={
        "numero": f"U{_num()}", "prix_nuit": 2600, "prix_nuit_usd": 20,
        "prix_moment": 1000, "prix_moment_usd": 8})
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["prix_nuit_usd"] == 20.0 and c["prix_moment_usd"] == 8.0
    dispo = client.get("/api/hotel/chambres/disponibles", headers=H).json()
    assert any(x["id"] == c["id"] and x["prix_nuit_usd"] == 20.0 for x in dispo)
    body = {"numero": c["numero"], "prix_nuit": 2600, "prix_nuit_usd": 25}
    r = client.put(f"/api/hotel/chambres/{c['id']}", headers=H, json=body)
    assert r.status_code == 200 and r.json()["prix_nuit_usd"] == 25.0 and r.json()["prix_moment_usd"] is None


def test_sejour_au_tarif_dollars(client, db):
    r = client.post("/api/hotel/chambres", headers=H, json={
        "numero": f"U{_num()}", "prix_nuit": 2600, "prix_nuit_usd": 20})
    ch = r.json()["id"]
    # 2 nuits × 20 $ = 40 $, taux 131 → 5 240 G ; payé 30 $
    r = _sejour(client, ch, devise_tarif="USD", devise_paiement="USD", taux_change=131, montant_paye=30)
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["devise_tarif"] == "USD"
    assert d["montant_total_usd"] == 40.0 and d["prix_unitaire_usd"] == 20.0
    assert d["montant_total"] == 5240.0 and d["taux_tarif"] == 131.0
    assert d["paye_usd"] == 30.0 and d["solde_usd"] == 10.0 and d["solde"] == 1310.0
    # Solde payé en dollars sans taux : celui de l'arrivée s'applique → soldé exactement
    client.put("/api/hotel/config", headers=H, json={"taux_usd": 140})
    r2 = client.post(f"/api/hotel/reservations/{d['id']}/paiement", headers=H,
                     json={"montant": 10, "devise": "USD"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["solde"] == 0.0 and r2.json()["paye_usd"] == 40.0
    assert client.get(f"/api/hotel/reservations/{d['id']}/fiche.pdf", headers=H).status_code == 200


def test_tarif_dollars_rabais_et_chambre_sans_prix_usd(client, db):
    r = client.post("/api/hotel/chambres", headers=H, json={
        "numero": f"U{_num()}", "prix_nuit": 2600, "prix_nuit_usd": 20})
    ch = r.json()["id"]
    r = _sejour(client, ch, devise_tarif="USD", devise_paiement="USD", taux_change=130, montant_facture=35)
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["montant_total_usd"] == 35.0 and d["montant_total"] == 4550.0 and d["solde_usd"] == 35.0
    # Chambre sans prix dollars : tarif USD refusé
    ch2 = _chambre(client, prix=2000)
    assert _sejour(client, ch2, devise_tarif="USD", devise_paiement="USD", taux_change=130).status_code == 422
    # Tarif gourdes par défaut : aucun champ dollars
    d3 = _sejour(client, _chambre(client, prix=2000)).json()
    assert d3["devise_tarif"] == "HTG" and d3["montant_total_usd"] is None and d3["solde_usd"] is None


# ══════════════════════════════════════════════════════════════════
# PISCINE — cartes Adulte / Jeune / Enfant, correction par l'admin
# ══════════════════════════════════════════════════════════════════

def test_prix_par_categorie_et_vente(client, db):
    r = client.put("/api/piscine/config", headers=H, json={"prix_adulte": 500, "prix_jeune": 300, "prix_enfant": 200})
    assert r.status_code == 200, r.text
    c = r.json()
    assert (c["prix_adulte"], c["prix_jeune"], c["prix_enfant"], c["prix_carte"]) == (500, 300, 200, 500)
    assert [x["code"] for x in c["categories"]] == ["ADULTE", "JEUNE", "ENFANT"]
    # 2 adultes + 1 jeune + 3 enfants = 1000 + 300 + 600 = 1900, rabais 100
    r = client.post("/api/piscine/ventes", headers=H, json={
        "adulte": 2, "jeune": 1, "enfant": 3, "rabais_type": "MONTANT", "rabais_valeur": 100, "lieu_vente": "DEVANT"})
    assert r.status_code == 201, r.text
    v = r.json()
    assert v["quantite"] == 6 and v["montant_brut"] == 1900 and v["montant_net"] == 1800
    assert v["detail"] == "2 Adulte · 1 Jeune · 3 Enfant"
    assert {x["code"]: x["montant"] for x in v["categories"]} == {"ADULTE": 1000, "JEUNE": 300, "ENFANT": 600}
    # Ancien écran (quantite seule) = cartes Adulte
    v2 = client.post("/api/piscine/ventes", headers=H, json={"quantite": 1}).json()
    assert v2["montant_net"] == 500 and v2["detail"] == "1 Adulte"
    # Catégorie sans prix : refusée
    client.put("/api/piscine/config", headers=H, json={"prix_enfant": 0})
    assert client.post("/api/piscine/ventes", headers=H, json={"enfant": 1}).status_code == 422
    client.put("/api/piscine/config", headers=H, json={"prix_enfant": 200})
    assert client.post("/api/piscine/ventes", headers=H, json={}).status_code == 422
    lst = client.get(f"/api/piscine/ventes?{PER}", headers=H).json()
    cats = {x["code"]: x["nb_cartes"] for x in lst["par_categorie"]}
    assert cats["ADULTE"] >= 3 and cats["JEUNE"] >= 1 and cats["ENFANT"] >= 3
    dash = client.get(f"/api/piscine/dashboard?{PER}", headers=H).json()
    assert dash["prix"] == {"ADULTE": 500, "JEUNE": 300, "ENFANT": 200} and dash["par_categorie"]


def test_correction_par_admin(client, db):
    client.put("/api/piscine/config", headers=H, json={"prix_adulte": 500, "prix_jeune": 300, "prix_enfant": 200})
    v = client.post("/api/piscine/ventes", headers=H, json={"adulte": 3}).json()
    # Le prix change après la vente : la correction garde les prix du jour de la vente
    client.put("/api/piscine/config", headers=H, json={"prix_adulte": 600})
    assert client.put(f"/api/piscine/ventes/{v['id']}", headers=H, json={"adulte": 2, "motif": " "}).status_code == 422
    r = client.put(f"/api/piscine/ventes/{v['id']}", headers=H, json={
        "adulte": 2, "enfant": 1, "motif": "Erreur : un enfant compté comme adulte"})
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["montant_net"] == 1200 and c["detail"] == "2 Adulte · 1 Enfant"
    assert c["corrige_par"] and c["motif_correction"].startswith("Erreur")
    client.put("/api/piscine/config", headers=H, json={"prix_adulte": 500})
    # Annulée : plus corrigeable
    client.post(f"/api/piscine/ventes/{v['id']}/annuler", headers=H)
    assert client.put(f"/api/piscine/ventes/{v['id']}", headers=H, json={"adulte": 1, "motif": "x"}).status_code == 409


def test_correction_refusee_a_une_caissiere(client, db):
    from fastapi import HTTPException
    from piscine_routes import _require_pdg_ou_admin
    from types import SimpleNamespace
    req = SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(id=999, role="caissier", role_id=None)))
    with pytest.raises(HTTPException) as e:
        _require_pdg_ou_admin(req, db)
    assert e.value.status_code == 403


def test_supprimer_sejour_saisi_par_erreur(client, db):
    ch = _chambre(client, prix=1000)
    d = _sejour(client, ch, montant_paye=20, devise_paiement="USD", taux_change=100).json()
    avant = client.get(f"/api/hotel/dashboard?{PER}", headers=H).json()["kpi"]
    assert client.post(f"/api/hotel/reservations/{d['id']}/supprimer", headers=H, json={"motif": ""}).status_code == 422
    r = client.post(f"/api/hotel/reservations/{d['id']}/supprimer", headers=H, json={"motif": "Doublon"})
    assert r.status_code == 200, r.text
    assert client.get(f"/api/hotel/reservations/{d['id']}", headers=H).status_code == 404
    apres = client.get(f"/api/hotel/dashboard?{PER}", headers=H).json()["kpi"]
    assert round(avant["encaisse_usd"] - apres["encaisse_usd"], 2) == 20.0
    chs = {c["id"]: c["statut"] for c in client.get("/api/hotel/chambres", headers=H).json()}
    assert chs[ch] == "DISPONIBLE"
    from models import AuditLog
    assert db.query(AuditLog).filter(AuditLog.action == "hotel_sejour_supprime").count() >= 1


def test_caisse_bar_devant_et_bar_piscine_separes(client, db):
    for lieu, mt in (("DEVANT", 700), ("PISCINE", 300)):
        r = client.post(f"/api/pos/depenses?lieu={lieu}", headers=H, json={
            "description": f"Glace {lieu}", "montant": mt, "lieu": lieu})
        assert r.status_code in (200, 201), r.text
    ad = client.get("/api/caisse/rapport", headers=H).json()["autres_departements"]
    b = ad["bars"]
    assert set(b) == {"DEVANT", "PISCINE"}
    assert b["DEVANT"]["libelle"] == "Bar Devant" and b["PISCINE"]["libelle"] == "Bar Piscine"
    # Les deux bars font exactement le total « bar »
    assert round(b["DEVANT"]["entrees"] + b["PISCINE"]["entrees"], 2) == ad["ventes_bar"]
    assert round(b["DEVANT"]["depenses"] + b["PISCINE"]["depenses"], 2) == ad["depenses_bar"]
    assert round(b["DEVANT"]["achats"] + b["PISCINE"]["achats"], 2) == ad["achats_bar"]
    assert b["DEVANT"]["depenses"] >= 700 and b["PISCINE"]["depenses"] >= 300
    for v in b.values():
        assert v["solde"] == round(v["entrees"] - v["sorties"], 2)


def test_historique_bars_laisse_tel_quel(client, db):
    from models import BarVente, BarAchat
    from database import _reprise_lieux_bars
    pid_p = _produit(client, lieu="PISCINE")
    v = BarVente(numero_ticket=f"H{pid_p}", montant_total=100, montant_paye=100,
                 mode_paiement="CASH", statut="PAYEE", lieu=None)
    a = BarAchat(produit_id=pid_p, quantite=5, prix_achat_unitaire=10, statut="CONFIRME", lieu=None)
    db.add_all([v, a]); db.commit()
    _reprise_lieux_bars()
    db.expire_all()
    assert db.get(BarVente, v.id).lieu is None and db.get(BarAchat, a.id).lieu is None
