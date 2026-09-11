"""
Gestion des caisses/cartons bar — traçabilité individuelle par QR code.
Préfixe /api/pos (même module que pos_routes.py : achats/stock/ventes bar).

Principe directeur : cette couche est ADDITIVE. Le stock agrégat par
produit (BarMouvementStock, seule source de vérité du stock courant —
voir stock_courant() dans pos_service.py) n'est jamais recalculé ni
dupliqué ici. Une BarCaisse donne une identité et une localisation
physique à une partie de ce stock ; elle ne le remplace pas.

Le crédit de stock à la génération réutilise pos_routes.approvisionner()
tel quel (même logique, mêmes notifications, même écriture BarMouvementStock)
au lieu de le réimplémenter.
"""
from __future__ import annotations

import base64
import io
from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db
from models import (
    BarProduit, BarAchat, BarDepartement, BarCaisse, BarCaisseMouvement,
    BarMouvementStock, Utilisateur,
)
from tz_utils import today_haiti

router = APIRouter(prefix="/api/pos", tags=["POS Bar — Caisses"])

_STATUTS_ACTIFS = ("TRANSFEREE", "EN_VENTE")


def _user(request: Request):
    return getattr(request.state, "user", None)


def _uid(request: Request) -> int | None:
    u = _user(request)
    return u.id if u else None


# ══════════════════════════════════════════════════════════════════
# DÉPARTEMENTS — liste gérée (Devant, Piscine, Derrière…)
# ══════════════════════════════════════════════════════════════════

class DepartementIn(BaseModel):
    nom: str


def _dep_norm(nom: str) -> str:
    import re
    return re.sub(r"\s+", " ", (nom or "").strip()).casefold()


@router.get("/departements")
def lister_departements(inclure_inactifs: bool = False, db: Session = Depends(get_db)):
    q = db.query(BarDepartement)
    if not inclure_inactifs:
        q = q.filter(BarDepartement.actif.is_(True))
    rows = q.order_by(BarDepartement.nom).all()
    return [{"id": d.id, "nom": d.nom, "actif": d.actif} for d in rows]


@router.post("/departements", status_code=201)
def creer_departement(data: DepartementIn, db: Session = Depends(get_db)):
    nom = (data.nom or "").strip()
    if not nom:
        raise HTTPException(400, "Le nom est requis.")
    norm = _dep_norm(nom)[:60]
    existant = db.query(BarDepartement).filter(BarDepartement.nom_norm == norm).first()
    if existant:
        if not existant.actif:
            existant.actif = True
            db.commit()
        return {"id": existant.id, "nom": existant.nom, "actif": existant.actif, "cree": False}
    d = BarDepartement(nom=nom[:60], nom_norm=norm, actif=True)
    db.add(d)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        gagnant = db.query(BarDepartement).filter(BarDepartement.nom_norm == norm).first()
        if gagnant:
            return {"id": gagnant.id, "nom": gagnant.nom, "actif": gagnant.actif, "cree": False}
        raise HTTPException(409, "Ce département existe déjà.")
    db.refresh(d)
    return {"id": d.id, "nom": d.nom, "actif": d.actif, "cree": True}


# ══════════════════════════════════════════════════════════════════
# CODE UNIQUE + QR
# ══════════════════════════════════════════════════════════════════

def _generer_code_unique(db: Session, annee: int) -> str:
    """CAISSE-{année}-{séquence sur 6 chiffres} — jamais réutilisé (contrainte
    UNIQUE en base en filet de sécurité, boucle de retry en cas de course)."""
    prefixe = f"CAISSE-{annee}-"
    n = db.query(BarCaisse).filter(BarCaisse.code_unique.like(prefixe + "%")).count() + 1
    for _ in range(1000):
        candidat = f"{prefixe}{n:06d}"
        if not db.query(BarCaisse).filter_by(code_unique=candidat).first():
            return candidat
        n += 1
    return f"{prefixe}{int(datetime.now().timestamp())}"


