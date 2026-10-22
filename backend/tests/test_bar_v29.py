"""
Plan d'amélioration Bar (achats et dépôt) : annulation d'achat, coût de
revient avec frais, quantité verrouillée après réception, retours et
transferts entre bars, colis ouvert, fournisseurs et dettes, permission
dépôt, seuils par bar, rentabilité par bar, réception partielle, retour
fournisseur, remplacement d'étiquette, étiquetage du stock existant,
contrôle par bar.
"""
import time
from decimal import Decimal

from models import BarAchat, BarCategorie, BarProduit

H = {"X-API-Key": "smoke-test-key"}


def _produit(db, upc=24, **kw):
    if not db.query(BarCategorie).filter_by(nom="test-boisson").first():
        db.add(BarCategorie(nom="test-boisson"))
        db.flush()
    p = BarProduit(nom=f"Biere-{time.time_ns()}", categorie="test-boisson", unite="Bouteille", actif=True,
                   seuil_alerte_stock=0, vendu_par_caisse=True, unites_par_caisse=upc, **kw)
    db.add(p)
    db.commit()
    return p


def _achat_api(client, p, qte, prix=50, **kw):
    r = client.post("/api/pos/achats", headers=H, json={"produit_id": p.id, "quantite": qte, "quantite_type": "unite",
                                                       "prix_achat_unitaire": prix, **kw})
    assert r.status_code == 201, r.text
    return r.json()["achat_id"]


def _recevoir(client, achat_id, verifier=True, **kw):
    colis = client.post(f"/api/pos/depot/receptions/{achat_id}", headers=H, json=kw).json()["colis"]
    if verifier:
        for c in colis:
            assert client.post("/api/pos/depot/colis/verifier", headers=H, json={"code": c["code"]}).status_code == 200
    return colis


def _stock(client, pid):
    return next(x for x in client.get("/api/pos/depot/stock", headers=H).json() if x["produit_id"] == pid)


