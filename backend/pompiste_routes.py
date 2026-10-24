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

import secrets
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
            meilleur, cle_m, src = float(r.metter_apres), cle, f"journal — {r.periode} du {r.date.strftime('%d/%m')}"
    q = (db.query(RapportPompisteLigne, RapportPompiste)
         .join(RapportPompiste, RapportPompisteLigne.rapport_id == RapportPompiste.id)
         .filter(RapportPompisteLigne.pompe_id == pompe_id,
                 RapportPompiste.statut == "SOUMIS",
                 RapportPompiste.date <= d, RapportPompiste.date >= d - timedelta(days=3)))
    if exclure_rapport_id:
        q = q.filter(RapportPompiste.id != exclure_rapport_id)
    for ligne, rap in q.all():
        cle = (rap.date, _RANG.get(rap.periode, 9))
        if cle < cible and (cle_m is None or cle > cle_m):
            meilleur, cle_m = float(ligne.metter_apres), cle
            src = f"rapport pompiste en attente — {rap.periode} du {rap.date.strftime('%d/%m')}"
    return meilleur, src


def _prix(db: Session, pompe: Pompe, d: date_type) -> float:
    import main  # import tardif : main importe ce module
    p = main._prix_vente_a_date(db, pompe.produit_id, d)
    return float(p) if p is not None else float(pompe.produit.prix_gallon or 0)


def _total_billetterie(billetterie: list, monnaie: float) -> float:
    total = 0.0
    for b in billetterie:
        total += int(b["coupure"]) * (int(b.get("liasses") or 0) * TAILLE_LIASSE + int(b.get("billets") or 0))
    return round(total + float(monnaie or 0), 2)


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
        }
        if avec_journal:
            j = (db.query(Releve).filter_by(date=r.date, periode=r.periode, pompe_id=l.pompe_id).first())
            d["journal"] = ({"metter_avant": float(j.metter_avant), "metter_apres": float(j.metter_apres),
                             "saisi_par": _nom(j.cree_par)} if j else None)
            prec, src = _index_precedent(db, l.pompe_id, r.date, r.periode, exclure_rapport_id=r.id)
            d["index_precedent"] = prec
            d["index_precedent_source"] = src
        lignes.append(d)
    return {
        "id": r.id, "date": str(r.date), "periode": r.periode, "statut": r.statut,
        "pompiste_id": r.utilisateur_id, "pompiste_nom": _nom(r.utilisateur),
        "charte_acceptee_at": r.charte_acceptee_at.isoformat() if r.charte_acceptee_at else None,
        "billetterie": r.billetterie or [], "monnaie": float(r.monnaie or 0),
        "montant_attendu": float(r.montant_attendu), "montant_compte": float(r.montant_compte),
        "ecart": float(r.ecart), "note_pompiste": r.note_pompiste,
        "soumis_at": r.soumis_at.isoformat() if r.soumis_at else None,
        "traite_par_nom": _nom(r.traite_par),
        "traite_at": r.traite_at.isoformat() if r.traite_at else None,
        "commentaire_superviseur": r.commentaire_superviseur,
        "lignes": lignes,
        "total_gallons": round(sum(x["gallons"] for x in lignes), 3),
    }


# ═════════════════════════════════════════════════════════════════════
# ESPACE POMPISTE
# ═════════════════════════════════════════════════════════════════════

def _valider_date(d: date_type) -> None:
    aujourd_hui = today_haiti()
    if d > aujourd_hui or d < aujourd_hui - timedelta(days=1):
        raise HTTPException(400, "Date invalide : seuls aujourd'hui et hier sont acceptés.")


