"""
Règles métier v26 (bpccp) testées par l'API, comme l'interface les utilise :
prix imposé par l'historique, continuité des compteurs, motifs obligatoires
à la clôture et pour un reste manuel, transferts séparés des charges,
anomalies traitées.
"""
import itertools
import time

H = {"X-API-Key": "smoke-test-key"}
_seq = itertools.count(1)


def _nouveau_produit(client):
    nom = f"Metier-{next(_seq)}-{time.time_ns()}"
    r = client.post("/api/produits", headers=H, json={"nom": nom, "prix_gallon": 500})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    r = client.post(f"/api/produits/{pid}/pompes", headers=H, json={"nom": f"{nom} P1"})
    assert r.status_code == 200, r.text
    return pid, r.json()["id"]


def _releve(client, pompe_id, jour, periode, avant, apres, prix=800, motif=None):
    body = {"date": jour, "periode": periode, "pompe_id": pompe_id,
            "prix_gallon": prix, "metter_avant": avant, "metter_apres": apres}
    if motif is not None:
        body["motif_ecart_compteur"] = motif
    return client.post("/api/releves", headers=H, json=body)


# ── Relevés ───────────────────────────────────────────────────────────────

def test_prix_impose_par_historique(client):
    pid, pompe = _nouveau_produit(client)
    r = client.post("/api/prix-vente", headers=H,
                    json={"produit_id": pid, "prix_vente_gallon": 750, "date_effet": "2026-01-01"})
    assert r.status_code == 201, r.text
    r = _releve(client, pompe, "2026-01-05", "Matin", 0, 10, prix=999)
    assert r.status_code == 200, r.text
    d = r.json()
    assert float(d["prix_gallon"]) == 750
    assert float(d["montant_vente"]) == 7500
    assert any("750" in a for a in d["avertissements"])


def test_prix_du_navigateur_si_aucun_historique(client):
    pid, pompe = _nouveau_produit(client)
    r = _releve(client, pompe, "2026-01-05", "Matin", 0, 10, prix=640)
    assert r.status_code == 200
    assert float(r.json()["prix_gallon"]) == 640


def test_compteur_discontinu_exige_un_motif(client):
    pid, pompe = _nouveau_produit(client)
    assert _releve(client, pompe, "2026-02-01", "Matin", 100, 150).status_code == 200
    # L'après-midi repart de 155 au lieu de 150 : 5 gal non vendus
    r = _releve(client, pompe, "2026-02-01", "Apres-midi", 155, 200)
    assert r.status_code == 409
    assert "motif" in r.json()["detail"].lower()
    r = _releve(client, pompe, "2026-02-01", "Apres-midi", 155, 200, motif="Essai de pompe")
    assert r.status_code == 200, r.text
    assert r.json()["ecart_compteur"] == 5
    assert r.json()["motif_ecart_compteur"] == "Essai de pompe"
    # Le lendemain, continuité respectée : aucun motif demandé
    assert _releve(client, pompe, "2026-02-02", "Matin", 200, 230).status_code == 200


def test_correction_garde_le_motif_existant(client):
    pid, pompe = _nouveau_produit(client)
    _releve(client, pompe, "2026-02-10", "Matin", 100, 150)
    _releve(client, pompe, "2026-02-10", "Apres-midi", 160, 200, motif="Panne compteur")
    # Correction du mètre après seulement : le motif déjà donné suffit
    r = _releve(client, pompe, "2026-02-10", "Apres-midi", 160, 210)
    assert r.status_code == 200, r.text
    assert r.json()["motif_ecart_compteur"] == "Panne compteur"


def test_avertissement_vente_sans_cargaison(client):
    pid, pompe = _nouveau_produit(client)
    r = _releve(client, pompe, "2026-03-01", "Matin", 0, 20)
    assert r.status_code == 200
    assert any("Aucune cargaison ouverte" in a for a in r.json()["avertissements"])


# ── Cargaisons ────────────────────────────────────────────────────────────

def _livraison(client, pid, jour, gallons, **extra):
    return client.post("/api/livraisons", headers=H, json={
        "produit_id": pid, "date_livraison": jour, "gallons_recus": gallons,
        "prix_achat_gallon": 700, **extra})


def test_reste_manuel_exige_un_motif(client):
    pid, _ = _nouveau_produit(client)
    r = _livraison(client, pid, "2026-04-01", 500, reste_avant=50)
    assert r.status_code == 400
    r = _livraison(client, pid, "2026-04-01", 500, reste_avant=50, motif_reste="Jauge du 01/04")
    assert r.status_code == 201, r.text
    assert r.json()["motif_reste_manuel"] == "Jauge du 01/04"


def test_reste_manuel_refuse_si_cargaison_ouverte(client):
    pid, _ = _nouveau_produit(client)
    assert _livraison(client, pid, "2026-04-01", 500).status_code == 201
    r = _livraison(client, pid, "2026-04-10", 500, reste_avant=50, motif_reste="Reste de l'ancienne")
    assert r.status_code == 409
    assert "Additionner" in r.json()["detail"]


