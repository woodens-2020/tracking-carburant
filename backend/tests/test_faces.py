"""
Pompes physiques et faces : Pompe 1 › Face A › Gazoline 1, Gazoline 2,
Diesel 1 ; Face B › Gazoline 3, Gazoline 4, Diesel 2. Les mètres (« pompes »
du journal) gardent leur historique ; on ne fait que les placer.
"""
import time

H = {"X-API-Key": "smoke-test-key"}


def _produit(client, nom):
    pid = client.post("/api/produits", headers=H, json={"nom": f"{nom}-{time.time_ns()}", "prix_gallon": 500}).json()["id"]
    return pid


def test_pompe_faces_metres(client):
    t = time.time_ns()
    gaz, die = _produit(client, "Gaz"), _produit(client, "Die")
    r = client.post("/api/distributeurs", headers=H, json={"nom": f"Pompe 1 {t}"})
    assert r.status_code == 201 and r.json()["faces"] == ["A", "B"]
    did = r.json()["id"]
    assert client.post("/api/distributeurs", headers=H, json={"nom": f"pompe 1 {t}"}).status_code == 409
    d = next(x for x in client.get("/api/distributeurs", headers=H).json()["distributeurs"] if x["id"] == did)
    fa, fb = (f["id"] for f in d["faces"])
    # Face A : Gazoline 1, Gazoline 2, Diesel 1 — Face B : Gazoline 3, Gazoline 4, Diesel 2
    for prod, nom, face in ((gaz, "Gazoline 1", fa), (gaz, "Gazoline 2", fa), (die, "Diesel 1", fa),
                            (gaz, "Gazoline 3", fb), (gaz, "Gazoline 4", fb)):
        r = client.post(f"/api/produits/{prod}/pompes", headers=H, json={"nom": nom, "face_id": face})
        assert r.status_code == 200 and r.json()["emplacement"].endswith(f"Face {'A' if face == fa else 'B'}"), r.text
    # Un mètre existant, placé ensuite
    d2 = client.post(f"/api/produits/{die}/pompes", headers=H, json={"nom": "Diesel 2"}).json()["id"]
    assert any(m["id"] == d2 for m in client.get("/api/distributeurs", headers=H).json()["sans_emplacement"])
    r = client.patch(f"/api/pompes/{d2}", headers=H, json={"face_id": fb})
    assert r.status_code == 200 and r.json()["emplacement"] == f"Pompe 1 {t} · Face B"
    d = next(x for x in client.get("/api/distributeurs", headers=H).json()["distributeurs"] if x["id"] == did)
    assert [[m["nom"] for m in f["metres"]] for f in d["faces"]] == [
        ["Gazoline 1", "Gazoline 2", "Diesel 1"], ["Gazoline 3", "Gazoline 4", "Diesel 2"]]
    # L'emplacement est donné avec les produits
    pompes = next(p for p in client.get("/api/produits", headers=H).json() if p["id"] == die)["pompes"]
    assert sorted(p["emplacement"] for p in pompes) == [f"Pompe 1 {t} · Face A", f"Pompe 1 {t} · Face B"]
    # On ne supprime pas une pompe / une face qui porte des mètres
    assert client.delete(f"/api/distributeurs/{did}", headers=H).status_code == 409
    assert client.delete(f"/api/faces/{fb}", headers=H).status_code == 409
    # Retirer un mètre de sa face, renommer la face, ajouter une face C
    assert client.patch(f"/api/pompes/{d2}", headers=H, json={"face_id": 0}).json()["emplacement"] is None
    assert client.patch(f"/api/faces/{fb}", headers=H, json={"nom": "Arrière"}).json()["nom"] == "Arrière"
    assert client.post(f"/api/distributeurs/{did}/faces", headers=H, json={"nom": "C"}).status_code == 201
    assert client.post(f"/api/distributeurs/{did}/faces", headers=H, json={"nom": "c"}).status_code == 409


def test_pompe_vide_supprimee(client):
    t = time.time_ns()
    did = client.post("/api/distributeurs", headers=H, json={"nom": f"Pompe vide {t}", "faces": ["A"]}).json()["id"]
    assert client.delete(f"/api/distributeurs/{did}", headers=H).status_code == 200
    assert not [x for x in client.get("/api/distributeurs", headers=H).json()["distributeurs"] if x["id"] == did]
