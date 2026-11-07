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


NOTE_FICHE_POMPISTE = ("Fiche créée à partir du compte pompiste : seul le nom est repris. "
                       "Complétez le salaire, le contrat et les coordonnées.")


def _date_du_compte(u: Utilisateur) -> date_type:
    c = getattr(u, "created_at", None)
    if c is None:
        return today_haiti()
    try:
        from tz_utils import HAITI_TZ
        return (c.astimezone(HAITI_TZ) if c.tzinfo else c).date()
    except Exception:
        return c.date()


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
    # Seul le nom vient du compte (qui n'est jamais modifié : le pompiste garde
    # son code et son PIN). Date d'embauche = création du compte ; salaire et
    # contrat sont à compléter dans Employés.
    emp = Employe(prenom=prenom.strip()[:100], nom=(nom or "").strip()[:100] or prenom.strip()[:100],
                  poste="Pompiste", date_embauche=_date_du_compte(u), salaire_base=0, type_contrat="CDI",
                  actif=u.actif, utilisateur_id=u.id, notes=NOTE_FICHE_POMPISTE)
    db.add(emp)
    db.flush()
    return emp


def rattacher_employes_pompistes(db: Session) -> dict:
    """Chaque compte pompiste a sa fiche employé (poste « Pompiste ») et
    chaque relevé venu de l'espace pompiste est attribué à son pompiste.
    Lancé au démarrage dans chaque station et depuis « Comptes pompistes ».
    Ne touche jamais un relevé déjà attribué."""
    role = assurer_role_pompiste(db)
    comptes = db.query(Utilisateur).filter_by(role_id=role.id).all()
    avant = {e.utilisateur_id for e in db.query(Employe).filter(Employe.utilisateur_id.isnot(None)).all()}
    for u in comptes:
        employe_du_compte(db, u)
    crees = sum(1 for u in comptes if u.id not in avant)
    # Fiches créées automatiquement avant ce réglage : date d'embauche = création
    # du compte (et non le jour de la création de la fiche). Rien d'autre ne change.
    for e in db.query(Employe).filter(Employe.notes.like("Fiche créée avec le compte pompiste%")).all():
        u = db.get(Utilisateur, e.utilisateur_id) if e.utilisateur_id else None
        if u:
            e.date_embauche = _date_du_compte(u)
            e.notes = NOTE_FICHE_POMPISTE
    attribues = 0
    for r in db.query(Releve).filter(Releve.rapport_pompiste_id.isnot(None), Releve.pompiste_id.is_(None)).all():
        rap = db.get(RapportPompiste, r.rapport_pompiste_id)
        emp = db.query(Employe).filter_by(utilisateur_id=rap.utilisateur_id).first() if rap else None
        if emp:
            r.pompiste_id = emp.id
            attribues += 1
    db.commit()
    return {"comptes": len(comptes), "fiches_creees": crees, "releves_attribues": attribues}


def est_employe_pompiste(db: Session, emp: Employe) -> bool:
    """Poste « Pompiste » ou fiche liée à un compte pompiste."""
    return (emp.poste or "").strip().lower() == "pompiste" or emp.id in ids_employes_pompistes(db)


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
                .filter(RapportPompiste.statut.in_(("CONFIRME", "RESOLU")), RapportPompiste.supprime_at.is_(None))
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
    """Démarrage : un rapport pas encore finalisé (soumis, en problème) n'a
    rien au journal officiel ni dans les Dépenses : tout y entre à la
    finalisation. Les relevés écrits avant (au « Terminer », ou à la
    confirmation d'un mètre par l'ancien fonctionnement) sont retirés s'ils
    sont identiques au rapport ; s'ils ont été modifiés ensuite à la main
    (module Saisie), ils deviennent une saisie ordinaire et sont gardés."""
    from models import Depense
    n = 0
    for rap in db.query(RapportPompiste).filter(RapportPompiste.statut.in_(("EN_COURS", "SOUMIS", "PROBLEME")),
                                                RapportPompiste.supprime_at.is_(None)).all():
        lignes = {l.pompe_id: l for l in rap.lignes}
        for r in db.query(Releve).filter_by(rapport_pompiste_id=rap.id).all():
            l = lignes.get(r.pompe_id)
            av, ap = _valeurs_finales(l) if l else (None, None)
            if l is not None and abs(float(r.metter_avant) - av) <= TOLERANCE and abs(float(r.metter_apres) - ap) <= TOLERANCE:
                db.delete(r)
            else:
                r.rapport_pompiste_id = None
            n += 1
        n += db.query(Depense).filter_by(rapport_pompiste_id=rap.id).delete()
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
                .filter(RapportPompiste.statut.in_(("SOUMIS", "PROBLEME")), RapportPompiste.supprime_at.is_(None),
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
    import main  # import tardif : main importe ce module
    cible = (d, _RANG[periode])
    meilleur, cle_m, src = None, None, None
    p = db.get(Pompe, pompe_id)
    dd = main.depart_applicable(p, d)
    qr = db.query(Releve).filter(Releve.pompe_id == pompe_id, Releve.date <= d)
    if dd:
        qr = qr.filter(Releve.date > dd)
    for r in (qr.order_by(Releve.date.desc()).limit(6).all()):
        cle = (r.date, _RANG.get(r.periode, 9))
        if cle < cible and (cle_m is None or cle > cle_m):
            meilleur, cle_m, src = float(r.metter_apres), cle, f"journal — {_per(r.periode)} du {r.date.strftime('%d/%m')}"
    q = (db.query(RapportPompisteLigne, RapportPompiste)
         .join(RapportPompiste, RapportPompisteLigne.rapport_id == RapportPompiste.id)
         .filter(RapportPompisteLigne.pompe_id == pompe_id,
                 RapportPompiste.statut.in_(("EN_COURS", "SOUMIS", "PROBLEME")), RapportPompiste.supprime_at.is_(None),
                 RapportPompiste.date <= d, RapportPompiste.date >= d - timedelta(days=3)))
    if exclure_rapport_id:
        q = q.filter(RapportPompiste.id != exclure_rapport_id)
    if dd:
        q = q.filter(RapportPompiste.date > dd)
    for ligne, rap in q.all():
        cle = (rap.date, _RANG.get(rap.periode, 9))
        if cle < cible and (cle_m is None or cle > cle_m):
            meilleur, cle_m = float(ligne.metter_apres), cle
            src = f"rapport pompiste — {_per(rap.periode)} du {rap.date.strftime('%d/%m')}"
    if meilleur is None:
        dep = main.base_depart(p, d)
        if dep is not None:
            meilleur = dep
            src = (f"mètre de départ du {p.date_depart.strftime('%d/%m')}" if p.date_depart
                   else "mètre de départ (enregistrement de la station)")
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


def _totaux_feuille(r: RapportPompiste) -> dict:
    """C = bons et chèques, D = crédits, F = crédit en caisse (fonds reçu de
    la caisse, à rendre) : C + D justifient des ventes, F est retiré du cash."""
    f = r.feuille or {}
    bons = round(sum(float(x["montant"]) for x in f.get("bons") or []), 2)
    credits = round(sum(float(x["montant"]) for x in f.get("credits") or []), 2)
    fonds = round(float((f.get("fonds_caisse") or {}).get("montant") or 0), 2)
    return {"bons": bons, "credits": credits, "fonds_caisse": fonds, "net": round(bons + credits - fonds, 2)}


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
            if prec is not None and abs(prec - av) > TOLERANCE:
                d["ecart_compteur"] = round(av - prec, 4)
                d["ecart_compteur_montant"] = round((av - prec) * float(l.prix_gallon), 2)
        d["pompe_saisie"] = ({"pompe_id": l.pompe_saisie_id, "pompe_nom": l.pompe_saisie.nom}
                             if l.pompe_saisie_id and l.pompe_saisie else None)
        lignes.append(d)
    placement = None
    if avec_journal and r.statut in _A_TRAITER and any("ecart_compteur" in x for x in lignes):
        import placement_metres
        try:
            placement = placement_metres.plan_rapport(db, r)
        except Exception:
            log.exception("Placement des mètres du rapport #%s", r.id)
        for pr in (placement or {}).get("propositions", []):
            x = next((y for y in lignes if y["pompe_id"] == pr["pompe_id"]), None)
            if x is not None:
                x["pompe_suggeree"] = {"pompe_id": pr["vers_pompe_id"], "pompe_nom": pr["vers"],
                                       "confiance": pr["confiance"], "raisons": pr["raisons"],
                                       "autre_carburant": pr["autre_carburant"], "echange": pr["echange"]}
    return {
        "placement": placement,
        "id": r.id, "date": str(r.date), "periode": r.periode, "statut": r.statut,
        "certifie": r.statut in ("CONFIRME", "RESOLU"),
        "pompiste_id": r.utilisateur_id, "pompiste_nom": _nom(r.utilisateur),
        "charte_acceptee_at": r.charte_acceptee_at.isoformat() if r.charte_acceptee_at else None,
        "billetterie": r.billetterie or [], "monnaie": float(r.monnaie or 0),
        "depenses": [dict(x, statut=x.get("statut") or "en_attente", index=i) for i, x in enumerate(r.depenses or [])],
        "total_depenses": float(r.total_depenses or 0),
        "total_depenses_retenues": _depenses_retenues(r),
        "depenses_a_decider": sum(1 for x in (r.depenses or []) if (x.get("statut") or "en_attente") == "en_attente"),
        "devises": r.devises or [],
        "feuille": r.feuille or {}, "totaux_feuille": _totaux_feuille(r),
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
        # Écart = argent compté + dépenses payées (sauf celles refusées par la direction)
        #         + bons et crédits − crédit en caisse − ventes
        "ecart_final": round(float(r.montant_compte) + _depenses_retenues(r) + _totaux_feuille(r)["net"]
                             - sum(x["montant"] for x in lignes), 2),
        "billetterie_confirmee": bool(r.billetterie_confirmee),
        "ecart_confirme": bool(r.ecart_confirme),
        "note_superviseur": r.note_superviseur,
        "probleme_commentaire": r.probleme_commentaire,
        "probleme_par_nom": _nom(r.probleme_par),
        "probleme_at": r.probleme_at.isoformat() if r.probleme_at else None,
        "supprime": (dict(r.suppression or {}, le=r.supprime_at.isoformat()) if r.supprime_at else None),
    }


# ═════════════════════════════════════════════════════════════════════
# ESPACE POMPISTE
# ═════════════════════════════════════════════════════════════════════

# Le pompiste choisit la date de son rapport dans un calendrier : jamais
# dans le futur, et au plus JOURS_EN_ARRIERE jours en arrière.
JOURS_EN_ARRIERE = 75        # saisie des rapports des deux derniers mois


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
            .filter_by(utilisateur_id=u.id, statut="EN_COURS").filter(RapportPompiste.supprime_at.is_(None))
            .order_by(RapportPompiste.id.desc()).first())


