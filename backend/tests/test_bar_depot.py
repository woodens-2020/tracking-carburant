"""
Circuit Bar du schéma UML : réception → génération des codes → mise en dépôt
et vérification (scan) → préparation de la livraison → scan des colis →
Bar devant / Bar piscine → mise à jour des stocks (dépôt et bar).
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
    assert r.status_code == 409 and "vérifié" in r.json()["detail"]["message"]


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
    # Inventaire du Bar devant : 15 comptés au lieu de 18 → ajustement −3
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


# ── Traçabilité et sécurité des codes ────────────────────────────────────

def _colis_livre(client, db, lieu="DEVANT"):
    p = _produit(db)
    a = _achat(db, p, 24)
    code = client.post(f"/api/pos/depot/receptions/{a.id}", headers=H, json={}).json()["colis"][0]["code"]
    client.post("/api/pos/depot/colis/verifier", headers=H, json={"code": code})
    liv = client.post("/api/pos/depot/livraisons", headers=H, json={"lieu": lieu}).json()
    client.post(f"/api/pos/depot/livraisons/{liv['id']}/scan", headers=H, json={"code": code})
    client.post(f"/api/pos/depot/livraisons/{liv['id']}/valider", headers=H)
    return p, code, liv


def test_code_signe_et_cle_de_controle(client, db):
    p = _produit(db)
    a = _achat(db, p, 24)
    code = client.post(f"/api/pos/depot/receptions/{a.id}", headers=H, json={}).json()["colis"][0]["code"]
    assert len(code) == 14 and code[:2] == "BC" and code[2:].isdigit()
    # Un chiffre de la clé modifié → rejeté avant toute lecture, et journalisé
    faux = code[:-1] + str((int(code[-1]) + 1) % 10)
    r = client.post("/api/pos/depot/colis/verifier", headers=H, json={"code": faux})
    assert r.status_code == 400 and "falsifié" in r.json()["detail"]
    # Un numéro de colis inventé avec une clé au hasard → rejeté aussi
    r = client.post("/api/pos/depot/colis/verifier", headers=H, json={"code": code[:4] + "999999" + code[-4:]})
    assert r.status_code == 400
    rejets = client.get("/api/pos/depot/journal?type=REJET", headers=H).json()
    assert any("Clé de contrôle" in (e["details"] or "") for e in rejets)
    # Le vrai code (avec tirets, tel qu'imprimé) est accepté
    lisible = f"{code[:4]}-{code[4:10]}-{code[10:]}"
    assert client.post("/api/pos/depot/colis/verifier", headers=H, json={"code": lisible}).status_code == 200


def test_entrees_dangereuses_rejetees(client, db):
    for texte in ["' OR 1=1 --", "<script>alert(1)</script>", "BC26000001; DROP TABLE bar_colis", "x" * 500, ""]:
        r = client.post("/api/pos/depot/colis/verifier", headers=H, json={"code": texte[:200]})
        assert r.status_code in (400, 422), (texte, r.status_code)
    # La table est intacte et le journal a gardé une trace neutralisée
    assert client.get("/api/pos/depot/colis", headers=H).status_code == 200
    for e in client.get("/api/pos/depot/journal?type=REJET", headers=H).json():
        assert "<" not in (e["code"] or "") and "'" not in (e["code"] or "")


def test_double_scan_un_seul_effet(client, db):
    """Deux vérifications du même colis : une seule entrée de stock."""
    p = _produit(db)
    a = _achat(db, p, 24)
    code = client.post(f"/api/pos/depot/receptions/{a.id}", headers=H, json={}).json()["colis"][0]["code"]
    r1 = client.post("/api/pos/depot/colis/verifier", headers=H, json={"code": code})
    r2 = client.post("/api/pos/depot/colis/verifier", headers=H, json={"code": code})
    assert (r1.status_code, r2.status_code) == (200, 409)
    assert _stock(client, p.id)["depot"] == 24
    # Double validation d'une livraison : un seul transfert
    liv = client.post("/api/pos/depot/livraisons", headers=H, json={"lieu": "PISCINE"}).json()
    client.post(f"/api/pos/depot/livraisons/{liv['id']}/scan", headers=H, json={"code": code})
    v1 = client.post(f"/api/pos/depot/livraisons/{liv['id']}/valider", headers=H)
    v2 = client.post(f"/api/pos/depot/livraisons/{liv['id']}/valider", headers=H)
    assert (v1.status_code, v2.status_code) == (200, 409)
    s = _stock(client, p.id)
    assert (s["depot"], s["piscine"], s["total"]) == (0, 24, 24)


def test_rescan_colis_livre_donne_la_fiche(client, db):
    p, code, liv = _colis_livre(client, db, lieu="PISCINE")
    # Rescanné en livraison : refusé, avec l'indication « déjà livré »
    liv2 = client.post("/api/pos/depot/livraisons", headers=H, json={"lieu": "DEVANT"}).json()
    r = client.post(f"/api/pos/depot/livraisons/{liv2['id']}/scan", headers=H, json={"code": code})
    assert r.status_code == 409 and r.json()["detail"]["deja_livre"] is True
    # Fiche de livraison
    f = client.post("/api/pos/depot/colis/consulter", headers=H, json={"code": code}).json()
    assert f["deja_livre"] is True
    assert f["colis"]["statut_libelle"] == "Déjà livré" and f["colis"]["lieu_nom"] == "Bar piscine"
    assert f["livraison"]["numero"] == liv["numero"] and f["livraison"]["valide_le"]
    types = [e["type"] for e in f["historique"]]
    assert types[:4] == ["CREE", "VERIFIE", "AJOUTE_LIVRAISON", "LIVRE"]
    assert "SCAN_REFUSE" in types and types[-1] == "CONSULTATION"
    assert all(e["horodatage"] for e in f["historique"])


# ── Schéma complet de l'utilisateur, de la réception à la vente au bar ────

def _caissiere(client):
    r = client.post("/api/employes", headers=H, json={
        "nom": f"Caisse{time.time_ns()}", "prenom": "Test", "poste": "Caissiere",
        "date_embauche": "2026-01-01", "salaire_base": 1000})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def test_schema_complet_reception_a_vente(client, db):
    """Réception → codes → dépôt et vérification → livraison → scan →
    Bar devant / Bar piscine → mise à jour des quantités (stock et dépôt),
    puis vente au bar : seul le stock du bar baisse."""
    p = _produit(db, unites_par_caisse=12)
    assert client.post(f"/api/pos/produits/{p.id}/prix", headers=H, json={"prix": 150}).status_code in (200, 201)

    # 1. Réception et enregistrement (achat de 4 caisses) + 2. génération des codes
    r = client.post("/api/pos/achats", headers=H, json={"produit_id": p.id, "quantite": 4, "quantite_type": "caisse",
                                                       "prix_achat_unitaire": 1200, "fournisseur": "Test"})
    achat_id = r.json()["achat_id"]
    colis = client.post(f"/api/pos/depot/receptions/{achat_id}", headers=H, json={}).json()["colis"]
    assert len(colis) == 4

    # 3. Mise en dépôt et vérification
    for c in colis:
        assert client.post("/api/pos/depot/colis/verifier", headers=H, json={"code": c["code"]}).status_code == 200
    assert _stock(client, p.id)["depot"] == 48

    # 4-5-6. Livraison, scan : 3 colis au Bar devant, 1 au Bar piscine
    for lieu, lot in (("DEVANT", colis[:3]), ("PISCINE", colis[3:])):
        liv = client.post("/api/pos/depot/livraisons", headers=H, json={"lieu": lieu}).json()
        for c in lot:
            assert client.post(f"/api/pos/depot/livraisons/{liv['id']}/scan", headers=H, json={"code": c["code"]}).status_code == 200
        assert client.post(f"/api/pos/depot/livraisons/{liv['id']}/valider", headers=H).status_code == 200

    # 7. Mise à jour des quantités (stock et dépôt)
    s = _stock(client, p.id)
    assert (s["depot"], s["devant"], s["piscine"], s["total"]) == (0, 36, 12, 48)

    # Vente sans session de caisse : refusée (elle sortirait du dépôt)
    r = client.post("/api/pos/ventes", headers=H, json={"lignes": [{"produit_id": p.id, "quantite": 2}]})
    assert r.status_code == 422 and "session" in r.json()["detail"]

    # Vente au Bar devant : seul le Bar devant baisse
    cid = _caissiere(client)
    assert client.post("/api/pos/caisse/sessions/ouvrir", headers=H, json={"caissier_id": cid, "lieu": "DEVANT"}).status_code in (200, 201)
    r = client.post("/api/pos/ventes", headers=H, json={"lignes": [{"produit_id": p.id, "quantite": 5}], "caissier_id": cid})
    assert r.status_code == 201, r.text
    s = _stock(client, p.id)
    assert (s["depot"], s["devant"], s["piscine"], s["total"]) == (0, 31, 12, 43)

    # Casse constatée au dépôt / au bar : ajustement sur le bon lieu
    assert client.post("/api/pos/stock/ajustement", headers=H, json={
        "produit_id": p.id, "quantite": 1, "type_mouvement": "CASSE", "motif": "Bouteille cassée au bar", "lieu": "PISCINE"}).status_code == 201
    assert _stock(client, p.id)["piscine"] == 11


# ── Livraison manuelle (sans scan) ───────────────────────────────────────

def _colis_recus(client, db, n_caisses=2, verifier=True):
    p = _produit(db, unites_par_caisse=12)
    a = _achat(db, p, 12 * n_caisses)
    colis = client.post(f"/api/pos/depot/receptions/{a.id}", headers=H, json={}).json()["colis"]
    if verifier:
        for c in colis:
            client.post("/api/pos/depot/colis/verifier", headers=H, json={"code": c["code"]})
    return p, colis


def _maintenant(delta_h=0):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(hours=delta_h)).isoformat()


def test_livraison_manuelle_rend_le_code_invalide(client, db):
    p, colis = _colis_recus(client, db, 2)
    r = client.post("/api/pos/depot/livraisons/manuelle", headers=H, json={
        "lieu": "DEVANT", "colis_ids": [c["id"] for c in colis],
        "motif": "Coupure internet, scanner hors ligne", "date_reelle": _maintenant(-2)})
    assert r.status_code == 201, r.text
    liv = r.json()
    assert liv["mode"] == "MANUELLE" and liv["statut"] == "VALIDEE" and liv["date_reelle"]
    s = _stock(client, p.id)
    assert (s["depot"], s["devant"], s["total"]) == (0, 24, 24)

    # Le code n'est plus utilisable : tout scan est refusé, avec « déjà livré »
    liv2 = client.post("/api/pos/depot/livraisons", headers=H, json={"lieu": "PISCINE"}).json()
    r = client.post(f"/api/pos/depot/livraisons/{liv2['id']}/scan", headers=H, json={"code": colis[0]["code"]})
    d = r.json()["detail"]
    assert r.status_code == 409 and d["deja_livre"] is True
    assert "sans scan" in d["message"] and "Bar devant" in d["message"]
    assert client.post("/api/pos/depot/colis/verifier", headers=H, json={"code": colis[0]["code"]}).status_code == 409

    # La fiche dit quand, où, comment et pourquoi
    f = client.post("/api/pos/depot/colis/consulter", headers=H, json={"code": colis[0]["code"]}).json()
    assert f["deja_livre"] and f["livraison"]["mode"] == "MANUELLE"
    assert "Coupure internet" in f["livraison"]["motif_manuel"]
    assert "LIVRE_MANUEL" in [e["type"] for e in f["historique"]]


def test_livraison_manuelle_colis_non_verifie(client, db):
    """Colis reçu mais jamais scanné au dépôt : il passe par le dépôt puis au bar."""
    p, colis = _colis_recus(client, db, 1, verifier=False)
    r = client.post("/api/pos/depot/livraisons/manuelle", headers=H, json={
        "lieu": "PISCINE", "colis_ids": [colis[0]["id"]],
        "motif": "Scanner en panne au dépôt", "date_reelle": _maintenant(-1)})
    assert r.status_code == 201, r.text
    s = _stock(client, p.id)
    assert (s["depot"], s["piscine"], s["a_verifier"]) == (0, 12, 0)


def test_livraison_manuelle_regles_strictes(client, db):
    p, colis = _colis_recus(client, db, 2)
    ids = [c["id"] for c in colis]
    base = {"lieu": "DEVANT", "colis_ids": ids, "date_reelle": _maintenant(-1)}
    assert client.post("/api/pos/depot/livraisons/manuelle", headers=H, json={**base, "motif": "panne"}).status_code == 422
    assert client.post("/api/pos/depot/livraisons/manuelle", headers=H,
                       json={**base, "motif": "Coupure internet", "date_reelle": _maintenant(+3)}).status_code == 422
    assert client.post("/api/pos/depot/livraisons/manuelle", headers=H,
                       json={**base, "motif": "Coupure internet", "date_reelle": _maintenant(-24 * 10)}).status_code == 422
    # Tout ou rien : si un colis est déjà livré, aucun colis ne part
    client.post("/api/pos/depot/livraisons/manuelle", headers=H,
                json={**base, "colis_ids": ids[:1], "motif": "Coupure internet"})
    r = client.post("/api/pos/depot/livraisons/manuelle", headers=H, json={**base, "motif": "Coupure internet encore"})
    assert r.status_code == 409 and "aucun colis" in r.json()["detail"]
    st = client.get(f"/api/pos/depot/colis/{ids[1]}/fiche", headers=H).json()["colis"]["statut"]
    assert st == "AU_DEPOT"
    assert _stock(client, p.id)["devant"] == 12


def test_une_livraison_par_bar_et_changement_de_destination(client, db):
    """Bar devant et Bar piscine ont chacun leur livraison en préparation ;
    une livraison scannée pour le mauvais bar peut être réorientée avant
    validation, et le stock arrive bien au bar choisi."""
    p = _produit(db)
    a = _achat(db, p, 48)
    colis = client.post(f"/api/pos/depot/receptions/{a.id}", headers=H, json={}).json()["colis"]
    for c in colis:
        assert client.post("/api/pos/depot/colis/verifier", headers=H, json={"code": c["code"]}).status_code == 200

    # Livraisons ouvertes d'éventuels tests précédents : on repart de zéro
    for l in client.get("/api/pos/depot/livraisons?statut=PREPARATION", headers=H).json():
        client.post(f"/api/pos/depot/livraisons/{l['id']}/annuler", headers=H)

    devant = client.post("/api/pos/depot/livraisons", headers=H, json={"lieu": "DEVANT"}).json()
    encore = client.post("/api/pos/depot/livraisons", headers=H, json={"lieu": "DEVANT"}).json()
    piscine = client.post("/api/pos/depot/livraisons", headers=H, json={"lieu": "PISCINE"}).json()
    assert encore["id"] == devant["id"]                      # pas de doublon pour un même bar
    assert piscine["id"] != devant["id"]                     # les deux bars en parallèle

    assert client.post(f"/api/pos/depot/livraisons/{devant['id']}/scan", headers=H,
                       json={"code": colis[0]["code"]}).status_code == 200
    # Vers le Bar piscine alors qu'il a déjà sa livraison : refusé
    r = client.post(f"/api/pos/depot/livraisons/{devant['id']}/destination", headers=H, json={"lieu": "PISCINE"})
    assert r.status_code == 409 and "Bar piscine" in r.json()["detail"]
    client.post(f"/api/pos/depot/livraisons/{piscine['id']}/annuler", headers=H)
    r = client.post(f"/api/pos/depot/livraisons/{devant['id']}/destination", headers=H, json={"lieu": "PISCINE"})
    assert r.status_code == 200 and r.json()["lieu"] == "PISCINE"
    assert client.post(f"/api/pos/depot/livraisons/{devant['id']}/valider", headers=H).status_code == 200
    s = _stock(client, p.id)
    assert (s["depot"], s["devant"], s["piscine"]) == (24, 0, 24)
    j = client.get("/api/pos/depot/journal?type=DESTINATION", headers=H).json()
    assert any("Bar devant → Bar piscine" in (e["details"] or "") for e in j)
