"""
Espace pompiste — saisie des compteurs par les pompistes eux-mêmes.

Circuit (en parallèle de la saisie habituelle par l'administration) :
  connexion identifiant + PIN → charte → index de chaque pompe → billetterie
  → récapitulatif → soumission (contrôle des index + rapprochement
  volume × prix vs argent compté) → rapport « soumis »
  → le superviseur confirme (en corrigeant au besoin) ou refuse avec un
  commentaire → la confirmation écrit les relevés officiels (journal des
  compteurs), qui alimentent ensuite rapports et tableaux de bord.

Deux préfixes :
  /api/pompiste/...              réservé aux comptes de rôle « Pompiste »
  /api/pompiste-supervision/...  superviseurs (admin, direction, carburant complet)
"""
from __future__ import annotations

import logging
import re
import secrets
import threading
import unicodedata
from datetime import timedelta
from datetime import date as date_type
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth import hash_code_acces, hash_password
from database import get_db
from models import (
    Employe, Pompe, Produit, RapportPompiste, RapportPompisteLigne, Releve,
    Role, Utilisateur,
)
from tz_utils import now_haiti, today_haiti

router = APIRouter(tags=["pompiste"])
log = logging.getLogger("pompiste")

ROLE_POMPISTE = "Pompiste"
PERIODES = ("Matin", "Apres-midi")
_RANG = {"Matin": 0, "Apres-midi": 1}
# Billets et pièces en circulation (gourdes). Une liasse = 100 billets.
COUPURES = (1000, 500, 250, 100, 50, 25, 10, 5)
TAILLE_LIASSE = 100
# Garde-fou contre une faute de frappe sur un index (zéro en trop…) :
# au-delà, l'index est refusé et le pompiste revient à la saisie.
MAX_GALLONS_PAR_POMPE_PERIODE = 5000
TOLERANCE = 0.001
_COMMENTAIRE_MIN = 5


# ── Identification des rôles ─────────────────────────────────────────

def est_role_pompiste(role: Optional[Role]) -> bool:
    return bool(role) and (role.nom or "").strip().lower() == ROLE_POMPISTE.lower()


def utilisateur_est_pompiste(db: Session, user_id: int) -> bool:
    u = db.get(Utilisateur, user_id)
    return bool(u) and est_role_pompiste(u.role_obj)


