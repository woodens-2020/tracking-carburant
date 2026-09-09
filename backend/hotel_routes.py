"""
Routes API section Hôtel — préfixe /api/hotel
Chambres · Employés · Réservations (nuit / moment)
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import List, Optional
from tz_utils import today_haiti

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db
from models import HotelChambre, HotelEmploye, HotelReservation, HotelDepense, HotelRapportNote, HotelProforma, RenflouementDepartement, Utilisateur, CategorieDepense
import listes_reference as lref

router = APIRouter(prefix="/api/hotel", tags=["Hotel"])


def _uid(request: Request) -> int | None:
    u = getattr(request.state, "user", None)
    return u.id if u else None


def _d(v) -> Decimal:
    return Decimal(str(v)) if v is not None else Decimal("0")


def _require_pdg_ou_admin_hotel(request: Request, db: Session = Depends(get_db)) -> Utilisateur:
    """Même logique que main.require_pdg_ou_admin — dupliquée ici pour
    éviter un import circulaire entre routers."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(403, "Non autorisé")
    if user.role in ("pdg", "admin"):
        return user
    if user.role_id:
        u = db.get(Utilisateur, user.id)
        if u and u.role_obj and u.role_obj.permissions.get("admin", False):
            return u
    raise HTTPException(403, "Accès réservé au PDG et aux administrateurs")


# ══════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════

def _chambre_dict(c: HotelChambre, nb_actives: int = 0) -> dict:
    return {
        "id":           c.id,
        "numero":       c.numero,
        "type_chambre": c.type_chambre,
        "etage":        c.etage,
        "capacite":     c.capacite,
        "prix_nuit":    float(_d(c.prix_nuit)),
        "prix_moment":  float(_d(c.prix_moment)) if c.prix_moment else None,
        "statut":       c.statut,
        "description":  c.description or "",
        "actif":        c.actif,
        "nb_reservations_actives": nb_actives,
    }


def _employe_dict(e: HotelEmploye) -> dict:
    return {
        "id":            e.id,
        "nom":           e.nom,
        "prenom":        e.prenom,
        "nom_complet":   f"{e.prenom} {e.nom}",
        "poste":         e.poste,
        "telephone":     e.telephone or "",
        "email":         e.email or "",
        "date_embauche": str(e.date_embauche) if e.date_embauche else None,
        "salaire_base":  float(_d(e.salaire_base)) if e.salaire_base else None,
        "actif":         e.actif,
        "notes":         e.notes or "",
    }


def _res_dict(r: HotelReservation) -> dict:
    return {
        "id":                 r.id,
        "chambre_id":         r.chambre_id,
        "chambre_numero":     r.chambre.numero if r.chambre else str(r.chambre_id),
        "chambre_type":       r.chambre.type_chambre if r.chambre else "",
        "client_nom":         r.client_nom,
        "client_contact":     r.client_contact or "",
        "client_id_piece":    r.client_id_piece or "",
        "type_sejour":        r.type_sejour,
        "date_arrivee":       r.date_arrivee.isoformat(),
        "date_depart_prevue": r.date_depart_prevue.isoformat(),
        "date_depart_reel":   r.date_depart_reel.isoformat() if r.date_depart_reel else None,
        "nb_nuits":           r.nb_nuits,
        "nb_heures":          float(_d(r.nb_heures)) if r.nb_heures else None,
        "prix_unitaire":      float(_d(r.prix_unitaire)),
        "montant_reference":  float(_d(r.prix_unitaire) * (r.nb_nuits or _d(r.nb_heures) or 1)),
        "montant_total":      float(_d(r.montant_total)),
        "remise":             max(0.0, float(_d(r.prix_unitaire) * (r.nb_nuits or _d(r.nb_heures) or 1) - _d(r.montant_total))),
        "montant_paye":       float(_d(r.montant_paye)),
        "solde":              float(_d(r.solde)),
        "statut":             r.statut,
        "mode_paiement":      r.mode_paiement or "",
        "notes":              r.notes or "",
        "employe_id":         r.employe_id,
        "employe_nom":        (f"{r.employe.prenom} {r.employe.nom}") if r.employe else None,
        "created_at":         r.created_at.isoformat(),
    }


# ══════════════════════════════════════════════════════════════════
# CHAMBRES
# ══════════════════════════════════════════════════════════════════

class ChambreIn(BaseModel):
    numero:       str
    type_chambre: str = "SIMPLE"
    etage:        Optional[int]   = None
    capacite:     int             = 1
    prix_nuit:    float           = Field(gt=0)
    prix_moment:  Optional[float] = Field(default=None, gt=0)
    description:  Optional[str]   = None
    actif:        bool            = True


@router.get("/chambres")
def liste_chambres(actif: Optional[bool] = Query(default=None), db: Session = Depends(get_db)):
    q = db.query(HotelChambre)
    if actif is not None:
        q = q.filter(HotelChambre.actif == actif)
    chambres = q.order_by(HotelChambre.numero).all()
    actives_map = {
        r.chambre_id: r.count
        for r in db.query(
            HotelReservation.chambre_id,
            func.count(HotelReservation.id).label("count")
        ).filter(HotelReservation.statut == "EN_COURS")
         .group_by(HotelReservation.chambre_id)
         .all()
    }
    return [_chambre_dict(c, actives_map.get(c.id, 0)) for c in chambres]


@router.get("/chambres/disponibles")
def chambres_disponibles(db: Session = Depends(get_db)):
    chambres = (
        db.query(HotelChambre)
        .filter(HotelChambre.statut == "DISPONIBLE", HotelChambre.actif == True)
        .order_by(HotelChambre.numero)
        .all()
    )
    return [_chambre_dict(c) for c in chambres]


