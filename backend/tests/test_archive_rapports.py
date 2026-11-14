"""Archive des rapports pompistes : PDF et texte simple nommés
<pompiste>_<date>_<période>, fiche papier jointe renommée, archive zip."""
import io
import json
import time
import zipfile

from fastapi.testclient import TestClient

H = {"X-API-Key": "smoke-test-key"}


def _rapport(client):
    from tz_utils import today_haiti
    nom = f"Arch-{time.time_ns()}"
    pid = client.post("/api/produits", headers=H, json={"nom": nom, "prix_gallon": 500}).json()["id"]
    pompe = client.post(f"/api/produits/{pid}/pompes", headers=H, json={"nom": f"{nom} P1"}).json()["id"]
    c = client.post("/api/pompiste-supervision/comptes", headers=H, json={"prenom": "Rosée", "nom": "Archive"}).json()
    import main
    p = TestClient(main.app)
    r = p.post("/api/login/pompiste", json={"username": c["username"], "code_acces": c["code_acces"]})
    assert r.status_code == 200, r.text
    jour = str(today_haiti())
    r = p.post("/api/pompiste/compteurs", json={"date": jour, "periode": "Apres-midi", "charte_acceptee": True,
               "lignes": [{"pompe_id": pompe, "metter_avant": 50, "metter_apres": 60, "debut_confirme": True}]})
    assert r.status_code in (200, 201), r.text
    r = p.post("/api/pompiste/terminer", json={"billetterie": [{"coupure": 1000, "liasses": 0, "billets": 5}], "monnaie": 0})
    assert r.status_code in (200, 201), r.text
    return r.json()["id"], jour


def test_pdf_texte_fiche_et_archive(client):
    rid, jour = _rapport(client)
    base = f"Rosee_Archive_{jour}_Apres-midi"

    r = client.get(f"/api/pompiste-supervision/rapports/{rid}/pdf", headers=H)
    assert r.status_code == 200 and r.content[:4] == b"%PDF"
    assert f'filename="{base}.pdf"' in r.headers["content-disposition"]

    r = client.get(f"/api/pompiste-supervision/rapports/{rid}/texte", headers=H)
    t = r.json()
    assert t["fichier"] == base and t["pompiste"] == "Rosée Archive" and t["total_gallons"] == 10
    assert t["meeters"][0]["index_debut"] == 50 and t["argent_compte"] == 5000

    for i in (1, 2):
        r = client.post(f"/api/pompiste-supervision/rapports/{rid}/fiches", headers=H,
                        files={"fichier": ("scan.pdf", b"%PDF-1.4 test", "application/pdf")})
        assert r.status_code == 201, r.text
    noms = [x["nom_fichier"] for x in client.get(f"/api/pompiste-supervision/rapports/{rid}/fiches", headers=H).json()]
    assert noms == [f"{base}.pdf", f"{base}_2.pdf"]
    r = client.post(f"/api/pompiste-supervision/rapports/{rid}/fiches", headers=H,
                    files={"fichier": ("x.txt", b"bonjour", "text/plain")})
    assert r.status_code == 400

    r = client.get(f"/api/pompiste-supervision/archive?date_debut={jour}&date_fin={jour}", headers=H)
    a = json.loads(r.content)
    assert any(x["rapport_numero"] == rid for x in a["rapports"])

    r = client.get(f"/api/pompiste-supervision/archive?date_debut={jour}&date_fin={jour}&format=zip", headers=H)
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert f"pdf/{base}.pdf" in z.namelist() and f"texte/{base}.json" in z.namelist()
    assert f"fiches/{base}_2.pdf" in z.namelist()