# Tout rapport non renvoyé au pompiste occupe ses pompes sur le créneau
_ACTIFS = ("EN_COURS", "SOUMIS", "PROBLEME", "CONFIRME", "RESOLU")


_EN_ATTENTE = ("EN_COURS", "SOUMIS", "PROBLEME")


def mettres_en_attente(db: Session, d: date_type, periode: Optional[str] = None) -> list[dict]:
    """Pompes d'un rapport pompiste pas encore finalisé dont le mètre n'est
    pas (encore) au journal : leur place est prise, la Saisie ne doit pas
    les ressaisir en parallèle."""
    out = []
    q = (db.query(RapportPompisteLigne, RapportPompiste)
         .join(RapportPompiste, RapportPompisteLigne.rapport_id == RapportPompiste.id)
         .filter(RapportPompiste.date == d, RapportPompiste.statut.in_(_EN_ATTENTE), RapportPompiste.supprime_at.is_(None)))
    if periode:
        q = q.filter(RapportPompiste.periode == periode)
    for l, rap in q.all():
        if db.query(Releve).filter_by(date=rap.date, periode=rap.periode, pompe_id=l.pompe_id).first():
            continue
        out.append({"pompe_id": l.pompe_id, "periode": rap.periode, "rapport_id": rap.id, "statut": rap.statut,
                    "pompiste": _nom(rap.utilisateur)})
    return out


