"""
Achats Bar — fournisseurs, règlement, annulation.

Complète les routes d'achat de pos_routes.py :
  - Fiche fournisseur (table fournisseurs) : nom unique sans doublon de
    casse ou d'espaces, téléphone, conditions. Les anciens achats (texte
    libre) sont rattachés automatiquement au démarrage.
  - Règlement : COMPTANT (payé à l'achat) ou CREDIT (dette fournisseur,
    soldée par des paiements). Liste des dettes fournisseurs.
  - Annulation d'un achat avec motif, permise seulement tant qu'aucun de
    ses UL n'a été contrôlé à la réception (sinon : retour fournisseur).
"""
from __future__ import annotations

import re
from datetime import date as date_type, datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db
from models import BarAchat, BarAchatPaiement, BarColis, Fournisseur, Utilisateur
from tz_utils import today_haiti

router = APIRouter(prefix="/api/pos", tags=["Achats Bar"])

MODES_PAIEMENT = ("CASH", "VIREMENT", "CHEQUE", "ZELLE", "MONCASH", "AUTRE")


# ── Droits : la direction (pas les employés de caisse) ───────────────────

def _require_gestion_achats(request: Request, db: Session = Depends(get_db)) -> Utilisateur:
    """Annuler un achat ou enregistrer un paiement fournisseur : administrateur,
    PDG, ou rôle avec le domaine Bar en accès complet (manager, comptable…).
    Jamais un compte de caisse."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(403, "Non autorisé.")
    if user.role in ("admin", "pdg"):
        return user
    from pos_routes import est_employe_caisse
    u = db.get(Utilisateur, user.id)
    if u and not est_employe_caisse(request, db):
        perms = (u.role_obj.permissions or {}) if u.role_obj else {}
        if perms.get("admin") or perms.get("bar") == "complet":
            return u
        nom = ((u.role_obj.nom if u.role_obj else "") or u.poste or "").strip().lower()
        if nom in ("manager", "comptable", "directeur général", "directeur", "gérant", "gerant"):
            return u
    raise HTTPException(403, "Réservé à la direction (administrateur, manager, comptable).")


# ── Fournisseurs ──────────────────────────────────────────────────────────

def normaliser_nom(nom: str) -> str:
    return re.sub(r"\s+", " ", (nom or "").strip()).casefold()


def fournisseur_par_nom(db: Session, nom: Optional[str]) -> Optional[Fournisseur]:
    """Fiche du fournisseur de ce nom, créée si elle n'existe pas encore."""
    propre = re.sub(r"\s+", " ", (nom or "").strip())[:150]
    if not propre:
        return None
    norm = normaliser_nom(propre)
    f = db.query(Fournisseur).filter(Fournisseur.nom_norm == norm).first()
    if not f:
        f = Fournisseur(nom=propre, nom_norm=norm)
        db.add(f)
        db.flush()
    return f


def rattacher_fournisseurs(db: Session) -> int:
    """Rattache les achats saisis en texte libre à une fiche fournisseur
    (« Brana », « BRANA » et « brana » → une seule fiche). Idempotent."""
    n = 0
    for a in db.query(BarAchat).filter(BarAchat.fournisseur_id.is_(None),
                                       BarAchat.fournisseur.isnot(None)).limit(20000).all():
        f = fournisseur_par_nom(db, a.fournisseur)
        if f:
            a.fournisseur_id = f.id
            a.fournisseur = f.nom
            n += 1
    if n:
        db.commit()
    return n


def cout_total_achat(a: BarAchat) -> Decimal:
    """Coût de l'achat : marchandise réellement reçue + frais annexes."""
    qte = Decimal(str(a.quantite_recue if a.quantite_recue is not None else a.quantite))
    marchandise = qte * Decimal(str(a.prix_achat_unitaire))
    frais = sum((Decimal(str(d.montant)) for d in a.depenses), Decimal("0"))
    return (marchandise + frais).quantize(Decimal("0.01"))


def reglement_achat(a: BarAchat) -> dict:
    """État du règlement d'un achat (pour la liste et la fiche)."""
    total = cout_total_achat(a)
    if a.statut == "ANNULE":
        return {"etat": "ANNULE", "paye": 0.0, "reste": 0.0, "en_retard": False}
    if (a.mode_reglement or "COMPTANT") == "COMPTANT":
        return {"etat": "PAYE", "paye": float(total), "reste": 0.0, "en_retard": False}
    paye = sum((Decimal(str(p.montant)) for p in a.paiements), Decimal("0"))
    reste = max(total - paye, Decimal("0"))
    etat = "PAYE" if reste == 0 else ("PARTIEL" if paye > 0 else "A_PAYER")
    en_retard = bool(reste > 0 and a.date_echeance and a.date_echeance < today_haiti())
    return {"etat": etat, "paye": float(paye), "reste": float(reste), "en_retard": en_retard}


