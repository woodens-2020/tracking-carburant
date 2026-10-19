"""
Circuit Bar du schéma UML : réception → génération des codes → mise en dépôt
et vérification (scan) → préparation de la livraison → scan des colis →
Bar avant / Bar piscine → mise à jour des stocks (dépôt et bar).
"""
import time
from decimal import Decimal

from models import BarAchat, BarCategorie, BarProduit

H = {"X-API-Key": "smoke-test-key"}


def _produit(db, unites_par_caisse=24):
    if not db.query(BarCategorie).filter_by(nom="test-boisson").first():
        db.add(BarCategorie(nom="test-boisson"))
        db.flush()
    p = BarProduit(nom=f"Biere-{time.time_ns()}", categorie="test-boisson", unite="Bouteille",
                   actif=True, seuil_alerte_stock=0, vendu_par_caisse=True,
                   unites_par_caisse=unites_par_caisse)
    db.add(p)
    db.commit()
    return p


def _achat(db, produit, quantite):
    a = BarAchat(produit_id=produit.id, quantite=Decimal(quantite), prix_achat_unitaire=Decimal("50"),
                 statut="EN_ATTENTE")
    db.add(a)
    db.commit()
    return a


def _stock(client, produit_id):
    return next(x for x in client.get("/api/pos/depot/stock", headers=H).json()
                if x["produit_id"] == produit_id)