def _transferer(client, lieu, colis, type_bon="TRANSFERT", origine=None):
    body = {"lieu": lieu, "type_bon": type_bon}
    if origine:
        body["origine"] = origine
    l = client.post("/api/pos/depot/livraisons", headers=H, json=body).json()
    for c in colis:
        r = client.post(f"/api/pos/depot/livraisons/{l['id']}/scan", headers=H, json={"code": c["code"]})
        assert r.status_code == 200, r.text
    r = client.post(f"/api/pos/depot/livraisons/{l['id']}/valider", headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def _annuler_bons_ouverts(client):
    for l in client.get("/api/pos/depot/livraisons?statut=PREPARATION", headers=H).json():
        client.post(f"/api/pos/depot/livraisons/{l['id']}/annuler", headers=H)


# ── 2. Annulation d'achat ─────────────────────────────────────────────────

def test_annulation_achat(client, db):
    p = _produit(db)
    a = _achat_api(client, p, 48)
    colis = _recevoir(client, a, verifier=False)
    assert client.post(f"/api/pos/achats/{a}/annuler", headers=H, json={"motif": "x"}).status_code == 422
    r = client.post(f"/api/pos/achats/{a}/annuler", headers=H, json={"motif": "Quantité mal saisie"})
    assert r.status_code == 200 and r.json()["colis_retires"] == 2
    d = client.get(f"/api/pos/achats/{a}", headers=H).json()
    assert d["statut"] == "ANNULE" and d["motif_annulation"] == "Quantité mal saisie" and not d["annulable"]
    assert all(c["statut"] == "ANNULE" for c in client.get(f"/api/pos/depot/colis?achat_id={a}", headers=H).json())
    assert client.post("/api/pos/depot/colis/verifier", headers=H, json={"code": colis[0]["code"]}).status_code == 409

    # Un colis déjà entré en stock : annulation refusée
    b = _achat_api(client, p, 24)
    _recevoir(client, b)
    r = client.post(f"/api/pos/achats/{b}/annuler", headers=H, json={"motif": "Erreur de saisie"})
    assert r.status_code == 409 and "retour fournisseur" in r.json()["detail"]


# ── 3. Coût de revient avec frais ; 4. quantité verrouillée ──────────────

def test_cout_de_revient_et_quantite_verrouillee(client, db):
    from pos_service import cmup
    p = _produit(db)
    r = client.post("/api/pos/achats", headers=H, json={
        "produit_id": p.id, "quantite": 24, "prix_achat_unitaire": 50,
        "depenses": [{"description": "Transport", "montant": 120}]})
    a = r.json()["achat_id"]
    _recevoir(client, a)
    assert float(cmup(p.id, db)) == 55.0                         # (24×50 + 120) / 24
    d = client.get(f"/api/pos/achats/{a}", headers=H).json()
    assert d["cout_revient_unitaire"] == 55.0

    r = client.put(f"/api/pos/achats/{a}", headers=H, json={"quantite": 12, "prix_achat_unitaire": 50})
    assert r.status_code == 409 and "quantité" in r.json()["detail"]
    r = client.put(f"/api/pos/achats/{a}", headers=H, json={"quantite": 24, "prix_achat_unitaire": 45})
    assert r.status_code == 200
    d = client.get(f"/api/pos/achats/{a}", headers=H).json()
    assert d["corrections"] and d["corrections"][0]["avant"]["prix_achat_unitaire"] == 50.0
    db.expire_all()
    assert float(cmup(p.id, db)) == 50.0                         # (24×45 + 120) / 24


# ── 5. Retour bar → dépôt et transfert entre bars ─────────────────────────

def test_retour_et_transfert_entre_bars(client, db):
    _annuler_bons_ouverts(client)
    p = _produit(db, upc=12)
    a = _achat_api(client, p, 36)
    colis = _recevoir(client, a)
    _transferer(client, "DEVANT", colis)
    s = _stock(client, p.id)
    assert (s["depot"], s["devant"], s["piscine"]) == (0, 36, 0)

    # Un colis fermé revient au dépôt
    r = _transferer(client, "DEVANT", colis[:1], type_bon="RETOUR", origine="DEVANT")
    assert r["type_bon"] == "RETOUR" and r["destination_nom"] == "Dépôt" and r["numero"].startswith("RET")
    s = _stock(client, p.id)
    assert (s["depot"], s["devant"], s["piscine"]) == (12, 24, 0)
    c0 = next(c for c in client.get(f"/api/pos/depot/colis?achat_id={a}", headers=H).json() if c["id"] == colis[0]["id"])
    assert c0["statut"] == "AU_DEPOT" and c0["lieu"] is None

    # Un colis passe du Bar devant au Bar piscine
    r = _transferer(client, "PISCINE", colis[1:2], type_bon="INTERBAR", origine="DEVANT")
    assert r["origine_nom"] == "Bar devant" and r["destination_nom"] == "Bar piscine"
    s = _stock(client, p.id)
    assert (s["depot"], s["devant"], s["piscine"]) == (12, 12, 12)

    # Vrac rendu par le bar, dans la limite de son stock
    l = client.post("/api/pos/depot/livraisons", headers=H, json={"lieu": "PISCINE", "type_bon": "RETOUR", "origine": "PISCINE"}).json()
    assert client.post(f"/api/pos/depot/livraisons/{l['id']}/vrac", headers=H, json={"produit_id": p.id, "quantite": 13}).status_code == 409
    assert client.post(f"/api/pos/depot/livraisons/{l['id']}/vrac", headers=H, json={"produit_id": p.id, "quantite": 5}).status_code == 200
    # Un colis du Bar devant ne peut pas partir d'un bon du Bar piscine
    r = client.post(f"/api/pos/depot/livraisons/{l['id']}/scan", headers=H, json={"code": colis[2]["code"]})
    assert r.status_code == 409 and "Bar devant" in r.json()["detail"]["message"]
    # Annulation : le colis reste au bar, avec sa fiche de transfert d'origine
    client.post(f"/api/pos/depot/livraisons/{l['id']}/annuler", headers=H)
    l2 = client.post("/api/pos/depot/livraisons", headers=H, json={"lieu": "DEVANT", "type_bon": "RETOUR", "origine": "DEVANT"}).json()
    assert client.post(f"/api/pos/depot/livraisons/{l2['id']}/scan", headers=H, json={"code": colis[2]["code"]}).status_code == 200
    client.post(f"/api/pos/depot/livraisons/{l2['id']}/annuler", headers=H)
    f = client.get(f"/api/pos/depot/colis/{colis[2]['id']}/fiche", headers=H).json()
    assert f["colis"]["statut"] == "LIVRE" and f["colis"]["lieu"] == "DEVANT" and f["livraison"]["type_bon"] == "TRANSFERT"


# ── 6. Ouvrir un colis au dépôt ───────────────────────────────────────────

def test_ouvrir_un_colis(client, db):
    _annuler_bons_ouverts(client)
    p = _produit(db)
    colis = _recevoir(client, _achat_api(client, p, 24))
    l = client.post("/api/pos/depot/livraisons", headers=H, json={"lieu": "PISCINE"}).json()
    # Colis fermé : pas de vrac disponible
    assert client.post(f"/api/pos/depot/livraisons/{l['id']}/vrac", headers=H, json={"produit_id": p.id, "quantite": 6}).status_code == 409
    r = client.post(f"/api/pos/depot/colis/{colis[0]['id']}/ouvrir", headers=H, json={})
    assert r.status_code == 200 and r.json()["statut"] == "OUVERT"
    assert client.post(f"/api/pos/depot/livraisons/{l['id']}/vrac", headers=H, json={"produit_id": p.id, "quantite": 6}).status_code == 200
    assert client.post(f"/api/pos/depot/livraisons/{l['id']}/valider", headers=H).status_code == 200
    s = _stock(client, p.id)
    assert (s["depot"], s["piscine"]) == (18, 6)
    r = client.post(f"/api/pos/depot/livraisons", headers=H, json={"lieu": "PISCINE"}).json()
    r = client.post(f"/api/pos/depot/livraisons/{r['id']}/scan", headers=H, json={"code": colis[0]["code"]})
    assert r.status_code == 409 and "déconditionnée" in r.json()["detail"]["message"]
    _annuler_bons_ouverts(client)


# ── 7. Fournisseurs, factures, dettes ─────────────────────────────────────

def test_fournisseurs_factures_et_dettes(client, db):
    p = _produit(db)
    nom = f"Brana {time.time_ns()}"
    a1 = _achat_api(client, p, 24, fournisseur=nom, numero_facture="F-100")
    a2 = _achat_api(client, p, 24, fournisseur="  " + nom.upper() + " ")
    d1, d2 = (client.get(f"/api/pos/achats/{x}", headers=H).json() for x in (a1, a2))
    assert d1["fournisseur_id"] and d1["fournisseur_id"] == d2["fournisseur_id"] and d2["fournisseur"] == nom
    r = client.post("/api/pos/achats", headers=H, json={"produit_id": p.id, "quantite": 1, "prix_achat_unitaire": 1,
                                                       "fournisseur": nom, "numero_facture": "f-100"})
    assert r.status_code == 409 and "déjà enregistrée" in r.json()["detail"]

    # À crédit : fournisseur obligatoire, paiements, dettes
    assert client.post("/api/pos/achats", headers=H, json={"produit_id": p.id, "quantite": 1, "prix_achat_unitaire": 1,
                                                          "mode_reglement": "CREDIT"}).status_code == 422
    a3 = _achat_api(client, p, 24, fournisseur=nom, mode_reglement="CREDIT", date_echeance="2099-01-31")
    d3 = client.get(f"/api/pos/achats/{a3}", headers=H).json()
    assert d3["reglement"] == {"etat": "A_PAYER", "paye": 0.0, "reste": 1200.0, "en_retard": False}
    assert client.post(f"/api/pos/achats/{a3}/paiements", headers=H, json={"montant": 1500}).status_code == 422
    r = client.post(f"/api/pos/achats/{a3}/paiements", headers=H, json={"montant": 500, "mode": "CASH"})
    assert r.status_code == 201 and r.json()["etat"] == "PARTIEL" and r.json()["reste"] == 700.0
    dettes = client.get("/api/pos/dettes-fournisseurs", headers=H).json()
    assert any(x["achat_id"] == a3 and x["reste"] == 700.0 for x in dettes["achats"])
    f = next(x for x in client.get("/api/pos/fournisseurs", headers=H).json() if x["id"] == d1["fournisseur_id"])
    assert f["nb_achats"] == 3 and f["reste_a_payer"] == 700.0
    # Paiement enregistré : l'achat ne s'annule plus ; payé comptant : rien à régler
    assert client.post(f"/api/pos/achats/{a3}/annuler", headers=H, json={"motif": "Erreur de saisie"}).status_code == 409
    assert client.post(f"/api/pos/achats/{a1}/paiements", headers=H, json={"montant": 10}).status_code == 409


# ── 8. Permission « dépôt » explicite ─────────────────────────────────────

def _client_role(client, nom, permissions):
    import main
    from fastapi.testclient import TestClient
    r = client.post("/api/admin/roles", headers=H, json={"nom": nom, "permissions": permissions})
    assert r.status_code in (200, 201), r.text
    n = time.time_ns()
    email, mdp, pin = f"d{n}@konekta.test", "MotDePasse123!", f"{n % 10**9:09d}"
    assert client.post("/api/admin/users", headers=H, json={
        "nom_complet": f"Test {nom}", "email": email, "username": f"d{n}", "password": mdp,
        "code_acces": pin, "role_id": r.json()["id"]}).status_code == 201
    c = TestClient(main.app)
    c.__enter__()
    assert c.post("/api/login", json={"email": email, "password": mdp, "code_acces": pin}).status_code == 200
    return c


def test_permission_depot(client, db):
    n = time.time_ns()
    sans = _client_role(client, f"Manager depot non {n}", {"bar": "complet", "depot": "aucun"})
    avec = _client_role(client, f"Magasin {n}", {"bar": "lecture", "depot": "operationnel"})
    try:
        body = {"lieu": "DEVANT", "type_bon": "INTERBAR", "origine": "PISCINE"}
        assert sans.post("/api/pos/depot/livraisons", json=body).status_code == 403
        r = avec.post("/api/pos/depot/livraisons", json=body)
        assert r.status_code == 201, r.text
        avec.post(f"/api/pos/depot/livraisons/{r.json()['id']}/annuler")
        roles = {x["nom"]: x for x in client.get("/api/admin/roles", headers=H).json()}
        assert roles[f"Magasin {n}"]["permissions"]["depot"] == "operationnel"
    finally:
        sans.__exit__(None, None, None)
        avec.__exit__(None, None, None)


# ── 9. Seuils par bar et « à transférer aujourd'hui » ─────────────────────

def test_a_transferer(client, db):
    p = _produit(db, upc=12, seuil_devant=Decimal(10), seuil_piscine=Decimal(0))
    _recevoir(client, _achat_api(client, p, 48))
    sugg = client.get("/api/pos/depot/a-transferer", headers=H).json()
    ligne = next(x for x in sugg["DEVANT"] if x["produit_id"] == p.id)
    assert ligne["seuil"] == 10 and ligne["propose_unites"] == 24 and ligne["propose_caisses"] == 2   # 2×10 − 0 → 2 caisses
    assert not ligne["a_racheter"] and ligne["propose_vrac"] == 0
    assert not any(x["produit_id"] == p.id for x in sugg["PISCINE"])                                  # pas de seuil au Bar piscine


# ── 10. Rentabilité par bar ──────────────────────────────────────────────

def test_rentabilite_par_bar(client):
    d = client.get("/api/pos/stats", headers=H).json()
    assert "par_lieu" in d and isinstance(d["par_lieu"], list)
    assert client.get("/api/pos/stats?lieu=DEVANT", headers=H).json()["lieu"] == "DEVANT"
    assert client.get("/api/pos/stats?lieu=CAVE", headers=H).status_code == 422


# ── 10. Réception partielle, retour fournisseur ──────────────────────────

def test_reception_partielle_et_retour_fournisseur(client, db):
    from pos_service import cmup
    p = _produit(db)
    a = _achat_api(client, p, 48, prix=50)
    r = client.post(f"/api/pos/depot/receptions/{a}", headers=H, json={"quantite_recue": 46})
    assert r.status_code == 422                                              # motif obligatoire
    r = client.post(f"/api/pos/depot/receptions/{a}", headers=H, json={"quantite_recue": 46, "motif_ecart": "2 bouteilles cassées"})
    assert r.status_code == 201 and [c["quantite"] for c in r.json()["colis"]] == [24, 22]
    for c in r.json()["colis"]:
        client.post("/api/pos/depot/colis/verifier", headers=H, json={"code": c["code"]})
    d = client.get(f"/api/pos/achats/{a}", headers=H).json()
    assert d["quantite_recue"] == 46 and d["cout_total"] == 2300.0

    colis = r.json()["colis"]
    assert client.post("/api/pos/depot/retour-fournisseur", headers=H, json={"colis_ids": [colis[1]["id"]], "motif": "ok"}).status_code == 422
    r = client.post("/api/pos/depot/retour-fournisseur", headers=H, json={"colis_ids": [colis[1]["id"]], "motif": "Produit périmé"})
    assert r.status_code == 201 and r.json()["unites_rendues"] == 22
    assert _stock(client, p.id)["depot"] == 24
    db.expire_all()
    assert float(cmup(p.id, db)) == 50.0
    # Tout ou rien : un colis déjà rendu fait échouer tout le retour
    r = client.post("/api/pos/depot/retour-fournisseur", headers=H, json={"colis_ids": [colis[0]["id"], colis[1]["id"]], "motif": "Produit périmé"})
    assert r.status_code == 409 and _stock(client, p.id)["depot"] == 24


# ── 10. Remplacement d'étiquette ──────────────────────────────────────────

def test_remplacer_etiquette(client, db):
    p = _produit(db)
    colis = _recevoir(client, _achat_api(client, p, 24))
    ancien = colis[0]["code"]
    r = client.post(f"/api/pos/depot/colis/{colis[0]['id']}/remplacer-etiquette", headers=H, json={"motif": "Étiquette mouillée"})
    assert r.status_code == 200 and r.json()["code"] != ancien
    r2 = client.post("/api/pos/depot/colis/consulter", headers=H, json={"code": ancien})
    assert r2.status_code == 409 and "remplacée" in r2.json()["detail"]["message"]
    assert client.post("/api/pos/depot/colis/consulter", headers=H, json={"code": r.json()["code"]}).status_code == 200


# ── 1. Inventaire d'ouverture : étiquetage du stock existant, contrôle par bar

def test_etiqueter_stock_existant_et_controle(client, db):
    p = _produit(db)
    assert client.post("/api/pos/depot/inventaire", headers=H, json={
        "lieu": "DEPOT", "motif": "Inventaire d'ouverture", "lignes": [{"produit_id": p.id, "quantite": 30}]}).status_code == 200
    assert client.post("/api/pos/depot/etiqueter-stock", headers=H, json={"produit_id": p.id, "nb_colis": 2, "unites_par_colis": 24}).status_code == 409
    r = client.post("/api/pos/depot/etiqueter-stock", headers=H, json={"produit_id": p.id, "nb_colis": 1, "unites_par_colis": 24})
    assert r.status_code == 201 and r.json()["colis"][0]["statut"] == "AU_DEPOT"
    assert _stock(client, p.id)["depot"] == 30                               # aucun mouvement : même stock

    etat = client.get("/api/pos/depot/ouverture", headers=H).json()
    assert {x["lieu"] for x in etat["lieux"]} == {"DEPOT", "DEVANT", "PISCINE"}
    if not etat["pret"]:
        assert client.post("/api/pos/depot/ouverture/controle", headers=H, json={"actif": True}).status_code == 409
    assert client.post("/api/pos/depot/ouverture/controle", headers=H, json={"actif": False}).json()["controle_par_bar"] is False


# ── Scan hors ligne : l'heure réelle reste au journal ────────────────────

def test_scan_hors_ligne(client, db):
    p = _produit(db)
    colis = _recevoir(client, _achat_api(client, p, 24), verifier=False)
    r = client.post("/api/pos/depot/colis/verifier", headers=H, json={"code": colis[0]["code"], "hors_ligne_le": "2026-10-20T14:05:00+00:00"})
    assert r.status_code == 200
    f = client.get(f"/api/pos/depot/colis/{colis[0]['id']}/fiche", headers=H).json()
    assert any("hors ligne" in (e["details"] or "") for e in f["historique"])


def test_colis_d_un_bar_ne_bloque_pas_le_vrac_du_depot(client, db):
    """Un colis en préparation de retour / transfert entre bars est dans un
    bar : il ne doit pas réduire le vrac disponible au dépôt."""
    _annuler_bons_ouverts(client)
    p = _produit(db, upc=12)
    colis = _recevoir(client, _achat_api(client, p, 24))
    _transferer(client, "DEVANT", colis[:1])
    assert client.post(f"/api/pos/depot/colis/{colis[1]['id']}/ouvrir", headers=H, json={}).status_code == 200
    ib = client.post("/api/pos/depot/livraisons", headers=H, json={"lieu": "PISCINE", "type_bon": "INTERBAR", "origine": "DEVANT"}).json()
    assert client.post(f"/api/pos/depot/livraisons/{ib['id']}/scan", headers=H, json={"code": colis[0]["code"]}).status_code == 200
    l = client.post("/api/pos/depot/livraisons", headers=H, json={"lieu": "DEVANT"}).json()
    assert client.post(f"/api/pos/depot/livraisons/{l['id']}/vrac", headers=H, json={"produit_id": p.id, "quantite": 12}).status_code == 200
    _annuler_bons_ouverts(client)


def test_caisse_d_un_bar_voit_le_stock_de_ce_bar(client, db):
    """Après un transfert dépôt → Bar piscine, la caisse du Bar piscine voit
    les unités arrivées ; celle du Bar devant ne les voit pas."""
    _annuler_bons_ouverts(client)
    p = _produit(db, upc=12)
    colis = _recevoir(client, _achat_api(client, p, 36))
    _transferer(client, "PISCINE", colis[:2])
    par_lieu = {}
    for lieu in ("PISCINE", "DEVANT"):
        prods = client.get(f"/api/pos/produits?actif=true&lieu={lieu}", headers=H).json()
        par_lieu[lieu] = next(x for x in prods if x["id"] == p.id)
    assert par_lieu["PISCINE"]["stock_courant"] == 24 and par_lieu["PISCINE"]["stock_total"] == 36
    assert par_lieu["DEVANT"]["stock_courant"] == 0
    tous = next(x for x in client.get("/api/pos/produits?actif=true", headers=H).json() if x["id"] == p.id)
    assert tous["stock_courant"] == 36


def test_verification_a_la_main_et_pendant_le_transfert(client, db):
    _annuler_bons_ouverts(client)
    p = _produit(db, upc=12)
    colis = _recevoir(client, _achat_api(client, p, 36), verifier=False)
    # À la main : deux colis, un troisième ignoré s'il est déjà vérifié
    r = client.post("/api/pos/depot/colis/verifier-manuel", headers=H, json={"colis_ids": [colis[0]["id"], colis[1]["id"]]})
    assert r.status_code == 200 and r.json()["verifies"] == 2 and r.json()["unites"] == 24
    r = client.post("/api/pos/depot/colis/verifier-manuel", headers=H, json={"colis_ids": [colis[0]["id"]]})
    assert r.json()["verifies"] == 0 and r.json()["ignores"] == 1
    assert _stock(client, p.id)["depot"] == 24
    f = client.get(f"/api/pos/depot/colis/{colis[0]['id']}/fiche", headers=H).json()
    assert any("à la main" in (e["details"] or "") for e in f["historique"])
    # Pendant le transfert : refusé sans l'option, vérifié + ajouté avec
    l = client.post("/api/pos/depot/livraisons", headers=H, json={"lieu": "PISCINE"}).json()
    r = client.post(f"/api/pos/depot/livraisons/{l['id']}/scan", headers=H, json={"code": colis[2]["code"]})
    assert r.status_code == 409 and r.json()["detail"]["statut"] == "A_VERIFIER"
    r = client.post(f"/api/pos/depot/livraisons/{l['id']}/scan", headers=H, json={"code": colis[2]["code"], "verifier_si_besoin": True})
    assert r.status_code == 200, r.text
    assert client.post(f"/api/pos/depot/livraisons/{l['id']}/valider", headers=H).status_code == 200
    s = _stock(client, p.id)
    assert (s["depot"], s["piscine"]) == (24, 12)


def test_scan_intelligent_et_transfert_direct(client, db):
    """Un scan, un choix : le colis part et les stocks sont à jour."""
    p = _produit(db, upc=12)
    colis = _recevoir(client, _achat_api(client, p, 36), verifier=False)
    _recevoir_verif = lambda c: client.post("/api/pos/depot/colis/verifier", headers=H, json={"code": c["code"]})
    _recevoir_verif(colis[0]); _recevoir_verif(colis[1])

    info = client.post("/api/pos/depot/colis/scan", headers=H, json={"code": colis[0]["code"]}).json()
    assert info["actions"] == ["DEVANT", "PISCINE", "OUVRIR"] and info["stock"]["DEPOT"] == 24
    assert client.post("/api/pos/depot/colis/scan", headers=H, json={"code": colis[2]["code"]}).json()["actions"][0] == "VERIFIER"

    # Dépôt → Bar piscine
    r = client.post("/api/pos/depot/colis/transferer", headers=H, json={"code": colis[0]["code"], "destination": "PISCINE"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["stock"] == {"DEPOT": 12, "DEVANT": 0, "PISCINE": 12} and d["bon"]["statut"] == "VALIDEE"
    assert d["actions"] == ["DEPOT", "DEVANT"]
    # Rescan du même colis vers le même bar : refusé
    assert client.post("/api/pos/depot/colis/transferer", headers=H, json={"code": colis[0]["code"], "destination": "PISCINE"}).status_code == 409
    # Bar piscine → Bar devant, puis retour au dépôt
    assert client.post("/api/pos/depot/colis/transferer", headers=H, json={"code": colis[0]["code"], "destination": "DEVANT"}).json()["stock"]["DEVANT"] == 12
    r = client.post("/api/pos/depot/colis/transferer", headers=H, json={"code": colis[0]["code"], "destination": "DEPOT"}).json()
    assert r["stock"] == {"DEPOT": 24, "DEVANT": 0, "PISCINE": 0} and r["colis"]["statut"] == "AU_DEPOT"
    # Colis à vérifier : refusé sans l'option, vérifié et envoyé avec
    assert client.post("/api/pos/depot/colis/transferer", headers=H, json={"code": colis[2]["code"], "destination": "DEVANT"}).status_code == 409
    r = client.post("/api/pos/depot/colis/transferer", headers=H, json={"code": colis[2]["code"], "destination": "DEVANT", "verifier_si_besoin": True})
    assert r.status_code == 200 and r.json()["stock"]["DEVANT"] == 12
    f = client.get(f"/api/pos/depot/colis/{colis[2]['id']}/fiche", headers=H).json()
    assert [e["type"] for e in f["historique"]][-2:] == ["VERIFIE", "LIVRE"]


def test_code_lu_en_azerty(client, db):
    """Douchette QWERTY branchée sur un clavier AZERTY : les chiffres arrivent
    en « &é"'(-è_çà » ; le code doit quand même être reconnu."""
    p = _produit(db)
    colis = _recevoir(client, _achat_api(client, p, 24), verifier=False)
    code = colis[0]["code"]
    azerty = code[:2] + code[2:].translate(str.maketrans("1234567890", "&é\"'(-è_çà"))
    r = client.post("/api/pos/depot/colis/scan", headers=H, json={"code": azerty})
    assert r.status_code == 200 and r.json()["colis"]["code"] == code