def _fournisseur_dict(f: Fournisseur, stats: Optional[dict] = None) -> dict:
    d = {"id": f.id, "nom": f.nom, "telephone": f.telephone, "adresse": f.adresse,
         "conditions": f.conditions, "notes": f.notes, "actif": f.actif}
    if stats is not None:
        d.update(stats)
    return d


class FournisseurIn(BaseModel):
    nom:        str = Field(min_length=2, max_length=150)
    telephone:  Optional[str] = Field(default=None, max_length=40)
    adresse:    Optional[str] = Field(default=None, max_length=200)
    conditions: Optional[str] = Field(default=None, max_length=200)
    notes:      Optional[str] = Field(default=None, max_length=300)
    actif:      bool = True


@router.get("/fournisseurs")
def lister_fournisseurs(db: Session = Depends(get_db)):
    """Fournisseurs avec, pour chacun : nombre d'achats, total acheté, reste à payer."""
    achats = (db.query(BarAchat).filter(BarAchat.fournisseur_id.isnot(None),
                                        BarAchat.statut != "ANNULE").all())
    stats: dict[int, dict] = {}
    for a in achats:
        s = stats.setdefault(a.fournisseur_id, {"nb_achats": 0, "total_achete": 0.0, "reste_a_payer": 0.0,
                                                "en_retard": 0, "dernier_achat": None})
        r = reglement_achat(a)
        s["nb_achats"] += 1
        s["total_achete"] += float(cout_total_achat(a))
        s["reste_a_payer"] += r["reste"]
        s["en_retard"] += 1 if r["en_retard"] else 0
        d = a.date_achat.isoformat() if a.date_achat else None
        if d and (not s["dernier_achat"] or d > s["dernier_achat"]):
            s["dernier_achat"] = d
    vide = {"nb_achats": 0, "total_achete": 0.0, "reste_a_payer": 0.0, "en_retard": 0, "dernier_achat": None}
    out = [_fournisseur_dict(f, {**vide, **stats.get(f.id, {})})
           for f in db.query(Fournisseur).order_by(Fournisseur.nom).all()]
    for d in out:
        d["total_achete"] = round(d["total_achete"], 2)
        d["reste_a_payer"] = round(d["reste_a_payer"], 2)
    return out


@router.post("/fournisseurs", status_code=201)
def creer_fournisseur(data: FournisseurIn, db: Session = Depends(get_db),
                      user=Depends(_require_gestion_achats)):
    norm = normaliser_nom(data.nom)
    existant = db.query(Fournisseur).filter(Fournisseur.nom_norm == norm).first()
    if existant:
        raise HTTPException(409, f"Le fournisseur « {existant.nom} » existe déjà.")
    f = Fournisseur(nom=re.sub(r"\s+", " ", data.nom.strip()), nom_norm=norm,
                    telephone=data.telephone, adresse=data.adresse,
                    conditions=data.conditions, notes=data.notes, actif=data.actif)
    db.add(f)
    db.commit()
    return _fournisseur_dict(f)


@router.put("/fournisseurs/{fournisseur_id}")
def modifier_fournisseur(fournisseur_id: int, data: FournisseurIn, db: Session = Depends(get_db),
                         user=Depends(_require_gestion_achats)):
    f = db.get(Fournisseur, fournisseur_id)
    if not f:
        raise HTTPException(404, "Fournisseur introuvable.")
    norm = normaliser_nom(data.nom)
    autre = db.query(Fournisseur).filter(Fournisseur.nom_norm == norm, Fournisseur.id != f.id).first()
    if autre:
        raise HTTPException(409, f"Un autre fournisseur s'appelle déjà « {autre.nom} ».")
    f.nom, f.nom_norm = re.sub(r"\s+", " ", data.nom.strip()), norm
    f.telephone, f.adresse, f.conditions, f.notes, f.actif = (
        data.telephone, data.adresse, data.conditions, data.notes, data.actif)
    # Le nom affiché sur les achats suit la fiche
    db.query(BarAchat).filter(BarAchat.fournisseur_id == f.id).update(
        {"fournisseur": f.nom}, synchronize_session=False)
    db.commit()
    return _fournisseur_dict(f)


# ── Règlement des achats à crédit ─────────────────────────────────────────

class PaiementIn(BaseModel):
    montant:   float = Field(gt=0, le=100_000_000)
    mode:      str = Field(default="CASH", max_length=20)
    reference: Optional[str] = Field(default=None, max_length=80)


