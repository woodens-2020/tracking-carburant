"""
Reprise des comptes pompistes depuis l'ancien système : le pompiste tape son
code puis son ancien PIN ; le PIN est vérifié par l'ancien système (simulé
ici) et le compte est créé avec ce même PIN. Mauvais PIN, compte de test ou
reprise inactive : rien n'est créé.
"""
from fastapi.testclient import TestClient

ANCIENS = {"ancienpompiste1234": ("Anne Ancienne", "123123123"), "pompiste.test": ("Test", "111111111")}


def _faux_ancien(url, chemin, corps, session=None):
    nom, pin = ANCIENS.get(corps.get("username"), (None, None))
    if chemin == "/api/pompiste/identifier":
        return (200, {"prenom": nom.split()[0]}) if nom else (404, {})
    if chemin == "/api/login/pompiste":
        return (200, {"nom_complet": nom}) if nom and corps.get("code_acces") == pin else (401, {})
    return 200, {}


def _client():
    import main
    return TestClient(main.app)


def test_reprise_avec_ancien_pin(client, monkeypatch):
    import reprise_pompistes
    from models import Utilisateur
    from database import session_principale
    monkeypatch.setattr(reprise_pompistes, "source", lambda request: "http://ancien.systeme")
    monkeypatch.setattr(reprise_pompistes, "_appel", _faux_ancien)
    c = _client()
    r = c.post("/api/pompiste/identifier", json={"username": "AncienPompiste1234"})
    assert r.status_code == 200 and r.json()["reprise"] is True and r.json()["prenom"] == "Anne"
    # Mauvais PIN : refusé, aucun compte créé
    assert c.post("/api/login/pompiste", json={"username": "ancienpompiste1234", "code_acces": "999999999"}).status_code == 401
    with session_principale() as db:
        assert db.query(Utilisateur).filter_by(username="ancienpompiste1234").first() is None
    # Bon PIN : compte créé avec le même code et le même PIN
    assert c.post("/api/login/pompiste", json={"username": "ancienpompiste1234", "code_acces": "123123123"}).status_code == 200
    assert c.get("/api/pompiste/contexte").status_code == 200
    with session_principale() as db:
        u = db.query(Utilisateur).filter_by(username="ancienpompiste1234").first()
        assert u is not None and u.nom_complet == "Anne Ancienne"
    # Les fois suivantes : connexion locale, sans l'ancien système
    monkeypatch.setattr(reprise_pompistes, "_appel", lambda *a, **k: (_ for _ in ()).throw(AssertionError("appel inutile")))
    assert _client().post("/api/login/pompiste", json={"username": "ancienpompiste1234", "code_acces": "123123123"}).status_code == 200
    r = _client().post("/api/pompiste/identifier", json={"username": "ancienpompiste1234"})
    assert r.status_code == 200 and "reprise" not in r.json()


def test_reprise_refusee(client, monkeypatch):
    import reprise_pompistes
    from models import Utilisateur
    from database import session_principale
    monkeypatch.setattr(reprise_pompistes, "_appel", _faux_ancien)
    # Compte de test exclu
    monkeypatch.setattr(reprise_pompistes, "source", lambda request: "http://ancien.systeme")
    c = _client()
    with session_principale() as db:
        deja = db.query(Utilisateur).filter_by(username="pompiste.test").first() is not None
    if not deja:
        assert c.post("/api/pompiste/identifier", json={"username": "pompiste.test"}).status_code == 404
        assert c.post("/api/login/pompiste", json={"username": "pompiste.test", "code_acces": "111111111"}).status_code == 401
    # Reprise inactive (staging, date dépassée, domaine inconnu) : code inconnu
    monkeypatch.setattr(reprise_pompistes, "source", lambda request: None)
    assert c.post("/api/pompiste/identifier", json={"username": "inconnu5555"}).status_code == 404


def test_source_par_domaine(monkeypatch):
    import reprise_pompistes
    from types import SimpleNamespace
    monkeypatch.delenv("REPRISE_POMPISTES_URL", raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    req = lambda h: SimpleNamespace(headers={"host": h})
    assert reprise_pompistes.source(req("konekta-cpv.com")) == "https://vertieres-app-staging.up.railway.app"
    assert reprise_pompistes.source(req("app.konekta-bpccp.com")) is None
    monkeypatch.setenv("APP_ENV", "staging")
    assert reprise_pompistes.source(req("konekta-cpv.com")) is None             # jamais sur le staging lui-même