def test_mise_a_zero_exige_un_motif(client):
    pid, pompe = _nouveau_produit(client)
    lv = _livraison(client, pid, "2026-05-01", 100).json()
    _releve(client, pompe, "2026-05-02", "Matin", 0, 60)      # reste 40
    r = client.post(f"/api/livraisons/{lv['id']}/cloturer", headers=H, json={"reporter_reste": False})
    assert r.status_code == 400
    r = client.post(f"/api/livraisons/{lv['id']}/cloturer", headers=H,
                    json={"reporter_reste": False, "motif": "Perte constatée à la jauge"})
    assert r.status_code == 200, r.text
    assert r.json()["motif_cloture"] == "Perte constatée à la jauge"
    assert r.json()["gallons_restants_cloture"] == 40


def test_report_ne_demande_pas_de_motif(client):
    pid, pompe = _nouveau_produit(client)
    a = _livraison(client, pid, "2026-06-01", 100).json()
    _releve(client, pompe, "2026-06-02", "Matin", 0, 60)
    b = _livraison(client, pid, "2026-06-03", 500).json()
    r = client.post(f"/api/livraisons/{a['id']}/cloturer", headers=H, json={"reporter_reste": True})
    assert r.status_code == 200, r.text
    assert r.json()["reste_reporte"] is True
    stock = next(s for s in client.get("/api/stock", headers=H).json()["stocks"] if s["produit_id"] == pid)
    assert stock["gallons_restants_cargaison_active"] == 540
    assert stock["gallons_restants"] == 540


# ── Caisse : transferts ───────────────────────────────────────────────────

def test_depot_banque_est_un_transfert_pas_une_charge(client):
    jour = "2026-07-15"
    for cat, montant in (("Dépot_en_Banque", 1000), ("Electricite", 200)):
        r = client.post("/api/depenses", headers=H, json={
            "categorie": cat, "description": "test", "montant": montant, "date_depense": jour})
        assert r.status_code in (200, 201), r.text
    d = client.get(f"/api/caisse/rapport?date_debut={jour}&date_fin={jour}", headers=H).json()
    assert d["depenses"]["total"] == 200
    assert d["transferts"]["total"] == 1000
    assert d["synthese"]["total_sorties"] == 1200      # le cash sort quand même
    assert d["synthese"]["total_charges"] == 200


# ── Anomalies ─────────────────────────────────────────────────────────────

def test_anomalie_compteur_discontinu_et_traitement(client):
    pid, pompe = _nouveau_produit(client)
    _releve(client, pompe, "2026-08-01", "Matin", 100, 150)
    _releve(client, pompe, "2026-08-01", "Apres-midi", 158, 200, motif="Vente de nuit oubliée")
    d = client.get("/api/anomalies?date=2026-08-01", headers=H).json()
    a = [x for x in d["anomalies"] if x["type"] == "COMPTEUR_DISCONTINU" and x["produit_id"] == pid]
    assert len(a) == 1 and a[0]["ecart"] == 8 and a[0]["gravite"] == "avertissement"
    # Le lendemain, l'anomalie du 01/08 n'est plus répétée
    d2 = client.get("/api/anomalies?date=2026-08-02", headers=H).json()
    assert not [x for x in d2["anomalies"] if x.get("produit_id") == pid and x["type"] == "COMPTEUR_DISCONTINU"]
    # Traitée → disparaît de la liste du jour
    r = client.post("/api/anomalies/traiter", headers=H, json={"cle": a[0]["cle"]})
    assert r.status_code == 201, r.text
    d3 = client.get("/api/anomalies?date=2026-08-01", headers=H).json()
    assert not [x for x in d3["anomalies"] if x["cle"] == a[0]["cle"]]
    assert d3["nb_traitees"] >= 1


def test_description_de_depense_obligatoire(client):
    r = client.post("/api/depenses", headers=H, json={
        "categorie": "Electricite", "description": "   ", "montant": 100, "date_depense": "2026-07-16"})
    assert r.status_code == 400 and "description" in r.json()["detail"].lower()


# ── Accès : la rentabilité du Bar est réservée à la direction ─────────────

def _compte(client, role_nom):
    roles = client.get("/api/admin/roles", headers=H).json()
    role = next((r for r in roles if r["nom"].lower() == role_nom.lower()), None)
    if not role:
        role = client.post("/api/admin/roles", headers=H, json={"nom": role_nom, "permissions": {"bar": "complet"}}).json()
    n = time.time_ns()
    email, mdp, pin = f"t{n}@konekta.test", "MotDePasse123!", f"{n % 10**9:09d}"
    r = client.post("/api/admin/users", headers=H, json={
        "nom_complet": f"Test {role_nom}", "email": email, "username": f"u{n}",
        "password": mdp, "code_acces": pin, "role_id": role["id"]})
    assert r.status_code == 201, r.text
    return email, mdp, pin


def _stats_en_tant_que(email, mdp, pin):
    import main
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:          # client séparé : sa propre session
        assert c.post("/api/login", json={"email": email, "password": mdp, "code_acces": pin}).status_code == 200
        return c.get("/api/pos/stats").status_code


def test_rentabilite_refusee_aux_employes_de_caisse(client):
    for role in ("Caissiere", "Barman / Caissier Bar"):
        assert _stats_en_tant_que(*_compte(client, role)) == 403, role
    assert _stats_en_tant_que(*_compte(client, "Comptable")) == 200
    assert client.get("/api/pos/stats", headers=H).status_code == 200      # administrateur
