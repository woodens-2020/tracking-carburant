"""
Une session de caisse ne se ferme qu'à sa soumission, jamais à minuit.

Régression : la session EN_COURS était retrouvée par `date_session == jour`,
donc invisible dès minuit (heure d'Haïti) — l'écran POS la montrait fermée,
les ventes de nuit partaient sans session ni bar, et l'employé devait en
ouvrir une nouvelle. Scénario simulé ici : session ouverte la veille à 22h,
on est « maintenant » le lendemain.

Lancer seul : cd backend && python -m pytest tests/test_session_minuit.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TMP = tempfile.mkdtemp(prefix="konekta-minuit-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMP, "tests.db").replace("\\", "/")
os.environ["ADMIN_API_KEY"] = "test-minuit-key"
os.environ["OTP_ENABLED"] = "false"
os.environ["SECURE_COOKIES"] = "false"

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

H = {"X-API-Key": "test-minuit-key"}


@pytest.fixture(scope="module")
def client():
    import main
    with TestClient(main.app) as c:
        yield c


@pytest.fixture()
def db(client):
    from database import SessionLocal
    s = SessionLocal()
    from models import Utilisateur
    if not s.query(Utilisateur).filter_by(username="admin").first():
        s.add(Utilisateur(username="admin", password_hash="x", nom_complet="Admin", role="admin", actif=True))
        s.commit()
    try:
        yield s
    finally:
        s.close()


_compteur = [0]


def _employe(db):
    from models import Employe
    from tz_utils import today_haiti
    _compteur[0] += 1
    e = Employe(nom="Caisse", prenom=f"Nuit{_compteur[0]}", poste="Caissière",
                date_embauche=today_haiti(), salaire_base=Decimal("0"), actif=True)
    db.add(e)
    db.commit()
    return e


def _hier_22h():
    from tz_utils import now_haiti
    n = now_haiti()
    # Converti en UTC : SQLite (base de test) stocke l'heure murale sans
    # fuseau, comme le serveur Railway (UTC) le ferait en Postgres.
    return (n - timedelta(days=1)).replace(hour=22, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def _jour_haiti(instant):
    from tz_utils import HAITI_TZ
    return instant.astimezone(HAITI_TZ).date()


def _session_bar_hier(db, emp, statut="EN_COURS", soumis_at=None):
    from models import BarSessionCaisse
    debut = _hier_22h()
    s = BarSessionCaisse(caissier_id=emp.id, date_session=_jour_haiti(debut), numero_session=1,
                         statut=statut, lieu="PISCINE", created_at=debut, soumis_at=soumis_at)
    db.add(s)
    db.commit()
    return s


def _vente_sans_session(db, emp, quand, montant="100"):
    from models import BarVente
    _compteur[0] += 1
    v = BarVente(numero_ticket=f"TMN{_compteur[0]:06d}", caissier_id=emp.id, date_heure=quand,
                 montant_total=Decimal(montant), mode_paiement="CASH", statut="PAYEE",
                 montant_paye=Decimal(montant), montant_restant=Decimal("0"))
    db.add(v)
    db.commit()
    return v


# ── Bar ────────────────────────────────────────────────────────────

def test_dashboard_garde_la_session_ouverte_la_veille(client, db):
    emp = _employe(db)
    s = _session_bar_hier(db, emp)
    d = client.get(f"/api/pos/caisse/dashboard?caissier_id={emp.id}", headers=H).json()
    assert d["session_id"] == s.id
    assert d["session_statut"] == "EN_COURS"
    assert d["lieu"] == "PISCINE"


def test_ouvrir_reprend_la_session_de_la_veille(client, db):
    from models import BarSessionCaisse
    emp = _employe(db)
    s = _session_bar_hier(db, emp)
    r = client.post("/api/pos/caisse/sessions/ouvrir", json={"caissier_id": emp.id, "lieu": "DEVANT"}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == s.id
    assert db.query(BarSessionCaisse).filter_by(caissier_id=emp.id).count() == 1


def test_vente_apres_minuit_rattachee_a_la_session(db):
    from pos_service import session_bar_en_cours
    emp = _employe(db)
    s = _session_bar_hier(db, emp)
    trouvee = session_bar_en_cours(emp.id, db)
    assert trouvee is not None and trouvee.id == s.id and trouvee.lieu == "PISCINE"


def test_ventes_de_nuit_comptees_dans_la_session(db):
    from caisse_routes import _ventes_session
    emp = _employe(db)
    s = _session_bar_hier(db, emp)
    debut = _hier_22h()
    v1 = _vente_sans_session(db, emp, debut + timedelta(minutes=30))   # 22h30
    v2 = _vente_sans_session(db, emp, debut + timedelta(hours=3))      # 1h du matin
    ids = {v.id for v in _ventes_session(s, db)}
    assert {v1.id, v2.id} <= ids


def test_session_soumise_apres_minuit_garde_ses_ventes_de_nuit(db):
    from caisse_routes import _ventes_session
    emp = _employe(db)
    debut = _hier_22h()
    s = _session_bar_hier(db, emp, statut="SOUMIS", soumis_at=debut + timedelta(hours=4))  # 2h
    v_nuit = _vente_sans_session(db, emp, debut + timedelta(hours=3))      # 1h : dedans
    v_apres = _vente_sans_session(db, emp, debut + timedelta(hours=5))     # 3h : après soumission
    ids = {v.id for v in _ventes_session(s, db)}
    assert v_nuit.id in ids
    assert v_apres.id not in ids


def test_fenetre_bornee_par_la_session_suivante(db):
    from caisse_routes import _ventes_session
    from models import BarSessionCaisse
    emp = _employe(db)
    debut = _hier_22h()
    s1 = _session_bar_hier(db, emp, statut="SOUMIS", soumis_at=debut + timedelta(hours=4))
    s2 = BarSessionCaisse(caissier_id=emp.id, date_session=_jour_haiti(debut + timedelta(hours=3)),
                          numero_session=1, statut="EN_COURS", lieu="DEVANT",
                          created_at=debut + timedelta(hours=3))
    db.add(s2)
    db.commit()
    v = _vente_sans_session(db, emp, debut + timedelta(hours=3, minutes=30))
    assert v.id not in {x.id for x in _ventes_session(s1, db)}
    assert v.id in {x.id for x in _ventes_session(s2, db)}


def test_session_soumise_le_jour_meme_inchangee(db):
    """Comportement historique conservé : soumise avant minuit → fenêtre
    jusqu'à la fin de son jour calendaire."""
    from tz_utils import fin_fenetre_session, bounds_haiti
    emp = _employe(db)
    debut = _hier_22h()
    s = _session_bar_hier(db, emp, statut="SOUMIS", soumis_at=debut + timedelta(hours=1))
    assert fin_fenetre_session(s, None) == bounds_haiti(s.date_session)[1]


