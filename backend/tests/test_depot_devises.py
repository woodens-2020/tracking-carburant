"""
v28 — Dépôt central (achats/réception, transferts, ventes, caisse) et
argent reçu en dollars dans tous les départements, toujours séparé des
gourdes.

Lancer seul : cd backend && python -m pytest tests/test_depot_devises.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="konekta-depot-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMP, "tests.db").replace("\\", "/")
os.environ["ADMIN_API_KEY"] = "test-depot-key"
os.environ["OTP_ENABLED"] = "false"
os.environ["SECURE_COOKIES"] = "false"

import pytest
from datetime import date, timedelta
from fastapi.testclient import TestClient

# Période large (hier → demain) : la base SQLite de test stocke l'heure UTC
# sans fuseau ; autour de minuit, « aujourd'hui » diffère entre UTC et Haïti.
PER = f"date_debut={date.today() - timedelta(days=1)}&date_fin={date.today() + timedelta(days=1)}"

H = {"X-API-Key": "test-depot-key"}
_n = [0]


@pytest.fixture(scope="module")
def client():
    import main
    with TestClient(main.app) as c:
        from database import SessionLocal
        from models import Utilisateur
        s = SessionLocal()
        if not s.query(Utilisateur).filter_by(username="admin").first():
            s.add(Utilisateur(username="admin", password_hash="x", nom_complet="Admin", role="admin", actif=True))
            s.commit()
        s.close()
        c.put("/api/devise/taux", headers=H, json={"taux_usd": 130})
        yield c


def _produit(client, prix=100, lieu=None):
    _n[0] += 1
    r = client.post("/api/pos/produits", headers=H, json={
        "nom": f"Prod D{_n[0]}", "categorie": "boisson", "prix_initial": prix, "lieu": lieu})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _achat_recu(client, pid, qte=20, prix=60):
    a = client.post("/api/depot/achats", headers=H, json={
        "fournisseur": "Brana", "numero_facture": "F-1",
        "lignes": [{"produit_id": pid, "quantite": qte, "prix_achat": prix}]}).json()
    r = client.post(f"/api/depot/achats/{a['id']}/reception", headers=H, json={
        "lignes": [{"ligne_id": a["lignes"][0]["id"], "quantite_recue": qte}]})
    assert r.status_code == 200 and r.json()["conforme"], r.text
    return a


def _stock(client, pid):
    return next(p for p in client.get("/api/depot/produits", headers=H).json() if p["id"] == pid)


# ── Dépôt : achats & réception ───────────────────────────────────

def test_achat_total_et_reception_conforme(client):
    pid = _produit(client)
    a = client.post("/api/depot/achats", headers=H, json={
        "fournisseur": "Brana", "lignes": [{"produit_id": pid, "quantite": 24, "prix_achat": 50}]})
    assert a.status_code == 201
    a = a.json()
    assert a["total"] == 1200.0 and a["statut"] == "EN_ATTENTE"
    assert _stock(client, pid)["stock_depot"] == 0          # pas encore reçu
    r = client.post(f"/api/depot/achats/{a['id']}/reception", headers=H, json={
        "lignes": [{"ligne_id": a["lignes"][0]["id"], "quantite_recue": 24}]}).json()
    assert r["conforme"] and r["achat"]["statut"] == "RECEPTIONNE"
    s = _stock(client, pid)
    assert s["stock_depot"] == 24 and s["cout_moyen"] == 50.0


def test_reception_avec_ecart_puis_correction(client):
    pid = _produit(client)
    a = client.post("/api/depot/achats", headers=H, json={
        "fournisseur": "Brana", "lignes": [{"produit_id": pid, "quantite": 10, "prix_achat": 40}]}).json()
    lid = a["lignes"][0]["id"]
    r = client.post(f"/api/depot/achats/{a['id']}/reception", headers=H, json={
        "lignes": [{"ligne_id": lid, "quantite_recue": 8}]}).json()
    assert not r["conforme"] and r["achat"]["statut"] == "ECART" and r["ecarts"]
    assert _stock(client, pid)["stock_depot"] == 0
    # Correction de l'achat (8 facturés) puis réception conforme
    a2 = client.put(f"/api/depot/achats/{a['id']}", headers=H, json={
        "fournisseur": "Brana", "lignes": [{"produit_id": pid, "quantite": 8, "prix_achat": 40}]}).json()
    r = client.post(f"/api/depot/achats/{a['id']}/reception", headers=H, json={
        "lignes": [{"ligne_id": a2["lignes"][0]["id"], "quantite_recue": 8}]}).json()
    assert r["conforme"] and _stock(client, pid)["stock_depot"] == 8


# ── Dépôt : transferts, ventes, caisse ───────────────────────────

def test_transfert_vers_bar_piscine_met_le_stock_a_jour(client):
    pid = _produit(client, prix=150)
    _achat_recu(client, pid, qte=20)
    t = client.post("/api/depot/transactions", headers=H, json={
        "destination": "PISCINE", "lignes": [{"produit_id": pid, "quantite": 6}]})
    assert t.status_code == 201, t.text
    t = t.json()
    assert t["statut"] == "VALIDEE" and t["total"] == 900.0 and t["type"] == "TRANSFERT"
    s = _stock(client, pid)
    assert s["stock_depot"] == 14 and s["stock_piscine"] == 6


def test_achat_reception_transfert_en_caisses(client):
    """Articles du dépôt comptés en caisses : 1 caisse = 24 unités."""
    pid = _produit(client, prix=100)
    assert client.put(f"/api/depot/produits/{pid}/config", headers=H, json={"unites_par_caisse": 24}).json()["unites_par_caisse"] == 24
    a = client.post("/api/depot/achats", headers=H, json={"fournisseur": "Brana", "lignes": [
        {"produit_id": pid, "nb_caisses": 3, "nb_unites": 6, "prix_caisse": 1250}]})
    assert a.status_code == 201, a.text
    a = a.json()
    l = a["lignes"][0]
    assert l["quantite"] == 78 and l["quantite_caisses"] == {"caisses": 3, "unites": 6.0}
    assert l["prix_caisse"] == 1250.0 and a["total"] == 4062.5          # 3 cs × 1250 + 6 × 1250/24
    # Réception : 3 caisses comptées (6 unités manquent) → écart
    r = client.post(f"/api/depot/achats/{a['id']}/reception", headers=H, json={
        "lignes": [{"ligne_id": l["id"], "nb_caisses": 3, "nb_unites": 0}]}).json()
    assert not r["conforme"] and r["achat"]["lignes"][0]["ecart_caisses"] == {"caisses": 0, "unites": -6.0}
    r = client.post(f"/api/depot/achats/{a['id']}/reception", headers=H, json={
        "lignes": [{"ligne_id": l["id"], "nb_caisses": 3, "nb_unites": 6}]}).json()
    assert r["conforme"]
    s = _stock(client, pid)
    assert s["stock_depot"] == 78 and s["stock_depot_caisses"] == {"caisses": 3, "unites": 6.0}
    assert s["unites_par_caisse"] == 24 and s["prix_vente_caisse"] == 2400.0
    assert s["dernier_prix_achat_caisse"] == 1250.0
    # Transfert de 2 caisses vers Bar Devant
    t = client.post("/api/depot/transactions", headers=H, json={
        "destination": "DEVANT", "lignes": [{"produit_id": pid, "nb_caisses": 2}]}).json()
    assert t["total"] == 4800.0 and t["lignes"][0]["quantite_caisses"] == {"caisses": 2, "unites": 0.0}
    s = _stock(client, pid)
    assert s["stock_depot"] == 30 and s["stock_devant"] == 48


def _creer(client, nom, cat="boisson", prix=100):
    r = client.post("/api/pos/produits", headers=H, json={"nom": nom, "categorie": cat, "prix_initial": prix})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_seuls_les_articles_du_bar_au_depot(client):
    ids = {
        "boisson": _creer(client, "Heineken Dépôt"),
        "cabri":   _creer(client, "Cabri en sauce", cat="plat", prix=800),
        "poulet":  _creer(client, "Poulet frit", cat="boisson", prix=600),     # mal classé : exclu par le nom
        "carte":   _creer(client, "Carte piscine adulte", prix=250),
    }
    noms = {p["id"] for p in client.get("/api/depot/produits", headers=H).json()}
    assert ids["boisson"] in noms
    assert not ({ids["cabri"], ids["poulet"], ids["carte"]} & noms)
    tous = {p["id"]: p["au_depot"] for p in client.get("/api/depot/produits?tous=true", headers=H).json()}
    assert tous[ids["cabri"]] is False
    # Un plat ne peut pas être acheté ni transféré par le dépôt
    assert client.post("/api/depot/achats", headers=H, json={"fournisseur": "X", "lignes": [
        {"produit_id": ids["cabri"], "quantite": 1, "prix_achat": 1}]}).status_code == 422
    # Ajouter / retirer à la main
    assert client.put(f"/api/depot/produits/{ids['poulet']}/depot", headers=H, json={"au_depot": True}).status_code == 200
    assert ids["poulet"] in {p["id"] for p in client.get("/api/depot/produits", headers=H).json()}
    assert client.put(f"/api/depot/produits/{ids['poulet']}/depot", headers=H, json={"au_depot": False}).status_code == 200
    assert ids["poulet"] not in {p["id"] for p in client.get("/api/depot/produits", headers=H).json()}


def test_creer_article_et_ajuster_stock(client):
    r = client.post("/api/depot/produits", headers=H, json={
        "nom": "Malta H", "categorie": "soft", "unites_par_caisse": 24, "prix_vente": 125, "seuil_min": 48})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    s = _stock(client, pid)
    assert s["unites_par_caisse"] == 24 and s["prix_vente"] == 125.0 and s["stock_depot"] == 0
    assert s["demarre"] is False
    # Pas encore démarré : « Ajuster » refusé, « Démarrer » demande la quantité de départ
    assert client.post(f"/api/depot/produits/{pid}/ajustement", headers=H, json={"nb_caisses": 1}).status_code == 409
    a = client.post(f"/api/depot/produits/{pid}/demarrer", headers=H, json={"nb_caisses": 5, "nb_unites": 3}).json()
    assert a["apres"] == 123 and a["difference"] == 123 and a["apres_caisses"] == {"caisses": 5, "unites": 3.0}
    assert _stock(client, pid)["demarre"] is True
    assert client.post(f"/api/depot/produits/{pid}/demarrer", headers=H, json={"nb_caisses": 1}).status_code == 409
    a = client.post(f"/api/depot/produits/{pid}/ajustement", headers=H, json={"nb_caisses": 4}).json()
    assert a["difference"] == -27
    assert _stock(client, pid)["stock_depot"] == 96
    # Retirer un article qui a du stock : refusé
    assert client.put(f"/api/depot/produits/{pid}/depot", headers=H, json={"au_depot": False}).status_code == 409


def test_remise_a_zero_du_depot(client):
    pid = _produit(client)
    _achat_recu(client, pid, qte=10)
    client.post("/api/depot/transactions", headers=H, json={"destination": "DEVANT", "lignes": [{"produit_id": pid, "quantite": 4}]})
    assert _stock(client, pid)["stock_devant"] == 4
    assert client.post("/api/depot/reinitialiser", headers=H, json={"confirmation": "oui"}).status_code == 422
    r = client.post("/api/depot/reinitialiser", headers=H, json={"confirmation": "REMETTRE A ZERO"})
    assert r.status_code == 200, r.text
    s = _stock(client, pid)
    assert s["stock_depot"] == 0 and s["stock_devant"] == 0
    assert client.get("/api/depot/achats", headers=H).json() == []
    assert client.get("/api/depot/transactions", headers=H).json() == []
    assert client.get("/api/depot/caisse", headers=H).json()["balance_htg"] == 0


def test_stock_depot_hors_stock_bar(client):
    """Le stock du dépôt n'est pas du stock bar : ni affiché ni vendable au Bar."""
    pid = _produit(client)
    _achat_recu(client, pid, qte=12)
    p = next(x for x in client.get("/api/pos/produits", headers=H).json() if x["id"] == pid)
    assert p["stock_courant"] == 0
    s = next(x for x in client.get("/api/pos/stock", headers=H).json() if x["produit_id"] == pid)
    assert s["stock_courant"] == 0
    assert client.post("/api/pos/ventes", headers=H, json={"lignes": [{"produit_id": pid, "quantite": 1}]}).status_code == 422