def _qr_png_bytes(texte: str) -> bytes:
    """QR code encodant uniquement l'identifiant unique — jamais de données
    sensibles ni métier dedans (voir doctrine sécurité : le scan interroge
    toujours le backend, le QR n'est qu'un pointeur)."""
    import qrcode
    img = qrcode.make(texte, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════════
# SÉRIALISATION
# ══════════════════════════════════════════════════════════════════

def _caisse_dict(c: BarCaisse) -> dict:
    return {
        "id":                 c.id,
        "code_unique":        c.code_unique,
        "produit_id":         c.produit_id,
        "produit_nom":        c.produit.nom if c.produit else None,
        "achat_id":           c.achat_id,
        "unites_par_caisse":  c.unites_par_caisse,
        "quantite_initiale":  c.quantite_initiale,
        "quantite_restante":  c.quantite_restante,
        "quantite_vendue":    c.quantite_initiale - c.quantite_restante,
        "statut":             c.statut,
        "emplacement_actuel": c.emplacement_actuel,
        "departement_id":     c.departement_id,
        "departement_nom":    c.departement.nom if c.departement else None,
        "cree_par_nom":       c.cree_par.nom_complet if c.cree_par else None,
        "transfere_par_nom":  c.transfere_par.nom_complet if c.transfere_par else None,
        "created_at":         c.created_at.isoformat() if c.created_at else None,
        "transferee_at":      c.transferee_at.isoformat() if c.transferee_at else None,
        "vente_debut_at":     c.vente_debut_at.isoformat() if c.vente_debut_at else None,
        "terminee_at":        c.terminee_at.isoformat() if c.terminee_at else None,
    }


def _mouvement_dict(m: BarCaisseMouvement) -> dict:
    return {
        "id":              m.id,
        "type_mouvement":  m.type_mouvement,
        "quantite":        m.quantite,
        "motif":           m.motif,
        "utilisateur_nom": m.utilisateur.nom_complet if m.utilisateur else None,
        "created_at":      m.created_at.isoformat() if m.created_at else None,
    }


# ══════════════════════════════════════════════════════════════════
# GÉNÉRATION — « Déclarer un achat / Générer les codes »
# ══════════════════════════════════════════════════════════════════

class GenererCaissesIn(BaseModel):
    produit_id:        int
    nb_caisses:        int             = Field(gt=0, le=1000)
    prix_achat_caisse: Optional[float] = Field(None, gt=0)
    fournisseur:       Optional[str]   = None
    notes:             Optional[str]   = None


@router.post("/caisses/generer", status_code=201)
def generer_caisses(data: GenererCaissesIn, request: Request, db: Session = Depends(get_db)):
    """Déclare un achat de N caisses d'un produit déjà configuré
    « vendu par caisse » et génère exactement N identifiants uniques —
    ni plus, ni moins (jamais nb_caisses × unités_par_caisse codes).

    Le crédit du stock agrégat réutilise pos_routes.approvisionner() :
    aucune double logique de calcul de stock."""
    produit = db.query(BarProduit).filter_by(id=data.produit_id).first()
    if not produit:
        raise HTTPException(404, "Produit introuvable.")
    if not produit.actif:
        raise HTTPException(422, f"« {produit.nom} » est inactif.")
    if not produit.vendu_par_caisse:
        raise HTTPException(
            422,
            f"« {produit.nom} » n'est pas configuré comme vendu par caisse "
            "(voir Gestion des produits) — activez « Vendu par caisse » d'abord.",
        )
    if not produit.unites_par_caisse or produit.unites_par_caisse < 1:
        raise HTTPException(422, f"« {produit.nom} » n'a pas d'unités par caisse définies.")

    upc = produit.unites_par_caisse

    # Réutilise l'approvisionnement existant pour créditer le stock agrégat
    # (BarAchat + BarMouvementStock ENTREE) — même comportement, mêmes
    # notifications, qu'un approvisionnement fait depuis la page Stock.
    from pos_routes import approvisionner, ApprovisionnementIn
    appro = approvisionner(
        produit.id,
        ApprovisionnementIn(
            nb_caisses=data.nb_caisses,
            nb_unites_vrac=0,
            prix_achat_caisse=data.prix_achat_caisse,
            notes=(f"Génération {data.nb_caisses} caisse(s) avec codes QR"
                   + (f" — {data.notes}" if data.notes else "")),
        ),
        request, db,
    )

    # achat_id le plus récent pour ce produit, s'il a été créé par
    # l'approvisionnement ci-dessus (uniquement si un prix a été fourni).
    achat_id = None
    if data.prix_achat_caisse:
        dernier = (
            db.query(BarAchat)
            .filter_by(produit_id=produit.id)
            .order_by(BarAchat.id.desc())
            .first()
        )
        achat_id = dernier.id if dernier else None
        if achat_id and data.fournisseur:
            dernier.fournisseur = data.fournisseur

    annee = today_haiti().year
    uid = _uid(request)
    caisses = []
    try:
        for _ in range(data.nb_caisses):
            code = _generer_code_unique(db, annee)
            c = BarCaisse(
                code_unique        = code,
                produit_id         = produit.id,
                achat_id           = achat_id,
                unites_par_caisse  = upc,
                quantite_initiale  = upc,
                quantite_restante  = upc,
                statut             = "AU_DEPOT",
                emplacement_actuel = "DEPOT",
                cree_par_id        = uid,
            )
            db.add(c)
            db.flush()
            caisses.append(c)
        db.commit()
    except Exception:
        # L'approvisionnement (crédit de stock agrégat) est déjà validé en
        # base à ce stade — on ne peut pas le "rollback" a posteriori. Pour
        # ne jamais laisser le stock agrégat et le nombre de caisses se
        # désynchroniser, on annule ici la partie caisses (rollback) puis on
        # compense le stock crédité par un mouvement inverse tracé, avant de
        # remonter l'erreur au client.
        db.rollback()
        db.add(BarMouvementStock(
            produit_id     = produit.id,
            quantite       = Decimal(str(-(data.nb_caisses * upc))),
            type_mouvement = "AJUSTEMENT",
            motif          = "Compensation — échec de la génération des caisses après approvisionnement",
            utilisateur_id = uid,
        ))
        db.commit()
        raise HTTPException(
            409,
            "La génération des caisses a échoué après le crédit de stock ; "
            "le stock a été automatiquement compensé (annulé). Réessayez.",
        )
    for c in caisses:
        db.refresh(c)

    return {
        "nb_caisses_generees": len(caisses),
        "produit_nom":         produit.nom,
        "unites_par_caisse":   upc,
        "total_unites":        len(caisses) * upc,
        "stock_apres":         appro.get("stock_apres"),
        "caisses":             [_caisse_dict(c) for c in caisses],
    }


# ══════════════════════════════════════════════════════════════════
# CONSULTATION
# ══════════════════════════════════════════════════════════════════

@router.get("/caisses")
def lister_caisses(
    statut:         Optional[str] = Query(default=None),
    produit_id:     Optional[int] = Query(default=None),
    departement_id: Optional[int] = Query(default=None),
    emplacement:    Optional[str] = Query(default=None),
    q:              Optional[str] = Query(default=None),
    limit:          int = Query(300, le=1000),
    db: Session = Depends(get_db),
):
    query = db.query(BarCaisse)
    if statut:
        query = query.filter(BarCaisse.statut == statut.upper())
    if produit_id:
        query = query.filter(BarCaisse.produit_id == produit_id)
    if departement_id:
        query = query.filter(BarCaisse.departement_id == departement_id)
    if emplacement:
        query = query.filter(BarCaisse.emplacement_actuel == emplacement.upper())
    if q:
        like = f"%{q.strip()}%"
        query = query.join(BarProduit).filter(
            (BarCaisse.code_unique.ilike(like)) | (BarProduit.nom.ilike(like))
        )
    rows = query.order_by(BarCaisse.created_at.desc()).limit(limit).all()
    return [_caisse_dict(c) for c in rows]


@router.get("/caisses/stats")
def stats_caisses(db: Session = Depends(get_db)):
    from sqlalchemy import func as _func
    rows = (
        db.query(BarCaisse.statut, _func.count(BarCaisse.id))
        .group_by(BarCaisse.statut)
        .all()
    )
    par_statut = {s: n for s, n in rows}
    return {
        "total":       sum(par_statut.values()),
        "au_depot":    par_statut.get("AU_DEPOT", 0),
        "transferees": par_statut.get("TRANSFEREE", 0),
        "en_vente":    par_statut.get("EN_VENTE", 0),
        "terminees":   par_statut.get("TERMINEE", 0),
        "annulees":    par_statut.get("ANNULEE", 0),
    }


@router.get("/caisses/code/{code}")
def caisse_par_code(code: str, db: Session = Depends(get_db)):
    """Résolution scan → caisse. Le backend est TOUJOURS interrogé (le QR
    n'est jamais considéré comme une preuve d'autorisation en lui-même)."""
    c = db.query(BarCaisse).filter_by(code_unique=code.strip().upper()).first()
    if not c:
        raise HTTPException(404, "Caisse introuvable pour ce code.")
    return _caisse_dict(c)


@router.get("/caisses/etiquettes.pdf")
def imprimer_etiquettes(ids: str = Query(..., description="IDs séparés par des virgules"),
                        db: Session = Depends(get_db)):
    """Une étiquette par caisse — nom du produit, numéro de caisse, nombre
    d'unités, QR code. Mise en page en grille pour impression multiple
    (5 caisses → 5 étiquettes, 50 caisses → 50 étiquettes).

    NOTE : ce chemin fixe (/caisses/etiquettes.pdf) doit impérativement être
    déclaré AVANT la route paramétrée /caisses/{caisse_id} ci-dessous —
    FastAPI/Starlette matche les routes dans l'ordre de déclaration, sinon
    "etiquettes.pdf" est capturé comme valeur de {caisse_id} et le parsing
    entier échoue (422)."""
    try:
        id_list = [int(x) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(422, "Paramètre ids invalide.")
    caisses = db.query(BarCaisse).filter(BarCaisse.id.in_(id_list)).order_by(BarCaisse.id).all()
    if not caisses:
        raise HTTPException(404, "Aucune caisse trouvée pour ces identifiants.")

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Image, Spacer
    from reportlab.lib.enums import TA_CENTER

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=0.8*cm, rightMargin=0.8*cm,
                            topMargin=0.8*cm, bottomMargin=0.8*cm,
                            title="Étiquettes caisses")
    st_prod = ParagraphStyle("prod", fontSize=11, fontName="Helvetica-Bold",
                             alignment=TA_CENTER, leading=13)
    st_code = ParagraphStyle("code", fontSize=9, fontName="Helvetica-Bold",
                             alignment=TA_CENTER, textColor=colors.HexColor("#1e3a5f"), leading=11)
    st_unites = ParagraphStyle("unites", fontSize=9.5, alignment=TA_CENTER,
                               textColor=colors.HexColor("#555555"))

    def _etiquette(c: BarCaisse):
        qr_bytes = _qr_png_bytes(c.code_unique)
        qr_img = Image(io.BytesIO(qr_bytes), width=2.6*cm, height=2.6*cm)
        cell = Table(
            [[Paragraph((c.produit.nom if c.produit else "—"), st_prod)],
             [qr_img],
             [Paragraph(f"Caisse {c.code_unique}", st_code)],
             [Paragraph(f"{c.unites_par_caisse} unités", st_unites)]],
            colWidths=[8.6*cm],
        )
        cell.setStyle(TableStyle([
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#999999")),
        ]))
        return cell

    cellules = [_etiquette(c) for c in caisses]
    # Grille 2 colonnes — une étiquette par ligne de tableau, 2 par rangée.
    lignes = []
    for i in range(0, len(cellules), 2):
        paire = cellules[i:i+2]
        while len(paire) < 2:
            paire.append("")
        lignes.append(paire)

    grille = Table(lignes, colWidths=[9.2*cm, 9.2*cm], rowHeights=[6.2*cm] * len(lignes))
    grille.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN",  (0, 0), (-1, -1), "CENTER"),
    ]))

    doc.build([grille])
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="etiquettes_caisses.pdf"'},
    )