@router.post("/achats/{achat_id}/paiements", status_code=201)
def payer_achat(achat_id: int, data: PaiementIn, db: Session = Depends(get_db),
                user=Depends(_require_gestion_achats)):
    """Enregistre un paiement sur un achat à crédit. Refuse un montant
    supérieur au reste à payer."""
    a = db.get(BarAchat, achat_id)
    if not a:
        raise HTTPException(404, "Achat introuvable.")
    if a.statut == "ANNULE":
        raise HTTPException(409, "Cet achat est annulé.")
    if (a.mode_reglement or "COMPTANT") != "CREDIT":
        raise HTTPException(409, "Cet achat a été payé comptant : il n'y a rien à régler.")
    mode = (data.mode or "CASH").upper()
    if mode not in MODES_PAIEMENT:
        raise HTTPException(422, f"Mode de paiement invalide : {', '.join(MODES_PAIEMENT)}.")
    reste = Decimal(str(reglement_achat(a)["reste"]))
    montant = Decimal(str(data.montant)).quantize(Decimal("0.01"))
    if montant > reste:
        raise HTTPException(422, f"Montant supérieur au reste à payer (G {float(reste):,.2f}).")
    db.add(BarAchatPaiement(achat_id=a.id, montant=montant, mode=mode,
                            reference=(data.reference or "").strip() or None, utilisateur_id=user.id))
    db.commit()
    db.refresh(a)
    return {"achat_id": a.id, **reglement_achat(a)}


@router.get("/dettes-fournisseurs")
def dettes_fournisseurs(db: Session = Depends(get_db)):
    """Achats à crédit non soldés, les plus urgents d'abord."""
    lignes = []
    for a in db.query(BarAchat).filter(BarAchat.mode_reglement == "CREDIT", BarAchat.statut != "ANNULE").all():
        r = reglement_achat(a)
        if r["reste"] <= 0:
            continue
        lignes.append({
            "achat_id": a.id, "fournisseur": a.fournisseur, "fournisseur_id": a.fournisseur_id,
            "numero_facture": a.numero_facture,
            "produit_nom": a.bar_produit.nom if a.bar_produit else (a.station_produit.nom if a.station_produit else None),
            "date_achat": a.date_achat.isoformat() if a.date_achat else None,
            "date_echeance": a.date_echeance.isoformat() if a.date_echeance else None,
            "cout_total": float(cout_total_achat(a)), **r,
        })
    lignes.sort(key=lambda x: (not x["en_retard"], x["date_echeance"] or "9999"))
    return {"total_du": round(sum(x["reste"] for x in lignes), 2),
            "en_retard": sum(1 for x in lignes if x["en_retard"]), "achats": lignes}


# ── Annulation ────────────────────────────────────────────────────────────

class AnnulationIn(BaseModel):
    motif: str = Field(min_length=5, max_length=250)


@router.post("/achats/{achat_id}/annuler")
def annuler_achat(achat_id: int, data: AnnulationIn, db: Session = Depends(get_db),
                  user=Depends(_require_gestion_achats)):
    """Annule un achat saisi par erreur. Permis tant qu'aucune UL n'est
    entré en stock (contrôlé à la réception) : ses UL encore « à contrôler » sont
    retirés et l'achat sort du coût moyen. Un achat payé à crédit dont un
    paiement est déjà enregistré ne peut pas être annulé (rembourser d'abord)."""
    from bar_depot_routes import _journaliser
    a = db.get(BarAchat, achat_id)
    if not a:
        raise HTTPException(404, "Achat introuvable.")
    if a.statut == "ANNULE":
        raise HTTPException(409, "Cet achat est déjà annulé.")
    motif = " ".join(data.motif.split())
    colis = db.query(BarColis).filter(BarColis.achat_id == a.id).all()
    entres = [c for c in colis if c.statut not in ("A_VERIFIER", "ANNULE")]
    if entres:
        raise HTTPException(409, f"{len(entres)} UL de cet achat sont déjà entrées en stock : "
                                 "l'achat ne peut plus être annulé. Faites un retour fournisseur au Dépôt Bar.")
    if a.paiements:
        raise HTTPException(409, "Un paiement est déjà enregistré sur cet achat : il ne peut pas être annulé.")
    n = (db.query(BarAchat).filter(BarAchat.id == a.id, BarAchat.statut != "ANNULE")
         .update({"statut": "ANNULE", "annule_le": datetime.now(timezone.utc),
                  "annule_par_id": user.id, "motif_annulation": motif[:255]}, synchronize_session=False))
    if n != 1:
        db.rollback()
        raise HTTPException(409, "Cet achat vient d'être annulé par quelqu'un d'autre.")
    retires = 0
    for c in colis:
        if c.statut == "A_VERIFIER":
            c.statut = "ANNULE"
            retires += 1
            _journaliser(db, "ANNULE", c, utilisateur_id=user.id, details=f"Achat #{a.id} annulé : {motif}"[:300])
    db.commit()
    return {"achat_id": a.id, "statut": "ANNULE", "colis_retires": retires, "motif": motif}