@router.get("/api/releves/en-attente")
def releves_en_attente(date: date_type, periode: Optional[str] = None,
                       _s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    return mettres_en_attente(db, date, periode)


def _creneaux_pris(db: Session, d: date_type) -> dict:
    """{pompe_id: {créneau: info}} des pompes déjà couvertes ce jour-là
    (rapport pompiste actif ou relevé du journal)."""
    pris: dict = {}
    for l, rap in (db.query(RapportPompisteLigne, RapportPompiste)
                   .join(RapportPompiste, RapportPompisteLigne.rapport_id == RapportPompiste.id)
                   .filter(RapportPompiste.date == d, RapportPompiste.statut.in_(_ACTIFS), RapportPompiste.supprime_at.is_(None)).all()):
        pris.setdefault(l.pompe_id, {})[rap.periode] = {"par": _nom(rap.utilisateur), "statut": rap.statut,
                                                         "rapport_utilisateur_id": rap.utilisateur_id}
    for r in db.query(Releve).filter(Releve.date == d).all():
        pris.setdefault(r.pompe_id, {}).setdefault(r.periode, {"par": "bureau", "statut": "CONFIRME",
                                                                "rapport_utilisateur_id": None})
    return pris


def creneau_automatique(db: Session, d: date_type, pompe_ids: list) -> str:
    """Le pompiste ne choisit plus matin / après-midi : premier rapport du
    jour de ces pompes = « Matin », le suivant = « Apres-midi » (deux
    rapports par pompe et par jour au plus)."""
    pris = _creneaux_pris(db, d)
    for per in PERIODES:
        if not any(per in pris.get(pid, {}) for pid in pompe_ids):
            return per
    occupees = [pid for pid in pompe_ids if len(pris.get(pid, {})) >= len(PERIODES)]
    noms = ", ".join(sorted(p.nom for p in db.query(Pompe).filter(Pompe.id.in_(occupees or pompe_ids)).all()))
    raise HTTPException(409, {"code": "deja_rapporte", "pompes": noms,
                              "message": f"Ces pompes ont déjà leurs deux rapports pour cette journée : {noms}."})


@router.get("/api/pompiste/contexte")
def contexte(date: Optional[date_type] = None, periode: Optional[str] = None,
             u: Utilisateur = Depends(_require_pompiste), db: Session = Depends(get_db)):
    ouvert = _en_cours(db, u)
    if ouvert:                       # le rapport ouvert impose son créneau
        d, periode = ouvert.date, ouvert.periode
    else:
        d = date or today_haiti()
        _valider_date(d)
    if periode is not None and periode not in PERIODES:
        raise HTTPException(400, "Période invalide")
    # Pompes déjà couvertes (par ce pompiste ou un collègue) : sur le créneau
    # du rapport ouvert, sinon quand la pompe a déjà ses deux rapports du jour
    prises = {}
    for pid, par_creneau in _creneaux_pris(db, d).items():
        if periode is not None:
            info = par_creneau.get(periode)
        else:
            info = par_creneau.get("Apres-midi") if len(par_creneau) >= len(PERIODES) else None
        if info:
            prises[pid] = {"par": info["par"], "statut": info["statut"],
                           "moi": info["rapport_utilisateur_id"] == u.id}
    produits = []
    for p in db.query(Produit).filter_by(actif=True).order_by(Produit.id).all():
        pompes = []
        for q in p.pompes:
            if not q.actif or not de_ma_station(u, q):
                continue
            # Pompes ajoutées dans « Produits & Pompes » : visibles ici dès
            # leur création (lecture directe, rien à configurer).
            pompes.append({"id": q.id, "nom": q.nom, "emplacement": q.emplacement,
                           "deja_rapportee": prises.get(q.id)})
        if pompes:
            produits.append({"id": p.id, "nom": p.nom,
                             "prix_gallon": _prix(db, p.pompes[0], d) if p.pompes else float(p.prix_gallon or 0),
                             "pompes": pompes})
    return {
        "pompiste": {"id": u.id, "nom": _nom(u), "username": u.username, "station": u.station_adresse,
                     "station_nom": _nom_station_courante()},
        "station": _fiche_station(),
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
    periode: Optional[str] = None        # absent : créneau attribué automatiquement
    charte_acceptee: bool
    lignes: list[LigneIn]


class DepensePompisteIn(BaseModel):
    description: str
    montant: float
    autorise_par: str


class DeviseIn(BaseModel):
    devise: str = "USD"
    montant: float


class BonIn(BaseModel):
    reference: str = ""
    montant: float


class CreditIn(BaseModel):
    nom: str
    montant: float


class FondsCaisseIn(BaseModel):
    nom: str = ""
    montant: float = 0


class TerminerIn(BaseModel):
    billetterie: list[BilletIn] = []
    monnaie: float = 0
    note: Optional[str] = None
    depenses: list[DepensePompisteIn] = []
    devises: list[DeviseIn] = []
    # Feuille : heures de travail, C = bons et chèques, D = crédits, F = crédit en caisse
    heures: Optional[str] = None
    bons: list[BonIn] = []
    credits: list[CreditIn] = []
    fonds_caisse: Optional[FondsCaisseIn] = None


MAX_LIGNES_FEUILLE = 30
MAX_MONTANT_LIGNE = 10_000_000


def _verifier_feuille(data: "TerminerIn") -> dict:
    """Contrôle des parties C, D et F de la feuille ; retourne ce qui est gardé."""
    texte = lambda v, n: " ".join((v or "").split())[:n]
    def montant(v, quoi):
        if v is None or v != v or v <= 0 or v > MAX_MONTANT_LIGNE:
            raise HTTPException(400, {"code": "feuille_montant", "message": f"{quoi} : montant invalide."})
        return round(float(v), 2)
    if len(data.bons) > MAX_LIGNES_FEUILLE or len(data.credits) > MAX_LIGNES_FEUILLE:
        raise HTTPException(400, f"Au plus {MAX_LIGNES_FEUILLE} lignes par partie de la feuille.")
    bons = [{"reference": texte(b.reference, 120), "montant": montant(b.montant, f"Bon / chèque {i}")}
            for i, b in enumerate(data.bons, 1)]
    credits = []
    for i, c in enumerate(data.credits, 1):
        nom = texte(c.nom, 120)
        if len(nom) < 2:
            raise HTTPException(400, {"code": "credit_nom", "message": f"Crédit {i} : indiquez le nom du client."})
        credits.append({"nom": nom, "montant": montant(c.montant, f"Crédit {i}")})
    fonds = None
    if data.fonds_caisse and (data.fonds_caisse.montant or 0) > 0:
        nom = texte(data.fonds_caisse.nom, 120)
        if len(nom) < 2:
            raise HTTPException(400, {"code": "fonds_nom",
                                      "message": "Crédit en caisse : indiquez qui a remis l'argent."})
        fonds = {"nom": nom, "montant": montant(data.fonds_caisse.montant, "Crédit en caisse")}
    out = {"bons": bons, "credits": credits, "fonds_caisse": fonds}
    heures = texte(data.heures, 40)
    if heures:
        out["heures"] = heures
    return out


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


def _nom_principale() -> str:
    from espaces_routes import nom_principale
    return nom_principale()


def _norm_station(v: Optional[str]) -> str:
    return " ".join((v or "").split()).casefold()


def _nom_station_courante() -> Optional[str]:
    """Nom de la station séparée où l'on se trouve (None = station principale)."""
    from database import espace_courant
    from espaces_routes import schemas_actifs
    sch = espace_courant()
    return schemas_actifs().get(sch) if sch else None


def autres_stations(request: Request, db: Session) -> list[dict]:
    """Pour le propriétaire : les autres stations (séparées, ou la principale
    depuis une station séparée) où il peut créer un compte pompiste."""
    from database import espace_courant, session_principale
    from espaces_routes import proprietaire
    from models import Espace
    if not proprietaire(request, db):
        return []
    courant = espace_courant()
    from espaces_routes import nom_principale
    out = [] if courant is None else [{"id": 0, "nom": nom_principale()}]
    with session_principale() as pdb:
        out += [{"id": e.id, "nom": e.nom} for e in pdb.query(Espace).filter(Espace.actif.is_(True)).order_by(Espace.id)
                if e.schema != courant]
    return out


def _fiche_station() -> dict:
    """En-tête de la feuille : nom, adresse et téléphones de la station (fiche des stations)."""
    try:
        from geospatial import _id_courant
        from database import session_principale
        from models import StationGeo
        with session_principale() as pdb:
            f = pdb.query(StationGeo).filter_by(station_id=_id_courant()).first()
            if not f:
                return {}
            return {"adresse": f.adresse, "commune": f.commune, "telephone": f.telephone,
                    "urgence_telephone": f.urgence_telephone}
    except Exception:
        return {}


def de_ma_station(u: Utilisateur, pompe) -> bool:
    """Le pompiste rattaché à une station ne voit et ne saisit que les mètres
    des pompes de cette adresse ; sans station, il voit tout. La station est
    celle de la pompe physique (les produits sont communs à tous)."""
    st = _norm_station(getattr(u, "station_adresse", None))
    return not st or _norm_station(getattr(pompe, "station", None)) == st


def _controler_lignes(db: Session, d: date_type, periode: str, lignes: list,
                      exclure_rapport_id: Optional[int] = None, utilisateur: Optional[Utilisateur] = None) -> list[dict]:
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
        if utilisateur is not None and not de_ma_station(utilisateur, pompe):
            err(l.pompe_id, "autre_station", f"{pompe.nom} n'est pas dans votre station.")
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
    if data.periode is not None and data.periode not in PERIODES:
        raise HTTPException(400, "Période invalide")
    if not data.lignes:
        raise HTTPException(400, "Saisissez l'index d'au moins une pompe.")
    rap = _en_cours(db, u)
    if rap and (rap.date != data.date or (data.periode is not None and rap.periode != data.periode)):
        raise HTTPException(409, {"code": "terminer_dabord",
                                  "message": f"Terminez d'abord votre rapport du "
                                             f"{rap.date.strftime('%d/%m')} (bouton « Terminer »)."})
    if not rap:
        _valider_date(data.date)
    if rap:
        data.periode = rap.periode
    elif data.periode is None:
        data.periode = creneau_automatique(db, data.date, [l.pompe_id for l in data.lignes])
    lignes = _controler_lignes(db, data.date, data.periode, data.lignes, utilisateur=u)

    # Une pompe = un seul rapport actif par créneau
    deja = (db.query(RapportPompisteLigne, RapportPompiste)
            .join(RapportPompiste, RapportPompisteLigne.rapport_id == RapportPompiste.id)
            .filter(RapportPompiste.date == data.date, RapportPompiste.periode == data.periode,
                    RapportPompiste.statut.in_(_ACTIFS), RapportPompiste.supprime_at.is_(None),
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
                               debut_confirme=data.debut_confirme)], utilisateur=u)
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
    try:
        request.state.depuis_rapport = rap.id
    except Exception:
        pass
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
    """Retire du journal les relevés écrits par ce rapport. Un relevé lié au
    rapport mais dont les index ne sont plus ceux du rapport (saisie faite ou
    corrigée à la main dans le module Saisie) n'est pas effacé : il redevient
    une saisie ordinaire (même règle que `nettoyer_journal_anticipe`)."""
    lignes = {l.pompe_id: l for l in rap.lignes}
    n = 0
    for r in db.query(Releve).filter_by(rapport_pompiste_id=rap.id).all():
        l = lignes.get(r.pompe_id)
        av, ap = _valeurs_finales(l) if l else (None, None)
        if l is not None and abs(float(r.metter_avant) - av) <= TOLERANCE                 and abs(float(r.metter_apres) - ap) <= TOLERANCE:
            db.delete(r)
            n += 1
        else:
            r.rapport_pompiste_id = None
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
    rap.feuille = _verifier_feuille(data)
    rap.billetterie = billetterie
    rap.monnaie = round(data.monnaie, 2)
    rap.depenses = depenses
    rap.total_depenses = round(sum(x["montant"] for x in depenses), 2)
    rap.devises = devises
    # Argent compté (gourdes + autres devises converties) + dépenses payées = ventes attendues
    rap.montant_compte = round(_total_billetterie(billetterie, data.monnaie) + sum(x["montant_htg"] for x in devises), 2)
    rap.ecart = round(float(rap.montant_compte) + float(rap.total_depenses) + _totaux_feuille(rap)["net"]
                      - float(rap.montant_attendu), 2)
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
            .filter(RapportPompiste.utilisateur_id == u.id, RapportPompiste.statut != "EN_COURS", RapportPompiste.supprime_at.is_(None))
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
    if rap.supprime_at is not None:
        raise HTTPException(409, "Ce rapport est supprimé : restaurez-le d'abord.")
    if rap.statut not in _A_TRAITER:
        raise HTTPException(409, "Ce rapport est déjà finalisé.")
    return rap


@router.get("/api/pompiste-supervision/rapports")
def lister(statut: Optional[str] = None, date_debut: Optional[date_type] = None,
           date_fin: Optional[date_type] = None, utilisateur_id: Optional[int] = None,
           _s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    """`statut` accepte une liste séparée par des virgules (ex. SOUMIS,PROBLEME).
    SUPPRIME = les rapports supprimés (corbeille), qu'on peut restaurer."""
    statuts = [x for x in (statut or "").split(",") if x]
    q = db.query(RapportPompiste)
    if "SUPPRIME" in statuts:
        q = q.filter(RapportPompiste.supprime_at.isnot(None))
        statuts = []
    else:
        q = q.filter(RapportPompiste.supprime_at.is_(None))
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
    vivants = db.query(RapportPompiste).filter(RapportPompiste.supprime_at.is_(None))
    a_verifier = vivants.filter_by(statut="SOUMIS").count()
    problemes = vivants.filter_by(statut="PROBLEME").count()
    non_termines = vivants.filter_by(statut="EN_COURS").count()
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


class SuppressionRapportIn(BaseModel):
    motif: str


@router.post("/api/pompiste-supervision/rapports/{rid}/supprimer")
def supprimer_rapport(rid: int, data: SuppressionRapportIn, request: Request,
                      s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    """Supprime un rapport mal fait, quel que soit son état (envoyé,
    problème, renvoyé ou finalisé) : ses relevés sortent du journal et ses
    dépenses des dépenses ; ses pompes sont libérées pour ce créneau. Le
    rapport part à la corbeille (filtre « Supprimés ») : on peut le restaurer.
    La trace (contenu, qui, motif) reste dans le journal d'audit."""
    import main
    from models import Depense
    rap = db.get(RapportPompiste, rid)
    if not rap or rap.supprime_at is not None:
        raise HTTPException(404, "Rapport introuvable")
    motif = " ".join((data.motif or "").split())
    if len(motif) < 5:
        raise HTTPException(400, "Indiquez pourquoi vous supprimez ce rapport (5 caractères minimum).")
    trace = {"rapport_id": rap.id, "pompiste": _nom(rap.utilisateur), "date": str(rap.date), "periode": rap.periode,
             "statut": rap.statut, "motif": motif[:300], "montant_attendu": float(rap.montant_attendu),
             "montant_compte": float(rap.montant_compte), "ecart": float(rap.ecart),
             "lignes": [{"pompe": l.pompe.nom, "avant": float(l.metter_avant), "apres": float(l.metter_apres)} for l in rap.lignes]}
    n_releves = _retirer_du_journal(db, rap)
    n_depenses = db.query(Depense).filter_by(rapport_pompiste_id=rap.id).delete()
    trace.update(releves_retires=n_releves, depenses_retirees=n_depenses)
    main._tracer(db, request, "rapport_pompiste_supprime", trace)
    rap.supprime_at = now_haiti()
    rap.suppression = {"motif": motif[:300], "par": _nom(s), "statut_avant": rap.statut,
                       "releves_retires": n_releves, "depenses_retirees": n_depenses}
    db.commit()
    log.info("Rapport pompiste #%s supprimé par l'utilisateur %s : %s", rid, s.id, motif)
    return {"ok": True, "releves_retires": n_releves, "depenses_retirees": n_depenses}


@router.post("/api/pompiste-supervision/rapports/{rid}/restaurer")
def restaurer_rapport(rid: int, request: Request, s: Utilisateur = Depends(_require_superviseur),
                      db: Session = Depends(get_db)):
    """Sort un rapport de la corbeille. Il revient « à vérifier » : chaque
    mètre, la billetterie, les dépenses et l'écart sont à reconfirmer (les
    corrections déjà faites sont gardées) — rien ne rentre au journal ni aux
    Dépenses sans une nouvelle confirmation."""
    import main
    rap = db.get(RapportPompiste, rid)
    if not rap or rap.supprime_at is None:
        raise HTTPException(404, "Rapport supprimé introuvable")
    avant = (rap.suppression or {}).get("statut_avant") or rap.statut
    pris = (db.query(RapportPompisteLigne, RapportPompiste)
            .join(RapportPompiste, RapportPompisteLigne.rapport_id == RapportPompiste.id)
            .filter(RapportPompiste.date == rap.date, RapportPompiste.periode == rap.periode,
                    RapportPompiste.statut.in_(_ACTIFS), RapportPompiste.supprime_at.is_(None),
                    RapportPompiste.id != rap.id,
                    RapportPompisteLigne.pompe_id.in_([l.pompe_id for l in rap.lignes])).all())
    if pris:
        noms = ", ".join(sorted({f"{l.pompe.nom} (rapport n°{r.id}, {_nom(r.utilisateur)})" for l, r in pris}))
        raise HTTPException(409, f"Impossible de restaurer : ce créneau est déjà rapporté — {noms}. "
                                 "Supprimez d'abord l'autre rapport.")
    rap.supprime_at, rap.suppression = None, None
    if rap.statut != "EN_COURS":
        rap.statut = "PROBLEME" if avant == "PROBLEME" else ("REJETE" if avant == "REJETE" else "SOUMIS")
    for l in rap.lignes:
        l.confirmee = False
    rap.billetterie_confirmee = False
    rap.ecart_confirme = False
    deps = [dict(x) for x in (rap.depenses or [])]
    for x in deps:
        x.update(statut="en_attente", motif=None, par=None, le=None)
        x.pop("vers_depenses", None)
    rap.depenses = deps
    rap.traite_par_id, rap.traite_at, rap.note_superviseur = None, None, None
    main._tracer(db, request, "rapport_pompiste_restaure",
                 {"rapport_id": rap.id, "pompiste": _nom(rap.utilisateur), "date": str(rap.date),
                  "periode": rap.periode, "statut_avant": avant, "statut": rap.statut})
    db.commit()
    db.refresh(rap)
    return _rapport_dict(db, rap, avec_journal=rap.statut in _A_TRAITER)


def _retirer_ligne_du_journal(db: Session, rap: RapportPompiste, l: RapportPompisteLigne) -> None:
    """Le mètre n'est plus confirmé : il sort de la Saisie, sauf s'il y a été
    modifié à la main (il redevient alors une saisie ordinaire)."""
    av, ap = _valeurs_finales(l)
    for r in db.query(Releve).filter_by(rapport_pompiste_id=rap.id, pompe_id=l.pompe_id).all():
        if abs(float(r.metter_avant) - av) <= TOLERANCE and abs(float(r.metter_apres) - ap) <= TOLERANCE:
            db.delete(r)
        else:
            r.rapport_pompiste_id = None


def _pompes_possibles(db: Session, rap: RapportPompiste, l: RapportPompisteLigne) -> list:
    """Pompes actives sur lesquelles ces index peuvent être remis, la plus
    probable d'abord : celle dont le dernier index connu est le début saisi
    (`suit`). `dans_rapport` = déjà dans ce rapport (on échange alors les
    index des deux pompes) ; `prise_par` = créneau déjà rapporté ailleurs."""
    av, _ap = _valeurs_finales(l)
    dans = {x.pompe_id for x in rap.lignes}
    prises = {}
    for lp, rp in (db.query(RapportPompisteLigne, RapportPompiste)
                   .join(RapportPompiste, RapportPompisteLigne.rapport_id == RapportPompiste.id)
                   .filter(RapportPompiste.date == rap.date, RapportPompiste.periode == rap.periode,
                           RapportPompiste.statut.in_(_ACTIFS), RapportPompiste.supprime_at.is_(None),
                           RapportPompiste.id != rap.id).all()):
        prises[lp.pompe_id] = f"rapport n°{rp.id} ({_nom(rp.utilisateur)})"
    out = []
    for p in db.query(Pompe).filter(Pompe.actif.is_(True), Pompe.id != l.pompe_id).order_by(Pompe.nom).all():
        prec, _src = _index_precedent(db, p.id, rap.date, rap.periode, exclure_rapport_id=rap.id)
        out.append({"pompe_id": p.id, "pompe_nom": p.nom, "produit_nom": p.produit.nom if p.produit else "",
                    "index_precedent": prec, "ecart": round(av - prec, 4) if prec is not None else None,
                    "suit": prec is not None and abs(prec - av) <= TOLERANCE and p.id not in prises,
                    "dans_rapport": p.id in dans, "prise_par": prises.get(p.id)})
    out.sort(key=lambda x: (not x["suit"], x["prise_par"] is not None,
                            abs(x["ecart"]) if x["ecart"] is not None else float("inf"), x["pompe_nom"]))
    return out


@router.get("/api/pompiste-supervision/rapports/{rid}/lignes/{pompe_id}/pompes-possibles")
def pompes_possibles(rid: int, pompe_id: int, _s: Utilisateur = Depends(_require_superviseur),
                     db: Session = Depends(get_db)):
    rap = _rapport_a_traiter(db, rid)
    l = next((x for x in rap.lignes if x.pompe_id == pompe_id), None)
    if not l:
        raise HTTPException(404, "Pompe absente du rapport")
    return _pompes_possibles(db, rap, l)


class ChangerDateIn(BaseModel):
    date: date_type
    periode: str
    motif: str


# Statuts dont on peut changer la date (EN_COURS : le pompiste est encore
# dessus ; REJETE : il le refait lui-même).
_DATE_MODIFIABLE = ("SOUMIS", "PROBLEME", "CONFIRME", "RESOLU")
_ANCIENNETE_MAX_JOURS = 62


def _voisins(db: Session, pompe_id: int, d: date_type, periode: str, rid: int,
             exclure_releve_id: Optional[int] = None) -> tuple:
    """Index de la pompe juste avant et juste après (d, periode), hors ce
    rapport (ou ce relevé) : journal officiel et rapports pompistes pas encore
    finalisés. Retourne ((fin précédente, source), (début suivant, source))."""
    import main
    cible = (d, _RANG[periode])
    p = db.get(Pompe, pompe_id)
    prec, suiv = None, None             # (clé, valeur, source)
    rel = (db.query(Releve).filter(Releve.pompe_id == pompe_id,
                                   (Releve.rapport_pompiste_id.is_(None)) | (Releve.rapport_pompiste_id != rid)))
    if exclure_releve_id:
        rel = rel.filter(Releve.id != exclure_releve_id)
    for r in rel.filter(Releve.date <= d).order_by(Releve.date.desc()).limit(6).all():
        cle = (r.date, _RANG.get(r.periode, 9))
        if cle < cible and (prec is None or cle > prec[0]):
            prec = (cle, float(r.metter_apres), f"journal — {_per(r.periode)} du {r.date.strftime('%d/%m')}")
    for r in rel.filter(Releve.date >= d).order_by(Releve.date.asc()).limit(6).all():
        cle = (r.date, _RANG.get(r.periode, 9))
        if cle > cible and (suiv is None or cle < suiv[0]):
            suiv = (cle, float(r.metter_avant), f"journal — {_per(r.periode)} du {r.date.strftime('%d/%m')}")
    for l, rp in (db.query(RapportPompisteLigne, RapportPompiste)
                  .join(RapportPompiste, RapportPompisteLigne.rapport_id == RapportPompiste.id)
                  .filter(RapportPompisteLigne.pompe_id == pompe_id, RapportPompiste.id != rid,
                          RapportPompiste.statut.in_(_EN_ATTENTE), RapportPompiste.supprime_at.is_(None),
                          RapportPompiste.date >= d - timedelta(days=3), RapportPompiste.date <= d + timedelta(days=3))
                  .all()):
        cle = (rp.date, _RANG.get(rp.periode, 9))
        av, ap = _valeurs_finales(l)
        src = f"rapport n°{rp.id} — {_per(rp.periode)} du {rp.date.strftime('%d/%m')}"
        if cle < cible and (prec is None or cle > prec[0]):
            prec = (cle, ap, src)
        if cle > cible and (suiv is None or cle < suiv[0]):
            suiv = (cle, av, src)
    if prec is None:
        dep = main.base_depart(p, d)
        if dep is not None:
            prec = (None, dep, "mètre de départ")
    return ((prec[1], prec[2]) if prec else (None, None)), ((suiv[1], suiv[2]) if suiv else (None, None))


def _analyse_date(db: Session, rap: RapportPompiste, d: date_type, periode: str) -> dict:
    """Tout ce que change le déplacement du rapport vers (d, periode) :
    `bloquants` (refus), `avertissements` (à savoir avant de valider) et le
    détail par pompe (continuité, saisie existante, prix)."""
    bloq, avert, lignes = [], [], []
    supprime = rap.supprime_at is not None
    if periode not in PERIODES:
        bloq.append("Période invalide.")
    if not supprime and rap.statut not in _DATE_MODIFIABLE:
        bloq.append("Un rapport non terminé ou renvoyé au pompiste ne peut pas changer de date "
                    "(le pompiste doit d'abord le terminer ou le refaire).")
    if d > today_haiti():
        bloq.append("La date ne peut pas être dans le futur.")
    if d == rap.date and periode == rap.periode:
        bloq.append("C'est déjà la date et la période de ce rapport.")
    if bloq:
        return {"bloquants": bloq, "avertissements": avert, "lignes": lignes}
    if (today_haiti() - d).days > _ANCIENNETE_MAX_JOURS:
        avert.append(f"Date ancienne ({(today_haiti() - d).days} jours) : vérifiez qu'il s'agit bien du bon jour.")
    # Créneau déjà rapporté (mêmes pompes, autre rapport vivant)
    pris = (db.query(RapportPompisteLigne, RapportPompiste)
            .join(RapportPompiste, RapportPompisteLigne.rapport_id == RapportPompiste.id)
            .filter(RapportPompiste.date == d, RapportPompiste.periode == periode,
                    RapportPompiste.statut.in_(_ACTIFS), RapportPompiste.supprime_at.is_(None),
                    RapportPompiste.id != rap.id,
                    RapportPompisteLigne.pompe_id.in_([l.pompe_id for l in rap.lignes])).all())
    if pris:
        noms = ", ".join(sorted({f"{l.pompe.nom} (rapport n°{r.id}, {_nom(r.utilisateur)})" for l, r in pris}))
        bloq.append(f"Ce créneau est déjà rapporté — {noms}. Supprimez ou déplacez d'abord l'autre rapport.")
    # Même pompiste, autre rapport sur ce créneau (autres pompes)
    autre = (db.query(RapportPompiste)
             .filter(RapportPompiste.utilisateur_id == rap.utilisateur_id, RapportPompiste.date == d,
                     RapportPompiste.periode == periode, RapportPompiste.id != rap.id,
                     RapportPompiste.statut.in_(_ACTIFS), RapportPompiste.supprime_at.is_(None)).first())
    if autre:
        avert.append(f"{_nom(rap.utilisateur)} a déjà le rapport n°{autre.id} sur ce créneau (autres pompes) : "
                     "il aura deux rapports pour la même période.")
    if rap.statut in ("CONFIRME", "RESOLU") and not supprime:
        avert.append("Rapport finalisé : il revient « à vérifier ». Ses mètres sortent du journal du "
                     f"{rap.date.strftime('%d/%m')} et entrent au {d.strftime('%d/%m')} quand vous les reconfirmez ; "
                     "billetterie et dépenses restent comme décidé.")
    elif any(l.confirmee for l in rap.lignes) and not supprime:
        avert.append("Les mètres déjà confirmés sortent du journal : à reconfirmer sur le nouveau créneau.")
    # Par pompe : continuité au nouveau créneau, saisie existante, prix
    for l in rap.lignes:
        av, ap = _valeurs_finales(l)
        (prec, src_p), (suiv, src_s) = _voisins(db, l.pompe_id, d, periode, rap.id)
        saisie = (db.query(Releve).filter_by(date=d, periode=periode, pompe_id=l.pompe_id)
                  .filter((Releve.rapport_pompiste_id.is_(None)) | (Releve.rapport_pompiste_id != rap.id)).first())
        prix_n = _prix(db, l.pompe, d)
        x = {"pompe_id": l.pompe_id, "pompe_nom": l.pompe.nom, "metter_avant": av, "metter_apres": ap,
             "index_precedent": prec, "source_precedent": src_p,
             "ecart_debut": round(av - prec, 4) if prec is not None else None,
             "index_suivant": suiv, "source_suivant": src_s,
             "ecart_fin": round(suiv - ap, 4) if suiv is not None else None,
             "saisie_existante": ({"metter_avant": float(saisie.metter_avant), "metter_apres": float(saisie.metter_apres),
                                   "saisi_par": _nom(saisie.cree_par)} if saisie else None),
             "prix_actuel": float(l.prix_gallon), "prix_nouveau": prix_n,
             "montant_actuel": round((ap - av) * float(l.prix_gallon), 2), "montant_nouveau": round((ap - av) * prix_n, 2)}
        x["suit"] = x["ecart_debut"] is not None and abs(x["ecart_debut"]) <= TOLERANCE
        x["enchaine"] = x["ecart_fin"] is None or abs(x["ecart_fin"]) <= TOLERANCE
        lignes.append(x)
        if x["ecart_debut"] is not None and not x["suit"]:
            sens = "recul" if x["ecart_debut"] < 0 else "saut"
            avert.append(f"{l.pompe.nom} : le début {av:,.3f} ne suit pas la fin précédente {prec:,.3f} "
                         f"({src_p}) — {sens} de {abs(x['ecart_debut']):,.3f} gal.".replace(",", " "))
        if not x["enchaine"]:
            avert.append(f"{l.pompe.nom} : la fin {ap:,.3f} ne rejoint pas le début suivant {suiv:,.3f} "
                         f"({src_s}) — écart de {abs(x['ecart_fin']):,.3f} gal.".replace(",", " "))
        if saisie:
            avert.append(f"{l.pompe.nom} : déjà saisi dans le module Saisie sur ce créneau "
                         f"({float(saisie.metter_avant):,.3f} → {float(saisie.metter_apres):,.3f}) ; à la confirmation, "
                         "les valeurs du rapport remplaceront cette saisie.".replace(",", " "))
        if abs(prix_n - float(l.prix_gallon)) > 0.0005:
            avert.append(f"{l.pompe.nom} : prix du {d.strftime('%d/%m')} = {prix_n:g} G/gal "
                         f"(au lieu de {float(l.prix_gallon):g}) : {x['montant_actuel']:,.2f} → {x['montant_nouveau']:,.2f} G."
                         .replace(",", " "))
        cree = getattr(l.pompe, "created_at", None)
        if cree is not None and cree.date() > d:
            avert.append(f"{l.pompe.nom} : pompe créée le {cree.strftime('%d/%m/%Y')}, après cette date.")
    # Ancien créneau : ce que deviennent les relevés suivants (rapport encore au journal)
    for l in (rap.lignes if not supprime else []):
        (prec_a, _sp), (suiv_a, src_a) = _voisins(db, l.pompe_id, rap.date, rap.periode, rap.id)
        if suiv_a is not None and prec_a is not None and abs(suiv_a - prec_a) > TOLERANCE:
            avert.append(f"{l.pompe.nom} : à l'ancien créneau ({_per(rap.periode)} du {rap.date.strftime('%d/%m')}), "
                         f"le relevé suivant ({src_a}) ne suivra plus rien — écart de {abs(suiv_a - prec_a):,.3f} gal."
                         .replace(",", " "))
    # Devises : taux du nouveau jour
    for x in rap.devises or []:
        t = taux_du_jour(db, d, x.get("devise", "USD"))
        if t is None:
            avert.append(f"Pas de taux {x.get('devise')} pour le {d.strftime('%d/%m')} : l'ancien taux est gardé.")
        elif abs(float(t.taux) - float(x.get("taux") or 0)) > 0.00001:
            avert.append(f"{x.get('devise')} : taux du {d.strftime('%d/%m')} = {float(t.taux):g} (au lieu de "
                         f"{float(x.get('taux') or 0):g}) — l'argent compté est recalculé.")
    nb_dep = sum(1 for x in rap.depenses or [] if x.get("statut") != "refusee")
    if nb_dep:
        avert.append(f"{nb_dep} dépense(s) du pompiste : elles suivent le rapport au {d.strftime('%d/%m')}"
                     + (" et sortent des Dépenses jusqu'à la nouvelle finalisation." if rap.statut in ("CONFIRME", "RESOLU")
                        and rap.supprime_at is None else "."))
    return {"bloquants": bloq, "avertissements": avert, "lignes": lignes}


@router.get("/api/pompiste-supervision/rapports/{rid}/changer-date/apercu")
def apercu_changer_date(rid: int, date: date_type, periode: str,
                        _s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    """Ce que changerait le déplacement du rapport, avant de valider."""
    rap = db.get(RapportPompiste, rid)
    if not rap:
        raise HTTPException(404, "Rapport introuvable")
    return _analyse_date(db, rap, date, periode)


@router.post("/api/pompiste-supervision/rapports/{rid}/changer-date")
def changer_date(rid: int, data: ChangerDateIn, request: Request,
                 s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    """Rapport enregistré sur le mauvais jour ou la mauvaise période : la
    direction le déplace (corbeille, à vérifier ou finalisé). Les mètres sont
    à reconfirmer sur le nouveau créneau ; prix et taux du nouveau jour ;
    dépenses du pompiste déplacées avec lui. Voir `_analyse_date`."""
    import main
    from models import Depense
    rap = db.get(RapportPompiste, rid)
    if not rap:
        raise HTTPException(404, "Rapport introuvable")
    motif = " ".join((data.motif or "").split())
    if len(motif) < 5:
        raise HTTPException(400, "Indiquez pourquoi vous changez la date (5 caractères minimum).")
    an = _analyse_date(db, rap, data.date, data.periode)
    if an["bloquants"]:
        code = 409 if any("déjà rapporté" in x or "non terminé" in x for x in an["bloquants"]) else 400
        raise HTTPException(code, " ".join(an["bloquants"]))
    supprime = rap.supprime_at is not None
    etait_finalise = not supprime and rap.statut in ("CONFIRME", "RESOLU")
    trace = {"rapport_id": rap.id, "pompiste": _nom(rap.utilisateur), "motif": motif[:300],
             "de": {"date": str(rap.date), "periode": rap.periode},
             "vers": {"date": str(data.date), "periode": data.periode}, "supprime": supprime,
             "statut_avant": rap.statut, "avertissements": an["avertissements"][:20]}
    if not supprime:
        for l in rap.lignes:
            _retirer_ligne_du_journal(db, rap, l)
            l.confirmee, l.confirmee_at = False, None
        rap.ecart_confirme = False
        if rap.statut in ("CONFIRME", "RESOLU"):
            rap.statut = "SOUMIS"
            rap.traite_par_id, rap.traite_at = None, None
    rap.date, rap.periode = data.date, data.periode
    for l in rap.lignes:
        l.prix_gallon = _prix(db, l.pompe, data.date)
    # Argent compté en devises : taux du nouveau jour (s'il existe)
    devises = [dict(x) for x in (rap.devises or [])]
    for x in devises:
        t = taux_du_jour(db, data.date, x.get("devise", "USD"))
        if t is not None:
            x["taux"] = float(t.taux)
            x["montant_htg"] = round(float(x["montant"]) * float(t.taux), 2)
    if devises:
        ancien = sum(float(x.get("montant_htg") or 0) for x in (rap.devises or []))
        rap.devises = devises
        rap.montant_compte = round(float(rap.montant_compte) - ancien + sum(x["montant_htg"] for x in devises), 2)
    _recalculer_attendu(rap)
    rap.ecart = round(float(rap.montant_compte) + float(rap.total_depenses or 0) + _totaux_feuille(rap)["net"]
                      - float(rap.montant_attendu), 2)
    # Le rapport n'est plus finalisé : ses dépenses sortent des Dépenses (rapport du
    # jour, caisse) comme ses mètres sortent du journal ; elles y reviendront, au
    # nouveau jour, à la nouvelle finalisation.
    if etait_finalise:
        db.query(Depense).filter_by(rapport_pompiste_id=rap.id).delete()
    for dep in db.query(Depense).filter_by(rapport_pompiste_id=rap.id).all():
        dep.date_depense = data.date
    main._tracer(db, request, "rapport_pompiste_date_changee", trace)
    db.commit()
    db.refresh(rap)
    out = _rapport_dict(db, rap, avec_journal=not supprime and rap.statut in _A_TRAITER)
    out["avertissements"] = an["avertissements"]
    return out


class ChangerPompeIn(BaseModel):
    pompe_id: int
    motif: str


@router.post("/api/pompiste-supervision/rapports/{rid}/lignes/{pompe_id}/changer-pompe")
def changer_pompe(rid: int, pompe_id: int, data: ChangerPompeIn, request: Request,
                  s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    """Le pompiste a mis ces index sur la mauvaise pompe : la direction les
    remet sur la bonne. Si la bonne pompe est déjà dans le rapport (index
    croisés), les index des deux pompes sont échangés. Le prix suit la pompe
    (son carburant). Les mètres touchés sont à reconfirmer ; la pompe choisie
    par le pompiste reste affichée."""
    import main
    rap = _rapport_a_traiter(db, rid)
    l = next((x for x in rap.lignes if x.pompe_id == pompe_id), None)
    if not l:
        raise HTTPException(404, "Pompe absente du rapport")
    motif = " ".join((data.motif or "").split())
    if len(motif) < 5:
        raise HTTPException(400, "Indiquez pourquoi vous changez de pompe (5 caractères minimum).")
    if data.pompe_id == pompe_id:
        raise HTTPException(400, "C'est déjà cette pompe.")
    neuve = db.get(Pompe, data.pompe_id)
    if not neuve or not neuve.actif:
        raise HTTPException(404, "Pompe introuvable ou inactive.")
    import placement_metres as _pm
    if _pm.activite(neuve) != _pm.activite(l.pompe):
        raise HTTPException(400, "Le propane et le carburant ne se mélangent jamais.")
    autre = next((x for x in rap.lignes if x.pompe_id == neuve.id), None)
    pris = (db.query(RapportPompisteLigne, RapportPompiste)
            .join(RapportPompiste, RapportPompisteLigne.rapport_id == RapportPompiste.id)
            .filter(RapportPompiste.date == rap.date, RapportPompiste.periode == rap.periode,
                    RapportPompiste.statut.in_(_ACTIFS), RapportPompiste.supprime_at.is_(None),
                    RapportPompiste.id != rap.id, RapportPompisteLigne.pompe_id == neuve.id).first())
    if pris:
        raise HTTPException(409, f"{neuve.nom} est déjà rapportée sur ce créneau (rapport n°{pris[1].id}, "
                                 f"{_nom(pris[1].utilisateur)}).")
    ancienne = l.pompe
    trace = {"rapport_id": rap.id, "date": str(rap.date), "periode": rap.periode, "motif": motif[:300],
             "de": ancienne.nom, "vers": neuve.nom, "echange": autre is not None,
             "index": list(_valeurs_finales(l))}
    # Les mètres touchés ne sont plus confirmés : ils sortent d'abord du journal
    for x in ([l, autre] if autre else [l]):
        _retirer_ligne_du_journal(db, rap, x)
        x.confirmee, x.confirmee_at = False, None
    import placement_metres
    if autre:
        # Index croisés : chaque pompe reprend les index de l'autre (prix inchangés)
        trace["index_autre"] = list(_valeurs_finales(autre))
        placement_metres.apprendre(db, l.pompe_saisie_id or l.pompe_id, l.pompe_id, autre.pompe_id,
                                   rap.date, utilisateur_id=rap.utilisateur_id)
        placement_metres.apprendre(db, autre.pompe_saisie_id or autre.pompe_id, autre.pompe_id, l.pompe_id,
                                   rap.date, utilisateur_id=rap.utilisateur_id)
        for k in ("metter_avant", "metter_apres", "metter_avant_corrige", "metter_apres_corrige"):
            va, vb = getattr(l, k), getattr(autre, k)
            setattr(l, k, vb)
            setattr(autre, k, va)
        sa, sb = l.pompe_saisie_id or l.pompe_id, autre.pompe_saisie_id or autre.pompe_id
        l.pompe_saisie_id = None if sb == l.pompe_id else sb
        autre.pompe_saisie_id = None if sa == autre.pompe_id else sa
    else:
        origine = l.pompe_saisie_id or l.pompe_id
        placement_metres.apprendre(db, origine, l.pompe_id, neuve.id, rap.date, utilisateur_id=rap.utilisateur_id)
        l.pompe, l.pompe_id = neuve, neuve.id
        l.prix_gallon = _prix(db, neuve, rap.date)
        l.pompe_saisie_id = None if origine == neuve.id else origine
    rap.ecart_confirme = False
    main._tracer(db, request, "rapport_pompiste_pompe_changee", trace)
    db.commit()
    db.refresh(rap)
    log.info("Rapport pompiste #%s : %s -> %s par l'utilisateur %s", rid, ancienne.nom, neuve.nom, s.id)
    return _rapport_dict(db, rap, avec_journal=True)


class ReclasserIn(BaseModel):
    motif: str
    plan: Optional[list] = None      # [{"pompe_id", "vers"}] ; absent = le plan proposé


@router.get("/api/pompiste-supervision/rapports/{rid}/placement")
def placement_rapport(rid: int, _s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    """Placement proposé par le moteur pour toutes les lignes du rapport."""
    import placement_metres
    return placement_metres.plan_rapport(db, _rapport_a_traiter(db, rid))


@router.post("/api/pompiste-supervision/rapports/{rid}/reclasser")
def reclasser(rid: int, data: ReclasserIn, request: Request,
              s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    """Applique d'un coup le placement proposé (ou celui envoyé) : chaque
    ligne retrouve son mètre, échanges et rotations compris. Les mètres
    touchés sont à reconfirmer ; l'erreur est retenue pour ce pompiste."""
    import main
    import placement_metres
    rap = _rapport_a_traiter(db, rid)
    motif = " ".join((data.motif or "").split())
    if len(motif) < 5:
        raise HTTPException(400, "Indiquez pourquoi vous changez de mètre (5 caractères minimum).")
    plan = data.plan if data.plan is not None else placement_metres.plan_rapport(db, rap)["plan"]
    try:
        mapping = {int(x["pompe_id"]): int(x["vers"]) for x in plan}
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "Plan invalide.")
    changements = placement_metres.appliquer_plan(db, rap, mapping)
    main._tracer(db, request, "rapport_pompiste_metres_reclasses",
                 {"rapport_id": rap.id, "date": str(rap.date), "periode": rap.periode,
                  "motif": motif[:300], "changements": changements})
    db.commit()
    db.refresh(rap)
    log.info("Rapport pompiste #%s : %d mètre(s) reclassé(s) par l'utilisateur %s", rid, len(changements), s.id)
    return _rapport_dict(db, rap, avec_journal=True)


class LigneConfIn(BaseModel):
    metter_avant: float
    metter_apres: float


@router.post("/api/pompiste-supervision/rapports/{rid}/lignes/{pompe_id}/confirmer")
def confirmer_ligne(rid: int, pompe_id: int, data: LigneConfIn, request: Request,
                    _s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    """Confirme un mètre, en le corrigeant si les valeurs envoyées diffèrent
    de celles du pompiste (qui restent conservées). Rien n'entre au journal,
    à la caisse ni dans les totaux avant la finalisation du rapport : le
    mètre reste « en attente de validation »."""
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
    _retirer_ligne_du_journal(db, rap, l)
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
    # 3. Dépenses confirmées (et envoyées) : elles entrent dans les Dépenses maintenant
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
        if (x.get("statut") == "confirmee" and _vers_depenses(x)
                and _ecrire_une_depense(db, rap, i, request, s)):
            n += 1
    return n


def _vers_depenses(x: dict) -> bool:
    """Une dépense confirmée va dans les Dépenses (et le rapport du jour),
    sauf si le superviseur a choisi de ne pas l'y envoyer."""
    return x.get("vers_depenses") is not False


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
    envoyer: Optional[bool] = None      # confirmer : envoyer aux Dépenses (oui par défaut)


def _decider_depense(db: Session, rid: int, i: int) -> tuple:
    rap = _rapport_a_traiter(db, rid)
    deps = [dict(x) for x in (rap.depenses or [])]
    if not (0 <= i < len(deps)):
        raise HTTPException(404, "Dépense introuvable dans ce rapport.")
    return rap, deps


@router.post("/api/pompiste-supervision/rapports/{rid}/depenses/{i}/confirmer")
def confirmer_depense(rid: int, i: int, request: Request, data: Optional[DepenseDecisionIn] = None,
                      s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    """Dépense du pompiste acceptée : elle réduit l'argent à remettre et, si
    `envoyer` (par défaut), elle entrera dans les Dépenses de la station et le
    rapport du jour à la finalisation du rapport (rien avant)."""
    rap, deps = _decider_depense(db, rid, i)
    envoyer = not (data and data.envoyer is False)
    deps[i].update(statut="confirmee", motif=None, par=_nom(s), le=now_haiti().isoformat(),
                   vers_depenses=envoyer)
    rap.depenses = deps
    rap.ecart_confirme = False
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
    deps[i].pop("vers_depenses", None)
    rap.depenses = deps
    rap.ecart_confirme = False
    _retirer_une_depense(db, rap, i)
    db.commit()
    db.refresh(rap)
    return _rapport_dict(db, rap, avec_journal=True)


class EnvoiDepenseIn(BaseModel):
    envoyer: bool


@router.post("/api/pompiste-supervision/rapports/{rid}/depenses/{i}/envoyer")
def envoyer_depense(rid: int, i: int, data: EnvoiDepenseIn, request: Request,
                    s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    """Envoie (ou retire) une dépense confirmée dans les Dépenses de la station
    et le rapport du jour — même après la finalisation du rapport. Le montant
    dû par le pompiste ne change pas : la dépense reste confirmée."""
    import main
    rap = db.get(RapportPompiste, rid)
    if not rap or rap.supprime_at is not None:
        raise HTTPException(404, "Rapport introuvable")
    deps = [dict(x) for x in (rap.depenses or [])]
    if not (0 <= i < len(deps)):
        raise HTTPException(404, "Dépense introuvable dans ce rapport.")
    if deps[i].get("statut") != "confirmee":
        raise HTTPException(409, "Confirmez d'abord la dépense.")
    if _vers_depenses(deps[i]) != data.envoyer:
        deps[i]["vers_depenses"] = data.envoyer
        rap.depenses = deps
        if rap.statut in ("CONFIRME", "RESOLU"):     # avant la finalisation : seulement le choix
            if data.envoyer:
                _ecrire_une_depense(db, rap, i, request, s)
            else:
                _retirer_une_depense(db, rap, i)
        main._tracer(db, request, "depense_pompiste_envoyee" if data.envoyer else "depense_pompiste_retenue",
                     {"rapport_pompiste_id": rap.id, "reference": _ref_depense(rap, i),
                      "description": deps[i].get("description"), "montant": deps[i].get("montant")})
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
            "station_adresse": u.station_adresse,
            "venu_d_ailleurs": bool(getattr(u, "compte_origine", None)),
            "pin_a_reprendre": not u.code_acces_hash,
            "dernier_rapport": str(dernier.date) if dernier else None}


def stations_connues(db: Session) -> list[dict]:
    """Toutes les adresses enregistrées sur les pompes physiques, avec leurs
    pompes : la liste des stations où un pompiste peut travailler."""
    from models import Distributeur
    out: dict = {}
    for d in db.query(Distributeur).order_by(Distributeur.ordre, Distributeur.id).all():
        a = " ".join((d.adresse or "").split())
        if not a:
            continue
        s = out.setdefault(_norm_station(a), {"adresse": a, "produits": []})
        s["produits"].append(d.nom)          # nom des pompes de la station
    return sorted(out.values(), key=lambda x: x["adresse"].casefold())


def _station_valide(db: Session, v: Optional[str]) -> Optional[str]:
    a = " ".join((v or "").split())
    if not a:
        return None
    for s in stations_connues(db):
        if _norm_station(s["adresse"]) == _norm_station(a):
            return s["adresse"]
    raise HTTPException(400, "Station inconnue : choisissez une adresse enregistrée sur les produits.")


@router.get("/api/pompiste-supervision/stations")
def lister_stations(_s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    return stations_connues(db)


@router.post("/api/pompiste-supervision/recuperer-employes")
def recuperer_employes(_s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    """Tous les pompistes enregistrés deviennent des employés (poste
    Pompiste), disponibles pour l'attribution des relevés."""
    return rattacher_employes_pompistes(db)


def _autorisations(request: Request, db: Session, comptes: list[dict]) -> None:
    """Propriétaire : pour chaque pompiste de cette station, les autres
    stations où il est autorisé (compte actif là-bas) ; pour un compte venu
    d'une autre station, le nom de celle-ci."""
    from database import espace_courant, session_espace
    from espaces_routes import nom_station
    from main import marque_origine
    autres = autres_stations(request, db)
    ici = espace_courant()
    actifs: dict = {}
    for st in autres:
        try:
            with session_espace(None if st["id"] == 0 else f"esp_{st['id']}") as sdb:
                actifs[st["id"]] = {m for (m,) in sdb.query(Utilisateur.compte_origine)
                                    .filter(Utilisateur.compte_origine.isnot(None), Utilisateur.actif.is_(True)).all()}
        except Exception:
            actifs[st["id"]] = set()
    for c in comptes:
        if c["venu_d_ailleurs"]:
            sch = (c.pop("_origine", "") or "").split(":")[0]
            try:
                c["origine_nom"] = nom_station(0 if sch == "principal" else int(sch.split("_")[1]))
            except (ValueError, IndexError):
                c["origine_nom"] = None
            continue
        c.pop("_origine", None)
        m = marque_origine(ici, c["id"])
        c["autorisations"] = [{"espace_id": st["id"], "nom": st["nom"], "autorise": m in actifs[st["id"]]} for st in autres]


@router.get("/api/pompiste-supervision/comptes")
def lister_comptes(request: Request, _s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    role = assurer_role_pompiste(db)
    rows = db.query(Utilisateur).filter_by(role_id=role.id).order_by(Utilisateur.nom_complet).all()
    employes = (db.query(Employe).filter(Employe.actif.is_(True))
                .order_by(Employe.prenom, Employe.nom).all())
    comptes = [dict(_compte_dict(db, u), _origine=u.compte_origine) for u in rows]
    _autorisations(request, db, comptes)
    return {
        "comptes": comptes,
        "stations": stations_connues(db),
        "station_courante": _nom_station_courante() or _nom_principale(),
        "autres_stations": autres_stations(request, db),
        "employes": [{"id": e.id, "nom": f"{e.prenom} {e.nom}", "poste": e.poste,
                      "lie": e.utilisateur_id is not None} for e in employes],
    }


class AutorisationIn(BaseModel):
    espace_id: int
    autorise: bool


@router.post("/api/pompiste-supervision/comptes/{uid}/stations")
def autoriser_station(uid: int, data: AutorisationIn, request: Request,
                      s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    """Le propriétaire autorise (ou retire) un pompiste de cette station à
    travailler aussi dans une autre : son compte y est créé (même code, même
    PIN). Sans autorisation, il ne peut entrer que dans sa station."""
    from database import definir_espace, espace_courant, retablir_espace, session_espace
    from main import _compte_dans_station, _tracer
    cible = {x["id"]: x for x in autres_stations(request, db)}.get(data.espace_id)
    if not cible:
        raise HTTPException(403, "Station introuvable, ou réservée au propriétaire.")
    u = db.get(Utilisateur, uid)
    if not u or not est_role_pompiste(u.role_obj):
        raise HTTPException(404, "Compte pompiste introuvable.")
    if u.compte_origine:
        raise HTTPException(400, "Ce compte vient d'une autre station : gérez-le depuis sa station d'origine.")
    ici = espace_courant()
    schema = None if data.espace_id == 0 else f"esp_{data.espace_id}"
    jeton = definir_espace(schema)
    try:
        with session_espace(schema) as edb:
            m = _compte_dans_station(edb, ici, u, creer=data.autorise)
            if m is not None:
                m.actif = data.autorise
                edb.commit()
    finally:
        retablir_espace(jeton)
    _tracer(db, request, "pompiste_station_autorisee" if data.autorise else "pompiste_station_retiree",
            {"compte": u.username, "station": cible["nom"]})
    db.commit()
    return {"espace_id": data.espace_id, "nom": cible["nom"], "autorise": data.autorise}


class CompteIn(BaseModel):
    prenom: str
    nom: str
    code_acces: Optional[str] = None     # vide = généré
    employe_id: Optional[int] = None
    station_adresse: Optional[str] = None   # adresse des produits ; vide = toutes les stations
    espace_id: Optional[int] = None         # autre station (0 = principale) : compte créé dans cette station
    username: Optional[str] = None          # code imposé (reprise d'un compte existant ailleurs) ; vide = généré
    reprendre_pin: bool = False             # PIN laissé vide : le pompiste tape son ancien PIN à la 1re connexion


def _slug(txt: str, n: int = 12) -> str:
    t = unicodedata.normalize("NFKD", txt).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", t)[:n]


def _identifiant_unique(db: Session, prenom: str, nom: str) -> str:
    """Code de connexion : prénom + nom + 4 chiffres aléatoires (ex. jeanpierre4821)."""
    base = (_slug(prenom) + _slug(nom)) or "pompiste"
    for _ in range(50):
        code = f"{base}{secrets.randbelow(10000):04d}"
        if not db.query(Utilisateur).filter(Utilisateur.username.ilike(code)).first():
            from espaces_routes import compte_ailleurs
            if not compte_ailleurs(username=code):       # unique dans toutes les stations
                return code
    raise HTTPException(500, "Impossible de générer un code unique, réessayez.")


def _nouveau_pin() -> str:
    return "".join(secrets.choice("0123456789") for _ in range(9))


@router.post("/api/pompiste-supervision/comptes", status_code=201)
def creer_compte(data: CompteIn, request: Request, _s: Utilisateur = Depends(_require_superviseur),
                 db: Session = Depends(get_db)):
    if data.espace_id is not None:
        # Compte créé dans une autre station : réservé au propriétaire
        from database import definir_espace, retablir_espace, session_espace
        cible = {x["id"]: x for x in autres_stations(request, db)}.get(data.espace_id)
        if not cible:
            raise HTTPException(403, "Station introuvable, ou réservée au propriétaire.")
        if data.espace_id == 0:
            schema = None
        else:
            from database import session_principale
            from models import Espace
            with session_principale() as pdb:
                schema = pdb.get(Espace, data.espace_id).schema
        if data.employe_id:
            raise HTTPException(400, "Pour lier un employé d'une autre station, entrez dans cette station.")
        jeton = definir_espace(schema)
        try:
            with session_espace(schema) as edb:
                out = _creer_compte(data.model_copy(update={"station_adresse": None}), edb)
        finally:
            retablir_espace(jeton)
        out["station_nom"] = cible["nom"]
        return out
    out = _creer_compte(data, db)
    out["station_nom"] = _nom_station_courante() or _nom_principale()
    return out


def _creer_compte(data: CompteIn, db: Session) -> dict:
    from admin_routes import _CODE_RE
    prenom, nom_famille = data.prenom.strip(), data.nom.strip()
    if not prenom or not nom_famille:
        raise HTTPException(400, "Le prénom et le nom sont requis.")
    nom = f"{prenom} {nom_famille}"
    pin = None if data.reprendre_pin else ((data.code_acces or "").strip() or _nouveau_pin())
    if pin is not None and not _CODE_RE.match(pin):
        raise HTTPException(400, "Le code PIN doit contenir exactement 9 chiffres.")
    if data.reprendre_pin and not data.username:
        raise HTTPException(400, "Le PIN ne peut être repris que pour un compte repris avec son code.")
    if data.username:
        # Reprise d'un compte (ex. depuis le staging) : le pompiste garde son code
        username = data.username.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,39}", username):
            raise HTTPException(400, "Code invalide : 3 à 40 caractères (lettres minuscules, chiffres, . _ -).")
        if db.query(Utilisateur).filter(Utilisateur.username.ilike(username)).first():
            raise HTTPException(409, f"Le code {username} existe déjà.")
        from espaces_routes import compte_ailleurs
        if compte_ailleurs(username=username):
            raise HTTPException(409, f"Le code {username} est déjà utilisé dans une autre station.")
    else:
        username = _identifiant_unique(db, prenom, nom_famille)
    station = _station_valide(db, data.station_adresse)
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
        code_acces_hash=hash_code_acces(pin) if pin else None,
        role="operateur", role_id=role.id, poste=role.nom, actif=True,
        station_adresse=station,
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
    out["code_acces"] = pin      # affiché une seule fois (None : PIN à reprendre)
    return out


class ComptePatch(BaseModel):
    actif: Optional[bool] = None
    nouveau_pin: bool = False
    station_adresse: Optional[str] = None   # "" = toutes les stations


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
    if data.nouveau_pin and u.compte_origine:
        raise HTTPException(400, "Ce pompiste vient d'une autre station : son PIN se change dans sa station d'origine.")
    if data.nouveau_pin:
        pin = _nouveau_pin()
        u.code_acces_hash = hash_code_acces(pin)
    if data.station_adresse is not None:
        u.station_adresse = _station_valide(db, data.station_adresse)
    db.commit()
    out = _compte_dict(db, u)
    if pin:
        out["code_acces"] = pin
    return out