@router.get("/caisses/{caisse_id}")
def detail_caisse(caisse_id: int, db: Session = Depends(get_db)):
    c = db.get(BarCaisse, caisse_id)
    if not c:
        raise HTTPException(404, "Caisse introuvable.")
    d = _caisse_dict(c)
    d["historique"] = [_mouvement_dict(m) for m in c.mouvements]
    return d


# ══════════════════════════════════════════════════════════════════
# TRANSFERT DÉPÔT → DÉPARTEMENT (scan)
# ══════════════════════════════════════════════════════════════════

class TransfererIn(BaseModel):
    departement_id: int


@router.post("/caisses/{caisse_id}/transferer", status_code=200)
def transferer_caisse(caisse_id: int, data: TransfererIn, request: Request, db: Session = Depends(get_db)):
    c = db.get(BarCaisse, caisse_id)
    if not c:
        raise HTTPException(404, "Caisse introuvable.")
    if c.statut != "AU_DEPOT":
        raise HTTPException(
            409,
            f"Cette caisse ne peut pas être transférée (statut actuel : {c.statut}).",
        )
    dep = db.query(BarDepartement).filter_by(id=data.departement_id, actif=True).first()
    if not dep:
        raise HTTPException(404, "Département introuvable ou inactif.")

    now = datetime.now(timezone.utc)
    uid = _uid(request)
    c.statut             = "TRANSFEREE"
    c.emplacement_actuel = "DEPARTEMENT"
    c.departement_id     = dep.id
    c.transfere_par_id   = uid
    c.transferee_at      = now

    db.add(BarCaisseMouvement(
        caisse_id=c.id, type_mouvement="TRANSFERT", quantite=c.quantite_restante,
        motif=f"Transfert dépôt → {dep.nom}", utilisateur_id=uid,
    ))

    from notifications_service import creer_notification
    creer_notification(
        db, module="pos", type_="caisse_transferee",
        titre=f"Caisse transférée — {c.produit.nom}",
        message=f"{c.code_unique} → {dep.nom} ({c.quantite_restante} unités)",
        lien="pos-caisses", dedupe_minutes=None,
    )

    db.commit()
    db.refresh(c)
    return _caisse_dict(c)