@router.get("/api/pompiste/contexte")
def contexte(date: Optional[date_type] = None, periode: Optional[str] = None,
             u: Utilisateur = Depends(_require_pompiste), db: Session = Depends(get_db)):
    d = date or today_haiti()
    _valider_date(d)
    if periode is None:
        periode = "Matin" if now_haiti().hour < 13 else "Apres-midi"
    if periode not in PERIODES:
        raise HTTPException(400, "Période invalide")
    # Pompes déjà couvertes par un rapport soumis ou confirmé sur ce créneau
    prises = {}
    for l, rap in (db.query(RapportPompisteLigne, RapportPompiste)
                   .join(RapportPompiste, RapportPompisteLigne.rapport_id == RapportPompiste.id)
                   .filter(RapportPompiste.date == d, RapportPompiste.periode == periode,
                           RapportPompiste.statut.in_(("SOUMIS", "CONFIRME"))).all()):
        prises[l.pompe_id] = {"par": _nom(rap.utilisateur), "statut": rap.statut,
                              "moi": rap.utilisateur_id == u.id}
    produits = []
    for p in db.query(Produit).filter_by(actif=True).order_by(Produit.id).all():
        pompes = []
        for q in p.pompes:
            if not q.actif:
                continue
            prec, src = _index_precedent(db, q.id, d, periode)
            pompes.append({"id": q.id, "nom": q.nom, "index_precedent": prec,
                           "index_precedent_source": src, "deja_rapportee": prises.get(q.id)})
        if pompes:
            produits.append({"id": p.id, "nom": p.nom,
                             "prix_gallon": _prix(db, p.pompes[0], d) if p.pompes else float(p.prix_gallon or 0),
                             "pompes": pompes})
    return {
        "pompiste": {"id": u.id, "nom": _nom(u), "username": u.username},
        "date": str(d), "periode": periode, "aujourd_hui": str(today_haiti()),
        "produits": produits, "coupures": list(COUPURES), "taille_liasse": TAILLE_LIASSE,
    }


class LigneIn(BaseModel):
    pompe_id: int
    metter_avant: float
    metter_apres: float


class BilletIn(BaseModel):
    coupure: int
    liasses: int = 0
    billets: int = 0


class RapportIn(BaseModel):
    date: date_type
    periode: str
    charte_acceptee: bool
    lignes: list[LigneIn]
    billetterie: list[BilletIn] = []
    monnaie: float = 0
    note: Optional[str] = None


def _controler_lignes(db: Session, d: date_type, periode: str, lignes: list,
                      exclure_rapport_id: Optional[int] = None) -> list[dict]:
    """Contrôle des index. Lève 422 avec la liste des pompes en erreur
    (l'interface renvoie le pompiste à la saisie de ces pompes)."""
    erreurs, vues, sortie = [], set(), []
    for l in lignes:
        pompe = db.get(Pompe, l.pompe_id)
        if not pompe or not pompe.actif:
            erreurs.append({"pompe_id": l.pompe_id, "message": "Pompe introuvable ou inactive"})
            continue
        if l.pompe_id in vues:
            erreurs.append({"pompe_id": l.pompe_id, "message": f"{pompe.nom} : saisie en double"})
            continue
        vues.add(l.pompe_id)
        if l.metter_avant < 0 or l.metter_apres < 0:
            erreurs.append({"pompe_id": l.pompe_id, "message": f"{pompe.nom} : un index ne peut pas être négatif"})
            continue
        if l.metter_apres < l.metter_avant - TOLERANCE:
            erreurs.append({"pompe_id": l.pompe_id,
                            "message": f"{pompe.nom} : l'index de fin ({l.metter_apres:.3f}) est plus petit "
                                       f"que l'index de début ({l.metter_avant:.3f}) — un compteur ne recule pas"})
            continue
        if l.metter_apres - l.metter_avant > MAX_GALLONS_PAR_POMPE_PERIODE:
            erreurs.append({"pompe_id": l.pompe_id,
                            "message": f"{pompe.nom} : {l.metter_apres - l.metter_avant:,.0f} gallons sur une période, "
                                       "c'est anormal — vérifiez l'index (chiffre en trop ?)"})
            continue
        prec, src = _index_precedent(db, l.pompe_id, d, periode, exclure_rapport_id)
        if prec is not None and abs(prec - l.metter_avant) > TOLERANCE:
            erreurs.append({"pompe_id": l.pompe_id,
                            "message": f"{pompe.nom} : l'index de début ({l.metter_avant:.3f}) ne correspond pas "
                                       f"au dernier index connu ({prec:.3f}, {src})"})
            continue
        sortie.append({"pompe": pompe, "avant": l.metter_avant, "apres": l.metter_apres})
    if erreurs:
        raise HTTPException(422, {"message": "Index invalide : corrigez la saisie.", "erreurs": erreurs})
    return sortie