def test_stock_insuffisant_refuse(client):
    pid = _produit(client)
    _achat_recu(client, pid, qte=3)
    r = client.post("/api/depot/transactions", headers=H, json={
        "destination": "DEVANT", "lignes": [{"produit_id": pid, "quantite": 5}]})
    assert r.status_code == 422 and "Stock insuffisant" in r.text


def test_vente_client_paiement_partiel_puis_dollars(client):
    pid = _produit(client, prix=200)
    _achat_recu(client, pid, qte=10)
    t = client.post("/api/depot/transactions", headers=H, json={
        "destination": "CLIENT", "client_nom": "Resto X", "lignes": [{"produit_id": pid, "quantite": 5}]}).json()
    assert t["total"] == 1000.0 and t["statut_paiement"] == "NON_PAYEE"
    p1 = client.post(f"/api/depot/transactions/{t['id']}/paiements", headers=H,
                     json={"montant": 350, "devise": "HTG", "methode": "CASH"}).json()
    assert p1["facture"]["statut_paiement"] == "PARTIELLE"
    p2 = client.post(f"/api/depot/transactions/{t['id']}/paiements", headers=H,
                     json={"montant": 5, "devise": "USD", "taux_change": 130, "methode": "ZELLE"})
    assert p2.status_code == 201, p2.text
    f = p2.json()["facture"]
    assert f["statut_paiement"] == "PAYEE" and f["paye_htg"] == 350.0 and f["paye_usd"] == 5.0
    # trop payé : refusé
    assert client.post(f"/api/depot/transactions/{t['id']}/paiements", headers=H,
                       json={"montant": 10, "methode": "CASH"}).status_code == 409

    j = client.get("/api/depot/paiements", headers=H).json()
    assert j["total_usd"] >= 5 and j["total_htg"] >= 350
    caisse = client.get("/api/depot/caisse", headers=H).json()
    assert caisse["balance_usd"] == j["total_usd"] and caisse["balance_htg"] == j["total_htg"]

    inst = client.get("/api/caisse/rapport", headers=H).json()["autres_departements"]
    assert inst["ventes_depot"] >= 350 and inst["dollars"]["depot"] >= 5