def test_deconnexion_bloquee_apres_minuit(db):
    import main
    from models import Utilisateur
    emp = _employe(db)
    u = Utilisateur(username=f"caisse{emp.id}", password_hash="x", nom_complet="C", role="operateur", actif=True)
    db.add(u)
    db.commit()
    emp.utilisateur_id = u.id
    db.commit()
    s = _session_bar_hier(db, emp)
    assert main._session_ouverte_pour(db, u).id == s.id


# ── Pâtisserie ─────────────────────────────────────────────────────

def test_patisserie_session_de_la_veille_conservee(client, db):
    from models import PatisserieSessionCaisse
    emp = _employe(db)
    debut = _hier_22h()
    s = PatisserieSessionCaisse(caissier_id=emp.id, date_session=_jour_haiti(debut), numero_session=1,
                                statut="EN_COURS", created_at=debut)
    db.add(s)
    db.commit()
    d = client.get(f"/api/patisserie/caisse/dashboard?caissier_id={emp.id}", headers=H).json()
    assert d["session_id"] == s.id and d["session_statut"] == "EN_COURS"
    r = client.post("/api/patisserie/caisse/sessions/ouvrir", json={"caissier_id": emp.id}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == s.id
    assert db.query(PatisserieSessionCaisse).filter_by(caissier_id=emp.id).count() == 1