# ══════════════════════════════════════════════════════════════════
# AJUSTEMENTS — casse, perte, correction (jamais confondu avec une vente)
# ══════════════════════════════════════════════════════════════════

class AjusterCaisseIn(BaseModel):
    type_mouvement: str    # CASSE | PERTE | CORRECTION
    quantite:       int = Field(gt=0)
    motif:          str


@router.post("/caisses/{caisse_id}/ajuster", status_code=200)
def ajuster_caisse(caisse_id: int, data: AjusterCaisseIn, request: Request, db: Session = Depends(get_db)):
    c = db.get(BarCaisse, caisse_id)
    if not c:
        raise HTTPException(404, "Caisse introuvable.")
    if c.statut not in ("TRANSFEREE", "EN_VENTE"):
        raise HTTPException(409, f"Ajustement impossible pour une caisse au statut {c.statut}.")
    type_mvt = (data.type_mouvement or "").upper()
    if type_mvt not in ("CASSE", "PERTE", "CORRECTION"):
        raise HTTPException(422, "type_mouvement doit être CASSE, PERTE ou CORRECTION.")
    if not data.motif or len(data.motif.strip()) < 5:
        raise HTTPException(422, "Le motif doit contenir au moins 5 caractères.")
    if data.quantite > c.quantite_restante:
        raise HTTPException(422, f"Quantité ({data.quantite}) supérieure au restant ({c.quantite_restante}).")

    c.quantite_restante -= data.quantite
    uid = _uid(request)
    db.add(BarCaisseMouvement(
        caisse_id=c.id, type_mouvement=type_mvt, quantite=-data.quantite,
        motif=data.motif.strip(), utilisateur_id=uid,
    ))
    _finaliser_si_epuisee(db, c, uid)
    db.commit()
    db.refresh(c)
    return _caisse_dict(c)