def test_annulation_remet_le_stock(client):
    pid = _produit(client)
    _achat_recu(client, pid, qte=10)
    t = client.post("/api/depot/transactions", headers=H, json={
        "destination": "DEVANT", "lignes": [{"produit_id": pid, "quantite": 4}]}).json()
    assert _stock(client, pid)["stock_devant"] == 4
    assert client.post(f"/api/depot/transactions/{t['id']}/annuler", headers=H).status_code == 200
    s = _stock(client, pid)
    assert s["stock_depot"] == 10 and s["stock_devant"] == 0


def test_config_prix_et_rapport(client):
    pid = _produit(client, prix=100)
    r = client.put(f"/api/depot/produits/{pid}/config", headers=H, json={"prix_vente": 175, "seuil_min": 5})
    assert r.json()["prix_vente"] == 175.0
    assert _stock(client, pid)["alerte"] is True
    rap = client.get("/api/depot/rapport", headers=H).json()
    assert "inventaire" in rap and "caisse" in rap and len(rap["ventes_transferts"]["par_destination"]) == 3


# ── Dollars dans les autres départements ─────────────────────────

def test_vente_bar_en_dollars_hors_cash_gourdes(client):
    pid = _produit(client, prix=260)
    client.post(f"/api/pos/produits/{pid}/approvisionnement", headers=H, json={"nb_unites_vrac": 10, "lieu": "DEVANT"})
    avant = client.get(f"/api/pos/dashboard/bar?{PER}", headers=H).json()["kpis"]
    v = client.post("/api/pos/ventes?lieu=DEVANT", headers=H, json={
        "lignes": [{"produit_id": pid, "quantite": 1}], "devise": "USD", "taux_change": 130})
    assert v.status_code == 201, v.text
    assert v.json()["devise"] == "USD" and v.json()["montant_devise"] == 2.0
    apres = client.get(f"/api/pos/dashboard/bar?{PER}", headers=H).json()["kpis"]
    assert apres["cash_jour"] == avant["cash_jour"]               # gourdes inchangées
    assert round(apres["cash_usd"] - avant.get("cash_usd", 0), 2) == 2.0
    assert client.get(f"/api/pos/grande-caisse?{PER}", headers=H).json()["caisse"]["disponible_usd"] >= 2.0


