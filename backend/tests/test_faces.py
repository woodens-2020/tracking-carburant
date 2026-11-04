"""
Pompes physiques sans faces : Pompe 1 › Gazoline 1, Gazoline 2, Diesel 1…
Les anciennes faces A / B sont regroupées sur la pompe au démarrage, sans
toucher aux mètres ni à leur historique.
"""
import time

H = {"X-API-Key": "smoke-test-key"}


def _produit(client, nom):
    pid = client.post("/api/produits", headers=H, json={"nom": f"{nom}-{time.time_ns()}", "prix_gallon": 500}).json()["id"]
    return pid


def _pompe(client, did):
    return next(x for x in client.get("/api/distributeurs", headers=H).json()["distributeurs"] if x["id"] == did)


def test_pompe_et_ses_metres(client):
    t = time.time_ns()
    gaz, die = _produit(client, "Gaz"), _produit(client, "Die")
    r = client.post("/api/distributeurs", headers=H, json={"nom": f"Pompe 1 {t}"})
    assert r.status_code == 201 and "faces" not in r.json()
    did = r.json()["id"]
    assert client.post("/api/distributeurs", headers=H, json={"nom": f"pompe 1 {t}"}).status_code == 409
    for prod, nom in ((gaz, "Gazoline 1"), (gaz, "Gazoline 2"), (die, "Diesel 1")):
        r = client.post(f"/api/produits/{prod}/pompes", headers=H, json={"nom": nom, "distributeur_id": did})
        assert r.status_code == 200 and r.json()["emplacement"] == f"Pompe 1 {t}", r.text
    # Un mètre existant, placé ensuite sur la pompe
    d2 = client.post(f"/api/produits/{die}/pompes", headers=H, json={"nom": "Diesel 2"}).json()["id"]
    assert any(m["id"] == d2 for m in client.get("/api/distributeurs", headers=H).json()["sans_emplacement"])
    r = client.patch(f"/api/pompes/{d2}", headers=H, json={"distributeur_id": did})
    assert r.status_code == 200 and r.json()["emplacement"] == f"Pompe 1 {t}" and r.json()["distributeur_id"] == did
    assert sorted(m["nom"] for m in _pompe(client, did)["metres"]) == ["Diesel 1", "Diesel 2", "Gazoline 1", "Gazoline 2"]
    pompes = next(p for p in client.get("/api/produits", headers=H).json() if p["id"] == die)["pompes"]
    assert {p["emplacement"] for p in pompes} == {f"Pompe 1 {t}"}
    # On ne supprime pas une pompe qui porte des mètres ; plus de faces à ajouter
    assert client.delete(f"/api/distributeurs/{did}", headers=H).status_code == 409
    assert client.post(f"/api/distributeurs/{did}/faces", headers=H, json={"nom": "B"}).status_code == 410
    assert client.patch(f"/api/pompes/{d2}", headers=H, json={"distributeur_id": 0}).json()["emplacement"] is None


def test_anciennes_faces_regroupees(client, db):
    """Pompe enregistrée avec des faces A / B (ancien format) : après la
    fusion, une seule pompe porte tous ses mètres, relevés intacts."""
    import main
    from models import Distributeur, Face, Pompe
    t = time.time_ns()
    gaz = _produit(client, "Gaz")
    d = Distributeur(nom=f"Ancienne {t}", adresse=None, ordre=99)
    db.add(d); db.flush()
    fa, fb = Face(distributeur_id=d.id, nom="A", ordre=0), Face(distributeur_id=d.id, nom="B", ordre=1)
    db.add_all([fa, fb]); db.flush()
    db.add_all([Pompe(produit_id=gaz, nom=f"G1 {t}", face_id=fa.id), Pompe(produit_id=gaz, nom=f"G2 {t}", face_id=fb.id)])
    db.commit()
    assert main.fusionner_faces(db) >= 1
    assert main.fusionner_faces(db) == 0                       # idempotent
    db.expire_all()
    assert db.query(Face).filter_by(distributeur_id=d.id).count() == 1
    p = _pompe(client, d.id)
    assert sorted(m["nom"] for m in p["metres"]) == [f"G1 {t}", f"G2 {t}"]
    assert {q.emplacement for q in db.query(Pompe).filter(Pompe.nom.in_([f"G1 {t}", f"G2 {t}"]))} == {f"Ancienne {t}"}


def test_pompe_vide_supprimee(client):
    t = time.time_ns()
    did = client.post("/api/distributeurs", headers=H, json={"nom": f"Pompe vide {t}"}).json()["id"]
    assert client.delete(f"/api/distributeurs/{did}", headers=H).status_code == 200
    assert not [x for x in client.get("/api/distributeurs", headers=H).json()["distributeurs"] if x["id"] == did]


def test_ordre_des_pompes(client):
    t = time.time_ns()
    a = client.post("/api/distributeurs", headers=H, json={"nom": f"Ordre A {t}"}).json()["id"]
    b = client.post("/api/distributeurs", headers=H, json={"nom": f"Ordre B {t}"}).json()["id"]
    assert client.post("/api/distributeurs/ordre", headers=H, json={"ids": [b, a]}).status_code == 200
    ids = [x["id"] for x in client.get("/api/distributeurs", headers=H).json()["distributeurs"]]
    assert ids[:2] == [b, a]
    assert client.post("/api/distributeurs/ordre", headers=H, json={"ids": [b, b]}).status_code == 400
    assert client.post("/api/distributeurs/ordre", headers=H, json={"ids": [999999]}).status_code == 400


def test_metre_supprime_puis_reactive(client):
    """Supprimer un mètre qui a des relevés le désactive (relevés gardés) ;
    « Réactiver » annule la suppression."""
    t = time.time_ns()
    gaz = _produit(client, "Gaz")
    m = client.post(f"/api/produits/{gaz}/pompes", headers=H, json={"nom": "Gazoline 1"}).json()["id"]
    r = client.post("/api/releves", headers=H, json={"date": "2025-02-03", "periode": "Matin", "pompe_id": m,
                                                     "prix_gallon": 500, "metter_avant": 100, "metter_apres": 150})
    assert r.status_code == 200, r.text
    assert client.delete(f"/api/pompes/{m}", headers=H).json()["desactivee"] is True
    pompes = lambda: next(p for p in client.get("/api/produits", headers=H).json() if p["id"] == gaz)["pompes"]
    assert pompes() == []
    off = [x for x in client.get("/api/distributeurs", headers=H).json()["desactives"] if x["id"] == m]
    assert len(off) == 1 and off[0]["nb_releves"] == 1 and off[0]["dernier_releve"] == "2025-02-03"
    # Recréer un mètre du même nom : refusé, on propose de le réactiver
    r = client.post(f"/api/produits/{gaz}/pompes", headers=H, json={"nom": "gazoline 1"})
    assert r.status_code == 409 and "réactivez" in r.json()["detail"]
    assert client.patch(f"/api/pompes/{m}", headers=H, json={"actif": True}).status_code == 200
    assert m in [p["id"] for p in pompes()]
    assert not [x for x in client.get("/api/distributeurs", headers=H).json()["desactives"] if x["id"] == m]
    rel = client.get("/api/releves?date=2025-02-03", headers=H).json()
    assert any(x["pompe_id"] == m and x["metter_apres"] == 150 for x in rel)
