"""
Logique métier du stock carburant : répartition des ventes entre cargaisons.

Chaque test crée son propre produit (nom unique) pour rester indépendant.
"""
import itertools
from datetime import date, datetime, timedelta, timezone

from models import Livraison, Pompe, Produit, Releve
from stock_service import fifo_allocation_livraisons, stock_restant

_seq = itertools.count(1)


def _produit(db):
    p = Produit(nom=f"Test-{next(_seq)}-{datetime.now().timestamp()}", prix_gallon=800)
    db.add(p); db.flush()
    po = Pompe(produit_id=p.id, nom=f"{p.nom} P1")
    db.add(po); db.flush()
    return p, po


def _livraison(db, p, jour, gallons, prix=700, **kw):
    l = Livraison(produit_id=p.id, date_livraison=jour, gallons_recus=gallons,
                  prix_achat_gallon=prix,
                  created_at=datetime.combine(jour, datetime.min.time(), timezone.utc) - timedelta(days=1),
                  **kw)
    db.add(l); db.flush()
    return l


class _Compteur:
    """Enchaîne des relevés continus sur une pompe (avant = après précédent)."""
    def __init__(self, db, pompe, depart=1000.0):
        self.db, self.pompe, self.index = db, pompe, depart

    def vend(self, jour, periode, gallons, prix=800):
        r = Releve(date=jour, periode=periode, pompe_id=self.pompe.id, prix_gallon=prix,
                   metter_avant=self.index, metter_apres=self.index + gallons,
                   nb_modifications=0,
                   created_at=datetime.combine(jour, datetime.min.time(), timezone.utc)
                   + timedelta(hours=10 if periode == "Matin" else 18))
        self.index += gallons
        self.db.add(r); self.db.flush()
        return r


def _jours(debut, n):
    return [debut + timedelta(days=i) for i in range(n)]


def test_une_cargaison_simple(db):
    p, po = _produit(db)
    l = _livraison(db, p, date(2026, 9, 1), 1000)
    c = _Compteur(db, po)
    for j in _jours(date(2026, 9, 1), 4):
        c.vend(j, "Matin", 50)
    a = {x["livraison_id"]: x for x in fifo_allocation_livraisons(db, p.id)}[l.id]
    assert a["gallons_consommes"] == 200
    assert a["gallons_restants"] == 800
    assert a["revenu_periode"] == 200 * 800


def test_pas_de_double_comptage_cargaison_antidatee(db):
    """Cas réel bpccp #43/#50 : une cargaison est clôturée (ventes figées),
    puis la suivante est saisie avec une date ANTÉRIEURE à cette clôture.
    Les ventes de la période commune ne doivent compter qu'une fois."""
    p, po = _produit(db)
    a43 = _livraison(db, p, date(2026, 8, 21), 1000)
    c = _Compteur(db, po)
    # 21/08 → 29/09 : 40 jours à 25 gal = 1000 gal vendus
    for j in _jours(date(2026, 8, 21), 40):
        c.vend(j, "Matin", 25)
    # Clôture de #43 : tout est consommé, reste figé à 0
    a43.terminee = True
    a43.gallons_restants_cloture = 0
    db.flush()
    # #50 saisie après coup, datée du 21/09 (avant la clôture du 29/09)
    a50 = _livraison(db, p, date(2026, 9, 21), 900)
    # Ventes après la clôture : 30/09 → 09/10, 10 jours à 30 gal = 300 gal
    for j in _jours(date(2026, 9, 30), 10):
        c.vend(j, "Matin", 30)

    alloc = {x["livraison_id"]: x for x in fifo_allocation_livraisons(db, p.id)}
    # Ancien calcul : #50 comptait aussi les 9 jours 21/09 → 29/09 (225 gal)
    assert alloc[a50.id]["gallons_consommes"] == 300
    assert alloc[a50.id]["gallons_restants"] == 600

    # Cohérence globale : livré − vendu − écarts = reste des cargaisons ouvertes
    s = stock_restant(db, p.id)
    assert s["gallons_restants"] == s["gallons_restants_cargaison_active"] == 600
    total_consomme = sum(x["gallons_consommes"] for x in alloc.values())
    assert total_consomme == s["gallons_vendus"] == 1300


def test_reste_reporte_reste_disponible(db):
    p, po = _produit(db)
    a = _livraison(db, p, date(2026, 7, 1), 500)
    c = _Compteur(db, po)
    for j in _jours(date(2026, 7, 1), 8):
        c.vend(j, "Matin", 50)                       # 400 vendus, reste 100
    b = _livraison(db, p, date(2026, 7, 9), 500)
    a.terminee = True
    a.gallons_restants_cloture = 100
    a.report_vers_livraison_id = b.id
    b.gallons_report_recu = 100
    db.flush()
    for j in _jours(date(2026, 7, 9), 4):
        c.vend(j, "Matin", 50)                       # 200 vendus
    alloc = {x["livraison_id"]: x for x in fifo_allocation_livraisons(db, p.id)}
    assert alloc[b.id]["gallons_disponibles"] == 600
    assert alloc[b.id]["gallons_restants"] == 400
    assert stock_restant(db, p.id)["gallons_restants"] == 400


def test_vente_avant_arrivee_reste_sur_ancienne_cargaison(db):
    """Une vente ne peut pas venir d'une cargaison arrivée après elle :
    le dépassement reste sur l'ancienne comme survente."""
    p, po = _produit(db)
    a = _livraison(db, p, date(2026, 6, 1), 100)
    b = _livraison(db, p, date(2026, 6, 10), 1000)
    c = _Compteur(db, po)
    for j in _jours(date(2026, 6, 1), 5):
        c.vend(j, "Matin", 30)                       # 150 vendus avant l'arrivée de b
    c.vend(date(2026, 6, 11), "Matin", 40)
    alloc = {x["livraison_id"]: x for x in fifo_allocation_livraisons(db, p.id)}
    assert alloc[a.id]["gallons_consommes"] == 100
    assert alloc[a.id]["survente"] == 50
    assert alloc[b.id]["gallons_consommes"] == 40


def test_ancienne_cargaison_ouverte_servie_en_premier(db):
    """Deux cargaisons ouvertes : la plus ancienne se vide d'abord."""
    p, po = _produit(db)
    a = _livraison(db, p, date(2026, 5, 1), 100)
    b = _livraison(db, p, date(2026, 5, 3), 500)
    c = _Compteur(db, po)
    c.vend(date(2026, 5, 1), "Matin", 60)
    c.vend(date(2026, 5, 4), "Matin", 100)          # 40 finissent a, 60 vont à b
    alloc = {x["livraison_id"]: x for x in fifo_allocation_livraisons(db, p.id)}
    assert alloc[a.id]["gallons_restants"] == 0
    assert alloc[b.id]["gallons_consommes"] == 60