def test_cuisine_patisserie_piscine_en_dollars(client):
    c = client.post("/api/cuisine/ventes", headers=H, json={
        "lignes": [{"nom_plat": "Riz", "quantite": 1, "prix_unitaire": 650}], "devise": "USD", "taux_change": 130})
    assert c.status_code in (200, 201), c.text
    assert c.json()["devise"] == "USD" and c.json()["montant_devise"] == 5.0

    client.put("/api/piscine/config", headers=H, json={"prix_carte": 260})
    p = client.post("/api/piscine/ventes", headers=H, json={"quantite": 1, "devise": "USD"}).json()
    assert p["devise"] == "USD" and p["montant_devise"] == 2.0
    d = client.get(f"/api/piscine/dashboard?{PER}", headers=H).json()["kpi"]
    assert d["recu_usd"] >= 2.0

    inst = client.get("/api/caisse/rapport", headers=H).json()["autres_departements"]["dollars"]
    assert inst["cuisine"] >= 5.0 and inst["piscine"] >= 2.0 and inst["bar"] >= 2.0
    assert inst["total"] >= inst["cuisine"] + inst["piscine"] + inst["bar"]


def test_taux_commun(client):
    assert client.put("/api/devise/taux", headers=H, json={"taux_usd": 135}).status_code == 200
    assert client.get("/api/devise/taux", headers=H).json()["taux_usd"] == 135.0
    assert client.get("/api/hotel/config", headers=H).json()["taux_usd"] == 135.0



