"""
Journal de la caisse principale : tous les mouvements d'argent du système
(ventes carburant, entrées des sources, dépenses, transferts, achats…) au
débit / crédit, solde cumulé, jours, contrôles rapprochés avec les modules.
"""
import time

H = {"X-API-Key": "smoke-test-key"}
J1, J2 = "2025-01-10", "2025-01-11"


def _journal(client, debut, fin):
    r = client.get(f"/api/caisse-principale/journal?date_debut={debut}&date_fin={fin}", headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def test_journal_de_caisse(client):
    nom = f"GL-{time.time_ns()}"
    pid = client.post("/api/produits", headers=H, json={"nom": nom, "prix_gallon": 100}).json()["id"]
    pompe = client.post(f"/api/produits/{pid}/pompes", headers=H, json={"nom": f"{nom} P1"}).json()["id"]
    ventes = 0.0
    for per, av, ap in (("Matin", 0, 10), ("Apres-midi", 10, 15)):
        r = client.post("/api/releves", headers=H, json={"date": J1, "periode": per, "pompe_id": pompe,
                                                          "prix_gallon": 100, "metter_avant": av, "metter_apres": ap})
        assert r.status_code in (200, 201), r.text
        ventes += r.json()["montant_vente"]
    src = client.get("/api/caisse-principale/sources", headers=H).json()[0]
    e1 = client.post("/api/caisse-principale/entrees", headers=H,
                     json={"source_id": src["id"], "montant": 5000, "date_entree": J1, "reference": "BRD-1"}).json()
    e2 = client.post("/api/caisse-principale/entrees", headers=H,
                     json={"source_id": src["id"], "montant": 999, "date_entree": J1}).json()
    client.post(f"/api/caisse-principale/entrees/{e2['id']}/annuler", headers=H, json={"motif": "Saisie en double"})
    for cat, m in (("Electricite", 800), ("Dépot_en_Banque", 2000)):
        r = client.post("/api/depenses", headers=H, json={"categorie": cat, "description": "test", "montant": m,
                                                          "date_depense": J2})
        assert r.status_code in (200, 201), r.text
    r = client.post("/api/achats", headers=H, json={"fournisseur": "Quincaillerie", "description": "Tuyau",
                                                     "categorie": "Equipement", "montant": 300, "date_achat": J2})
    assert r.status_code in (200, 201), r.text

    j = _journal(client, J1, J2)
    L = {l["cle"]: l for l in j["lignes"]}
    vte = [l for l in j["lignes"] if l["type"] == "vente_carburant" and l["produit_id"] == pid]
    assert len(vte) == 2 and abs(sum(l["debit"] for l in vte) - ventes) < 0.01      # une ligne par période
    assert L[f"ENT-{e1['id']}"]["debit"] == 5000 and L[f"ENT-{e1['id']}"]["reference"] == "BRD-1"
    assert L[f"ENT-{e2['id']}"]["annulee"] is True                                    # visible, pas comptée
    assert {l["type"] for l in j["lignes"] if l["date"] == J2} >= {"depense", "transfert", "achat"}
    assert all(l["credit"] == 0 for l in j["lignes"] if l["sens"] == "D")

    # Solde cumulé ligne après ligne, clôture = ouverture + débits − crédits
    s = j["solde_ouverture"]
    for l in j["lignes"]:
        s = round(s + (0 if l["annulee"] else l["debit"] - l["credit"]), 2)
        assert abs(l["solde"] - s) < 0.01
    assert abs(j["solde_cloture"] - (j["solde_ouverture"] + j["total_debit"] - j["total_credit"])) < 0.01
    assert abs(j["solde_cloture"] - j["lignes"][-1]["solde"]) < 0.01

    # Jours depuis l'opération, saisie tardive signalée (enregistrée en 2026 pour 2025)
    assert all(l["jours"] > 300 for l in j["lignes"])
    ctl = {c["code"]: c for c in j["controles"]}
    assert ctl["equilibre"]["niveau"] == "ok", ctl["equilibre"]
    assert ctl["saisies_tardives"]["niveau"] == "alerte" and f"DEP-" in " ".join(ctl["saisies_tardives"]["cles"])
    assert ctl["annulees"]["cles"] == [f"ENT-{e2['id']}"]
    assert ctl["jours_sans_ventes"]["jours"] == [J2]

    # Le solde d'ouverture du lendemain reprend la fin du premier jour
    fin_j1 = [l["solde"] for l in j["lignes"] if l["date"] == J1][-1]
    assert abs(_journal(client, J2, J2)["solde_ouverture"] - fin_j1) < 0.01

    # Totaux par compte
    comptes = {(c["sens"], c["compte"]): c for c in j["par_compte"]}
    assert comptes[("C", "Electricite")]["credit"] == 800