@router.post("/api/pompiste/rapports", status_code=201)
def soumettre(data: RapportIn, u: Utilisateur = Depends(_require_pompiste), db: Session = Depends(get_db)):
    if not data.charte_acceptee:
        raise HTTPException(400, "La charte de responsabilité doit être acceptée.")
    _valider_date(data.date)
    if data.periode not in PERIODES:
        raise HTTPException(400, "Période invalide")
    if not data.lignes:
        raise HTTPException(400, "Saisissez l'index d'au moins une pompe.")
    for b in data.billetterie:
        if b.coupure not in COUPURES:
            raise HTTPException(400, f"Coupure inconnue : {b.coupure}")
        if b.liasses < 0 or b.billets < 0:
            raise HTTPException(400, "Les quantités de billets ne peuvent pas être négatives.")
    if data.monnaie < 0:
        raise HTTPException(400, "La monnaie ne peut pas être négative.")

    lignes = _controler_lignes(db, data.date, data.periode, data.lignes)

    # Une pompe = un seul rapport actif par créneau
    deja = (db.query(RapportPompisteLigne, RapportPompiste)
            .join(RapportPompiste, RapportPompisteLigne.rapport_id == RapportPompiste.id)
            .filter(RapportPompiste.date == data.date, RapportPompiste.periode == data.periode,
                    RapportPompiste.statut.in_(("SOUMIS", "CONFIRME")),
                    RapportPompisteLigne.pompe_id.in_([x["pompe"].id for x in lignes])).all())
    if deja:
        noms = ", ".join(sorted({f"{l.pompe.nom} ({_nom(r.utilisateur)})" for l, r in deja}))
        raise HTTPException(409, f"Déjà rapporté sur ce créneau : {noms}.")

    billetterie = [b.model_dump() if hasattr(b, "model_dump") else b.dict()
                   for b in data.billetterie if (b.liasses or b.billets)]
    rap = RapportPompiste(
        date=data.date, periode=data.periode, utilisateur_id=u.id, statut="SOUMIS",
        charte_acceptee_at=now_haiti(), billetterie=billetterie,
        monnaie=round(data.monnaie, 2),
        note_pompiste=(data.note or "").strip()[:500] or None,
    )
    attendu = 0.0
    for x in lignes:
        prix = _prix(db, x["pompe"], data.date)
        attendu += (x["apres"] - x["avant"]) * prix
        rap.lignes.append(RapportPompisteLigne(pompe_id=x["pompe"].id, prix_gallon=prix,
                                               metter_avant=x["avant"], metter_apres=x["apres"]))
    compte = _total_billetterie(billetterie, data.monnaie)
    rap.montant_attendu = round(attendu, 2)
    rap.montant_compte = compte
    rap.ecart = round(compte - attendu, 2)
    db.add(rap)
    db.commit()
    db.refresh(rap)

    try:
        from notifications_service import creer_notification
        creer_notification(
            db, module="carburant", type_="rapport_pompiste",
            titre=f"Rapport pompiste à confirmer — {_nom(u)}",
            message=(f"{rap.periode.replace('Apres-midi', 'Après-midi')} du {rap.date.strftime('%d/%m')} · "
                     f"écart {float(rap.ecart):+,.0f} G"),
            lien="pompistes-rapports", dedupe_minutes=None,
        )
        db.commit()
    except Exception:
        db.rollback()
    return _rapport_dict(db, rap)


@router.get("/api/pompiste/rapports")
def mes_rapports(u: Utilisateur = Depends(_require_pompiste), db: Session = Depends(get_db)):
    rows = (db.query(RapportPompiste).filter_by(utilisateur_id=u.id)
            .order_by(RapportPompiste.id.desc()).limit(20).all())
    return [_rapport_dict(db, r) for r in rows]