@router.post("/chambres", status_code=201)
def creer_chambre(data: ChambreIn, db: Session = Depends(get_db)):
    num = data.numero.strip().upper()
    if db.query(HotelChambre).filter_by(numero=num).first():
        raise HTTPException(409, f"Chambre « {num} » existe déjà.")
    if data.type_chambre not in ("SIMPLE", "DOUBLE", "SUITE", "VIP"):
        raise HTTPException(422, "type_chambre doit être SIMPLE, DOUBLE, SUITE ou VIP.")
    c = HotelChambre(
        numero       = num,
        type_chambre = data.type_chambre,
        etage        = data.etage,
        capacite     = data.capacite,
        prix_nuit    = Decimal(str(data.prix_nuit)),
        prix_moment  = Decimal(str(data.prix_moment)) if data.prix_moment else None,
        statut       = "DISPONIBLE",
        description  = data.description,
        actif        = data.actif,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return _chambre_dict(c)


@router.put("/chambres/{chambre_id}")
def modifier_chambre(chambre_id: int, data: ChambreIn, db: Session = Depends(get_db)):
    c = db.query(HotelChambre).filter_by(id=chambre_id).first()
    if not c:
        raise HTTPException(404, "Chambre introuvable.")
    num = data.numero.strip().upper()
    doublon = db.query(HotelChambre).filter(
        HotelChambre.numero == num, HotelChambre.id != chambre_id
    ).first()
    if doublon:
        raise HTTPException(409, f"Chambre « {num} » existe déjà.")
    c.numero       = num
    c.type_chambre = data.type_chambre
    c.etage        = data.etage
    c.capacite     = data.capacite
    c.prix_nuit    = Decimal(str(data.prix_nuit))
    c.prix_moment  = Decimal(str(data.prix_moment)) if data.prix_moment else None
    c.description  = data.description
    c.actif        = data.actif
    db.commit()
    return _chambre_dict(c)


@router.patch("/chambres/{chambre_id}/statut")
def changer_statut_chambre(chambre_id: int, statut: str = Query(...), db: Session = Depends(get_db)):
    if statut not in ("DISPONIBLE", "OCCUPEE", "MAINTENANCE", "FERMEE"):
        raise HTTPException(422, "Statut invalide.")
    c = db.query(HotelChambre).filter_by(id=chambre_id).first()
    if not c:
        raise HTTPException(404, "Chambre introuvable.")
    c.statut = statut
    db.commit()
    return {"ok": True, "statut": c.statut}


@router.delete("/chambres/{chambre_id}", status_code=200)
def supprimer_chambre(chambre_id: int, db: Session = Depends(get_db)):
    from sqlalchemy.exc import IntegrityError
    c = db.query(HotelChambre).filter_by(id=chambre_id).first()
    if not c:
        raise HTTPException(404, "Chambre introuvable.")
    nb = db.query(HotelReservation).filter_by(chambre_id=chambre_id).count()
    if nb > 0:
        raise HTTPException(409, f"Impossible : {nb} réservation(s) liées à cette chambre.")
    try:
        db.delete(c)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Chambre utilisée — impossible de supprimer.")
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════
# EMPLOYÉS HÔTEL
# ══════════════════════════════════════════════════════════════════

class EmployeHotelIn(BaseModel):
    nom:           str
    prenom:        str
    poste:         str = "RECEPTIONNISTE"
    telephone:     Optional[str]  = None
    email:         Optional[str]  = None
    date_embauche: Optional[str]  = None
    salaire_base:  Optional[float] = None
    actif:         bool           = True
    notes:         Optional[str]  = None


@router.get("/employes")
def liste_employes(actif: Optional[bool] = Query(default=None), db: Session = Depends(get_db)):
    q = db.query(HotelEmploye)
    if actif is not None:
        q = q.filter(HotelEmploye.actif == actif)
    return [_employe_dict(e) for e in q.order_by(HotelEmploye.nom).all()]


@router.post("/employes", status_code=201)
def creer_employe(data: EmployeHotelIn, db: Session = Depends(get_db)):
    postes_valides = ("RECEPTIONNISTE", "FEMME_DE_CHAMBRE", "GERANT", "SECURITE", "AUTRE")
    if data.poste not in postes_valides:
        raise HTTPException(422, f"poste doit être parmi {postes_valides}.")
    from datetime import date as dt_type
    emb = None
    if data.date_embauche:
        try:
            emb = dt_type.fromisoformat(data.date_embauche)
        except ValueError:
            raise HTTPException(422, "date_embauche invalide (YYYY-MM-DD).")
    e = HotelEmploye(
        nom           = data.nom.strip(),
        prenom        = data.prenom.strip(),
        poste         = data.poste,
        telephone     = data.telephone,
        email         = data.email,
        date_embauche = emb,
        salaire_base  = Decimal(str(data.salaire_base)) if data.salaire_base else None,
        actif         = data.actif,
        notes         = data.notes,
    )
    db.add(e)
    db.commit()
    db.refresh(e)
    return _employe_dict(e)


@router.put("/employes/{employe_id}")
def modifier_employe(employe_id: int, data: EmployeHotelIn, db: Session = Depends(get_db)):
    e = db.query(HotelEmploye).filter_by(id=employe_id).first()
    if not e:
        raise HTTPException(404, "Employé introuvable.")
    from datetime import date as dt_type
    emb = None
    if data.date_embauche:
        try:
            emb = dt_type.fromisoformat(data.date_embauche)
        except ValueError:
            raise HTTPException(422, "date_embauche invalide.")
    e.nom           = data.nom.strip()
    e.prenom        = data.prenom.strip()
    e.poste         = data.poste
    e.telephone     = data.telephone
    e.email         = data.email
    e.date_embauche = emb
    e.salaire_base  = Decimal(str(data.salaire_base)) if data.salaire_base else None
    e.actif         = data.actif
    e.notes         = data.notes
    db.commit()
    return _employe_dict(e)


@router.delete("/employes/{employe_id}", status_code=200)
def supprimer_employe(employe_id: int, db: Session = Depends(get_db)):
    e = db.query(HotelEmploye).filter_by(id=employe_id).first()
    if not e:
        raise HTTPException(404, "Employé introuvable.")
    e.actif = False
    db.commit()
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════
# RÉSERVATIONS
# ══════════════════════════════════════════════════════════════════

class ReservationIn(BaseModel):
    chambre_id:      int
    client_nom:      str
    client_contact:  Optional[str] = None
    client_id_piece: str
    type_sejour:     str           = "NUIT"      # NUIT | MOMENT
    date_arrivee:    str                         # ISO datetime
    nb_nuits:        Optional[int]  = None       # si NUIT
    nb_heures:       Optional[float] = None      # si MOMENT
    montant_facture: Optional[float] = None      # montant réellement facturé (rabais PDG) — fait foi
    montant_paye:    float          = 0
    mode_paiement:   Optional[str]  = None
    employe_id:      Optional[int]  = None
    notes:           Optional[str]  = None


class PaiementIn(BaseModel):
    montant:       float = Field(gt=0)
    mode_paiement: Optional[str] = None


@router.get("/reservations")
def liste_reservations(
    statut:     Optional[str] = Query(default=None),
    chambre_id: Optional[int] = Query(default=None),
    db: Session = Depends(get_db),
):
    q = db.query(HotelReservation).order_by(HotelReservation.date_arrivee.desc())
    if statut:
        q = q.filter(HotelReservation.statut == statut.upper())
    if chambre_id:
        q = q.filter(HotelReservation.chambre_id == chambre_id)
    return [_res_dict(r) for r in q.limit(300).all()]


@router.get("/reservations/en-cours")
def reservations_en_cours(db: Session = Depends(get_db)):
    res = (
        db.query(HotelReservation)
        .filter(HotelReservation.statut == "EN_COURS")
        .order_by(HotelReservation.date_arrivee)
        .all()
    )
    return [_res_dict(r) for r in res]


@router.get("/reservations/{res_id}")
def detail_reservation(res_id: int, db: Session = Depends(get_db)):
    r = db.query(HotelReservation).filter_by(id=res_id).first()
    if not r:
        raise HTTPException(404, "Réservation introuvable.")
    return _res_dict(r)


@router.post("/reservations", status_code=201)
def creer_reservation(data: ReservationIn, request: Request, db: Session = Depends(get_db)):
    chambre = db.query(HotelChambre).filter_by(id=data.chambre_id).first()
    if not chambre:
        raise HTTPException(404, "Chambre introuvable.")
    if chambre.statut != "DISPONIBLE":
        raise HTTPException(409, f"Chambre {chambre.numero} n'est pas disponible (statut : {chambre.statut}).")

    if not data.client_nom.strip():
        raise HTTPException(422, "Nom du client requis.")
    if not data.client_id_piece.strip():
        raise HTTPException(422, "NIF / Pièce d'identité requise.")
    if data.type_sejour not in ("NUIT", "MOMENT"):
        raise HTTPException(422, "type_sejour doit être NUIT ou MOMENT.")

    try:
        date_arr = datetime.fromisoformat(data.date_arrivee.replace("Z", "+00:00"))
        if date_arr.tzinfo is None:
            date_arr = date_arr.replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(422, "date_arrivee invalide (ISO 8601).")

    if data.type_sejour == "NUIT":
        if not data.nb_nuits or data.nb_nuits < 1:
            raise HTTPException(422, "nb_nuits requis (≥ 1) pour séjour NUIT.")
        prix_unit = _d(chambre.prix_nuit)
        nb_nuits  = data.nb_nuits
        nb_heures = None
        date_dep  = date_arr + timedelta(days=nb_nuits)
        montant_ref = prix_unit * nb_nuits
    else:  # MOMENT
        if not data.nb_heures or data.nb_heures <= 0:
            raise HTTPException(422, "nb_heures requis (> 0) pour séjour MOMENT.")
        if not chambre.prix_moment:
            raise HTTPException(422, f"La chambre {chambre.numero} n'a pas de prix moment configuré.")
        prix_unit = _d(chambre.prix_moment)
        nb_nuits  = None
        nb_heures = Decimal(str(data.nb_heures))
        date_dep  = date_arr + timedelta(hours=float(nb_heures))
        montant_ref = prix_unit * nb_heures

    # Le montant facturé saisi par la réception fait foi (le PDG accorde
    # parfois un rabais à certains clients). Robuste : une saisie absente /
    # vide / ≤ 0 / illisible retombe sur le prix de référence calculé.
    montant = montant_ref
    if data.montant_facture is not None:
        try:
            mf = Decimal(str(data.montant_facture))
            if mf > 0:
                montant = mf
        except (ValueError, ArithmeticError):
            pass

    montant_paye = min(Decimal(str(data.montant_paye)), montant)
    if montant_paye < 0:
        montant_paye = Decimal("0")
    solde        = montant - montant_paye

    r = HotelReservation(
        chambre_id         = chambre.id,
        client_nom         = data.client_nom.strip(),
        client_contact     = data.client_contact,
        client_id_piece    = data.client_id_piece,
        type_sejour        = data.type_sejour,
        date_arrivee       = date_arr,
        date_depart_prevue = date_dep,
        nb_nuits           = nb_nuits,
        nb_heures          = nb_heures,
        prix_unitaire      = prix_unit,
        montant_total      = montant,
        montant_paye       = montant_paye,
        solde              = solde,
        statut             = "EN_COURS",
        mode_paiement      = data.mode_paiement,
        employe_id         = data.employe_id,
        notes              = data.notes,
    )
    db.add(r)

    # Marquer la chambre comme occupée
    chambre.statut = "OCCUPEE"

    _remise_txt = ""
    if montant < montant_ref:
        _remise_txt = f" · rabais {montant_ref - montant:g} G (réf. {montant_ref:g} G)"
    from notifications_service import creer_notification
    creer_notification(
        db, module="hotel", type_="nouvelle_reservation",
        titre=f"Nouvelle réservation — {r.client_nom}",
        message=f"Chambre {chambre.numero} · {r.type_sejour} · {montant:g} G{_remise_txt}",
        lien="hotel-reservation",
        dedupe_minutes=None,
    )

    db.commit()
    db.refresh(r)
    return _res_dict(r)


@router.post("/reservations/{res_id}/paiement", status_code=200)
def ajouter_paiement(res_id: int, data: PaiementIn, db: Session = Depends(get_db)):
    r = db.query(HotelReservation).filter_by(id=res_id).first()
    if not r:
        raise HTTPException(404, "Réservation introuvable.")
    if r.statut != "EN_COURS":
        raise HTTPException(409, "Réservation non active.")
    montant = Decimal(str(data.montant))
    r.montant_paye += montant
    r.solde = max(Decimal("0"), r.montant_total - r.montant_paye)
    if data.mode_paiement:
        r.mode_paiement = data.mode_paiement
    db.commit()
    return _res_dict(r)


@router.post("/reservations/{res_id}/terminer", status_code=200)
def terminer_reservation(res_id: int, db: Session = Depends(get_db)):
    r = db.query(HotelReservation).filter_by(id=res_id).first()
    if not r:
        raise HTTPException(404, "Réservation introuvable.")
    if r.statut != "EN_COURS":
        raise HTTPException(409, "Réservation déjà terminée ou annulée.")
    r.statut          = "TERMINEE"
    r.date_depart_reel = datetime.now(tz=timezone.utc)
    # Remettre la chambre disponible
    if r.chambre:
        r.chambre.statut = "DISPONIBLE"
    db.commit()
    return _res_dict(r)


@router.post("/reservations/{res_id}/annuler", status_code=200)
def annuler_reservation(res_id: int, db: Session = Depends(get_db)):
    r = db.query(HotelReservation).filter_by(id=res_id).first()
    if not r:
        raise HTTPException(404, "Réservation introuvable.")
    if r.statut != "EN_COURS":
        raise HTTPException(409, "Réservation déjà terminée ou annulée.")
    r.statut = "ANNULEE"
    r.date_depart_reel = datetime.now(tz=timezone.utc)
    if r.chambre:
        r.chambre.statut = "DISPONIBLE"
    db.commit()
    return _res_dict(r)


# ══════════════════════════════════════════════════════════════════
# DOCUMENTS IMPRIMABLES — reçu client / pro forma
# ══════════════════════════════════════════════════════════════════

def _fmt_g(v) -> str:
    return f"{float(v or 0):,.2f}".replace(",", " ") + " G"


def _dt_fr(dt) -> str:
    """Datetime → 'JJ/MM/AAAA à HHhMM' en heure d'Haïti."""
    if not dt:
        return "—"
    try:
        from tz_utils import HAITI_TZ
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(HAITI_TZ)
    except Exception:
        pass
    return dt.strftime("%d/%m/%Y à %Hh%M")


def _branding_lignes() -> tuple[str, list[str]]:
    """(nom, [lignes sous-titre]) de l'institution — vide si le tenant n'a
    pas personnalisé son identité (évite d'imprimer « NATIVITE » ailleurs)."""
    try:
        from main import _branding_val
        nom = _branding_val("nom")
        sous = [x for x in (_branding_val("raison_sociale"), _branding_val("complexe"),
                            _branding_val("adresse")) if x]
        tel = " · ".join(x for x in (_branding_val("telephone1"), _branding_val("telephone2")) if x)
        if tel:
            sous.append("Tél : " + tel)
        return nom, sous
    except Exception:
        return "", []


def _pdf_entete(story, ACCENT):
    """Ajoute l'en-tête institution (nom + sous-titres + filet) au `story`."""
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph, HRFlowable
    from reportlab.lib.enums import TA_CENTER
    DARK = colors.HexColor("#1A1A2E")
    GREY = colors.HexColor("#6B7280")
    st_org  = ParagraphStyle("org",  fontSize=13.5, fontName="Helvetica-Bold",
                             textColor=DARK, alignment=TA_CENTER, spaceAfter=2)
    st_orgs = ParagraphStyle("orgs", fontSize=8.5, textColor=GREY,
                             alignment=TA_CENTER, leading=11)
    nom, sous = _branding_lignes()
    if nom:
        story.append(Paragraph(nom, st_org))
    for l in sous:
        story.append(Paragraph(l, st_orgs))
    story.append(HRFlowable(width="100%", thickness=1.4, color=ACCENT,
                            spaceBefore=8 if nom else 0, spaceAfter=4))


@router.get("/reservations/{res_id}/fiche.pdf")
def fiche_reservation_pdf(res_id: int, db: Session = Depends(get_db)):
    """Reçu client imprimable pour un enregistrement (Moment ou Nuit) :
    en-tête institution, infos client, séjour, montant facturé (rabais
    éventuel visible), payé / solde, espace signature."""
    r = db.query(HotelReservation).filter_by(id=res_id).first()
    if not r:
        raise HTTPException(404, "Réservation introuvable.")

    import io
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import cm
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
    from reportlab.lib.enums import TA_CENTER

    DARK   = colors.HexColor("#1A1A2E")
    ACCENT = colors.HexColor("#10b981")
    GREY   = colors.HexColor("#6B7280")

    d = _res_dict(r)
    est_moment = r.type_sejour == "MOMENT"
    duree = f"{r.nb_nuits} nuit(s)" if not est_moment else f"{d['nb_heures'] or 0:g} h"
    ref   = d["montant_reference"]
    remise = d["remise"]

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=1.9*cm, rightMargin=1.9*cm,
                            topMargin=1.6*cm, bottomMargin=1.6*cm,
                            title=f"Reçu — {r.client_nom}")
    st_title = ParagraphStyle("title", fontSize=17, fontName="Helvetica-Bold",
                              textColor=DARK, alignment=TA_CENTER, spaceBefore=4, spaceAfter=3)
    st_sub   = ParagraphStyle("sub", fontSize=9.5, textColor=GREY, alignment=TA_CENTER, spaceAfter=6)
    st_sec   = ParagraphStyle("sec", fontSize=10.5, fontName="Helvetica-Bold",
                              textColor=ACCENT, spaceBefore=13, spaceAfter=5)
    st_body  = ParagraphStyle("body", fontSize=9.5, textColor=DARK, leading=13)

    story = []
    _pdf_entete(story, ACCENT)
    story.append(Paragraph("REÇU CLIENT", st_title))
    story.append(Paragraph(
        f"{'Séjour Moment' if est_moment else 'Séjour Nuit'} &nbsp;—&nbsp; N&deg; {r.id} "
        f"&nbsp;—&nbsp; émis le {_dt_fr(datetime.now(timezone.utc))}", st_sub))

    story.append(Paragraph("Client", st_sec))
    cli_rows = [
        ["Nom complet", r.client_nom],
        ["Contact", r.client_contact or "—"],
        ["NIF / Pièce", r.client_id_piece or "—"],
    ]
    t_cli = Table(cli_rows, colWidths=[4.6*cm, 12.6*cm])
    t_cli.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("TEXTCOLOR", (0, 0), (0, -1), GREY),
        ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, 0), (-1, -2), 0.3, colors.HexColor("#E5E7EB")),
    ]))
    story.append(t_cli)

    story.append(Paragraph("Séjour", st_sec))
    sej_rows = [
        ["Chambre", f"{d['chambre_numero']}  ({d['chambre_type'] or '—'})"],
        ["Arrivée", _dt_fr(r.date_arrivee)],
        ["Départ prévu", _dt_fr(r.date_depart_prevue)],
        ["Durée", duree],
    ]
    t_sej = Table(sej_rows, colWidths=[4.6*cm, 12.6*cm])
    t_sej.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("TEXTCOLOR", (0, 0), (0, -1), GREY),
        ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, 0), (-1, -2), 0.3, colors.HexColor("#E5E7EB")),
    ]))
    story.append(t_sej)

    story.append(Paragraph("Montant", st_sec))
    mont_rows = [["Libellé", "Montant"]]
    if remise > 0:
        mont_rows.append(["Tarif de référence", _fmt_g(ref)])
        mont_rows.append(["Rabais accordé", "- " + _fmt_g(remise)])
    mont_rows.append(["Payé", _fmt_g(d["montant_paye"])])
    mont_rows.append(["Solde restant", _fmt_g(d["solde"])])
    if r.mode_paiement:
        mont_rows.append(["Mode de paiement", r.mode_paiement])
    t_m = Table(mont_rows, colWidths=[11.2*cm, 6*cm])
    t_m.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), DARK),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#F7F7F9"), colors.white]),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D5D5DD")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5), ("TOPPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(t_m)
    story.append(Spacer(1, 5))

    net_tbl = Table([["MONTANT TOTAL FACTURÉ", _fmt_g(d["montant_total"])]], colWidths=[11.2*cm, 6*cm])
    net_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), ACCENT),
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.white),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 12),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("LEFTPADDING", (0, 0), (0, 0), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9), ("TOPPADDING", (0, 0), (-1, -1), 9),
    ]))
    story.append(net_tbl)

    if r.notes:
        story.append(Paragraph("Notes", st_sec))
        story.append(Paragraph(str(r.notes).replace("\n", "<br/>"), st_body))

    story.append(Spacer(1, 30))
    story.append(Paragraph(
        "Fait à ...................................................   le ......... / ......... / ..............",
        st_body))
    story.append(Spacer(1, 42))
    sign = Table([
        ["______________________________", "", "______________________________"],
        ["Signature du client", "", "Signature de la réception"],
    ], colWidths=[7.3*cm, 2.6*cm, 7.3*cm])
    sign.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 1), (-1, 1), GREY),
        ("TOPPADDING", (0, 1), (-1, 1), 3),
    ]))
    story.append(sign)

    story.append(Spacer(1, 22))
    story.append(HRFlowable(width="100%", thickness=0.4, color=colors.grey))
    _nom, _ = _branding_lignes()
    story.append(Paragraph(
        f"Reçu généré le {_dt_fr(datetime.now(timezone.utc))}" + (f" — {_nom}" if _nom else ""),
        ParagraphStyle("foot", fontSize=7, textColor=colors.grey, alignment=TA_CENTER, spaceBefore=4)))

    doc.build(story)
    buf.seek(0)
    fname = f"recu_{r.client_nom}_{r.id}.pdf".replace(" ", "_")
    return StreamingResponse(buf, media_type="application/pdf",
                             headers={"Content-Disposition": f'inline; filename="{fname}"'})