# ── Démarrage du dépôt, transfert « comme une vente », journal ──────────

def test_demarrage_groupe_et_remise_a_zero(client):
    a, b = _produit(client), _produit(client)
    r = client.post("/api/depot/demarrer", headers=H, json={"lignes": [
        {"produit_id": a, "nb_unites": 12}, {"produit_id": b, "nb_unites": 0}]})
    assert r.status_code == 201, r.text
    assert _stock(client, a)["stock_depot"] == 12 and _stock(client, b)["demarre"] is True
    client.post("/api/depot/reinitialiser", headers=H, json={"confirmation": "REMETTRE A ZERO"})
    s = _stock(client, a)
    assert s["stock_depot"] == 0 and s["demarre"] is False


def test_remise_a_zero_initiale_une_seule_fois(client):
    from database import _depot_remise_a_zero_initiale, SessionLocal
    from models import AuditLog
    pid = _produit(client)
    client.post(f"/api/depot/produits/{pid}/demarrer", headers=H, json={"nb_unites": 7})
    s = SessionLocal()
    deja = s.query(AuditLog).filter(AuditLog.action == "depot_demarrage_v1").count()
    s.close()
    assert deja == 1                       # fait au lancement de l'application
    _depot_remise_a_zero_initiale()        # relancé : ne refait rien
    assert _stock(client, pid)["stock_depot"] == 7
    # Sans le marqueur (base de prod avant ce déploiement) : tout est effacé
    s = SessionLocal()
    s.query(AuditLog).filter(AuditLog.action == "depot_demarrage_v1").delete()
    s.commit(); s.close()
    _depot_remise_a_zero_initiale()
    st = _stock(client, pid)
    assert st["stock_depot"] == 0 and st["demarre"] is False
    s = SessionLocal()
    assert s.query(AuditLog).filter(AuditLog.action == "depot_demarrage_v1").count() == 1
    s.close()


def test_transfert_comme_une_vente_et_journal(client):
    pid = _produit(client, prix=150)
    client.post(f"/api/depot/produits/{pid}/demarrer", headers=H, json={"nb_unites": 30})
    r = client.post("/api/depot/transactions", headers=H, json={
        "destination": "PISCINE", "recu_par": "Marie (bar piscine)",
        "lignes": [{"produit_id": pid, "quantite": 10}],
        "paiement": {"montant": 1000, "devise": "HTG", "methode": "CASH"}})
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["recu_par"] == "Marie (bar piscine)" and t["total"] == 1500.0
    assert t["statut"] == "VALIDEE" and t["statut_paiement"] == "PARTIELLE" and t["paye_htg"] == 1000.0
    s = _stock(client, pid)
    assert s["stock_depot"] == 20 and s["stock_piscine"] == 10
    # Le stock du bar piscine est celui que voient les caisses du bar
    from database import SessionLocal
    from pos_service import stock_courant
    db = SessionLocal()
    assert stock_courant(pid, db, lieu="PISCINE") == 10
    db.close()
    j = client.get(f"/api/depot/transferts/journal?{PER}&produit_id={pid}", headers=H).json()
    assert len(j["lignes"]) == 1
    l = j["lignes"][0]
    assert l["destination"] == "PISCINE" and l["recu_par"] == "Marie (bar piscine)"
    assert l["quantite"] == 10 and l["montant"] == 1500.0
    assert j["par_article"][0]["par_destination"] == {"Bar Piscine": 10.0}
    # Paiement sans validation : refusé
    assert client.post("/api/depot/transactions", headers=H, json={
        "destination": "DEVANT", "valider": False, "lignes": [{"produit_id": pid, "quantite": 1}],
        "paiement": {"montant": 10}}).status_code == 422
