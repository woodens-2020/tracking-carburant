"""
Responsable de plusieurs stations : le propriétaire l'autorise dans une autre
station ; après la connexion, il choisit la station où il travaille et y
entre sans retaper son PIN ; plus tard, changer de station demande le PIN ;
retiré, il en sort tout de suite.
"""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

PROPRIO = {"email": "admin@konekta.local", "password": "admin123", "code_acces": "123456789"}


def _c():
    import main
    return TestClient(main.app)


def _station(p, nom, email, pin):
    st = {"produits": [{"cle": "g", "nom": f"Gazoline {nom}", "prix": 900}],
          "pompes": [{"nom": "Pompe 1", "faces": [{"nom": "A", "metres": [{"produit": "g", "nom": "G1", "metre_depart": 10}]}]}]}
    r = p.post("/api/espaces", json={"nom": nom, "adresse": f"{nom}, Haïti", "station": st,
                                     "gerant": {"nom_complet": f"Gérant {nom}", "email": email,
                                                "password": "MotDePasse2026", "pin": pin}})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_responsable_plusieurs_stations(client):
    p = _c()
    assert p.post("/api/login", json=PROPRIO).status_code == 200
    a = _station(p, "Resp A", "resp.a@exemple.ht", "111222333")
    b = _station(p, "Resp B", "resp.b@exemple.ht", "444555666")
    g = _c()
    assert g.post("/api/login", json={"email": "resp.a@exemple.ht", "password": "MotDePasse2026",
                                      "code_acces": "111222333"}).status_code == 200
    m = g.get("/api/espaces/mes-stations").json()
    ok = {x["id"]: x["autorise"] for x in m["stations"]}
    assert m["fraiche"] and ok[a] and not ok[b] and not ok[0]
    assert g.post("/api/espaces/aller", json={"cible_id": b}).status_code == 403
    # Le propriétaire l'autorise à Resp B
    lst = p.get("/api/espaces/responsables").json()["responsables"]
    moi = next(x for x in lst if x["email"] == "resp.a@exemple.ht")
    assert moi["station_id"] == a and not any(x["autorise"] for x in moi["autorisations"])
    assert g.get("/api/espaces/responsables").status_code == 403          # réservé au propriétaire
    r = p.post("/api/espaces/responsables/autoriser", json={"station_id": a, "compte_id": moi["id"],
                                                            "espace_id": b, "autorise": True})
    assert r.status_code == 200, r.text
    m = g.get("/api/espaces/mes-stations").json()
    assert {x["id"]: x["autorise"] for x in m["stations"]}[b]
    # Juste connecté : il entre sans PIN et voit les données de Resp B
    r = g.post("/api/espaces/aller", json={"cible_id": b})
    assert r.status_code == 200, r.text
    assert g.get("/api/espaces").json()["courant"] == f"esp_{b}"
    assert {x["nom"] for x in g.get("/api/produits").json()} == {"Gazoline Resp B"}
    assert g.get("/api/me").json()["email"] == "resp.a@exemple.ht"
    # Plus tard : le PIN est demandé
    from database import session_espace
    from models import SessionToken
    with session_espace(f"esp_{b}") as sdb:
        sdb.query(SessionToken).update({"created_at": datetime.now(timezone.utc) - timedelta(hours=1)})
        sdb.commit()
    assert not g.get("/api/espaces/mes-stations").json()["fraiche"]
    assert g.post("/api/espaces/aller", json={"cible_id": a}).status_code == 400
    assert g.post("/api/espaces/aller", json={"cible_id": a, "pin": "111222333"}).status_code == 200
    assert {x["nom"] for x in g.get("/api/produits").json()} == {"Gazoline Resp A"}
    # Retour à Resp B, puis le propriétaire le retire : il en sort tout de suite
    assert g.post("/api/espaces/aller", json={"cible_id": b}).status_code == 200
    r = p.post("/api/espaces/responsables/autoriser", json={"station_id": a, "compte_id": moi["id"],
                                                            "espace_id": b, "autorise": False})
    assert r.status_code == 200
    assert g.get("/api/produits").status_code == 401
    # Il se reconnecte dans sa station ; Resp B n'est plus proposée
    g2 = _c()
    assert g2.post("/api/login", json={"email": "resp.a@exemple.ht", "password": "MotDePasse2026",
                                       "code_acces": "111222333"}).status_code == 200
    assert not {x["id"]: x["autorise"] for x in g2.get("/api/espaces/mes-stations").json()["stations"]}[b]
    assert g2.post("/api/espaces/aller", json={"cible_id": b}).status_code == 403