# ══════════════════════════════════════════════════════════════════
# STATS / DASHBOARD
# ══════════════════════════════════════════════════════════════════

@router.get("/stats")
def stats_hotel(db: Session = Depends(get_db)):
    total_chambres   = db.query(HotelChambre).filter_by(actif=True).count()
    chambres_occup   = db.query(HotelChambre).filter_by(statut="OCCUPEE").count()
    chambres_dispo   = db.query(HotelChambre).filter_by(statut="DISPONIBLE", actif=True).count()
    sejours_actifs   = db.query(HotelReservation).filter_by(statut="EN_COURS").count()
    revenu_mois      = db.query(func.sum(HotelReservation.montant_paye)).filter(
        HotelReservation.statut.in_(["EN_COURS", "TERMINEE"]),
    ).scalar() or Decimal("0")
    solde_total      = db.query(func.sum(HotelReservation.solde)).filter(
        HotelReservation.statut == "EN_COURS"
    ).scalar() or Decimal("0")
    return {
        "total_chambres":  total_chambres,
        "chambres_occupees": chambres_occup,
        "chambres_dispo":  chambres_dispo,
        "taux_occupation": round(chambres_occup / total_chambres * 100, 1) if total_chambres else 0,
        "sejours_actifs":  sejours_actifs,
        "revenu_total":    float(revenu_mois),
        "solde_en_attente": float(solde_total),
    }


def _hotel_bornes(d_debut, d_fin) -> tuple[datetime, datetime]:
    return (
        datetime(d_debut.year, d_debut.month, d_debut.day, tzinfo=timezone.utc),
        datetime(d_fin.year,   d_fin.month,   d_fin.day, 23, 59, 59, tzinfo=timezone.utc),
    )


def _hotel_reservations_query(db: Session, dt_debut, dt_fin, type_sejour=None, type_chambre=None):
    """Requete de base des reservations arrivees dans la periode, avec filtres
    optionnels type de sejour (NUIT/MOMENT) et type de chambre (SIMPLE/DOUBLE/
    SUITE/VIP) — reutilisee par tous les blocs du dashboard pour rester coherents
    entre eux quels que soient les filtres actifs."""
    q = db.query(HotelReservation).filter(
        HotelReservation.date_arrivee >= dt_debut, HotelReservation.date_arrivee <= dt_fin
    )
    if type_sejour:
        q = q.filter(HotelReservation.type_sejour == type_sejour)
    if type_chambre:
        q = q.join(HotelChambre, HotelReservation.chambre_id == HotelChambre.id).filter(HotelChambre.type_chambre == type_chambre)
    return q


def _hotel_kpis_periode(db: Session, d_debut, d_fin, type_sejour=None, type_chambre=None) -> dict:
    """Revenu encaisse (montant_paye), montant facture (montant_total) et solde
    impaye des reservations de la periode — memes conventions que
    _get_rapport_data, pour que le tableau de bord et le rapport affichent
    toujours des chiffres identiques."""
    dt_debut, dt_fin = _hotel_bornes(d_debut, d_fin)
    reservations = _hotel_reservations_query(db, dt_debut, dt_fin, type_sejour, type_chambre).all()
    return {
        "revenu":  sum(float(_d(r.montant_paye or 0))  for r in reservations),
        "facture": sum(float(_d(r.montant_total or 0)) for r in reservations),
        "solde":   sum(float(_d(r.solde or 0))          for r in reservations),
        "nb":      len(reservations),
    }


def _bucket_evolution_hotel(evolution: list[dict], nb_jours: int) -> list[dict]:
    """Regroupe l'evolution quotidienne par semaine (<=90j) ou par mois (plus long)
    — meme seuil que l'Analyse Cuisine et le Rapport Analytique carburant."""
    if not evolution:
        return evolution
    from datetime import date as date_type
    mensuel = nb_jours > 90
    buckets: dict[str, dict] = {}
    for e in evolution:
        d = date_type.fromisoformat(e["date"])
        key = d.strftime("%Y-%m") if mensuel else (d - timedelta(days=d.weekday())).isoformat()
        b = buckets.setdefault(key, {"date": key, "revenu": 0.0})
        b["revenu"] += e["revenu"]
    result = sorted(buckets.values(), key=lambda x: x["date"])
    for r in result:
        r["revenu"] = round(r["revenu"], 2)
    return result