def assurer_role_pompiste(db: Session) -> Role:
    """Crée le rôle système « Pompiste » s'il n'existe pas (idempotent)."""
    r = next((x for x in db.query(Role).all() if est_role_pompiste(x)), None)
    if r:
        return r
    r = Role(
        nom=ROLE_POMPISTE,
        description="Accès limité à l'espace de saisie des compteurs (/pompiste)",
        permissions={"finance": "aucun", "bar": "aucun", "cuisine": "aucun",
                     "hotel": "aucun", "employes": "aucun", "carburant": "aucun",
                     "admin": False},
        est_admin=False, est_systeme=True,
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _norm(txt: str) -> str:
    t = unicodedata.normalize("NFKD", txt or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", t)


def employe_du_compte(db: Session, u: Utilisateur, prenom: Optional[str] = None,
                      nom: Optional[str] = None) -> Employe:
    """Fiche employé du compte pompiste (créée au besoin). C'est elle qui
    apparaît dans le filtre « Pompiste » du journal et dans les relevés
    (`releves.pompiste_id`), comme pour une saisie dans le module Saisie.
    Réutilise un employé « Pompiste » existant de même nom s'il n'est lié à
    aucun compte."""
    emp = db.query(Employe).filter_by(utilisateur_id=u.id).first()
    if emp:
        return emp
    if prenom is None:
        morceaux = (u.nom_complet or u.username).split()
        prenom, nom = morceaux[0], " ".join(morceaux[1:]) or morceaux[0]
    cle = _norm(prenom) + _norm(nom)
    for e in db.query(Employe).filter(Employe.utilisateur_id.is_(None), Employe.actif.is_(True)).all():
        if (e.poste or "").strip().lower() == "pompiste" and _norm(e.prenom) + _norm(e.nom) == cle:
            e.utilisateur_id = u.id
            return e
    emp = Employe(prenom=prenom.strip()[:100], nom=(nom or "").strip()[:100] or prenom.strip()[:100],
                  poste="Pompiste", date_embauche=today_haiti(), salaire_base=0, type_contrat="CDI",
                  actif=u.actif, utilisateur_id=u.id,
                  notes="Fiche créée avec le compte pompiste (espace /pompiste).")
    db.add(emp)
    db.flush()
    return emp


def rattacher_employes_pompistes(db: Session) -> None:
    """Démarrage : comptes pompistes sans fiche employé (créés avant ce
    rattachement) et relevés de l'espace pompiste sans pompiste attribué."""
    role = assurer_role_pompiste(db)
    for u in db.query(Utilisateur).filter_by(role_id=role.id).all():
        employe_du_compte(db, u)
    for r in db.query(Releve).filter(Releve.rapport_pompiste_id.isnot(None), Releve.pompiste_id.is_(None)).all():
        rap = db.get(RapportPompiste, r.rapport_pompiste_id)
        emp = db.query(Employe).filter_by(utilisateur_id=rap.utilisateur_id).first() if rap else None
        if emp:
            r.pompiste_id = emp.id
    db.commit()


class _RequeteInterne:
    """Requête minimale pour upsert_releve hors d'un appel HTTP (seul
    request.state.user.id est lu) : le relevé est attribué au pompiste."""
    def __init__(self, user_id: int):
        from types import SimpleNamespace
        self.state = SimpleNamespace(user=SimpleNamespace(id=user_id))


def rattraper_journal(db: Session) -> int:
    """Démarrage : tout rapport terminé (soumis, en problème, finalisé) dont
    un mètre manque dans le journal des compteurs y est écrit — notamment
    les rapports soumis avant que l'espace pompiste n'écrive le journal au
    « Terminer ». Idempotent : une pompe déjà dans le journal pour ce
    créneau n'est pas touchée (saisie parallèle du module Saisie gardée)."""
    n = 0
    rapports = (db.query(RapportPompiste)
                .filter(RapportPompiste.statut.in_(("CONFIRME", "RESOLU")))
                .order_by(RapportPompiste.date, RapportPompiste.id).all())
    rapports.sort(key=lambda r: (r.date, _RANG.get(r.periode, 9), r.id))     # Matin avant Après-midi
    for rap in rapports:
        manquantes = [l.pompe_id for l in rap.lignes
                      if not db.query(Releve).filter_by(date=rap.date, periode=rap.periode, pompe_id=l.pompe_id).first()]
        for pid in manquantes:
            _ecrire_journal(db, rap, _RequeteInterne(rap.utilisateur_id), remplacer_saisie=False, seulement=pid)
            n += 1
    if n:
        log.info("Journal des compteurs : %d mètre(s) de rapports pompistes rattrapé(s)", n)
    return n


def _valeurs_finales(l: RapportPompisteLigne):
    av = float(l.metter_avant_corrige if l.metter_avant_corrige is not None else l.metter_avant)
    ap = float(l.metter_apres_corrige if l.metter_apres_corrige is not None else l.metter_apres)
    return av, ap


def nettoyer_journal_anticipe(db: Session) -> int:
    """Démarrage : un rapport pas encore finalisé (soumis, en problème) ne
    doit rien avoir dans le journal officiel. Les relevés écrits par l'ancien
    fonctionnement (au « Terminer ») sont retirés s'ils sont identiques au
    rapport ; s'ils ont été modifiés ensuite à la main (module Saisie), ils
    deviennent une saisie ordinaire et sont gardés."""
    n = 0
    for rap in db.query(RapportPompiste).filter(RapportPompiste.statut.in_(("EN_COURS", "SOUMIS", "PROBLEME"))).all():
        lignes = {l.pompe_id: l for l in rap.lignes}
        for r in db.query(Releve).filter_by(rapport_pompiste_id=rap.id).all():
            l = lignes.get(r.pompe_id)
            av, ap = _valeurs_finales(l) if l else (None, None)
            if l is not None and abs(float(r.metter_avant) - av) <= TOLERANCE and abs(float(r.metter_apres) - ap) <= TOLERANCE:
                db.delete(r)
            else:
                r.rapport_pompiste_id = None
            n += 1
    db.commit()
    if n:
        log.info("Journal officiel : %d relevé(s) de rapports non finalisés retirés", n)
    return n


class LignePendante:
    """Mètre d'un rapport pompiste pas encore finalisé, présenté au Journal
    Meters comme un relevé (mêmes attributs que Releve) mais marqué
    « en attente » : il n'est compté dans aucun total, ni caisse, ni stock."""
    en_attente = True
    nb_modifications = 0

    def __init__(self, rap: RapportPompiste, l: RapportPompisteLigne, employe: Optional[Employe]):
        self.id = f"R{rap.id}-{l.pompe_id}"
        self.date, self.periode = rap.date, rap.periode
        self.pompe_id, self.pompe = l.pompe_id, l.pompe
        self.metter_avant, self.metter_apres = _valeurs_finales(l)
        self.prix_gallon = float(l.prix_gallon)
        self.pompiste, self.pompiste_id = employe, (employe.id if employe else None)
        self.rapport_pompiste, self.rapport_pompiste_id = rap, rap.id
        self.cree_par = rap.utilisateur

    @property
    def quantite(self) -> float:
        return round(self.metter_apres - self.metter_avant, 4)

    @property
    def montant_vente(self) -> float:
        return round(self.quantite * self.prix_gallon, 2)


def lignes_en_attente(db: Session, d_debut: date_type, d_fin: date_type) -> list:
    """Mètres des rapports soumis / en problème de la période. Une pompe déjà
    présente au journal officiel pour le même créneau (saisie du module
    Saisie) n'est pas doublée."""
    out = []
    for rap in (db.query(RapportPompiste)
                .filter(RapportPompiste.statut.in_(("SOUMIS", "PROBLEME")),
                        RapportPompiste.date >= d_debut, RapportPompiste.date <= d_fin).all()):
        employe = db.query(Employe).filter_by(utilisateur_id=rap.utilisateur_id).first()
        for l in rap.lignes:
            if db.query(Releve).filter_by(date=rap.date, periode=rap.periode, pompe_id=l.pompe_id).first():
                continue
            out.append(LignePendante(rap, l, employe))
    return out


def ids_employes_pompistes(db: Session) -> set:
    role = next((x for x in db.query(Role).all() if est_role_pompiste(x)), None)
    if not role:
        return set()
    return {e.id for e in db.query(Employe).join(Utilisateur, Employe.utilisateur_id == Utilisateur.id)
            .filter(Utilisateur.role_id == role.id).all()}


def _user(request: Request, db: Session) -> Utilisateur:
    proxy = getattr(request.state, "user", None)
    if not proxy:
        raise HTTPException(401, "Non authentifié")
    u = db.get(Utilisateur, proxy.id)
    if not u or not u.actif:
        raise HTTPException(401, "Compte inactif")
    return u


def _require_pompiste(request: Request, db: Session = Depends(get_db)) -> Utilisateur:
    u = _user(request, db)
    if not est_role_pompiste(u.role_obj):
        raise HTTPException(403, "Espace réservé aux comptes pompistes")
    return u


def peut_superviser(u: Utilisateur) -> bool:
    if u.role in ("admin", "pdg"):
        return True
    r = u.role_obj
    if not r or est_role_pompiste(r):
        return False
    perms = r.permissions or {}
    if r.est_admin or perms.get("admin") or perms.get("carburant") == "complet":
        return True
    nom = (r.nom or "").lower()
    return any(m in nom for m in ("directeur", "superviseur", "manager", "gérant", "gerant"))


def _require_superviseur(request: Request, db: Session = Depends(get_db)) -> Utilisateur:
    u = _user(request, db)
    if not peut_superviser(u):
        raise HTTPException(403, "Réservé aux superviseurs (direction / administration)")
    return u


# ── Index précédent ──────────────────────────────────────────────────

def _index_precedent(db: Session, pompe_id: int, d: date_type, periode: str,
                     exclure_rapport_id: Optional[int] = None):
    """Dernier index connu de la pompe avant (d, periode) : journal officiel
    ou rapport pompiste encore en attente de confirmation (sinon le pompiste
    suivant ne pourrait pas enchaîner tant que le superviseur n'est pas passé).
    Retourne (valeur, libellé de la source) ou (None, None)."""
    cible = (d, _RANG[periode])
    meilleur, cle_m, src = None, None, None
    for r in (db.query(Releve)
              .filter(Releve.pompe_id == pompe_id, Releve.date <= d)
              .order_by(Releve.date.desc()).limit(6).all()):
        cle = (r.date, _RANG.get(r.periode, 9))
        if cle < cible and (cle_m is None or cle > cle_m):
            meilleur, cle_m, src = float(r.metter_apres), cle, f"journal — {_per(r.periode)} du {r.date.strftime('%d/%m')}"
    q = (db.query(RapportPompisteLigne, RapportPompiste)
         .join(RapportPompiste, RapportPompisteLigne.rapport_id == RapportPompiste.id)
         .filter(RapportPompisteLigne.pompe_id == pompe_id,
                 RapportPompiste.statut.in_(("EN_COURS", "SOUMIS", "PROBLEME")),
                 RapportPompiste.date <= d, RapportPompiste.date >= d - timedelta(days=3)))
    if exclure_rapport_id:
        q = q.filter(RapportPompiste.id != exclure_rapport_id)
    for ligne, rap in q.all():
        cle = (rap.date, _RANG.get(rap.periode, 9))
        if cle < cible and (cle_m is None or cle > cle_m):
            meilleur, cle_m = float(ligne.metter_apres), cle
            src = f"rapport pompiste — {_per(rap.periode)} du {rap.date.strftime('%d/%m')}"
    return meilleur, src


def _per(p: str) -> str:
    return "Après-midi" if p == "Apres-midi" else p


def _prix(db: Session, pompe: Pompe, d: date_type) -> float:
    import main  # import tardif : main importe ce module
    p = main._prix_vente_a_date(db, pompe.produit_id, d)
    return float(p) if p is not None else float(pompe.produit.prix_gallon or 0)


def _total_billetterie(billetterie: list, monnaie: float) -> float:
    total = 0.0
    for b in billetterie:
        total += int(b["coupure"]) * (int(b.get("liasses") or 0) * TAILLE_LIASSE + int(b.get("billets") or 0))
    return round(total + float(monnaie or 0), 2)


def _depenses_retenues(r: RapportPompiste) -> float:
    """Dépenses qui réduisent l'argent à remettre : toutes, sauf celles que la
    direction a refusées (le pompiste doit alors cet argent)."""
    return round(sum(float(x["montant"]) for x in (r.depenses or []) if x.get("statut") != "refusee"), 2)


def _nom(u: Optional[Utilisateur]) -> Optional[str]:
    return (u.nom_complet or u.username) if u else None


def _rapport_dict(db: Session, r: RapportPompiste, avec_journal: bool = False) -> dict:
    lignes = []
    for l in r.lignes:
        av = float(l.metter_avant_corrige if l.metter_avant_corrige is not None else l.metter_avant)
        ap = float(l.metter_apres_corrige if l.metter_apres_corrige is not None else l.metter_apres)
        d = {
            "pompe_id": l.pompe_id, "pompe_nom": l.pompe.nom,
            "produit_id": l.pompe.produit_id, "produit_nom": l.pompe.produit.nom,
            "prix_gallon": float(l.prix_gallon),
            "metter_avant": float(l.metter_avant), "metter_apres": float(l.metter_apres),
            "metter_avant_corrige": float(l.metter_avant_corrige) if l.metter_avant_corrige is not None else None,
            "metter_apres_corrige": float(l.metter_apres_corrige) if l.metter_apres_corrige is not None else None,
            "gallons": round(ap - av, 3),
            "montant": round((ap - av) * float(l.prix_gallon), 2),
            "confirmee": bool(l.confirmee),
        }
        if avec_journal:
            j = (db.query(Releve).filter_by(date=r.date, periode=r.periode, pompe_id=l.pompe_id).first())
            d["journal"] = ({"metter_avant": float(j.metter_avant), "metter_apres": float(j.metter_apres),
                             "saisi_par": _nom(j.cree_par), "modifie_par": _nom(j.modifie_par),
                             "de_ce_rapport": j.rapport_pompiste_id == r.id}
                            if j else None)
            prec, src = _index_precedent(db, l.pompe_id, r.date, r.periode, exclure_rapport_id=r.id)
            d["index_precedent"] = prec
            d["index_precedent_source"] = src
        lignes.append(d)
    return {
        "id": r.id, "date": str(r.date), "periode": r.periode, "statut": r.statut,
        "pompiste_id": r.utilisateur_id, "pompiste_nom": _nom(r.utilisateur),
        "charte_acceptee_at": r.charte_acceptee_at.isoformat() if r.charte_acceptee_at else None,
        "billetterie": r.billetterie or [], "monnaie": float(r.monnaie or 0),
        "depenses": [dict(x, statut=x.get("statut") or "en_attente", index=i) for i, x in enumerate(r.depenses or [])],
        "total_depenses": float(r.total_depenses or 0),
        "total_depenses_retenues": _depenses_retenues(r),
        "depenses_a_decider": sum(1 for x in (r.depenses or []) if (x.get("statut") or "en_attente") == "en_attente"),
        "devises": r.devises or [],
        "montant_a_remettre": round(float(r.montant_attendu) - float(r.total_depenses or 0), 2),
        "montant_attendu": float(r.montant_attendu), "montant_compte": float(r.montant_compte),
        "ecart": float(r.ecart), "note_pompiste": r.note_pompiste,
        "soumis_at": r.soumis_at.isoformat() if r.soumis_at else None,
        "traite_par_nom": _nom(r.traite_par),
        "traite_at": r.traite_at.isoformat() if r.traite_at else None,
        "commentaire_superviseur": r.commentaire_superviseur,
        "lignes": lignes,
        "total_gallons": round(sum(x["gallons"] for x in lignes), 3),
        # Après corrections éventuelles du superviseur
        "montant_attendu_final": round(sum(x["montant"] for x in lignes), 2),
        # Écart = argent compté + dépenses payées (sauf celles refusées par la direction) − ventes
        "ecart_final": round(float(r.montant_compte) + _depenses_retenues(r) - sum(x["montant"] for x in lignes), 2),
        "billetterie_confirmee": bool(r.billetterie_confirmee),
        "ecart_confirme": bool(r.ecart_confirme),
        "note_superviseur": r.note_superviseur,
        "probleme_commentaire": r.probleme_commentaire,
        "probleme_par_nom": _nom(r.probleme_par),
        "probleme_at": r.probleme_at.isoformat() if r.probleme_at else None,
    }


# ═════════════════════════════════════════════════════════════════════
# ESPACE POMPISTE
# ═════════════════════════════════════════════════════════════════════

# Le pompiste choisit la date de son rapport dans un calendrier : jamais
# dans le futur, et au plus JOURS_EN_ARRIERE jours en arrière.
JOURS_EN_ARRIERE = 31


def _valider_date(d: date_type) -> None:
    aujourd_hui = today_haiti()
    if d > aujourd_hui:
        raise HTTPException(400, {"code": "date_future", "message": "Date invalide : un rapport ne peut pas être daté dans le futur."})
    if d < aujourd_hui - timedelta(days=JOURS_EN_ARRIERE):
        raise HTTPException(400, {"code": "date_ancienne",
                                  "message": f"Date invalide : au plus {JOURS_EN_ARRIERE} jours en arrière."})


def _en_cours(db: Session, u: Utilisateur) -> Optional[RapportPompiste]:
    """Rapport du pompiste dont les compteurs sont envoyés mais pas encore
    terminé (billetterie à faire). Un seul à la fois."""
    return (db.query(RapportPompiste)
            .filter_by(utilisateur_id=u.id, statut="EN_COURS")
            .order_by(RapportPompiste.id.desc()).first())


# Tout rapport non renvoyé au pompiste occupe ses pompes sur le créneau
_ACTIFS = ("EN_COURS", "SOUMIS", "PROBLEME", "CONFIRME", "RESOLU")


@router.get("/api/pompiste/contexte")
def contexte(date: Optional[date_type] = None, periode: Optional[str] = None,
             u: Utilisateur = Depends(_require_pompiste), db: Session = Depends(get_db)):
    ouvert = _en_cours(db, u)
    if ouvert:                       # le rapport ouvert impose son créneau
        d, periode = ouvert.date, ouvert.periode
    else:
        d = date or today_haiti()
        _valider_date(d)
        if periode is None:
            periode = "Matin" if now_haiti().hour < 13 else "Apres-midi"
    if periode not in PERIODES:
        raise HTTPException(400, "Période invalide")
    # Pompes déjà couvertes sur ce créneau (par ce pompiste ou un collègue)
    prises = {}
    for l, rap in (db.query(RapportPompisteLigne, RapportPompiste)
                   .join(RapportPompiste, RapportPompisteLigne.rapport_id == RapportPompiste.id)
                   .filter(RapportPompiste.date == d, RapportPompiste.periode == periode,
                           RapportPompiste.statut.in_(_ACTIFS)).all()):
        prises[l.pompe_id] = {"par": _nom(rap.utilisateur), "statut": rap.statut,
                              "moi": rap.utilisateur_id == u.id}
    produits = []
    for p in db.query(Produit).filter_by(actif=True).order_by(Produit.id).all():
        pompes = []
        for q in p.pompes:
            if not q.actif:
                continue
            # Pompes ajoutées dans « Produits & Pompes » : visibles ici dès
            # leur création (lecture directe, rien à configurer).
            pompes.append({"id": q.id, "nom": q.nom, "deja_rapportee": prises.get(q.id)})
        if pompes:
            produits.append({"id": p.id, "nom": p.nom, "adresse": p.adresse,
                             "prix_gallon": _prix(db, p.pompes[0], d) if p.pompes else float(p.prix_gallon or 0),
                             "pompes": pompes})
    return {
        "pompiste": {"id": u.id, "nom": _nom(u), "username": u.username},
        "date": str(d), "periode": periode, "aujourd_hui": str(today_haiti()),
        "date_min": str(today_haiti() - timedelta(days=JOURS_EN_ARRIERE)),
        "produits": produits, "coupures": list(COUPURES), "taille_liasse": TAILLE_LIASSE,
        "en_cours": _rapport_dict(db, ouvert) if ouvert else None,
        "taux": _taux_dict(taux_du_jour(db, d)),
    }


class LigneIn(BaseModel):
    pompe_id: int
    metter_avant: float
    metter_apres: float
    # Le pompiste a relu le compteur et maintient un index de début différent
    # du dernier index enregistré (l'écart reste visible du superviseur).
    debut_confirme: bool = False


class BilletIn(BaseModel):
    coupure: int
    liasses: int = 0
    billets: int = 0


class CompteursIn(BaseModel):
    date: date_type
    periode: str
    charte_acceptee: bool
    lignes: list[LigneIn]


class DepensePompisteIn(BaseModel):
    description: str
    montant: float
    autorise_par: str


class DeviseIn(BaseModel):
    devise: str = "USD"
    montant: float


class TerminerIn(BaseModel):
    billetterie: list[BilletIn] = []
    monnaie: float = 0
    note: Optional[str] = None
    depenses: list[DepensePompisteIn] = []
    devises: list[DeviseIn] = []


DEVISES = ("USD",)
MAX_DEPENSES = 30


def taux_du_jour(db: Session, d: date_type, devise: str = "USD"):
    """Dernier taux en vigueur à la date du rapport. Rapport daté d'avant le
    premier taux fixé (rapport en retard) : le taux le plus proche après."""
    from models import TauxDevise
    q = db.query(TauxDevise).filter(TauxDevise.devise == devise)
    return (q.filter(TauxDevise.date_effet <= d).order_by(TauxDevise.date_effet.desc(), TauxDevise.id.desc()).first()
            or q.filter(TauxDevise.date_effet > d).order_by(TauxDevise.date_effet.asc(), TauxDevise.id.desc()).first())


def _taux_dict(t) -> Optional[dict]:
    if not t:
        return None
    return {"id": t.id, "devise": t.devise, "taux": float(t.taux), "date_effet": str(t.date_effet),
            "saisi_par_nom": _nom(t.saisi_par), "saisi_le": t.created_at.isoformat() if t.created_at else None}


def _verifier_depenses(depenses: list) -> list:
    if len(depenses) > MAX_DEPENSES:
        raise HTTPException(400, f"Au plus {MAX_DEPENSES} dépenses par rapport.")
    out = []
    for i, x in enumerate(depenses, 1):
        desc = " ".join((x.description or "").split())
        aut = " ".join((x.autorise_par or "").split())
        if len(desc) < 3:
            raise HTTPException(400, {"code": "depense_description", "message": f"Dépense {i} : décrivez la dépense."})
        if not (x.montant > 0) or x.montant > 10_000_000:
            raise HTTPException(400, {"code": "depense_montant", "message": f"Dépense {i} : montant invalide."})
        if len(aut) < 2:
            raise HTTPException(400, {"code": "depense_autorise", "message": f"Dépense {i} : indiquez qui l'a autorisée."})
        out.append({"description": desc[:200], "montant": round(x.montant, 2), "autorise_par": aut[:120]})
    return out


def _verifier_devises(db: Session, devises: list, d: date_type) -> list:
    out = []
    for x in devises:
        dev = (x.devise or "USD").upper()
        if dev not in DEVISES:
            raise HTTPException(400, f"Devise inconnue : {dev}")
        if not x.montant:
            continue
        if x.montant < 0 or x.montant > 1_000_000:
            raise HTTPException(400, {"code": "devise_montant", "message": f"Montant en {dev} invalide."})
        t = taux_du_jour(db, d, dev)
        if not t:
            raise HTTPException(409, {"code": "taux_absent",
                                      "message": f"Le taux du jour {dev} n'est pas configuré : demandez à la direction."})
        out.append({"devise": dev, "montant": round(x.montant, 2), "taux": float(t.taux),
                    "montant_htg": round(x.montant * float(t.taux), 2)})
    return out


def _controler_lignes(db: Session, d: date_type, periode: str, lignes: list,
                      exclure_rapport_id: Optional[int] = None) -> list[dict]:
    """Contrôle des index. Lève 422 avec la liste des pompes en erreur ; chaque
    erreur porte un `code` que l'interface traduit (français / kreyòl) et
    renvoie le pompiste à l'écran de la pompe.

    Le pompiste lit et saisit lui-même les DEUX index : le dernier index connu
    ne lui est jamais montré. S'il diffère de l'index de début saisi, on lui
    demande de relire le compteur et de confirmer (code `debut_different`)."""
    erreurs, vues, sortie = [], set(), []

    def err(pid, code, message, **extra):
        erreurs.append({"pompe_id": pid, "code": code, "message": message, **extra})

    for l in lignes:
        pompe = db.get(Pompe, l.pompe_id)
        if not pompe or not pompe.actif:
            err(l.pompe_id, "inactive", "Pompe introuvable ou inactive")
            continue
        if l.pompe_id in vues:
            err(l.pompe_id, "double", f"{pompe.nom} : saisie en double")
            continue
        vues.add(l.pompe_id)
        if l.metter_avant < 0 or l.metter_apres < 0:
            err(l.pompe_id, "negatif", f"{pompe.nom} : un index ne peut pas être négatif")
            continue
        if l.metter_apres < l.metter_avant - TOLERANCE:
            err(l.pompe_id, "fin_inferieure",
                f"{pompe.nom} : l'index de fin ({l.metter_apres:.3f}) est plus petit "
                f"que l'index de début ({l.metter_avant:.3f}) — un compteur ne recule pas")
            continue
        g = l.metter_apres - l.metter_avant
        if g > MAX_GALLONS_PAR_POMPE_PERIODE:
            err(l.pompe_id, "anormal", f"{pompe.nom} : {g:,.0f} gallons sur une période, "
                "c'est anormal — vérifiez l'index (chiffre en trop ?)", gallons=round(g))
            continue
        prec, _src = _index_precedent(db, l.pompe_id, d, periode, exclure_rapport_id)
        if prec is not None and abs(prec - l.metter_avant) > TOLERANCE and not l.debut_confirme:
            err(l.pompe_id, "debut_different",
                f"{pompe.nom} : l'index de début ne correspond pas au dernier index enregistré. "
                "Relisez le compteur ; si vous êtes sûr, confirmez.")
            continue
        sortie.append({"pompe": pompe, "avant": l.metter_avant, "apres": l.metter_apres})
    if erreurs:
        raise HTTPException(422, {"message": "Index invalide : corrigez la saisie.", "erreurs": erreurs})
    return sortie


def _recalculer_attendu(rap: RapportPompiste) -> None:
    rap.montant_attendu = round(sum((float(l.metter_apres) - float(l.metter_avant)) * float(l.prix_gallon)
                                    for l in rap.lignes), 2)


@router.post("/api/pompiste/compteurs", status_code=201)
def envoyer_compteurs(data: CompteursIn, u: Utilisateur = Depends(_require_pompiste),
                      db: Session = Depends(get_db)):
    """Étape 1 : envoi des compteurs d'un carburant. S'ajoute au rapport en
    cours du créneau (créé au premier envoi). L'argent se compte ensuite,
    une seule fois, au « Terminer »."""
    if not data.charte_acceptee:
        raise HTTPException(400, "La charte de responsabilité doit être acceptée.")
    if data.periode not in PERIODES:
        raise HTTPException(400, "Période invalide")
    if not data.lignes:
        raise HTTPException(400, "Saisissez l'index d'au moins une pompe.")
    rap = _en_cours(db, u)
    if rap and (rap.date != data.date or rap.periode != data.periode):
        raise HTTPException(409, {"code": "terminer_dabord",
                                  "message": f"Terminez d'abord votre rapport du {_per(rap.periode)} "
                                             f"{rap.date.strftime('%d/%m')} (bouton « Terminer »)."})
    if not rap:
        _valider_date(data.date)
    lignes = _controler_lignes(db, data.date, data.periode, data.lignes)

    # Une pompe = un seul rapport actif par créneau
    deja = (db.query(RapportPompisteLigne, RapportPompiste)
            .join(RapportPompiste, RapportPompisteLigne.rapport_id == RapportPompiste.id)
            .filter(RapportPompiste.date == data.date, RapportPompiste.periode == data.periode,
                    RapportPompiste.statut.in_(_ACTIFS),
                    RapportPompisteLigne.pompe_id.in_([x["pompe"].id for x in lignes])).all())
    if deja:
        noms = ", ".join(sorted({f"{l.pompe.nom} ({_nom(r.utilisateur)})" for l, r in deja}))
        raise HTTPException(409, {"code": "deja_rapporte", "pompes": noms,
                                  "message": f"Déjà rapporté sur ce créneau : {noms}."})

    if not rap:
        rap = RapportPompiste(date=data.date, periode=data.periode, utilisateur_id=u.id,
                              statut="EN_COURS", charte_acceptee_at=now_haiti(), billetterie=[])
        db.add(rap)
    for x in lignes:
        rap.lignes.append(RapportPompisteLigne(pompe_id=x["pompe"].id, prix_gallon=_prix(db, x["pompe"], data.date),
                                               metter_avant=x["avant"], metter_apres=x["apres"]))
    db.flush()
    _recalculer_attendu(rap)
    db.commit()
    db.refresh(rap)
    return _rapport_dict(db, rap)


@router.delete("/api/pompiste/compteurs/{pompe_id}")
def retirer_compteur(pompe_id: int, u: Utilisateur = Depends(_require_pompiste),
                     db: Session = Depends(get_db)):
    """Retire une pompe du rapport en cours (erreur de saisie) ; un rapport
    vidé de toutes ses pompes est supprimé."""
    rap = _en_cours(db, u)
    ligne = next((l for l in (rap.lignes if rap else []) if l.pompe_id == pompe_id), None)
    if not ligne:
        raise HTTPException(404, "Cette pompe n'est pas dans votre rapport en cours.")
    rap.lignes.remove(ligne)
    db.flush()
    if not rap.lignes:
        db.delete(rap)
        db.commit()
        return {"en_cours": None}
    _recalculer_attendu(rap)
    db.commit()
    db.refresh(rap)
    return {"en_cours": _rapport_dict(db, rap)}


class ModifLigneIn(BaseModel):
    metter_avant: float
    metter_apres: float
    debut_confirme: bool = False


@router.put("/api/pompiste/compteurs/{pompe_id}")
def modifier_compteur(pompe_id: int, data: ModifLigneIn, u: Utilisateur = Depends(_require_pompiste),
                      db: Session = Depends(get_db)):
    """Corrige l'index d'une pompe du rapport en cours, depuis le résumé
    affiché avant l'envoi final."""
    rap = _en_cours(db, u)
    ligne = next((l for l in (rap.lignes if rap else []) if l.pompe_id == pompe_id), None)
    if not ligne:
        raise HTTPException(404, "Cette pompe n'est pas dans votre rapport en cours.")
    _controler_lignes(db, rap.date, rap.periode,
                      [LigneIn(pompe_id=pompe_id, metter_avant=data.metter_avant, metter_apres=data.metter_apres,
                               debut_confirme=data.debut_confirme)])
    ligne.metter_avant, ligne.metter_apres = data.metter_avant, data.metter_apres
    db.flush()
    _recalculer_attendu(rap)
    db.commit()
    db.refresh(rap)
    return _rapport_dict(db, rap)


def _ecrire_journal(db: Session, rap: RapportPompiste, request: Request, *, remplacer_saisie: bool,
                    motif: Optional[str] = None, seulement: Optional[int] = None) -> list[str]:
    """Écrit les mètres du rapport dans le journal des compteurs (`releves`),
    avec exactement les règles du module Saisie (prix officiel à la date,
    continuité des compteurs, alertes de stock) via `upsert_releve`.

    - au « Terminer » du pompiste : le journal reçoit ses mètres tout de suite
      (statut du rapport : à vérifier) ; une saisie déjà faite dans le module
      Saisie pour la même pompe n'est pas écrasée — le superviseur tranche ;
    - à chaque correction / à la finalisation : mise à jour des mêmes relevés ;
      la finalisation (`remplacer_saisie=True`) remplace aussi une saisie
      faite en parallèle.
    Retourne les avertissements (prix, stock, conflit…)."""
    import main
    employe = employe_du_compte(db, rap.utilisateur)
    avertissements = []
    for l in rap.lignes:
        if seulement is not None and l.pompe_id != seulement:
            continue
        av = float(l.metter_avant_corrige if l.metter_avant_corrige is not None else l.metter_avant)
        ap = float(l.metter_apres_corrige if l.metter_apres_corrige is not None else l.metter_apres)
        existant = db.query(Releve).filter_by(date=rap.date, periode=rap.periode, pompe_id=l.pompe_id).first()
        if existant and existant.rapport_pompiste_id != rap.id and not remplacer_saisie:
            avertissements.append(f"{l.pompe.nom} : déjà saisie dans le module Saisie — "
                                  "le journal garde cette saisie jusqu'à la finalisation du rapport.")
            continue
        identique = existant and abs(float(existant.metter_avant) - av) <= TOLERANCE \
            and abs(float(existant.metter_apres) - ap) <= TOLERANCE
        if not identique:
            try:
                out = main.upsert_releve(main.ReleveIn(
                    date=rap.date, periode=rap.periode, pompe_id=l.pompe_id,
                    prix_gallon=float(l.prix_gallon), metter_avant=av, metter_apres=ap,
                    motif_ecart_compteur=motif or f"Rapport pompiste n°{rap.id} — début lu par le pompiste"),
                    request, db)
                avertissements += out.get("avertissements", [])
            except HTTPException as exc:
                det = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
                avertissements.append(f"{l.pompe.nom} : non écrit dans le journal — {det}")
                continue
        r = db.query(Releve).filter_by(date=rap.date, periode=rap.periode, pompe_id=l.pompe_id).first()
        if r:
            r.rapport_pompiste_id = rap.id
            r.pompiste_id = employe.id
    db.commit()
    return avertissements


def _retirer_du_journal(db: Session, rap: RapportPompiste) -> int:
    n = 0
    for r in db.query(Releve).filter_by(rapport_pompiste_id=rap.id).all():
        db.delete(r)
        n += 1
    return n


def _url_publique(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    hote = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{hote}/"


def _emails_admins(db: Session) -> list[str]:
    out = set()
    for x in db.query(Utilisateur).filter(Utilisateur.actif.is_(True), Utilisateur.email.isnot(None)).all():
        r = x.role_obj
        if x.role in ("admin", "pdg") or (r and (r.est_admin or (r.permissions or {}).get("admin"))):
            out.add(x.email.strip().lower())
    return sorted(out)


def _prevenir_admins(db: Session, rap: RapportPompiste, request: Request) -> None:
    """Email aux administrateurs, envoyé en arrière-plan (la soumission du
    pompiste ne doit jamais attendre ni échouer à cause du serveur mail)."""
    dest = _emails_admins(db)
    if not dest:
        return
    fmt = lambda v: f"{v:,.0f} G".replace(",", " ")
    args = dict(destinataires=dest, pompiste=_nom(rap.utilisateur) or "",
                creneau=f"{_per(rap.periode)} du {rap.date.strftime('%d/%m/%Y')}",
                gallons=f"{sum(float(l.metter_apres) - float(l.metter_avant) for l in rap.lignes):,.3f} gal".replace(",", " "),
                attendu=fmt(float(rap.montant_attendu)), compte=fmt(float(rap.montant_compte)),
                ecart=("+" if float(rap.ecart) > 0 else "") + fmt(float(rap.ecart)),
                lien=_url_publique(request))

    def envoyer():
        try:
            from otp_service import send_rapport_pompiste_email
            send_rapport_pompiste_email(**args)
        except Exception as exc:
            log.warning("Email rapport pompiste non envoyé : %s", exc)
    threading.Thread(target=envoyer, daemon=True).start()


@router.post("/api/pompiste/terminer")
def terminer(data: TerminerIn, request: Request, u: Utilisateur = Depends(_require_pompiste),
             db: Session = Depends(get_db)):
    """Étape 2 : « Terminer » — billetterie de toute la période, rapprochement
    avec l'ensemble des compteurs envoyés, puis envoi au superviseur."""
    rap = _en_cours(db, u)
    if not rap or not rap.lignes:
        raise HTTPException(409, {"code": "rien_a_terminer",
                                  "message": "Aucun compteur envoyé : saisissez d'abord vos pompes."})
    for b in data.billetterie:
        if b.coupure not in COUPURES:
            raise HTTPException(400, f"Coupure inconnue : {b.coupure}")
        if b.liasses < 0 or b.billets < 0:
            raise HTTPException(400, "Les quantités de billets ne peuvent pas être négatives.")
        if b.liasses * TAILLE_LIASSE + b.billets > 100_000:
            raise HTTPException(400, f"Nombre de billets de {b.coupure} G anormal.")
    if data.monnaie < 0 or data.monnaie > 1_000_000:
        raise HTTPException(400, "Montant de monnaie invalide.")
    billetterie = [b.model_dump() if hasattr(b, "model_dump") else b.dict()
                   for b in data.billetterie if (b.liasses or b.billets)]
    _recalculer_attendu(rap)
    depenses = _verifier_depenses(data.depenses)
    devises = _verifier_devises(db, data.devises, rap.date)
    rap.billetterie = billetterie
    rap.monnaie = round(data.monnaie, 2)
    rap.depenses = depenses
    rap.total_depenses = round(sum(x["montant"] for x in depenses), 2)
    rap.devises = devises
    # Argent compté (gourdes + autres devises converties) + dépenses payées = ventes attendues
    rap.montant_compte = round(_total_billetterie(billetterie, data.monnaie) + sum(x["montant_htg"] for x in devises), 2)
    rap.ecart = round(float(rap.montant_compte) + float(rap.total_depenses) - float(rap.montant_attendu), 2)
    rap.note_pompiste = (data.note or "").strip()[:500] or None
    rap.statut = "SOUMIS"
    rap.soumis_at = now_haiti()
    db.commit()
    db.refresh(rap)

    try:
        from notifications_service import creer_notification
        creer_notification(
            db, module="carburant", type_="rapport_pompiste",
            titre=f"Rapport pompiste à vérifier — {_nom(u)}",
            message=f"{_per(rap.periode)} du {rap.date.strftime('%d/%m')} · écart {float(rap.ecart):+,.0f} G",
            lien="pompistes-rapports", dedupe_minutes=None,
        )
        db.commit()
    except Exception:
        db.rollback()
    # Les chiffres n'entrent dans le système (journal officiel, caisse, stock)
    # qu'à la finalisation par la direction ; d'ici là le Journal Meters
    # affiche le rapport « en attente de validation » (lignes_en_attente).
    _prevenir_admins(db, rap, request)
    db.refresh(rap)
    out = _rapport_dict(db, rap)
    out["avertissements"] = []
    return out


@router.get("/api/pompiste/rapports")
def mes_rapports(u: Utilisateur = Depends(_require_pompiste), db: Session = Depends(get_db)):
    rows = (db.query(RapportPompiste)
            .filter(RapportPompiste.utilisateur_id == u.id, RapportPompiste.statut != "EN_COURS")
            .order_by(RapportPompiste.id.desc()).limit(20).all())
    return [_rapport_dict(db, r) for r in rows]


# ═════════════════════════════════════════════════════════════════════
# SUPERVISION
# Vérification élément par élément : chaque mètre, la billetterie, l'écart.
# Puis « Finaliser » avec une note (CONFIRME), ou « Signaler un problème »
# (PROBLEME) puis, une fois réglé, « Résolu et finalisé » (RESOLU).
# La finalisation écrit le journal des compteurs.
# ═════════════════════════════════════════════════════════════════════

_A_TRAITER = ("SOUMIS", "PROBLEME")


def _rapport_a_traiter(db: Session, rid: int) -> RapportPompiste:
    rap = db.get(RapportPompiste, rid)
    if not rap:
        raise HTTPException(404, "Rapport introuvable")
    if rap.statut not in _A_TRAITER:
        raise HTTPException(409, "Ce rapport est déjà finalisé.")
    return rap


@router.get("/api/pompiste-supervision/rapports")
def lister(statut: Optional[str] = None, date_debut: Optional[date_type] = None,
           date_fin: Optional[date_type] = None, utilisateur_id: Optional[int] = None,
           _s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    """`statut` accepte une liste séparée par des virgules (ex. SOUMIS,PROBLEME)."""
    statuts = [x for x in (statut or "").split(",") if x]
    q = db.query(RapportPompiste)
    if statuts:
        q = q.filter(RapportPompiste.statut.in_(statuts))
    else:
        q = q.filter(RapportPompiste.statut != "EN_COURS")
    if date_debut:
        q = q.filter(RapportPompiste.date >= date_debut)
    if date_fin:
        q = q.filter(RapportPompiste.date <= date_fin)
    if utilisateur_id:
        q = q.filter(RapportPompiste.utilisateur_id == utilisateur_id)
    rows = q.all()
    # À traiter : ordre chronologique (la continuité des compteurs impose de
    # finaliser le Matin avant l'Après-midi). Historique : plus récents d'abord.
    a_traiter = bool(statuts) and set(statuts) <= set(_A_TRAITER + ("EN_COURS",))
    rows.sort(key=lambda r: (r.date, _RANG.get(r.periode, 9), r.id), reverse=not a_traiter)
    return [_rapport_dict(db, r, avec_journal=(r.statut in _A_TRAITER)) for r in rows[:200]]


@router.get("/api/pompiste-supervision/resume")
def resume(_s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    a_verifier = db.query(RapportPompiste).filter_by(statut="SOUMIS").count()
    problemes = db.query(RapportPompiste).filter_by(statut="PROBLEME").count()
    non_termines = db.query(RapportPompiste).filter_by(statut="EN_COURS").count()
    return {"a_verifier": a_verifier, "problemes": problemes, "non_termines": non_termines,
            "a_confirmer": a_verifier + problemes}


@router.delete("/api/pompiste-supervision/rapports/{rid}")
def annuler_non_termine(rid: int, s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    """Supprime un rapport jamais terminé (pompiste parti sans « Terminer ») :
    il bloquait ses pompes pour ce créneau. Aucun relevé n'a été écrit."""
    rap = db.get(RapportPompiste, rid)
    if not rap:
        raise HTTPException(404, "Rapport introuvable")
    if rap.statut != "EN_COURS":
        raise HTTPException(409, "Seul un rapport non terminé peut être annulé.")
    log.info("Rapport pompiste #%s (non terminé) annulé par l'utilisateur %s", rid, s.id)
    db.delete(rap)
    db.commit()
    return {"ok": True}


class LigneConfIn(BaseModel):
    metter_avant: float
    metter_apres: float


@router.post("/api/pompiste-supervision/rapports/{rid}/lignes/{pompe_id}/confirmer")
def confirmer_ligne(rid: int, pompe_id: int, data: LigneConfIn, request: Request,
                    _s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    """Confirme un mètre, en le corrigeant si les valeurs envoyées diffèrent
    de celles du pompiste (qui restent conservées)."""
    rap = _rapport_a_traiter(db, rid)
    l = next((x for x in rap.lignes if x.pompe_id == pompe_id), None)
    if not l:
        raise HTTPException(404, "Pompe absente du rapport")
    av, ap = float(data.metter_avant), float(data.metter_apres)
    if av < 0 or ap < av - TOLERANCE:
        raise HTTPException(400, f"{l.pompe.nom} : l'index de fin doit être supérieur ou égal au début.")
    if abs(av - float(l.metter_avant)) > TOLERANCE or abs(ap - float(l.metter_apres)) > TOLERANCE:
        l.metter_avant_corrige, l.metter_apres_corrige = av, ap
    else:
        l.metter_avant_corrige = l.metter_apres_corrige = None
    l.confirmee, l.confirmee_at = True, now_haiti()
    rap.ecart_confirme = False          # l'écart dépend des mètres : à revoir après une correction
    db.commit()
    db.refresh(rap)
    return _rapport_dict(db, rap, avec_journal=True)


@router.post("/api/pompiste-supervision/rapports/{rid}/lignes/{pompe_id}/reprendre")
def reprendre_ligne(rid: int, pompe_id: int, _s: Utilisateur = Depends(_require_superviseur),
                    db: Session = Depends(get_db)):
    rap = _rapport_a_traiter(db, rid)
    l = next((x for x in rap.lignes if x.pompe_id == pompe_id), None)
    if not l:
        raise HTTPException(404, "Pompe absente du rapport")
    l.confirmee, l.confirmee_at = False, None
    db.commit()
    db.refresh(rap)
    return _rapport_dict(db, rap, avec_journal=True)


class ConfirmeIn(BaseModel):
    confirme: bool = True


@router.post("/api/pompiste-supervision/rapports/{rid}/billetterie")
def confirmer_billetterie(rid: int, data: ConfirmeIn, _s: Utilisateur = Depends(_require_superviseur),
                          db: Session = Depends(get_db)):
    rap = _rapport_a_traiter(db, rid)
    rap.billetterie_confirmee = data.confirme
    db.commit()
    db.refresh(rap)
    return _rapport_dict(db, rap, avec_journal=True)


@router.post("/api/pompiste-supervision/rapports/{rid}/ecart")
def confirmer_ecart(rid: int, data: ConfirmeIn, _s: Utilisateur = Depends(_require_superviseur),
                    db: Session = Depends(get_db)):
    rap = _rapport_a_traiter(db, rid)
    if data.confirme and not (all(l.confirmee for l in rap.lignes) and rap.billetterie_confirmee):
        raise HTTPException(409, "Confirmez d'abord chaque mètre et la billetterie.")
    if data.confirme and any((x.get("statut") or "en_attente") == "en_attente" for x in (rap.depenses or [])):
        raise HTTPException(409, "Confirmez ou refusez d'abord chaque dépense du pompiste.")
    rap.ecart_confirme = data.confirme
    db.commit()
    db.refresh(rap)
    return _rapport_dict(db, rap, avec_journal=True)


class CommentaireIn(BaseModel):
    commentaire: str


@router.post("/api/pompiste-supervision/rapports/{rid}/probleme")
def signaler_probleme(rid: int, data: CommentaireIn, s: Utilisateur = Depends(_require_superviseur),
                      db: Session = Depends(get_db)):
    rap = _rapport_a_traiter(db, rid)
    commentaire = (data.commentaire or "").strip()
    if len(commentaire) < _COMMENTAIRE_MIN:
        raise HTTPException(400, "Décrivez le problème (commentaire obligatoire).")
    rap.statut = "PROBLEME"
    rap.probleme_commentaire = commentaire[:1000]
    rap.probleme_at = now_haiti()
    rap.probleme_par_id = s.id
    db.commit()
    db.refresh(rap)
    return _rapport_dict(db, rap, avec_journal=True)


class FinaliserIn(BaseModel):
    note: int
    commentaire: Optional[str] = None
    motif_ecart_compteur: Optional[str] = None


@router.post("/api/pompiste-supervision/rapports/{rid}/finaliser")
def finaliser(rid: int, data: FinaliserIn, request: Request,
              s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    """Finalise le rapport : CONFIRME (correct) ou RESOLU (après un problème).
    Écrit le journal des compteurs avec les valeurs confirmées."""
    import main
    rap = _rapport_a_traiter(db, rid)
    manquants = [l.pompe.nom for l in rap.lignes if not l.confirmee]
    if manquants:
        raise HTTPException(409, "Mètres pas encore confirmés : " + ", ".join(manquants) + ".")
    if not rap.billetterie_confirmee:
        raise HTTPException(409, "Confirmez d'abord la billetterie.")
    if not rap.ecart_confirme:
        raise HTTPException(409, "Confirmez d'abord l'écart de caisse.")
    if data.note < 0 or data.note > 10:
        raise HTTPException(400, "La note du rapport va de 0 à 10.")
    commentaire = (data.commentaire or "").strip()
    motif = (data.motif_ecart_compteur or "").strip()
    corrige = any(l.metter_avant_corrige is not None for l in rap.lignes)
    resolution = rap.statut == "PROBLEME"
    if resolution and len(commentaire) < _COMMENTAIRE_MIN:
        raise HTTPException(400, "Expliquez comment le problème a été résolu.")
    if corrige and len(commentaire) < _COMMENTAIRE_MIN:
        raise HTTPException(400, "Expliquez la correction des mètres dans le commentaire.")

    # 1. Contrôles AVANT toute écriture (le journal est écrit pompe par pompe :
    #    on refuse tout plutôt que d'en écrire la moitié).
    finales = []
    for l in rap.lignes:
        av = float(l.metter_avant_corrige if l.metter_avant_corrige is not None else l.metter_avant)
        ap = float(l.metter_apres_corrige if l.metter_apres_corrige is not None else l.metter_apres)
        prec = main._releve_precedent(db, l.pompe_id, rap.date, rap.periode)
        ecart = round(av - float(prec.metter_apres), 4) if prec else 0.0
        existant = db.query(Releve).filter_by(date=rap.date, periode=rap.periode, pompe_id=l.pompe_id).first()
        motif_existant = existant.motif_ecart_compteur if existant else None
        if abs(ecart) > main.TOLERANCE_ECART_COMPTEUR and len(motif) < main.MOTIF_ECART_MIN_CARACTERES \
                and not (motif_existant and existant and abs(float(existant.metter_avant) - av) <= TOLERANCE):
            raise HTTPException(409, {
                "message": (f"{l.pompe.nom} : l'index de début ({av:.3f}) ne suit pas le journal "
                            f"({float(prec.metter_apres):.3f}, {_per(prec.periode)} du {prec.date.strftime('%d/%m/%Y')}). "
                            "Corrigez le mètre, finalisez d'abord le rapport précédent, "
                            "ou indiquez le motif de l'écart."),
                "motif_requis": True, "pompe_id": l.pompe_id})
        identique = existant and abs(float(existant.metter_avant) - av) <= TOLERANCE \
            and abs(float(existant.metter_apres) - ap) <= TOLERANCE
        if existant and not identique and existant.nb_modifications >= main.MAX_MODIFICATIONS_PAR_RELEVE:
            raise HTTPException(403, f"{l.pompe.nom} : le relevé du journal a atteint sa limite de modifications.")
        finales.append((l, av, ap, identique))

    # 2. Journal des compteurs — mêmes règles que le module Saisie
    avertissements = _ecrire_journal(db, rap, request, remplacer_saisie=True, motif=motif or None)
    # 3. Dépenses confirmées : déjà dans les Dépenses depuis leur confirmation
    #    (filet de sécurité si l'une manquait)
    _ecrire_depenses(db, rap, request, s)

    rap.statut = "RESOLU" if resolution else "CONFIRME"
    rap.note_superviseur = data.note
    rap.traite_par_id = s.id
    rap.traite_at = now_haiti()
    rap.commentaire_superviseur = commentaire[:1000] or None
    db.commit()
    db.refresh(rap)
    out = _rapport_dict(db, rap)
    out["avertissements"] = avertissements
    return out


CATEGORIE_DEPENSE_POMPISTE = "Dépense pompiste"


def _ecrire_depenses(db: Session, rap: RapportPompiste, request: Request, s: Utilisateur) -> int:
    """Une Depense par dépense déclarée, référence « RP<id>-<n> » : rejouer la
    finalisation ne crée jamais de doublon."""
    from models import Depense, CategorieDepense
    import listes_reference as lref
    if not rap.depenses:
        return 0
    n = 0
    for i, x in enumerate(rap.depenses):
        if x.get("statut") == "confirmee" and _ecrire_une_depense(db, rap, i, request, s):
            n += 1
    return n


def _ref_depense(rap: RapportPompiste, i: int) -> str:
    return f"RP{rap.id}-{i + 1}"


def _ecrire_une_depense(db: Session, rap: RapportPompiste, i: int, request: Request, s: Utilisateur) -> bool:
    from models import Depense, CategorieDepense
    import listes_reference as lref
    ref = _ref_depense(rap, i)
    if db.query(Depense).filter_by(reference=ref).first():
        return False
    try:
        categorie = lref.resoudre(db, CategorieDepense, None, request=request, defaut=CATEGORIE_DEPENSE_POMPISTE)
    except HTTPException:
        categorie = CATEGORIE_DEPENSE_POMPISTE
    x = rap.depenses[i]
    db.add(Depense(categorie=categorie or CATEGORIE_DEPENSE_POMPISTE, description=x["description"][:300],
                   montant=x["montant"], date_depense=rap.date, beneficiaire=None, reference=ref,
                   notes=(f"Payée par le pompiste {_nom(rap.utilisateur)} sur ses ventes ({_per(rap.periode)}), "
                          f"autorisée par {x['autorise_par']}. Rapport pompiste n°{rap.id}.")[:500],
                   saisi_par_id=s.id, rapport_pompiste_id=rap.id))
    return True


def _retirer_une_depense(db: Session, rap: RapportPompiste, i: int) -> None:
    from models import Depense
    db.query(Depense).filter_by(reference=_ref_depense(rap, i), rapport_pompiste_id=rap.id).delete()


class DepenseDecisionIn(BaseModel):
    motif: Optional[str] = None


def _decider_depense(db: Session, rid: int, i: int) -> tuple:
    rap = _rapport_a_traiter(db, rid)
    deps = [dict(x) for x in (rap.depenses or [])]
    if not (0 <= i < len(deps)):
        raise HTTPException(404, "Dépense introuvable dans ce rapport.")
    return rap, deps


@router.post("/api/pompiste-supervision/rapports/{rid}/depenses/{i}/confirmer")
def confirmer_depense(rid: int, i: int, request: Request, s: Utilisateur = Depends(_require_superviseur),
                      db: Session = Depends(get_db)):
    """Dépense du pompiste acceptée : elle entre tout de suite dans les
    Dépenses de la station (sans sortir de la caisse principale)."""
    rap, deps = _decider_depense(db, rid, i)
    deps[i].update(statut="confirmee", motif=None, par=_nom(s), le=now_haiti().isoformat())
    rap.depenses = deps
    rap.ecart_confirme = False
    _ecrire_une_depense(db, rap, i, request, s)
    db.commit()
    db.refresh(rap)
    return _rapport_dict(db, rap, avec_journal=True)


@router.post("/api/pompiste-supervision/rapports/{rid}/depenses/{i}/refuser")
def refuser_depense(rid: int, i: int, data: DepenseDecisionIn, s: Utilisateur = Depends(_require_superviseur),
                    db: Session = Depends(get_db)):
    """Dépense non acceptée : elle ne réduit plus l'argent à remettre (le
    pompiste doit cet argent) et n'entre pas dans les Dépenses."""
    motif = " ".join((data.motif or "").split())
    if len(motif) < 5:
        raise HTTPException(400, "Indiquez pourquoi la dépense est refusée (5 caractères au moins).")
    rap, deps = _decider_depense(db, rid, i)
    deps[i].update(statut="refusee", motif=motif[:300], par=_nom(s), le=now_haiti().isoformat())
    rap.depenses = deps
    rap.ecart_confirme = False
    _retirer_une_depense(db, rap, i)
    db.commit()
    db.refresh(rap)
    return _rapport_dict(db, rap, avec_journal=True)


@router.post("/api/pompiste-supervision/rapports/{rid}/depenses/{i}/reprendre")
def reprendre_depense(rid: int, i: int, _s: Utilisateur = Depends(_require_superviseur),
                      db: Session = Depends(get_db)):
    rap, deps = _decider_depense(db, rid, i)
    deps[i].update(statut="en_attente", motif=None, par=None, le=None)
    rap.depenses = deps
    rap.ecart_confirme = False
    _retirer_une_depense(db, rap, i)
    db.commit()
    db.refresh(rap)
    return _rapport_dict(db, rap, avec_journal=True)


# ── Taux du jour des devises ────────────────────────────────────────────

class TauxIn(BaseModel):
    taux: float
    devise: str = "USD"
    date_effet: Optional[date_type] = None


@router.get("/api/pompiste-supervision/taux")
def lister_taux(devise: str = "USD", _s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    from models import TauxDevise
    hist = (db.query(TauxDevise).filter_by(devise=devise.upper())
            .order_by(TauxDevise.date_effet.desc(), TauxDevise.id.desc()).limit(60).all())
    return {"actuel": _taux_dict(taux_du_jour(db, today_haiti(), devise.upper())),
            "historique": [_taux_dict(t) for t in hist]}


@router.post("/api/pompiste-supervision/taux", status_code=201)
def fixer_taux(data: TauxIn, s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    from models import TauxDevise
    dev = (data.devise or "USD").upper()
    if dev not in DEVISES:
        raise HTTPException(400, f"Devise inconnue : {dev}")
    if not (0 < data.taux < 100_000):
        raise HTTPException(400, "Taux invalide.")
    d = data.date_effet or today_haiti()
    if d > today_haiti():
        raise HTTPException(400, "Le taux ne peut pas être daté dans le futur.")
    t = TauxDevise(devise=dev, taux=round(data.taux, 4), date_effet=d, saisi_par_id=s.id)
    db.add(t)
    db.commit()
    db.refresh(t)
    return _taux_dict(t)


@router.post("/api/pompiste-supervision/rapports/{rid}/rejeter")
def rejeter(rid: int, data: CommentaireIn, s: Utilisateur = Depends(_require_superviseur),
            db: Session = Depends(get_db)):
    """Renvoie le rapport au pompiste, qui en refait un nouveau."""
    rap = _rapport_a_traiter(db, rid)
    commentaire = (data.commentaire or "").strip()
    if len(commentaire) < _COMMENTAIRE_MIN:
        raise HTTPException(400, "Indiquez au pompiste ce qui ne va pas (commentaire obligatoire).")
    _retirer_du_journal(db, rap)       # il refera un rapport : pas de doublon dans le journal
    for i in range(len(rap.depenses or [])):
        _retirer_une_depense(db, rap, i)
    rap.statut = "REJETE"
    rap.traite_par_id = s.id
    rap.traite_at = now_haiti()
    rap.commentaire_superviseur = commentaire[:1000]
    db.commit()
    db.refresh(rap)
    return _rapport_dict(db, rap)


# ── Comptes pompistes ────────────────────────────────────────────────

def _compte_dict(db: Session, u: Utilisateur) -> dict:
    emp = db.query(Employe).filter_by(utilisateur_id=u.id).first()
    dernier = (db.query(RapportPompiste).filter_by(utilisateur_id=u.id)
               .order_by(RapportPompiste.id.desc()).first())
    return {"id": u.id, "username": u.username, "nom_complet": u.nom_complet, "actif": u.actif,
            "employe_id": emp.id if emp else None,
            "employe_nom": f"{emp.prenom} {emp.nom}" if emp else None,
            "dernier_rapport": str(dernier.date) if dernier else None}


@router.get("/api/pompiste-supervision/comptes")
def lister_comptes(_s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    role = assurer_role_pompiste(db)
    rows = db.query(Utilisateur).filter_by(role_id=role.id).order_by(Utilisateur.nom_complet).all()
    employes = (db.query(Employe).filter(Employe.actif.is_(True))
                .order_by(Employe.prenom, Employe.nom).all())
    return {
        "comptes": [_compte_dict(db, u) for u in rows],
        "employes": [{"id": e.id, "nom": f"{e.prenom} {e.nom}", "poste": e.poste,
                      "lie": e.utilisateur_id is not None} for e in employes],
    }


class CompteIn(BaseModel):
    prenom: str
    nom: str
    code_acces: Optional[str] = None     # vide = généré
    employe_id: Optional[int] = None


def _slug(txt: str, n: int = 12) -> str:
    t = unicodedata.normalize("NFKD", txt).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", t)[:n]


def _identifiant_unique(db: Session, prenom: str, nom: str) -> str:
    """Code de connexion : prénom + nom + 4 chiffres aléatoires (ex. jeanpierre4821)."""
    base = (_slug(prenom) + _slug(nom)) or "pompiste"
    for _ in range(50):
        code = f"{base}{secrets.randbelow(10000):04d}"
        if not db.query(Utilisateur).filter(Utilisateur.username.ilike(code)).first():
            return code
    raise HTTPException(500, "Impossible de générer un code unique, réessayez.")


def _nouveau_pin() -> str:
    return "".join(secrets.choice("0123456789") for _ in range(9))


@router.post("/api/pompiste-supervision/comptes", status_code=201)
def creer_compte(data: CompteIn, _s: Utilisateur = Depends(_require_superviseur),
                 db: Session = Depends(get_db)):
    from admin_routes import _CODE_RE
    prenom, nom_famille = data.prenom.strip(), data.nom.strip()
    if not prenom or not nom_famille:
        raise HTTPException(400, "Le prénom et le nom sont requis.")
    nom = f"{prenom} {nom_famille}"
    pin = (data.code_acces or "").strip() or _nouveau_pin()
    if not _CODE_RE.match(pin):
        raise HTTPException(400, "Le code PIN doit contenir exactement 9 chiffres.")
    username = _identifiant_unique(db, prenom, nom_famille)
    emp = None
    if data.employe_id:
        emp = db.get(Employe, data.employe_id)
        if not emp:
            raise HTTPException(404, "Employé introuvable")
        if emp.utilisateur_id:
            raise HTTPException(409, "Cet employé a déjà un compte.")
    role = assurer_role_pompiste(db)
    u = Utilisateur(
        username=username, nom_complet=nom,
        # Pas de mot de passe utilisable : la connexion pompiste se fait par
        # identifiant + PIN, et ce compte ne passe jamais par /api/login.
        password_hash=hash_password(secrets.token_urlsafe(24)),
        code_acces_hash=hash_code_acces(pin),
        role="operateur", role_id=role.id, poste=role.nom, actif=True,
    )
    db.add(u)
    db.flush()
    if emp:
        emp.utilisateur_id = u.id
    else:
        employe_du_compte(db, u, prenom, nom_famille)   # apparaît dans les filtres « Pompiste »
    db.commit()
    db.refresh(u)
    out = _compte_dict(db, u)
    out["code_acces"] = pin      # affiché une seule fois
    return out


class ComptePatch(BaseModel):
    actif: Optional[bool] = None
    nouveau_pin: bool = False


@router.patch("/api/pompiste-supervision/comptes/{uid}")
def modifier_compte(uid: int, data: ComptePatch, _s: Utilisateur = Depends(_require_superviseur),
                    db: Session = Depends(get_db)):
    u = db.get(Utilisateur, uid)
    if not u or not est_role_pompiste(u.role_obj):
        raise HTTPException(404, "Compte pompiste introuvable")
    pin = None
    if data.actif is not None:
        u.actif = data.actif
        emp = db.query(Employe).filter_by(utilisateur_id=u.id).first()
        if emp:
            emp.actif = data.actif
        if not data.actif:
            from models import SessionToken
            db.query(SessionToken).filter_by(user_id=u.id).delete()
    if data.nouveau_pin:
        pin = _nouveau_pin()
        u.code_acces_hash = hash_code_acces(pin)
    db.commit()
    out = _compte_dict(db, u)
    if pin:
        out["code_acces"] = pin
    return out