@router.post("/caisses/{caisse_id}/annuler", status_code=200)
def annuler_caisse(caisse_id: int, request: Request, db: Session = Depends(get_db)):
    """Annule une caisse encore AU DÉPÔT (générée par erreur). N'affecte
    jamais le stock agrégat déjà crédité — décision commerciale distincte,
    à traiter séparément (ajustement stock) si le lot n'existe pas
    physiquement."""
    c = db.get(BarCaisse, caisse_id)
    if not c:
        raise HTTPException(404, "Caisse introuvable.")
    if c.statut != "AU_DEPOT":
        raise HTTPException(409, "Seule une caisse encore au dépôt peut être annulée.")
    c.statut = "ANNULEE"
    db.add(BarCaisseMouvement(
        caisse_id=c.id, type_mouvement="ANNULATION", quantite=0,
        motif="Caisse annulée", utilisateur_id=_uid(request),
    ))
    db.commit()
    return {"ok": True, "id": c.id, "statut": c.statut}


# ══════════════════════════════════════════════════════════════════
# CONSOMMATION FIFO — appelée depuis pos_service.encaisser_vente()
# ══════════════════════════════════════════════════════════════════

def _finaliser_si_epuisee(db: Session, c: BarCaisse, uid: Optional[int]) -> None:
    if c.quantite_restante <= 0 and c.statut != "TERMINEE":
        c.quantite_restante = 0
        c.statut        = "TERMINEE"
        c.terminee_at   = datetime.now(timezone.utc)
        c.termine_par_id = uid
        from notifications_service import creer_notification
        creer_notification(
            db, module="pos", type_="caisse_terminee",
            titre=f"Caisse terminée — {c.produit.nom if c.produit else ''}",
            message=(f"{c.code_unique} · {c.quantite_initiale} unités initiales, "
                     f"toutes vendues/consommées."),
            lien="pos-caisses", dedupe_minutes=None,
        )