@router.get("/dashboard")
def dashboard_hotel(
    date_debut:   Optional[str] = Query(default=None),
    date_fin:     Optional[str] = Query(default=None),
    type_sejour:  Optional[str] = Query(default=None),
    type_chambre: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    """Tableau de bord hôtel : KPI (avec facturé/encaissé/impayé) + comparaison
    période précédente, répartition des chambres par statut/type, évolution du
    revenu, arrivées/départs du jour, départs à venir. Filtrable par type de
    séjour (NUIT/MOMENT) et type de chambre (SIMPLE/DOUBLE/SUITE/VIP). Agrège
    tout en un seul appel (même esprit que /api/gi/dashboard)."""
    from datetime import date as date_type

    type_sejour = type_sejour.strip().upper() if type_sejour else None
    if type_sejour not in ("NUIT", "MOMENT"):
        type_sejour = None
    type_chambre = type_chambre.strip().upper() if type_chambre else None
    if type_chambre not in ("SIMPLE", "DOUBLE", "SUITE", "VIP"):
        type_chambre = None

    today = today_haiti()
    try:
        d_debut = datetime.strptime(date_debut, "%Y-%m-%d").date() if date_debut else date_type(today.year, today.month, 1)
    except ValueError:
        d_debut = date_type(today.year, today.month, 1)
    try:
        d_fin = datetime.strptime(date_fin, "%Y-%m-%d").date() if date_fin else today
    except ValueError:
        d_fin = today
    if d_debut > d_fin:
        raise HTTPException(422, "date_debut doit précéder date_fin")

    dt_debut, dt_fin = _hotel_bornes(d_debut, d_fin)

    # ── Chambres (filtrées par type si demandé) ──
    _chambres_q = db.query(HotelChambre).filter_by(actif=True)
    if type_chambre:
        _chambres_q = _chambres_q.filter(HotelChambre.type_chambre == type_chambre)
    chambres = _chambres_q.all()
    total_chambres = len(chambres)
    chambres_occup = sum(1 for c in chambres if c.statut == "OCCUPEE")
    chambres_dispo = sum(1 for c in chambres if c.statut == "DISPONIBLE")
    chambres_maint = sum(1 for c in chambres if c.statut == "MAINTENANCE")

    _sejours_actifs_q = db.query(HotelReservation).filter_by(statut="EN_COURS")
    if type_sejour:
        _sejours_actifs_q = _sejours_actifs_q.filter(HotelReservation.type_sejour == type_sejour)
    if type_chambre:
        _sejours_actifs_q = _sejours_actifs_q.join(HotelChambre, HotelReservation.chambre_id == HotelChambre.id).filter(HotelChambre.type_chambre == type_chambre)
    sejours_actifs = _sejours_actifs_q.count()

    # ── KPI période + comparaison période précédente de durée égale ──
    kpi_periode = _hotel_kpis_periode(db, d_debut, d_fin, type_sejour, type_chambre)

    nb_jours   = (d_fin - d_debut).days + 1
    prev_fin   = d_debut - timedelta(days=1)
    prev_debut = prev_fin - timedelta(days=nb_jours - 1)
    kpi_precedent = _hotel_kpis_periode(db, prev_debut, prev_fin, type_sejour, type_chambre)

    # ── Caisse Hôtel : revenu réellement encaissé + renflouements - dépenses ──
    depenses_periode = float(
        db.query(func.sum(HotelDepense.montant))
        .filter(HotelDepense.date_depense >= dt_debut, HotelDepense.date_depense <= dt_fin)
        .scalar() or 0
    )
    renflouements_periode = float(
        db.query(func.sum(RenflouementDepartement.montant))
        .filter(RenflouementDepartement.departement == "HOTEL",
                RenflouementDepartement.date_renflouement >= dt_debut,
                RenflouementDepartement.date_renflouement <= dt_fin)
        .scalar() or 0
    )
    caisse_disponible = round(kpi_periode["revenu"] + renflouements_periode - depenses_periode, 2)

    # ── Répartition des chambres par statut (donut) ──
    statut_counts: dict[str, int] = {}
    for c in chambres:
        statut_counts[c.statut] = statut_counts.get(c.statut, 0) + 1
    statut_chambres = [{"statut": s, "nb": n} for s, n in statut_counts.items()]

    # ── Répartition par type de chambre (barres) — inutile si déjà filtré sur un type ──
    par_type: dict[str, dict] = {}
    for c in chambres:
        t = c.type_chambre or "SIMPLE"
        if t not in par_type:
            par_type[t] = {"type_chambre": t, "nb_chambres": 0, "nb_occupees": 0, "revenu": 0.0}
        par_type[t]["nb_chambres"] += 1
        if c.statut == "OCCUPEE":
            par_type[t]["nb_occupees"] += 1

    _revenu_type_q = (
        db.query(HotelChambre.type_chambre, func.sum(HotelReservation.montant_paye))
        .join(HotelReservation, HotelReservation.chambre_id == HotelChambre.id)
        .filter(HotelReservation.date_arrivee >= dt_debut, HotelReservation.date_arrivee <= dt_fin)
    )
    if type_sejour:
        _revenu_type_q = _revenu_type_q.filter(HotelReservation.type_sejour == type_sejour)
    if type_chambre:
        _revenu_type_q = _revenu_type_q.filter(HotelChambre.type_chambre == type_chambre)
    revenu_type_rows = _revenu_type_q.group_by(HotelChambre.type_chambre).all()
    for t, revenu in revenu_type_rows:
        t = t or "SIMPLE"
        if t not in par_type:
            par_type[t] = {"type_chambre": t, "nb_chambres": 0, "nb_occupees": 0, "revenu": 0.0}
        par_type[t]["revenu"] = float(revenu or 0)

    # ── Évolution quotidienne du revenu (courbe) ──
    reservations_periode = _hotel_reservations_query(db, dt_debut, dt_fin, type_sejour, type_chambre).all()
    evo_map: dict[str, float] = {}
    for r in reservations_periode:
        k = r.date_arrivee.date().isoformat()
        evo_map[k] = evo_map.get(k, 0.0) + float(_d(r.montant_paye or 0))
    evolution = [{"date": k, "revenu": round(v, 2)} for k, v in sorted(evo_map.items())]
    if nb_jours > 32:
        evolution = _bucket_evolution_hotel(evolution, nb_jours)

    # ── Arrivées & départs du jour ──
    dt_today, dt_today_fin = _hotel_bornes(today, today)
    _arrivees_q = db.query(HotelReservation).filter(
        HotelReservation.date_arrivee >= dt_today, HotelReservation.date_arrivee <= dt_today_fin
    )
    _departs_q = db.query(HotelReservation).filter(
        HotelReservation.date_depart_prevue >= dt_today,
        HotelReservation.date_depart_prevue <= dt_today_fin,
        HotelReservation.statut == "EN_COURS",
    )
    if type_sejour:
        _arrivees_q = _arrivees_q.filter(HotelReservation.type_sejour == type_sejour)
        _departs_q  = _departs_q.filter(HotelReservation.type_sejour == type_sejour)
    if type_chambre:
        _arrivees_q = _arrivees_q.join(HotelChambre, HotelReservation.chambre_id == HotelChambre.id).filter(HotelChambre.type_chambre == type_chambre)
        _departs_q  = _departs_q.join(HotelChambre, HotelReservation.chambre_id == HotelChambre.id).filter(HotelChambre.type_chambre == type_chambre)
    arrivees_jour = _arrivees_q.all()
    departs_jour  = _departs_q.all()
    arrivees_departs_jour = sorted(
        [
            {
                "reservation_id": r.id,
                "client_nom":     r.client_nom,
                "chambre_numero": r.chambre.numero if r.chambre else str(r.chambre_id),
                "heure":          r.date_arrivee.strftime("%H:%M"),
                "type":           "arrivee",
                "statut":         r.statut,
            }
            for r in arrivees_jour
        ] + [
            {
                "reservation_id": r.id,
                "client_nom":     r.client_nom,
                "chambre_numero": r.chambre.numero if r.chambre else str(r.chambre_id),
                "heure":          r.date_depart_prevue.strftime("%H:%M"),
                "type":           "depart",
                "statut":         r.statut,
            }
            for r in departs_jour
        ],
        key=lambda x: x["heure"],
    )

    # ── Départs à venir (séjours en cours, triés par date de départ prévue) ──
    _prochains_q = db.query(HotelReservation).filter(HotelReservation.statut == "EN_COURS")
    if type_sejour:
        _prochains_q = _prochains_q.filter(HotelReservation.type_sejour == type_sejour)
    if type_chambre:
        _prochains_q = _prochains_q.join(HotelChambre, HotelReservation.chambre_id == HotelChambre.id).filter(HotelChambre.type_chambre == type_chambre)
    prochains = _prochains_q.order_by(HotelReservation.date_depart_prevue.asc()).limit(10).all()
    departs_a_venir = [
        {
            "reservation_id":      r.id,
            "client_nom":          r.client_nom,
            "chambre_numero":      r.chambre.numero if r.chambre else str(r.chambre_id),
            "date_depart_prevue":  r.date_depart_prevue.isoformat(),
            "montant_total":       float(_d(r.montant_total)),
            "solde":               float(_d(r.solde)),
            "statut":              r.statut,
        }
        for r in prochains
    ]

    return {
        "periode":            {"debut": str(d_debut), "fin": str(d_fin)},
        "periode_precedente": {"debut": str(prev_debut), "fin": str(prev_fin)},
        "filtres":            {"type_sejour": type_sejour, "type_chambre": type_chambre},
        "kpi": {
            "total_chambres":       total_chambres,
            "chambres_occupees":    chambres_occup,
            "chambres_dispo":       chambres_dispo,
            "chambres_maintenance": chambres_maint,
            "taux_occupation":      round(chambres_occup / total_chambres * 100, 1) if total_chambres else 0.0,
            "sejours_actifs":       sejours_actifs,
            "nb_arrivees_periode":  kpi_periode["nb"],
            "revenu_periode":       round(kpi_periode["revenu"], 2),
            "revenu_periode_prec":  round(kpi_precedent["revenu"], 2),
            "montant_facture":      round(kpi_periode["facture"], 2),
            "solde_impaye":         round(kpi_periode["solde"], 2),
            "depenses_periode":     round(depenses_periode, 2),
            "renflouements_periode": round(renflouements_periode, 2),
            "caisse_disponible":    caisse_disponible,
        },
        "statut_chambres":       statut_chambres,
        "par_type_chambre":      sorted(par_type.values(), key=lambda x: x["revenu"], reverse=True),
        "evolution_revenu":      evolution,
        "arrivees_departs_jour": arrivees_departs_jour,
        "departs_a_venir":       departs_a_venir,
    }


def _get_rapport_data(
    db: Session,
    date_debut: Optional[str],
    date_fin: Optional[str],
    type_sejour: Optional[str] = None,
) -> dict:
    from datetime import date as date_type
    from collections import defaultdict

    # Filtre optionnel par type de séjour (NUIT | MOMENT) — aucune valeur = tous
    type_sejour = type_sejour.strip().upper() if type_sejour else None
    if type_sejour not in ("NUIT", "MOMENT"):
        type_sejour = None

    today = today_haiti()
    try:
        d_debut = datetime.strptime(date_debut, "%Y-%m-%d").date() if date_debut else date_type(today.year, today.month, 1)
    except ValueError:
        d_debut = date_type(today.year, today.month, 1)
    try:
        d_fin = datetime.strptime(date_fin, "%Y-%m-%d").date() if date_fin else today
    except ValueError:
        d_fin = today

    dt_debut = datetime(d_debut.year, d_debut.month, d_debut.day, tzinfo=timezone.utc)
    dt_fin   = datetime(d_fin.year,   d_fin.month,   d_fin.day, 23, 59, 59, tzinfo=timezone.utc)

    _res_query = db.query(HotelReservation).filter(
        HotelReservation.date_arrivee >= dt_debut,
        HotelReservation.date_arrivee <= dt_fin,
    )
    if type_sejour:
        _res_query = _res_query.filter(HotelReservation.type_sejour == type_sejour)
    reservations = _res_query.all()

    moments = [r for r in reservations if r.type_sejour == "MOMENT"]
    nuits   = [r for r in reservations if r.type_sejour == "NUIT"]

    def _sum(lst, field):
        return float(sum(_d(getattr(r, field) or 0) for r in lst))

    par_chambre: dict = defaultdict(lambda: {"nb": 0, "revenu": 0.0, "moments": 0, "nuits": 0})
    for r in reservations:
        ch = r.chambre.numero if r.chambre else str(r.chambre_id)
        par_chambre[ch]["nb"]     += 1
        par_chambre[ch]["revenu"] += float(_d(r.montant_paye or 0))
        par_chambre[ch]["moments"] += 1 if r.type_sejour == "MOMENT" else 0
        par_chambre[ch]["nuits"]   += 1 if r.type_sejour == "NUIT"   else 0

    dt_today     = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    dt_today_end = datetime(today.year, today.month, today.day, 23, 59, 59, tzinfo=timezone.utc)
    _today_query = db.query(HotelReservation).filter(
        HotelReservation.date_arrivee >= dt_today,
        HotelReservation.date_arrivee <= dt_today_end,
    )
    if type_sejour:
        _today_query = _today_query.filter(HotelReservation.type_sejour == type_sejour)
    today_res = _today_query.all()

    _actifs_query = db.query(HotelReservation).filter_by(statut="EN_COURS")
    _actifs_solde_query = db.query(func.sum(HotelReservation.solde)).filter_by(statut="EN_COURS")
    if type_sejour:
        _actifs_query       = _actifs_query.filter(HotelReservation.type_sejour == type_sejour)
        _actifs_solde_query = _actifs_solde_query.filter(HotelReservation.type_sejour == type_sejour)
    actifs_nb    = _actifs_query.count()
    actifs_solde = float(_actifs_solde_query.scalar() or 0)

    # Commentaires du rapport (une note par jour, saisie par la réception)
    notes = (
        db.query(HotelRapportNote)
        .filter(HotelRapportNote.date_rapport >= d_debut,
                HotelRapportNote.date_rapport <= d_fin)
        .order_by(HotelRapportNote.date_rapport.desc())
        .all()
    )

    return {
        "periode": {"debut": str(d_debut), "fin": str(d_fin)},
        "notes": [
            {
                "date":    str(n.date_rapport),
                "texte":   n.texte,
                "auteur":  n.auteur.nom_complet if n.auteur else None,
                "maj_le":  n.maj_le.isoformat() if n.maj_le else None,
            }
            for n in notes
        ],
        "kpis": {
            "nb_total":              len(reservations),
            "nb_moments":            len(moments),
            "nb_nuits":              len(nuits),
            "revenu_total":          _sum(reservations, "montant_paye"),
            "revenu_moments":        _sum(moments,      "montant_paye"),
            "revenu_nuits":          _sum(nuits,        "montant_paye"),
            "montant_total_facture": _sum(reservations, "montant_total"),
            "solde_impaye":          _sum(reservations, "solde"),
        },
        "aujourd_hui": {
            "nb":      len(today_res),
            "moments": sum(1 for r in today_res if r.type_sejour == "MOMENT"),
            "nuits":   sum(1 for r in today_res if r.type_sejour == "NUIT"),
            "revenu":  sum(float(_d(r.montant_paye or 0)) for r in today_res),
        },
        "actifs": {
            "nb":               actifs_nb,
            "solde_en_attente": actifs_solde,
        },
        "par_chambre": sorted(
            [{"numero": ch, **v} for ch, v in par_chambre.items()],
            key=lambda x: x["revenu"], reverse=True
        ),
    }


@router.get("/rapport")
def rapport_hotel(
    date_debut:  Optional[str] = Query(default=None),
    date_fin:    Optional[str] = Query(default=None),
    type_sejour: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    return _get_rapport_data(db, date_debut, date_fin, type_sejour)


class RapportNoteIn(BaseModel):
    date_rapport: str
    texte:        str


@router.put("/rapport/note")
def enregistrer_note_rapport(data: RapportNoteIn, request: Request, db: Session = Depends(get_db)):
    """Enregistre (ou remplace, ou efface si vide) le commentaire du rapport
    hôtel d'une journée. Visible par la direction dans le rapport et ses
    exports."""
    from datetime import date as _date
    try:
        d = _date.fromisoformat(data.date_rapport)
    except ValueError:
        raise HTTPException(400, "Date invalide (AAAA-MM-JJ).")
    texte = (data.texte or "").strip()
    user  = getattr(request.state, "user", None)
    n = db.query(HotelRapportNote).filter_by(date_rapport=d).first()
    if not texte:
        if n:
            db.delete(n)
            db.commit()
        return {"date": str(d), "texte": "", "supprimee": True}
    if n:
        n.texte = texte[:1000]
        n.auteur_id = user.id if user else None
    else:
        n = HotelRapportNote(date_rapport=d, texte=texte[:1000],
                             auteur_id=user.id if user else None)
        db.add(n)
    db.commit(); db.refresh(n)
    return {
        "date":   str(n.date_rapport),
        "texte":  n.texte,
        "auteur": n.auteur.nom_complet if n.auteur else None,
        "maj_le": n.maj_le.isoformat() if n.maj_le else None,
    }


@router.get("/rapport/pdf")
def rapport_hotel_pdf(
    date_debut:  Optional[str] = Query(default=None),
    date_fin:    Optional[str] = Query(default=None),
    type_sejour: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    import io
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import cm
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.enums import TA_CENTER

    data = _get_rapport_data(db, date_debut, date_fin, type_sejour)
    buf  = io.BytesIO()

    doc  = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=1.5*cm, rightMargin=1.5*cm,
        topMargin=1.5*cm, bottomMargin=1.5*cm,
    )
    styles    = getSampleStyleSheet()
    s_title   = ParagraphStyle("title", parent=styles["Title"], fontSize=18, spaceAfter=4)
    s_sub     = ParagraphStyle("sub",   parent=styles["Normal"], fontSize=10, textColor=colors.grey, spaceAfter=10)
    s_section = ParagraphStyle("sec",   parent=styles["Heading2"], fontSize=11, spaceBefore=14, spaceAfter=6, textColor=colors.HexColor("#1e3a5f"))

    HDR_BG = colors.HexColor("#1e3a5f")
    ALT_BG = colors.HexColor("#f0f4f8")
    GRID_C = colors.HexColor("#cccccc")

    def _gdes(v):
        return f"G {float(v):,.2f}".replace(",", " ")

    def _tbl(header, rows, widths):
        data_t = [header] + rows
        t = Table(data_t, colWidths=widths)
        t.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1,  0), HDR_BG),
            ("TEXTCOLOR",     (0, 0), (-1,  0), colors.white),
            ("FONTNAME",      (0, 0), (-1,  0), "Helvetica-Bold"),
            ("FONTSIZE",      (0, 0), (-1, -1), 9),
            ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
            ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.white, ALT_BG]),
            ("GRID",          (0, 0), (-1, -1), 0.25, GRID_C),
            ("TOPPADDING",    (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        return t

    elems = []
    _ts_norm = type_sejour.strip().upper() if type_sejour else None
    _filtre_label = {"NUIT": " — Nuits uniquement", "MOMENT": " — Moments uniquement"}.get(_ts_norm, "")
    elems.append(Paragraph("Rapport Hôtel", s_title))
    elems.append(Paragraph(f"Période : {data['periode']['debut']}  →  {data['periode']['fin']}{_filtre_label}", s_sub))

    auj = data["aujourd_hui"]
    elems.append(Paragraph("Aujourd'hui", s_section))
    elems.append(_tbl(
        ["Arrivées", "Moments", "Nuits", "Revenu"],
        [[str(auj["nb"]), str(auj["moments"]), str(auj["nuits"]), _gdes(auj["revenu"])]],
        [3.5*cm, 3.5*cm, 3.5*cm, 5*cm],
    ))

    k = data["kpis"]
    elems.append(Paragraph(f"Période : {data['periode']['debut']} → {data['periode']['fin']}", s_section))
    elems.append(_tbl(
        ["Total", "Moments", "Nuits", "Revenu total", "Impayé"],
        [[str(k["nb_total"]), str(k["nb_moments"]), str(k["nb_nuits"]),
          _gdes(k["revenu_total"]), _gdes(k["solde_impaye"])]],
        [2.5*cm, 2.5*cm, 2.5*cm, 5*cm, 4*cm],
    ))

    elems.append(Paragraph("Revenus par type", s_section))
    elems.append(_tbl(
        ["Type", "Séjours", "Revenu"],
        [
            ["Moments", str(k["nb_moments"]), _gdes(k["revenu_moments"])],
            ["Nuits",   str(k["nb_nuits"]),   _gdes(k["revenu_nuits"])],
        ],
        [5*cm, 4*cm, 7*cm],
    ))

    if data["par_chambre"]:
        elems.append(Paragraph("Performance par chambre", s_section))
        elems.append(_tbl(
            ["Chambre", "Total", "Moments", "Nuits", "Revenu"],
            [[ch["numero"], str(ch["nb"]), str(ch["moments"]), str(ch["nuits"]), _gdes(ch["revenu"])]
             for ch in data["par_chambre"]],
            [3.5*cm, 2.5*cm, 3*cm, 3*cm, 5*cm],
        ))

    if data.get("notes"):
        elems.append(Paragraph("Commentaires", s_section))
        for n in data["notes"]:
            meta = n["date"] + (f" — {n['auteur']}" if n.get("auteur") else "")
            elems.append(Paragraph(f"<b>{meta}</b>", ParagraphStyle(
                "noteMeta", parent=styles["Normal"], fontSize=9, textColor=colors.grey, spaceBefore=6)))
            elems.append(Paragraph(str(n["texte"]).replace("\n", "<br/>"), ParagraphStyle(
                "noteTxt", parent=styles["Normal"], fontSize=10, spaceAfter=4)))

    elems.append(Spacer(1, 0.5*cm))
    elems.append(Paragraph(
        f"Généré le {datetime.now(timezone.utc).strftime('%d/%m/%Y à %H:%M')} UTC",
        ParagraphStyle("footer", parent=styles["Normal"], fontSize=8, textColor=colors.grey),
    ))

    doc.build(elems)
    buf.seek(0)
    fname = f"rapport_hotel_{data['periode']['debut']}_{data['periode']['fin']}.pdf"
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


@router.get("/rapport/xlsx")
def rapport_hotel_xlsx(
    date_debut:  Optional[str] = Query(default=None),
    date_fin:    Optional[str] = Query(default=None),
    type_sejour: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    import io
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    data = _get_rapport_data(db, date_debut, date_fin, type_sejour)

    HDR_FILL = PatternFill("solid", fgColor="1E3A5F")
    ALT_FILL = PatternFill("solid", fgColor="F0F4F8")
    HDR_FONT = Font(bold=True, color="FFFFFF")
    BOLD     = Font(bold=True)
    _side    = Side(style="thin", color="BBBBBB")
    BORDER   = Border(left=_side, right=_side, top=_side, bottom=_side)

    def _gdes(v):
        return f"G {float(v):,.2f}".replace(",", " ")

    def _hdr(ws, row, cols):
        for ci, val in enumerate(cols, 1):
            c = ws.cell(row=row, column=ci, value=val)
            c.font = HDR_FONT; c.fill = HDR_FILL
            c.alignment = Alignment(horizontal="center"); c.border = BORDER

    def _row(ws, row, vals, alt=False):
        fill = ALT_FILL if alt else PatternFill()
        for ci, val in enumerate(vals, 1):
            c = ws.cell(row=row, column=ci, value=val)
            c.fill = fill; c.border = BORDER
            c.alignment = Alignment(horizontal="right" if isinstance(val, (int, float)) else "left")

    wb = openpyxl.Workbook()

    # ── Feuille 1 : Résumé ────────────────────────────────────────
    ws1 = wb.active
    ws1.title = "Résumé"
    ws1.column_dimensions["A"].width = 28
    ws1.column_dimensions["B"].width = 20

    r = 1
    t = ws1.cell(r, 1, "Rapport Hôtel"); t.font = Font(bold=True, size=14)
    ws1.merge_cells(f"A{r}:B{r}"); r += 1
    ws1.cell(r, 1, f"Période : {data['periode']['debut']} → {data['periode']['fin']}")
    ws1.merge_cells(f"A{r}:B{r}"); r += 2

    ws1.cell(r, 1, "Aujourd'hui").font = BOLD; r += 1
    auj = data["aujourd_hui"]
    for lbl, val in [("Arrivées du jour", auj["nb"]), ("Moments", auj["moments"]),
                     ("Nuits", auj["nuits"]), ("Revenu du jour", _gdes(auj["revenu"]))]:
        ws1.cell(r, 1, lbl); ws1.cell(r, 2, val); r += 1

    r += 1
    ws1.cell(r, 1, "Période").font = BOLD; r += 1
    k = data["kpis"]
    for lbl, val in [
        ("Total séjours", k["nb_total"]), ("Moments", k["nb_moments"]), ("Nuits", k["nb_nuits"]),
        ("Revenu total",  _gdes(k["revenu_total"])),
        ("Rev. Moments",  _gdes(k["revenu_moments"])),
        ("Rev. Nuits",    _gdes(k["revenu_nuits"])),
        ("Montant facturé", _gdes(k["montant_total_facture"])),
        ("Impayé",          _gdes(k["solde_impaye"])),
    ]:
        ws1.cell(r, 1, lbl); ws1.cell(r, 2, val); r += 1

    r += 1
    ws1.cell(r, 1, "Actifs en cours").font = BOLD; r += 1
    ac = data["actifs"]
    ws1.cell(r, 1, "Clients en cours"); ws1.cell(r, 2, ac["nb"]); r += 1
    ws1.cell(r, 1, "Solde en attente"); ws1.cell(r, 2, _gdes(ac["solde_en_attente"])); r += 1

    if data.get("notes"):
        r += 1
        ws1.cell(r, 1, "Commentaires").font = BOLD; r += 1
        for n in data["notes"]:
            ws1.cell(r, 1, n["date"] + (f" — {n['auteur']}" if n.get("auteur") else "")).font = BOLD
            ws1.cell(r, 2, n["texte"]); r += 1

    # ── Feuille 2 : Par Chambre ───────────────────────────────────
    ws2 = wb.create_sheet("Par Chambre")
    for i, w in enumerate([14, 10, 12, 10, 20], 1):
        ws2.column_dimensions[get_column_letter(i)].width = w
    _hdr(ws2, 1, ["Chambre", "Total", "Moments", "Nuits", "Revenu"])
    for idx, ch in enumerate(data["par_chambre"], 2):
        _row(ws2, idx,
             [ch["numero"], ch["nb"], ch["moments"], ch["nuits"], _gdes(ch["revenu"])],
             alt=(idx % 2 == 1))

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"rapport_hotel_{data['periode']['debut']}_{data['periode']['fin']}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={fname}"},
    )


# ══════════════════════════════════════════════════════════════════
# RENFLOUEMENT CAISSE HÔTEL
# ══════════════════════════════════════════════════════════════════

def _renflouement_dict_hotel(r: RenflouementDepartement) -> dict:
    return {
        "id":                r.id,
        "montant":           float(r.montant),
        "source":            r.source,
        "date_renflouement": r.date_renflouement.isoformat() if r.date_renflouement else None,
        "enregistre_par":    r.enregistre_par.nom_complet if r.enregistre_par else None,
        "notes":             r.notes,
    }


@router.get("/renflouements")
def lister_renflouements_hotel(
    date_debut: Optional[str] = Query(default=None),
    date_fin:   Optional[str] = Query(default=None),
    limit: int = Query(50, le=200),
    db: Session = Depends(get_db),
):
    q = db.query(RenflouementDepartement).filter(RenflouementDepartement.departement == "HOTEL")
    if date_debut:
        q = q.filter(RenflouementDepartement.date_renflouement >= datetime.strptime(date_debut, "%Y-%m-%d").replace(hour=0,  minute=0,  second=0,  tzinfo=timezone.utc))
    if date_fin:
        q = q.filter(RenflouementDepartement.date_renflouement <= datetime.strptime(date_fin,   "%Y-%m-%d").replace(hour=23, minute=59, second=59, tzinfo=timezone.utc))
    rows = q.order_by(RenflouementDepartement.date_renflouement.desc()).limit(limit).all()
    return {"renflouements": [_renflouement_dict_hotel(r) for r in rows]}


@router.post("/renflouements", status_code=201)
def creer_renflouement_hotel(
    data: dict, db: Session = Depends(get_db),
    _user: Utilisateur = Depends(_require_pdg_ou_admin_hotel),
):
    montant = float(data.get("montant") or 0)
    if montant <= 0:
        raise HTTPException(400, "Le montant doit être positif.")
    r = RenflouementDepartement(
        departement="HOTEL",
        montant=montant,
        source=(data.get("source") or "").strip() or None,
        notes=(data.get("notes") or "").strip() or None,
        enregistre_par_id=_user.id,
    )
    db.add(r)
    from notifications_service import creer_notification
    creer_notification(
        db, module="hotel", type_="renflouement_departement",
        titre=f"Renflouement Caisse Hôtel — {montant:,.2f} G",
        message=(data.get("source") or "Source non précisée"),
        lien="hotel-dashboard",
        dedupe_minutes=None,
    )
    db.commit(); db.refresh(r)
    return _renflouement_dict_hotel(r)


# ══════════════════════════════════════════════════════════════════
# DÉPENSES HÔTEL
# ══════════════════════════════════════════════════════════════════

def _est_admin_utilisateur_hotel(request: Request, db: Session) -> bool:
    """Même logique que cuisine_routes._est_admin_utilisateur — dupliquée
    ici pour éviter un import circulaire entre routers."""
    user = getattr(request.state, "user", None)
    if not user:
        return False
    if user.role == "admin":
        return True
    if user.role_id:
        u = db.get(Utilisateur, user.id)
        if u and u.role_obj and u.role_obj.permissions.get("admin", False):
            return True
    return False


def _verifier_permission_date_hotel(request: Request, db: Session, nouvelle_date, ancienne_date=None):
    """Réserve l'antidatage (date < aujourd'hui) aux administrateurs — même
    règle que Cuisine, pour éviter les entrées rétroactives non contrôlées."""
    if not nouvelle_date:
        return
    aujourdhui = today_haiti()
    if nouvelle_date.date() >= aujourdhui:
        return
    if ancienne_date and ancienne_date.date() == nouvelle_date.date():
        return
    if not _est_admin_utilisateur_hotel(request, db):
        raise HTTPException(403, "Seul un administrateur peut enregistrer une entrée à une date passée.")


def _parse_date_saisie_hotel(raw):
    """Une date seule (sans heure) est stockée à MIDI UTC, pas minuit — voir
    cuisine_routes._parse_date_saisie pour l'explication complète du fuseau."""
    from datetime import date as _date, time as _time
    if not raw:
        return None
    try:
        if "T" in raw:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return datetime.combine(_date.fromisoformat(raw), _time(12, 0)).replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


@router.get("/depenses")
def liste_depenses_hotel(
    date_debut: Optional[str] = Query(default=None),
    date_fin:   Optional[str] = Query(default=None),
    categorie:  Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    from datetime import date as date_type, time as time_type
    today = today_haiti()
    d_debut = date_type.fromisoformat(date_debut) if date_debut else today.replace(day=1)
    d_fin   = date_type.fromisoformat(date_fin)   if date_fin   else today
    dt_deb = datetime.combine(d_debut, time_type.min).replace(tzinfo=timezone.utc)
    dt_fin = datetime.combine(d_fin,   time_type.max).replace(tzinfo=timezone.utc)

    q = (
        db.query(HotelDepense)
        .filter(HotelDepense.date_depense >= dt_deb, HotelDepense.date_depense <= dt_fin)
    )
    if categorie:
        q = q.filter(HotelDepense.categorie == categorie)
    deps = q.order_by(HotelDepense.date_depense.desc()).all()
    from pieces_jointes_routes import compter_pieces_jointes_par_entite
    nb_pj = compter_pieces_jointes_par_entite(db, "hotel_depense", [d.id for d in deps])
    return [
        {
            "id":          d.id,
            "description": d.description,
            "categorie":   d.categorie or "AUTRE",
            "montant":     float(_d(d.montant)),
            "date_depense": d.date_depense.isoformat(),
            "jours":       (today - d.date_depense.date()).days,
            "fournisseur": d.fournisseur or "",
            "notes":       d.notes or "",
            "sans_justificatif": nb_pj.get(d.id, 0) == 0,
        }
        for d in deps
    ]


@router.post("/depenses")
def ajouter_depense_hotel(data: dict, request: Request, db: Session = Depends(get_db)):
    desc = (data.get("description") or "").strip()
    if not desc:
        raise HTTPException(400, "La description est requise")
    montant = float(data.get("montant") or 0)
    if montant <= 0:
        raise HTTPException(400, "Le montant doit être positif")

    date_dep = _parse_date_saisie_hotel(data.get("date_depense")) or datetime.now(timezone.utc)
    _verifier_permission_date_hotel(request, db, date_dep)

    categorie = lref.resoudre(db, CategorieDepense, data.get("categorie"),
                              request=request, defaut="AUTRE", label="catégorie")
    d = HotelDepense(
        description  = desc,
        categorie    = categorie,
        montant      = Decimal(str(montant)),
        date_depense = date_dep,
        fournisseur  = (data.get("fournisseur") or "").strip() or None,
        notes        = (data.get("notes") or "").strip() or None,
    )
    db.add(d)
    from pieces_jointes_routes import notifier_si_sans_justificatif
    notifier_si_sans_justificatif(db, "hotel", desc, "hotel-depenses")
    db.commit(); db.refresh(d)
    return {"id": d.id, "message": "Dépense enregistrée"}


@router.put("/depenses/{dep_id}")
def modifier_depense_hotel(dep_id: int, data: dict, request: Request, db: Session = Depends(get_db)):
    d = db.query(HotelDepense).filter_by(id=dep_id).first()
    if not d:
        raise HTTPException(404, "Dépense introuvable")
    if "description" in data and data["description"]:
        d.description = data["description"].strip()
    if "categorie"   in data:
        d.categorie = lref.resoudre(db, CategorieDepense, data.get("categorie"),
                                    request=request, defaut="AUTRE", label="catégorie")
    if "montant" in data:
        montant = float(data["montant"] or 0)
        if montant <= 0:
            raise HTTPException(400, "Le montant doit être positif")
        d.montant = Decimal(str(montant))
    if "fournisseur" in data: d.fournisseur = (data["fournisseur"] or "").strip() or None
    if "notes"       in data: d.notes       = (data["notes"] or "").strip() or None
    if "date_depense" in data:
        parsed = _parse_date_saisie_hotel(data.get("date_depense"))
        if parsed:
            _verifier_permission_date_hotel(request, db, parsed, d.date_depense)
            d.date_depense = parsed
    db.commit()
    return {"message": "Dépense modifiée"}


@router.delete("/depenses/{dep_id}")
def supprimer_depense_hotel(dep_id: int, db: Session = Depends(get_db)):
    d = db.query(HotelDepense).filter_by(id=dep_id).first()
    if not d:
        raise HTTPException(404, "Dépense introuvable")
    db.delete(d); db.commit()
    return {"message": "Dépense supprimée"}


# ══════════════════════════════════════════════════════════════════
# PRO FORMA / RÉSERVATIONS À VENIR
# ──────────────────────────────────────────────────────────────────
# Devis imprimable ET réservation planifiée pour un client futur. Les
# chambres ne sont PAS bloquées tant que le client n'est pas arrivé
# (bouton « Convertir en séjour » à la réception). Les montants saisis
# font foi. À la confirmation → notification de rappel pour l'arrivée ;
# à l'approche de la date → 2ᵉ rappel (paresseux, sans planificateur).
# ══════════════════════════════════════════════════════════════════

class LigneProformaIn(BaseModel):
    designation:   str
    chambre_id:    Optional[int]   = None
    qte:           float           = 1
    prix_unitaire: float           = 0
    montant:       Optional[float] = None   # si fourni → fait foi


class ProformaIn(BaseModel):
    type_doc:            str                  = "PROFORMA"   # PROFORMA | RESERVATION
    client_nom:          str
    client_contact:      Optional[str]        = None
    client_id_piece:     Optional[str]        = None
    date_arrivee_prevue: str
    date_depart_prevue:  Optional[str]        = None
    nb_nuits:            Optional[int]        = None
    nb_personnes:        Optional[int]        = None
    lignes:             List[LigneProformaIn] = []
    montant_total:       Optional[float]      = None   # si absent → somme des lignes
    acompte:             float                = 0
    notes:              Optional[str]         = None


class ConvertirProformaIn(BaseModel):
    chambre_id:    Optional[int] = None
    mode_paiement: Optional[str] = None


def _parse_dt_hotel(raw: Optional[str], champ: str, obligatoire: bool = True):
    if not raw:
        if obligatoire:
            raise HTTPException(422, f"{champ} requis.")
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        raise HTTPException(422, f"{champ} invalide (ISO 8601).")


def _normaliser_lignes(lignes: List[LigneProformaIn], db: Session) -> tuple[list[dict], Decimal]:
    """Renvoie ([lignes normalisées avec montant + numéro de chambre], total)."""
    out: list[dict] = []
    total = Decimal("0")
    for l in lignes:
        des = (l.designation or "").strip()
        if not des:
            continue
        qte = Decimal(str(l.qte or 0)) or Decimal("1")
        pu  = Decimal(str(l.prix_unitaire or 0))
        if l.montant is not None:
            try:
                mt = Decimal(str(l.montant))
            except (ValueError, ArithmeticError):
                mt = qte * pu
        else:
            mt = qte * pu
        if mt < 0:
            mt = Decimal("0")
        num = None
        if l.chambre_id:
            ch = db.get(HotelChambre, l.chambre_id)
            num = ch.numero if ch else None
        out.append({
            "designation":   des,
            "chambre_id":    l.chambre_id,
            "chambre_numero": num,
            "qte":           float(qte),
            "prix_unitaire": float(pu),
            "montant":       float(mt),
        })
        total += mt
    return out, total


def _pf_numero(db: Session, type_doc: str) -> str:
    prefixe = "RS" if type_doc == "RESERVATION" else "PF"
    annee = today_haiti().year
    base = f"{prefixe}-{annee}-"
    n = db.query(HotelProforma).filter(HotelProforma.numero.like(base + "%")).count() + 1
    for _ in range(50):
        cand = f"{base}{n:04d}"
        if not db.query(HotelProforma).filter_by(numero=cand).first():
            return cand
        n += 1
    return f"{base}{int(datetime.now().timestamp())}"


def _pf_dict(p: HotelProforma) -> dict:
    return {
        "id":                  p.id,
        "numero":              p.numero,
        "type_doc":            p.type_doc,
        "client_nom":          p.client_nom,
        "client_contact":      p.client_contact or "",
        "client_id_piece":     p.client_id_piece or "",
        "date_arrivee_prevue": p.date_arrivee_prevue.isoformat() if p.date_arrivee_prevue else None,
        "date_depart_prevue":  p.date_depart_prevue.isoformat() if p.date_depart_prevue else None,
        "nb_nuits":            p.nb_nuits,
        "nb_personnes":        p.nb_personnes,
        "lignes":              p.lignes or [],
        "montant_total":       float(_d(p.montant_total)),
        "acompte":             float(_d(p.acompte)),
        "solde":               float(_d(p.montant_total) - _d(p.acompte)),
        "statut":              p.statut,
        "notes":               p.notes or "",
        "reservation_id":      p.reservation_id,
        "created_at":          p.created_at.isoformat() if p.created_at else None,
        "maj_le":              p.maj_le.isoformat() if p.maj_le else None,
    }


def _pf_rappels_arrivee(db: Session):
    """Rappel paresseux (pas de planificateur dans l'appli) : pour chaque
    réservation confirmée dont l'arrivée est dans ≤ 48 h et non encore
    rappelée, on émet une notification « préparer la chambre »."""
    from notifications_service import creer_notification
    limite = datetime.now(timezone.utc) + timedelta(hours=48)
    a_traiter = (
        db.query(HotelProforma)
        .filter(
            HotelProforma.statut == "CONFIRMEE",
            HotelProforma.rappel_arrivee_notifie.is_(False),
            HotelProforma.date_arrivee_prevue <= limite,
        )
        .all()
    )
    if not a_traiter:
        return
    for p in a_traiter:
        chs = ", ".join(sorted({str(l.get("chambre_numero")) for l in (p.lignes or [])
                                if l.get("chambre_numero")})) or "à définir"
        creer_notification(
            db, module="hotel", type_="reservation_arrivee_proche",
            titre=f"Arrivée imminente — {p.client_nom}",
            message=f"{p.numero} · arrivée {_dt_fr(p.date_arrivee_prevue)} · "
                    f"chambre(s) : {chs} — préparer la/les chambre(s).",
            lien="hotel-proforma", dedupe_minutes=None,
        )
        p.rappel_arrivee_notifie = True
    db.commit()


@router.get("/proformas")
def liste_proformas(
    statut:   Optional[str] = Query(default=None),
    type_doc: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    try:
        _pf_rappels_arrivee(db)
    except Exception:
        db.rollback()
    q = db.query(HotelProforma).order_by(HotelProforma.date_arrivee_prevue.desc())
    if statut:
        q = q.filter(HotelProforma.statut == statut.upper())
    if type_doc:
        q = q.filter(HotelProforma.type_doc == type_doc.upper())
    return [_pf_dict(p) for p in q.limit(300).all()]


@router.get("/proformas/{pf_id}")
def detail_proforma(pf_id: int, db: Session = Depends(get_db)):
    p = db.get(HotelProforma, pf_id)
    if not p:
        raise HTTPException(404, "Pro forma introuvable.")
    return _pf_dict(p)


@router.post("/proformas", status_code=201)
def creer_proforma(data: ProformaIn, request: Request, db: Session = Depends(get_db)):
    if not (data.client_nom or "").strip():
        raise HTTPException(422, "Nom du client requis.")
    type_doc = (data.type_doc or "PROFORMA").upper()
    if type_doc not in ("PROFORMA", "RESERVATION"):
        type_doc = "PROFORMA"
    date_arr = _parse_dt_hotel(data.date_arrivee_prevue, "Date d'arrivée prévue")
    date_dep = _parse_dt_hotel(data.date_depart_prevue, "Date de départ prévue", obligatoire=False)

    lignes, total_lignes = _normaliser_lignes(data.lignes, db)
    montant_total = total_lignes
    if data.montant_total is not None:
        try:
            mt = Decimal(str(data.montant_total))
            if mt >= 0:
                montant_total = mt
        except (ValueError, ArithmeticError):
            pass
    acompte = max(Decimal("0"), _d(data.acompte))
    if acompte > montant_total:
        acompte = montant_total

    p = HotelProforma(
        numero              = _pf_numero(db, type_doc),
        type_doc            = type_doc,
        client_nom          = data.client_nom.strip(),
        client_contact      = (data.client_contact or "").strip() or None,
        client_id_piece     = (data.client_id_piece or "").strip() or None,
        date_arrivee_prevue = date_arr,
        date_depart_prevue  = date_dep,
        nb_nuits            = data.nb_nuits,
        nb_personnes        = data.nb_personnes,
        lignes             = lignes,
        montant_total       = montant_total,
        acompte             = acompte,
        statut              = "BROUILLON",
        notes              = (data.notes or "").strip() or None,
        cree_par_id         = _uid(request),
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return _pf_dict(p)


@router.put("/proformas/{pf_id}")
def modifier_proforma(pf_id: int, data: ProformaIn, request: Request, db: Session = Depends(get_db)):
    p = db.get(HotelProforma, pf_id)
    if not p:
        raise HTTPException(404, "Pro forma introuvable.")
    if p.statut in ("CONVERTIE", "ANNULEE"):
        raise HTTPException(409, "Ce document ne peut plus être modifié.")
    if not (data.client_nom or "").strip():
        raise HTTPException(422, "Nom du client requis.")

    p.client_nom          = data.client_nom.strip()
    p.client_contact      = (data.client_contact or "").strip() or None
    p.client_id_piece     = (data.client_id_piece or "").strip() or None
    p.date_arrivee_prevue = _parse_dt_hotel(data.date_arrivee_prevue, "Date d'arrivée prévue")
    p.date_depart_prevue  = _parse_dt_hotel(data.date_depart_prevue, "Date de départ prévue", obligatoire=False)
    p.nb_nuits            = data.nb_nuits
    p.nb_personnes        = data.nb_personnes

    lignes, total_lignes = _normaliser_lignes(data.lignes, db)
    p.lignes = lignes
    montant_total = total_lignes
    if data.montant_total is not None:
        try:
            mt = Decimal(str(data.montant_total))
            if mt >= 0:
                montant_total = mt
        except (ValueError, ArithmeticError):
            pass
    p.montant_total = montant_total
    acompte = max(Decimal("0"), _d(data.acompte))
    p.acompte = acompte if acompte <= montant_total else montant_total
    p.notes = (data.notes or "").strip() or None
    db.commit()
    db.refresh(p)
    return _pf_dict(p)


@router.post("/proformas/{pf_id}/confirmer", status_code=200)
def confirmer_proforma(pf_id: int, db: Session = Depends(get_db)):
    p = db.get(HotelProforma, pf_id)
    if not p:
        raise HTTPException(404, "Pro forma introuvable.")
    if p.statut != "BROUILLON":
        raise HTTPException(409, "Seul un brouillon peut être confirmé.")
    p.statut = "CONFIRMEE"

    chs = ", ".join(sorted({str(l.get("chambre_numero")) for l in (p.lignes or [])
                            if l.get("chambre_numero")})) or "à définir"
    from notifications_service import creer_notification
    creer_notification(
        db, module="hotel", type_="reservation_a_preparer",
        titre=f"Réservation confirmée — {p.client_nom}",
        message=f"{p.numero} · arrivée {_dt_fr(p.date_arrivee_prevue)} · "
                f"chambre(s) : {chs} · {_fmt_g(p.montant_total)} — penser à préparer la/les chambre(s).",
        lien="hotel-proforma", dedupe_minutes=None,
    )
    p.rappel_confirme_notifie = True
    # Si l'arrivée est déjà proche, le rappel J-2 part dans la foulée.
    if p.date_arrivee_prevue and p.date_arrivee_prevue <= datetime.now(timezone.utc) + timedelta(hours=48):
        creer_notification(
            db, module="hotel", type_="reservation_arrivee_proche",
            titre=f"Arrivée imminente — {p.client_nom}",
            message=f"{p.numero} · arrivée {_dt_fr(p.date_arrivee_prevue)} · "
                    f"chambre(s) : {chs} — préparer la/les chambre(s).",
            lien="hotel-proforma", dedupe_minutes=None,
        )
        p.rappel_arrivee_notifie = True
    db.commit()
    db.refresh(p)
    return _pf_dict(p)


@router.post("/proformas/{pf_id}/annuler", status_code=200)
def annuler_proforma(pf_id: int, db: Session = Depends(get_db)):
    p = db.get(HotelProforma, pf_id)
    if not p:
        raise HTTPException(404, "Pro forma introuvable.")
    if p.statut == "CONVERTIE":
        raise HTTPException(409, "Un document déjà converti en séjour ne peut être annulé.")
    p.statut = "ANNULEE"
    db.commit()
    return _pf_dict(p)


@router.post("/proformas/{pf_id}/convertir", status_code=201)
def convertir_proforma(pf_id: int, data: ConvertirProformaIn, request: Request, db: Session = Depends(get_db)):
    """Transforme la pro forma en séjour réel (arrivée du client) : crée
    une HotelReservation NUIT avec les montants de la pro forma (qui font
    foi), occupe la chambre, et marque la pro forma CONVERTIE."""
    p = db.get(HotelProforma, pf_id)
    if not p:
        raise HTTPException(404, "Pro forma introuvable.")
    if p.statut not in ("BROUILLON", "CONFIRMEE"):
        raise HTTPException(409, "Ce document ne peut pas être converti.")

    chambre_id = data.chambre_id
    if not chambre_id:
        for l in (p.lignes or []):
            if l.get("chambre_id"):
                chambre_id = l["chambre_id"]
                break
    if not chambre_id:
        raise HTTPException(422, "Aucune chambre associée — précisez la chambre à occuper.")
    chambre = db.get(HotelChambre, chambre_id)
    if not chambre:
        raise HTTPException(404, "Chambre introuvable.")
    if chambre.statut != "DISPONIBLE":
        raise HTTPException(409, f"Chambre {chambre.numero} n'est pas disponible (statut : {chambre.statut}).")

    nb_nuits = p.nb_nuits or 1
    if nb_nuits < 1:
        nb_nuits = 1
    date_arr = p.date_arrivee_prevue or datetime.now(timezone.utc)
    date_dep = p.date_depart_prevue or (date_arr + timedelta(days=nb_nuits))
    prix_unit = _d(chambre.prix_nuit)
    montant   = _d(p.montant_total) if _d(p.montant_total) > 0 else prix_unit * nb_nuits
    montant_paye = min(_d(p.acompte), montant)
    solde = montant - montant_paye

    r = HotelReservation(
        chambre_id         = chambre.id,
        client_nom         = p.client_nom,
        client_contact     = p.client_contact,
        client_id_piece    = p.client_id_piece,
        type_sejour        = "NUIT",
        date_arrivee       = date_arr,
        date_depart_prevue = date_dep,
        nb_nuits           = nb_nuits,
        nb_heures          = None,
        prix_unitaire      = prix_unit,
        montant_total      = montant,
        montant_paye       = montant_paye,
        solde              = solde,
        statut             = "EN_COURS",
        mode_paiement      = data.mode_paiement,
        notes              = (f"Issu de {p.numero}" + (f" — {p.notes}" if p.notes else ""))[:300],
    )
    db.add(r)
    db.flush()
    chambre.statut = "OCCUPEE"
    p.statut = "CONVERTIE"
    p.reservation_id = r.id

    from notifications_service import creer_notification
    creer_notification(
        db, module="hotel", type_="nouvelle_reservation",
        titre=f"Séjour ouvert — {r.client_nom}",
        message=f"Chambre {chambre.numero} · issu de {p.numero} · {_fmt_g(montant)}",
        lien="hotel-reservation", dedupe_minutes=None,
    )
    db.commit()
    db.refresh(r)
    return {"proforma": _pf_dict(p), "reservation": _res_dict(r)}


@router.get("/proformas/{pf_id}/pdf")
def proforma_pdf(pf_id: int, db: Session = Depends(get_db)):
    p = db.get(HotelProforma, pf_id)
    if not p:
        raise HTTPException(404, "Pro forma introuvable.")

    import io
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import cm
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
    from reportlab.lib.enums import TA_CENTER

    DARK   = colors.HexColor("#1A1A2E")
    ACCENT = colors.HexColor("#6366f1")
    GREY   = colors.HexColor("#6B7280")

    est_resa = p.type_doc == "RESERVATION"
    titre_doc = "CONFIRMATION DE RÉSERVATION" if est_resa else "PRO FORMA"

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=1.9*cm, rightMargin=1.9*cm,
                            topMargin=1.6*cm, bottomMargin=1.6*cm,
                            title=f"{titre_doc} {p.numero}")
    st_title = ParagraphStyle("title", fontSize=17, fontName="Helvetica-Bold",
                              textColor=DARK, alignment=TA_CENTER, spaceBefore=4, spaceAfter=3)
    st_sub   = ParagraphStyle("sub", fontSize=9.5, textColor=GREY, alignment=TA_CENTER, spaceAfter=6)
    st_sec   = ParagraphStyle("sec", fontSize=10.5, fontName="Helvetica-Bold",
                              textColor=ACCENT, spaceBefore=13, spaceAfter=5)
    st_body  = ParagraphStyle("body", fontSize=9.5, textColor=DARK, leading=13)

    story = []
    _pdf_entete(story, ACCENT)
    story.append(Paragraph(titre_doc, st_title))
    _statut_txt = {"BROUILLON": "Brouillon", "CONFIRMEE": "Confirmée",
                   "CONVERTIE": "Convertie en séjour", "ANNULEE": "Annulée"}.get(p.statut, p.statut)
    story.append(Paragraph(
        f"N&deg; {p.numero} &nbsp;—&nbsp; {_statut_txt} &nbsp;—&nbsp; établi le "
        f"{_dt_fr(p.created_at or datetime.now(timezone.utc))}", st_sub))

    story.append(Paragraph("Client", st_sec))
    cli_rows = [
        ["Nom complet", p.client_nom],
        ["Contact", p.client_contact or "—"],
        ["NIF / Pièce", p.client_id_piece or "—"],
        ["Personnes", str(p.nb_personnes) if p.nb_personnes else "—"],
    ]
    t_cli = Table(cli_rows, colWidths=[4.6*cm, 12.6*cm])
    t_cli.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("TEXTCOLOR", (0, 0), (0, -1), GREY),
        ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, 0), (-1, -2), 0.3, colors.HexColor("#E5E7EB")),
    ]))
    story.append(t_cli)

    story.append(Paragraph("Séjour prévu", st_sec))
    sej_rows = [
        ["Arrivée prévue", _dt_fr(p.date_arrivee_prevue)],
        ["Départ prévu", _dt_fr(p.date_depart_prevue) if p.date_depart_prevue else "—"],
        ["Nombre de nuits", str(p.nb_nuits) if p.nb_nuits else "—"],
    ]
    t_sej = Table(sej_rows, colWidths=[4.6*cm, 12.6*cm])
    t_sej.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("TEXTCOLOR", (0, 0), (0, -1), GREY),
        ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, 0), (-1, -2), 0.3, colors.HexColor("#E5E7EB")),
    ]))
    story.append(t_sej)

    story.append(Paragraph("Détail", st_sec))
    det_rows = [["Désignation", "Chambre", "Qté", "P.U.", "Montant"]]
    for l in (p.lignes or []):
        det_rows.append([
            l.get("designation", ""),
            l.get("chambre_numero") or "—",
            f"{l.get('qte', 1):g}",
            _fmt_g(l.get("prix_unitaire", 0)),
            _fmt_g(l.get("montant", 0)),
        ])
    if len(det_rows) == 1:
        det_rows.append(["—", "—", "—", "—", _fmt_g(0)])
    t_det = Table(det_rows, colWidths=[6.8*cm, 2.4*cm, 1.4*cm, 3.1*cm, 3.5*cm])
    t_det.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), DARK),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#F7F7F9"), colors.white]),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D5D5DD")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5), ("TOPPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(t_det)
    story.append(Spacer(1, 5))

    rec_rows = [["TOTAL", _fmt_g(p.montant_total)]]
    if _d(p.acompte) > 0:
        rec_rows.append(["Acompte reçu", _fmt_g(p.acompte)])
        rec_rows.append(["Solde à régler", _fmt_g(_d(p.montant_total) - _d(p.acompte))])
    t_rec = Table(rec_rows, colWidths=[13.2*cm, 4*cm])
    t_rec.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10.5),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("LEFTPADDING", (0, 0), (0, 0), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7), ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("GRID", (0, 1), (-1, -1), 0.4, colors.HexColor("#D5D5DD")),
    ]))
    story.append(t_rec)

    if p.notes:
        story.append(Paragraph("Notes", st_sec))
        story.append(Paragraph(str(p.notes).replace("\n", "<br/>"), st_body))

    story.append(Spacer(1, 14))
    story.append(Paragraph(
        "Ce document ne vaut pas facture. Les chambres sont garanties à réception de l'acompte "
        "et jusqu'à l'heure d'arrivée prévue.", ParagraphStyle(
            "mention", fontSize=8, textColor=GREY, leading=11)))

    story.append(Spacer(1, 30))
    sign = Table([
        ["______________________________", "", "______________________________"],
        ["Signature du client", "", "Signature de la direction"],
    ], colWidths=[7.3*cm, 2.6*cm, 7.3*cm])
    sign.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 1), (-1, 1), GREY),
        ("TOPPADDING", (0, 1), (-1, 1), 3),
    ]))
    story.append(sign)

    story.append(Spacer(1, 20))
    story.append(HRFlowable(width="100%", thickness=0.4, color=colors.grey))
    _nom, _ = _branding_lignes()
    story.append(Paragraph(
        f"Document généré le {_dt_fr(datetime.now(timezone.utc))}" + (f" — {_nom}" if _nom else ""),
        ParagraphStyle("foot", fontSize=7, textColor=colors.grey, alignment=TA_CENTER, spaceBefore=4)))

    doc.build(story)
    buf.seek(0)
    fname = f"{p.numero}_{p.client_nom}.pdf".replace(" ", "_")
    return StreamingResponse(buf, media_type="application/pdf",
                             headers={"Content-Disposition": f'inline; filename="{fname}"'})