# ═════════════════════════════════════════════════════════════════════
# SUPERVISION
# ═════════════════════════════════════════════════════════════════════

@router.get("/api/pompiste-supervision/rapports")
def lister(statut: Optional[str] = None, date_debut: Optional[date_type] = None,
           date_fin: Optional[date_type] = None,
           _s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    q = db.query(RapportPompiste)
    if statut:
        q = q.filter(RapportPompiste.statut == statut)
    if date_debut:
        q = q.filter(RapportPompiste.date >= date_debut)
    if date_fin:
        q = q.filter(RapportPompiste.date <= date_fin)
    rows = q.all()
    # À confirmer : ordre chronologique (la continuité des compteurs impose de
    # confirmer le Matin avant l'Après-midi). Historique : plus récents d'abord.
    cle = lambda r: (r.date, _RANG.get(r.periode, 9), r.id)
    rows.sort(key=cle, reverse=(statut != "SOUMIS"))
    return [_rapport_dict(db, r, avec_journal=(r.statut == "SOUMIS")) for r in rows[:200]]


@router.get("/api/pompiste-supervision/resume")
def resume(_s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    return {"a_confirmer": db.query(RapportPompiste).filter_by(statut="SOUMIS").count()}


class ConfirmerIn(BaseModel):
    lignes: list[LigneIn] = []          # vide = index du pompiste acceptés tels quels
    commentaire: Optional[str] = None
    motif_ecart_compteur: Optional[str] = None


@router.post("/api/pompiste-supervision/rapports/{rid}/confirmer")
def confirmer(rid: int, data: ConfirmerIn, request: Request,
              s: Utilisateur = Depends(_require_superviseur), db: Session = Depends(get_db)):
    import main
    rap = db.get(RapportPompiste, rid)
    if not rap:
        raise HTTPException(404, "Rapport introuvable")
    if rap.statut != "SOUMIS":
        raise HTTPException(409, "Ce rapport a déjà été traité.")

    corr = {l.pompe_id: l for l in data.lignes}
    inconnues = set(corr) - {l.pompe_id for l in rap.lignes}
    if inconnues:
        raise HTTPException(400, "Correction sur une pompe absente du rapport.")
    commentaire = (data.commentaire or "").strip()
    motif = (data.motif_ecart_compteur or "").strip()

    # 1. Valeurs finales + contrôles AVANT toute écriture (le journal est
    #    écrit pompe par pompe : on refuse tout plutôt que d'en écrire la moitié).
    finales, corrige = [], False
    for l in rap.lignes:
        c = corr.get(l.pompe_id)
        av = float(c.metter_avant) if c else float(l.metter_avant)
        ap = float(c.metter_apres) if c else float(l.metter_apres)
        change = abs(av - float(l.metter_avant)) > TOLERANCE or abs(ap - float(l.metter_apres)) > TOLERANCE
        corrige = corrige or change
        if av < 0 or ap < av - TOLERANCE:
            raise HTTPException(400, f"{l.pompe.nom} : index incohérents (fin < début).")
        prec = main._releve_precedent(db, l.pompe_id, rap.date, rap.periode)
        ecart = round(av - float(prec.metter_apres), 4) if prec else 0.0
        existant = db.query(Releve).filter_by(date=rap.date, periode=rap.periode, pompe_id=l.pompe_id).first()
        motif_existant = existant.motif_ecart_compteur if existant else None
        if abs(ecart) > main.TOLERANCE_ECART_COMPTEUR and len(motif) < main.MOTIF_ECART_MIN_CARACTERES \
                and not (motif_existant and existant and abs(float(existant.metter_avant) - av) <= TOLERANCE):
            raise HTTPException(409, {
                "message": (f"{l.pompe.nom} : l'index de début ({av:.3f}) ne suit pas le journal "
                            f"({float(prec.metter_apres):.3f}, {prec.periode} du {prec.date.strftime('%d/%m/%Y')}). "
                            "Corrigez l'index, confirmez d'abord le rapport précédent, "
                            "ou indiquez le motif de l'écart."),
                "motif_requis": True, "pompe_id": l.pompe_id})
        identique = existant and abs(float(existant.metter_avant) - av) <= TOLERANCE \
            and abs(float(existant.metter_apres) - ap) <= TOLERANCE
        if existant and not identique and existant.nb_modifications >= main.MAX_MODIFICATIONS_PAR_RELEVE:
            raise HTTPException(403, f"{l.pompe.nom} : le relevé du journal a atteint sa limite de modifications.")
        finales.append((l, av, ap, change, identique))
    if corrige and len(commentaire) < _COMMENTAIRE_MIN:
        raise HTTPException(400, "Expliquez la correction des index dans le commentaire.")

    # 2. Écriture dans le journal des compteurs — mêmes règles que la saisie
    #    normale (prix officiel, continuité, alertes de stock).
    employe = db.query(Employe).filter_by(utilisateur_id=rap.utilisateur_id).first()
    avertissements = []
    for l, av, ap, change, identique in finales:
        if change:
            l.metter_avant_corrige, l.metter_apres_corrige = av, ap
        if not identique:
            out = main.upsert_releve(main.ReleveIn(
                date=rap.date, periode=rap.periode, pompe_id=l.pompe_id,
                prix_gallon=float(l.prix_gallon), metter_avant=av, metter_apres=ap,
                motif_ecart_compteur=motif or None), request, db)
            avertissements += out.get("avertissements", [])
        if employe:
            r = db.query(Releve).filter_by(date=rap.date, periode=rap.periode, pompe_id=l.pompe_id).first()
            if r:
                r.pompiste_id = employe.id

    rap.statut = "CONFIRME"
    rap.traite_par_id = s.id
    rap.traite_at = now_haiti()
    rap.commentaire_superviseur = commentaire[:1000] or None
    db.commit()
    db.refresh(rap)
    out = _rapport_dict(db, rap)
    out["avertissements"] = avertissements
    return out


class RejeterIn(BaseModel):
    commentaire: str


@router.post("/api/pompiste-supervision/rapports/{rid}/rejeter")
def rejeter(rid: int, data: RejeterIn, s: Utilisateur = Depends(_require_superviseur),
            db: Session = Depends(get_db)):
    rap = db.get(RapportPompiste, rid)
    if not rap:
        raise HTTPException(404, "Rapport introuvable")
    if rap.statut != "SOUMIS":
        raise HTTPException(409, "Ce rapport a déjà été traité.")
    commentaire = (data.commentaire or "").strip()
    if len(commentaire) < _COMMENTAIRE_MIN:
        raise HTTPException(400, "Indiquez au pompiste ce qui ne va pas (commentaire obligatoire).")
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
    nom_complet: str
    username: str
    code_acces: Optional[str] = None     # vide = généré
    employe_id: Optional[int] = None


def _nouveau_pin() -> str:
    return "".join(secrets.choice("0123456789") for _ in range(9))


@router.post("/api/pompiste-supervision/comptes", status_code=201)
def creer_compte(data: CompteIn, _s: Utilisateur = Depends(_require_superviseur),
                 db: Session = Depends(get_db)):
    from admin_routes import _CODE_RE, _USERNAME_RE
    nom = data.nom_complet.strip()
    username = data.username.strip().lower()
    if not nom:
        raise HTTPException(400, "Le nom complet est requis.")
    if not _USERNAME_RE.match(username):
        raise HTTPException(400, "Identifiant invalide (3 à 60 caractères : lettres, chiffres, . _ -).")
    pin = (data.code_acces or "").strip() or _nouveau_pin()
    if not _CODE_RE.match(pin):
        raise HTTPException(400, "Le code PIN doit contenir exactement 9 chiffres.")
    if db.query(Utilisateur).filter(Utilisateur.username.ilike(username)).first():
        raise HTTPException(409, f"L'identifiant « {username} » est déjà utilisé.")
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