def test_circuit_complet(client, db):
    p = _produit(db)
    a = _achat(db, p, 60)                                   # 2 caisses de 24 + 12

    # 1-2. Réception → 3 colis à vérifier, aucun stock encore
    r = client.post(f"/api/pos/depot/receptions/{a.id}", headers=H, json={})
    assert r.status_code == 201, r.text
    colis = r.json()["colis"]
    assert [c["quantite"] for c in colis] == [24, 24, 12]
    assert all(c["statut"] == "A_VERIFIER" and c["code"].startswith("BC") for c in colis)
    assert _stock(client, p.id)["total"] == 0
    assert _stock(client, p.id)["a_verifier"] == 60

    # Étiquettes imprimables
    r = client.get(f"/api/pos/depot/etiquettes.pdf?achat_id={a.id}", headers=H)
    assert r.status_code == 200 and r.content[:4] == b"%PDF"
    assert b"/MediaBox [ 0 0 144 72 ]" in r.content          # 2 × 1 pouce
    assert b"/Count 3" in r.content                            # une étiquette par colis

    # 3. Vérification au dépôt (scan) → stock du dépôt
    for c in colis:
        r = client.post("/api/pos/depot/colis/verifier", headers=H, json={"code": c["code"].lower()})
        assert r.status_code == 200, r.text
    assert client.post("/api/pos/depot/colis/verifier", headers=H,
                       json={"code": colis[0]["code"]}).status_code == 409   # double scan
    s = _stock(client, p.id)
    assert (s["depot"], s["devant"], s["piscine"], s["total"]) == (60, 0, 0, 60)

    # 4-6. Livraison vers le Bar piscine : scan de 2 colis
    liv = client.post("/api/pos/depot/livraisons", headers=H, json={"lieu": "PISCINE"}).json()
    for c in colis[:2]:
        r = client.post(f"/api/pos/depot/livraisons/{liv['id']}/scan", headers=H,
                        json={"code": f"https://app/colis/{c['code']}"})       # URL de QR acceptée
        assert r.status_code == 200, r.text
    assert client.post(f"/api/pos/depot/livraisons/{liv['id']}/scan", headers=H,
                       json={"code": colis[0]["code"]}).status_code == 409
    # Le 3e colis est étiqueté : il ne peut pas partir en vrac
    r = client.post(f"/api/pos/depot/livraisons/{liv['id']}/vrac", headers=H,
                    json={"produit_id": p.id, "quantite": 5})
    assert r.status_code == 409

    # 7. Validation → stocks mis à jour, total inchangé
    r = client.post(f"/api/pos/depot/livraisons/{liv['id']}/valider", headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["statut"] == "VALIDEE"
    s = _stock(client, p.id)
    assert (s["depot"], s["devant"], s["piscine"], s["total"]) == (12, 0, 48, 60)
    statuts = {c["code"]: c for c in client.get(f"/api/pos/depot/colis?achat_id={a.id}", headers=H).json()}
    assert statuts[colis[0]["code"]]["statut"] == "LIVRE" and statuts[colis[0]["code"]]["lieu"] == "PISCINE"
    assert statuts[colis[2]["code"]]["statut"] == "AU_DEPOT"


def test_colis_non_verifie_ne_peut_pas_partir(client, db):
    p = _produit(db)
    a = _achat(db, p, 24)
    code = client.post(f"/api/pos/depot/receptions/{a.id}", headers=H, json={}).json()["colis"][0]["code"]
    liv = client.post("/api/pos/depot/livraisons", headers=H, json={"lieu": "DEVANT"}).json()
    r = client.post(f"/api/pos/depot/livraisons/{liv['id']}/scan", headers=H, json={"code": code})
    assert r.status_code == 409 and "vérifié" in r.json()["detail"]


def test_annuler_livraison_remet_au_depot(client, db):
    p = _produit(db)
    a = _achat(db, p, 24)
    code = client.post(f"/api/pos/depot/receptions/{a.id}", headers=H, json={}).json()["colis"][0]["code"]
    client.post("/api/pos/depot/colis/verifier", headers=H, json={"code": code})
    liv = client.post("/api/pos/depot/livraisons", headers=H, json={"lieu": "DEVANT"}).json()
    client.post(f"/api/pos/depot/livraisons/{liv['id']}/scan", headers=H, json={"code": code})
    assert client.post(f"/api/pos/depot/livraisons/{liv['id']}/annuler", headers=H).status_code == 200
    c = client.get(f"/api/pos/depot/colis/code/{code}", headers=H).json()
    assert c["statut"] == "AU_DEPOT" and c["livraison_id"] is None
    assert _stock(client, p.id)["depot"] == 24


def test_inventaire_et_livraison_en_vrac(client, db):
    """Stock existant sans étiquette : inventaire du dépôt, puis livraison en vrac."""
    p = _produit(db)
    r = client.post("/api/pos/depot/inventaire", headers=H, json={
        "lieu": "DEPOT", "motif": "Inventaire d'ouverture", "lignes": [{"produit_id": p.id, "quantite": 30}]})
    assert r.status_code == 200 and r.json()["nb_ajustes"] == 1
    liv = client.post("/api/pos/depot/livraisons", headers=H, json={"lieu": "DEVANT"}).json()
    assert client.post(f"/api/pos/depot/livraisons/{liv['id']}/vrac", headers=H,
                       json={"produit_id": p.id, "quantite": 40}).status_code == 409
    assert client.post(f"/api/pos/depot/livraisons/{liv['id']}/vrac", headers=H,
                       json={"produit_id": p.id, "quantite": 18}).status_code == 200
    assert client.post(f"/api/pos/depot/livraisons/{liv['id']}/valider", headers=H).status_code == 200
    s = _stock(client, p.id)
    assert (s["depot"], s["devant"], s["total"]) == (12, 18, 30)
    # Inventaire du Bar avant : 15 comptés au lieu de 18 → ajustement −3
    r = client.post("/api/pos/depot/inventaire", headers=H, json={
        "lieu": "DEVANT", "motif": "Comptage du soir", "lignes": [{"produit_id": p.id, "quantite": 15}]})
    assert r.json()["ajustements"][0]["ecart"] == -3
    assert _stock(client, p.id)["devant"] == 15


def test_confirmer_achat_receptionne(client, db):
    """L'ancien bouton « Confirmer » d'un achat génère maintenant les colis."""
    p = _produit(db, unites_par_caisse=12)
    a = _achat(db, p, 24)
    r = client.post(f"/api/pos/achats/{a.id}/confirmer", headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["nb_colis"] == 2
    assert _stock(client, p.id)["a_verifier"] == 24


def test_fonctions_retirees(client, db):
    p = _produit(db)
    r = client.post(f"/api/pos/produits/{p.id}/approvisionnement", headers=H,
                    json={"nb_caisses": 1, "nb_unites_vrac": 0})
    assert r.status_code in (410, 422)
    assert client.get("/api/pos/commandes", headers=H).status_code in (404, 405)