def decrementer_caisses_fifo(db: Session, produit_id: int, quantite_vendue, vente_id: int,
                             utilisateur_id: Optional[int]) -> None:
    """Répartit une quantité vendue sur les caisses actives du produit,
    la plus ancienne transférée d'abord (FIFO). Best-effort : si les
    caisses trackées ne couvrent pas toute la quantité (ex. stock ancien
    non tracké), on décrémente ce qui est disponible et on s'arrête —
    ne doit JAMAIS faire échouer la vente elle-même (voir appelant)."""
    restant_a_decompter = int(round(float(quantite_vendue)))
    if restant_a_decompter <= 0:
        return
    caisses = (
        db.query(BarCaisse)
        .filter(BarCaisse.produit_id == produit_id,
                BarCaisse.statut.in_(_STATUTS_ACTIFS),
                BarCaisse.quantite_restante > 0)
        .order_by(BarCaisse.transferee_at.asc().nullsfirst(), BarCaisse.id.asc())
        .all()
    )
    for c in caisses:
        if restant_a_decompter <= 0:
            break
        if c.statut == "TRANSFEREE":
            c.statut = "EN_VENTE"
            c.vente_debut_at = datetime.now(timezone.utc)
        pris = min(c.quantite_restante, restant_a_decompter)
        c.quantite_restante -= pris
        restant_a_decompter -= pris
        db.add(BarCaisseMouvement(
            caisse_id=c.id, type_mouvement="VENTE", quantite=-pris,
            motif=f"Vente #{vente_id}", reference_vente_id=vente_id,
            utilisateur_id=utilisateur_id,
        ))
        _finaliser_si_epuisee(db, c, utilisateur_id)
    # Pas de commit ici : appelé à l'intérieur de la transaction de la vente,
    # c'est l'appelant (encaisser_vente) qui commit.
