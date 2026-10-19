"""
Dépôt Bar — circuit de la marchandise, du fournisseur jusqu'aux bars.
Préfixe /api/pos/depot.

    1. Réception et enregistrement  → achat bar (Achats Bar), puis
                                      POST /receptions/{achat_id}
    2. Génération des codes-barres  → un colis par caisse/carton, étiquettes
                                      GET /etiquettes.pdf
    3. Mise en dépôt et vérification→ scan de chaque colis au dépôt
                                      POST /colis/verifier   (stock DÉPÔT +)
    4. Préparation de la livraison  → POST /livraisons {lieu}
    5. Lecture / scan des colis     → POST /livraisons/{id}/scan
    6. Bar avant / Bar piscine      → destination choisie à l'étape 4
    7. Mise à jour des stocks       → POST /livraisons/{id}/valider
                                      (stock DÉPÔT −, stock du bar +)

Le stock reste calculé à partir de BarMouvementStock (source unique) :
lieu NULL = dépôt, DEVANT = Bar avant, PISCINE = Bar piscine. Une
livraison crée deux mouvements TRANSFERT de même quantité : le stock total
ne change pas, seule sa localisation change.
"""
from __future__ import annotations

import io
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from models import (
    BarAchat, BarColis, BarLivraisonInterne, BarLivraisonLigne,
    BarMouvementStock, BarProduit, Utilisateur,
)
from pos_service import stock_par_lieu_tous_produits
from tz_utils import now_haiti

router = APIRouter(prefix="/api/pos/depot", tags=["Dépôt Bar"])

LIEUX = {"DEVANT": "Bar avant", "PISCINE": "Bar piscine"}
MAX_COLIS_PAR_RECEPTION = 500


# ── Droits ────────────────────────────────────────────────────────────────

def _require_depot(request: Request, db: Session = Depends(get_db)) -> Utilisateur:
    """Administrateur, PDG, manager, ou responsable de dépôt (rôle dont le
    nom contient « dépôt », ou poste Magasinier / Gestionnaire de stock)."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(403, "Non autorisé.")
    if user.role in ("admin", "pdg"):
        return user
    u = db.get(Utilisateur, user.id)
    if u:
        perms = (u.role_obj.permissions or {}) if u.role_obj else {}
        if perms.get("admin"):
            return u
        nom_role = ((u.role_obj.nom if u.role_obj else "") or "").strip().lower()
        poste = (u.poste or "").strip().lower()
        if nom_role == "manager" or poste == "manager":
            return u
        if any(m in nom_role for m in ("dépôt", "depot", "dépot")):
            return u
        if poste in ("magasinier", "gestionnaire de stock", "responsable logistique"):
            return u
    raise HTTPException(403, "Réservé au dépôt (administrateur, manager ou responsable de dépôt).")


# ── Utilitaires ───────────────────────────────────────────────────────────

def _code_saisi(texte: str) -> str:
    """Accepte le code seul ou une URL contenant le code (scan de QR)."""
    t = (texte or "").strip()
    if "/" in t:
        t = t.rstrip("/").rsplit("/", 1)[-1]
    return t.upper()


def _colis_dict(c: BarColis) -> dict:
    return {
        "id":           c.id,
        "code":         c.code,
        "produit_id":   c.produit_id,
        "produit_nom":  c.produit.nom if c.produit else None,
        "unite":        c.produit.unite if c.produit else None,
        "achat_id":     c.achat_id,
        "quantite":     float(c.quantite),
        "statut":       c.statut,
        "lieu":         c.lieu,
        "livraison_id": c.livraison_id,
        "cree_le":      c.cree_le.isoformat() if c.cree_le else None,
        "verifie_le":   c.verifie_le.isoformat() if c.verifie_le else None,
        "verifie_par":  c.verifie_par.nom_complet if c.verifie_par else None,
    }


def _livraison_dict(l: BarLivraisonInterne, details: bool = True) -> dict:
    d = {
        "id":          l.id,
        "numero":      l.numero,
        "lieu":        l.lieu,
        "lieu_nom":    LIEUX.get(l.lieu, l.lieu),
        "statut":      l.statut,
        "notes":       l.notes,
        "cree_le":     l.cree_le.isoformat() if l.cree_le else None,
        "cree_par":    l.cree_par.nom_complet if l.cree_par else None,
        "valide_le":   l.valide_le.isoformat() if l.valide_le else None,
        "valide_par":  l.valide_par.nom_complet if l.valide_par else None,
        "nb_lignes":   len(l.lignes),
        "total_unites": float(sum(Decimal(str(x.quantite)) for x in l.lignes)),
    }
    if details:
        d["lignes"] = [{
            "id":          x.id,
            "produit_id":  x.produit_id,
            "produit_nom": x.produit.nom if x.produit else None,
            "colis_code":  x.colis.code if x.colis else None,
            "quantite":    float(x.quantite),
        } for x in l.lignes]
    return d


def _stock_depot(db: Session, produit_id: int) -> Decimal:
    st = stock_par_lieu_tous_produits(db).get(produit_id)
    if not st:
        return Decimal("0")
    return st["total"] - st["devant"] - st["piscine"]


def _quantite_reservee(db: Session, produit_id: int, sauf_livraison: Optional[int] = None) -> Decimal:
    """Unités en vrac déjà réservées par des livraisons en préparation."""
    q = (db.query(BarLivraisonLigne)
         .join(BarLivraisonInterne, BarLivraisonLigne.livraison_id == BarLivraisonInterne.id)
         .filter(BarLivraisonInterne.statut == "PREPARATION",
                 BarLivraisonLigne.produit_id == produit_id,
                 BarLivraisonLigne.colis_id.is_(None)))
    if sauf_livraison:
        q = q.filter(BarLivraisonInterne.id != sauf_livraison)
    return sum((Decimal(str(x.quantite)) for x in q.all()), Decimal("0"))


# ══════════════════════════════════════════════════════════════════════════
# 1-2. RÉCEPTION → génération des colis et des codes
# ══════════════════════════════════════════════════════════════════════════

class ReceptionIn(BaseModel):
    # Unités par colis. Par défaut : unités par caisse du produit (une
    # étiquette par caisse), sinon un seul colis pour toute la quantité.
    unites_par_colis: Optional[int] = Field(default=None, gt=0)


def receptionner_achat(db: Session, achat: BarAchat, unites_par_colis: Optional[int],
                       utilisateur_id: Optional[int]) -> List[BarColis]:
    """Confirme l'achat et génère ses colis (statut A_VERIFIER). Idempotent :
    un achat déjà réceptionné renvoie ses colis existants. Ne touche PAS au
    stock : il n'entre au dépôt qu'à la vérification (scan) de chaque colis."""
    existants = db.query(BarColis).filter(BarColis.achat_id == achat.id).order_by(BarColis.id).all()
    if existants:
        return existants
    if not achat.produit_id:
        raise HTTPException(422, "Seuls les achats de produits bar passent par le dépôt.")
    produit = db.get(BarProduit, achat.produit_id)
    qte = Decimal(str(achat.quantite))
    taille = unites_par_colis or (produit.unites_par_caisse if produit and produit.vendu_par_caisse else None)
    tailles: List[Decimal] = []
    if taille:
        taille_d = Decimal(taille)
        nb_pleins = int((qte / taille_d).to_integral_value(rounding=ROUND_DOWN))
        tailles = [taille_d] * nb_pleins
        reste = qte - taille_d * nb_pleins
        if reste > 0:
            tailles.append(reste)
    else:
        tailles = [qte]
    if len(tailles) > MAX_COLIS_PAR_RECEPTION:
        raise HTTPException(422, f"Trop de colis ({len(tailles)}) — augmentez le nombre d'unités par colis.")

    achat.statut = "CONFIRME"
    colis = []
    for t in tailles:
        c = BarColis(code="TMP-" + uuid.uuid4().hex[:20], produit_id=achat.produit_id,
                     achat_id=achat.id, quantite=t, statut="A_VERIFIER")
        db.add(c)
        db.flush()
        c.code = f"BC{now_haiti():%y}{c.id:06d}"
        colis.append(c)
    return colis


@router.post("/receptions/{achat_id}", status_code=201)
def receptionner(achat_id: int, data: ReceptionIn, request: Request,
                 db: Session = Depends(get_db), user=Depends(_require_depot)):
    achat = db.get(BarAchat, achat_id)
    if not achat:
        raise HTTPException(404, "Achat introuvable.")
    if achat.statut == "ANNULE":
        raise HTTPException(409, "Cet achat est annulé.")
    colis = receptionner_achat(db, achat, data.unites_par_colis, user.id)
    db.commit()
    return {"achat_id": achat.id, "nb_colis": len(colis), "colis": [_colis_dict(c) for c in colis]}


@router.get("/receptions")
def receptions_a_traiter(db: Session = Depends(get_db)):
    """Achats bar pas encore réceptionnés (sans colis), et colis à vérifier."""
    achats = (db.query(BarAchat)
              .filter(BarAchat.produit_id.isnot(None), BarAchat.statut != "ANNULE")
              .order_by(BarAchat.date_achat.desc()).limit(300).all())
    avec_colis = {a for (a,) in db.query(BarColis.achat_id).filter(BarColis.achat_id.isnot(None)).distinct()}
    a_receptionner = [{
        "achat_id":    a.id,
        "produit_id":  a.produit_id,
        "produit_nom": a.bar_produit.nom if a.bar_produit else None,
        "quantite":    float(a.quantite),
        "unites_par_caisse": a.bar_produit.unites_par_caisse if a.bar_produit and a.bar_produit.vendu_par_caisse else None,
        "fournisseur": a.fournisseur,
        "date_achat":  a.date_achat.isoformat() if a.date_achat else None,
        "statut":      a.statut,
    } for a in achats if a.id not in avec_colis and a.statut == "EN_ATTENTE"]
    a_verifier = db.query(BarColis).filter(BarColis.statut == "A_VERIFIER").order_by(BarColis.id).all()
    return {
        "achats_a_receptionner": a_receptionner,
        "colis_a_verifier": [_colis_dict(c) for c in a_verifier],
    }


# ══════════════════════════════════════════════════════════════════════════
# Étiquettes (QR + code lisible)
# ══════════════════════════════════════════════════════════════════════════

@router.get("/etiquettes.pdf")
def etiquettes(achat_id: Optional[int] = None, ids: Optional[str] = None,
               db: Session = Depends(get_db)):
    from reportlab.graphics import renderPDF
    from reportlab.graphics.barcode.qr import QrCodeWidget
    from reportlab.graphics.shapes import Drawing
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    q = db.query(BarColis)
    if achat_id:
        q = q.filter(BarColis.achat_id == achat_id)
    elif ids:
        try:
            liste = [int(x) for x in ids.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(400, "ids invalide.")
        q = q.filter(BarColis.id.in_(liste))
    else:
        q = q.filter(BarColis.statut == "A_VERIFIER")
    colis = q.order_by(BarColis.id).all()
    if not colis:
        raise HTTPException(404, "Aucun colis à imprimer.")

    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=A4)
    larg_page, haut_page = A4
    cols, rangs = 3, 8
    marge_x, marge_y = 8 * mm, 10 * mm
    lw = (larg_page - 2 * marge_x) / cols
    lh = (haut_page - 2 * marge_y) / rangs
    qr_taille = lh - 6 * mm
    for i, c in enumerate(colis):
        pos = i % (cols * rangs)
        if i and pos == 0:
            pdf.showPage()
        x = marge_x + (pos % cols) * lw
        y = haut_page - marge_y - (pos // cols + 1) * lh
        pdf.setLineWidth(0.3)
        pdf.rect(x + 1 * mm, y + 1 * mm, lw - 2 * mm, lh - 2 * mm)
        w = QrCodeWidget(c.code)
        b = w.getBounds()
        d = Drawing(qr_taille, qr_taille, transform=[qr_taille / (b[2] - b[0]), 0, 0,
                                                     qr_taille / (b[3] - b[1]), 0, 0])
        d.add(w)
        renderPDF.draw(d, pdf, x + 2 * mm, y + 3 * mm)
        tx = x + qr_taille + 4 * mm
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawString(tx, y + lh - 9 * mm, c.code)
        pdf.setFont("Helvetica", 8)
        nom = (c.produit.nom if c.produit else "")[:24]
        pdf.drawString(tx, y + lh - 14 * mm, nom)
        unite = (c.produit.unite if c.produit else "u.") or "u."
        pdf.drawString(tx, y + lh - 18 * mm, f"{float(c.quantite):g} {unite}")
        if c.achat_id:
            pdf.drawString(tx, y + lh - 22 * mm, f"Achat #{c.achat_id}")
    pdf.save()
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/pdf",
                             headers={"Content-Disposition": 'inline; filename="etiquettes-colis.pdf"'})


# ══════════════════════════════════════════════════════════════════════════
# 3. Mise en dépôt et vérification (scan)
# ══════════════════════════════════════════════════════════════════════════

class ScanIn(BaseModel):
    code: str


@router.get("/colis")
def lister_colis(statut: Optional[str] = None, achat_id: Optional[int] = None,
                 db: Session = Depends(get_db)):
    q = db.query(BarColis)
    if statut:
        q = q.filter(BarColis.statut == statut.upper())
    if achat_id:
        q = q.filter(BarColis.achat_id == achat_id)
    return [_colis_dict(c) for c in q.order_by(BarColis.id.desc()).limit(1000).all()]


@router.get("/colis/code/{code}")
def colis_par_code(code: str, db: Session = Depends(get_db)):
    c = db.query(BarColis).filter(BarColis.code == _code_saisi(code)).first()
    if not c:
        raise HTTPException(404, f"Colis « {_code_saisi(code)} » inconnu.")
    return _colis_dict(c)


@router.post("/colis/verifier")
def verifier_colis(data: ScanIn, request: Request, db: Session = Depends(get_db),
                   user=Depends(_require_depot)):
    """Scan au dépôt : le colis est physiquement présent → son contenu entre
    dans le stock du dépôt."""
    code = _code_saisi(data.code)
    c = db.query(BarColis).filter(BarColis.code == code).first()
    if not c:
        raise HTTPException(404, f"Colis « {code} » inconnu.")
    if c.statut != "A_VERIFIER":
        etats = {"AU_DEPOT": "déjà vérifié au dépôt", "EN_PREPARATION": "dans une livraison en préparation",
                 "LIVRE": f"déjà livré au {LIEUX.get(c.lieu, c.lieu)}", "ANNULE": "annulé"}
        raise HTTPException(409, f"Colis {code} {etats.get(c.statut, c.statut)}.")
    c.statut = "AU_DEPOT"
    c.verifie_le = datetime.now(timezone.utc)
    c.verifie_par_id = user.id
    db.add(BarMouvementStock(
        produit_id=c.produit_id, type_mouvement="ENTREE", quantite=c.quantite, lieu=None,
        motif=f"Colis {c.code} vérifié au dépôt" + (f" (achat #{c.achat_id})" if c.achat_id else ""),
        utilisateur_id=user.id,
    ))
    db.commit()
    restants = (db.query(BarColis).filter(BarColis.achat_id == c.achat_id,
                                          BarColis.statut == "A_VERIFIER").count()
                if c.achat_id else 0)
    return {**_colis_dict(c), "restants_achat": restants}


@router.post("/colis/{colis_id}/annuler")
def annuler_colis(colis_id: int, db: Session = Depends(get_db), user=Depends(_require_depot)):
    """Retire un colis généré par erreur (seulement avant vérification)."""
    c = db.get(BarColis, colis_id)
    if not c:
        raise HTTPException(404, "Colis introuvable.")
    if c.statut != "A_VERIFIER":
        raise HTTPException(409, "Seul un colis pas encore vérifié peut être annulé.")
    c.statut = "ANNULE"
    db.commit()
    return _colis_dict(c)


# ══════════════════════════════════════════════════════════════════════════
# Stock par lieu
# ══════════════════════════════════════════════════════════════════════════

@router.get("/stock")
def stock_par_lieu(db: Session = Depends(get_db)):
    """Stock de chaque produit au dépôt, au Bar avant et au Bar piscine
    (dépôt + Bar avant + Bar piscine = total)."""
    st = stock_par_lieu_tous_produits(db)
    produits = db.query(BarProduit).filter(BarProduit.actif.is_(True)).order_by(BarProduit.nom).all()
    a_verifier = {}
    for c in db.query(BarColis).filter(BarColis.statut == "A_VERIFIER").all():
        a_verifier[c.produit_id] = a_verifier.get(c.produit_id, 0.0) + float(c.quantite)
    out = []
    for p in produits:
        s = st.get(p.id, {"devant": Decimal(0), "piscine": Decimal(0), "total": Decimal(0)})
        depot = s["total"] - s["devant"] - s["piscine"]
        out.append({
            "produit_id": p.id, "nom": p.nom, "categorie": p.categorie, "unite": p.unite,
            "unites_par_caisse": p.unites_par_caisse if p.vendu_par_caisse else None,
            "depot":   float(depot),
            "devant":  float(s["devant"]),
            "piscine": float(s["piscine"]),
            "total":   float(s["total"]),
            "a_verifier": a_verifier.get(p.id, 0.0),
            "seuil_alerte": float(p.seuil_alerte_stock or 0),
        })
    return out


# ══════════════════════════════════════════════════════════════════════════
# 4-7. Livraisons dépôt → bar
# ══════════════════════════════════════════════════════════════════════════

class LivraisonIn(BaseModel):
    lieu:  str
    notes: Optional[str] = None


class VracIn(BaseModel):
    produit_id: int
    quantite:   float = Field(gt=0)


def _livraison_en_preparation(db: Session, livraison_id: int) -> BarLivraisonInterne:
    l = db.get(BarLivraisonInterne, livraison_id)
    if not l:
        raise HTTPException(404, "Livraison introuvable.")
    if l.statut != "PREPARATION":
        raise HTTPException(409, f"Livraison {l.numero} déjà {l.statut.lower()}.")
    return l


@router.post("/livraisons", status_code=201)
def creer_livraison(data: LivraisonIn, request: Request, db: Session = Depends(get_db),
                    user=Depends(_require_depot)):
    lieu = (data.lieu or "").upper()
    if lieu not in LIEUX:
        raise HTTPException(422, "Destination invalide : DEVANT (Bar avant) ou PISCINE (Bar piscine).")
    l = BarLivraisonInterne(numero="TMP-" + uuid.uuid4().hex[:20], lieu=lieu, statut="PREPARATION",
                            notes=(data.notes or "").strip()[:300] or None, cree_par_id=user.id)
    db.add(l)
    db.flush()
    l.numero = f"LIV{now_haiti():%y%m%d}-{l.id:04d}"
    db.commit()
    return _livraison_dict(l)


@router.get("/livraisons")
def lister_livraisons(statut: Optional[str] = None, db: Session = Depends(get_db)):
    q = db.query(BarLivraisonInterne)
    if statut:
        q = q.filter(BarLivraisonInterne.statut == statut.upper())
    return [_livraison_dict(l, details=False)
            for l in q.order_by(BarLivraisonInterne.id.desc()).limit(200).all()]


@router.get("/livraisons/{livraison_id}")
def detail_livraison(livraison_id: int, db: Session = Depends(get_db)):
    l = db.get(BarLivraisonInterne, livraison_id)
    if not l:
        raise HTTPException(404, "Livraison introuvable.")
    return _livraison_dict(l)


@router.post("/livraisons/{livraison_id}/scan")
def scanner_colis_livraison(livraison_id: int, data: ScanIn, db: Session = Depends(get_db),
                            user=Depends(_require_depot)):
    l = _livraison_en_preparation(db, livraison_id)
    code = _code_saisi(data.code)
    c = db.query(BarColis).filter(BarColis.code == code).first()
    if not c:
        raise HTTPException(404, f"Colis « {code} » inconnu.")
    if c.statut == "A_VERIFIER":
        raise HTTPException(409, f"Colis {code} pas encore vérifié au dépôt : scannez-le d'abord en réception.")
    if c.statut == "EN_PREPARATION":
        ou = "cette livraison" if c.livraison_id == l.id else "une autre livraison"
        raise HTTPException(409, f"Colis {code} déjà scanné dans {ou}.")
    if c.statut != "AU_DEPOT":
        raise HTTPException(409, f"Colis {code} n'est pas au dépôt (statut {c.statut}).")
    c.statut = "EN_PREPARATION"
    c.livraison_id = l.id
    db.add(BarLivraisonLigne(livraison_id=l.id, produit_id=c.produit_id, colis_id=c.id, quantite=c.quantite))
    db.commit()
    db.refresh(l)
    return {**_livraison_dict(l), "colis_scanne": _colis_dict(c)}


@router.post("/livraisons/{livraison_id}/vrac")
def ajouter_vrac(livraison_id: int, data: VracIn, db: Session = Depends(get_db),
                 user=Depends(_require_depot)):
    """Ajoute une quantité sans étiquette, prise dans le stock du dépôt
    (stock présent avant la mise en place des étiquettes, unités au détail)."""
    l = _livraison_en_preparation(db, livraison_id)
    p = db.get(BarProduit, data.produit_id)
    if not p:
        raise HTTPException(404, "Produit introuvable.")
    qte = Decimal(str(data.quantite))
    dispo = _stock_depot(db, p.id) - _quantite_reservee(db, p.id) - _colis_au_depot(db, p.id)
    if qte > dispo:
        raise HTTPException(409, f"Stock en vrac insuffisant au dépôt pour « {p.nom} » "
                                 f"(disponible : {float(max(dispo, 0)):g}).")
    db.add(BarLivraisonLigne(livraison_id=l.id, produit_id=p.id, quantite=qte))
    db.commit()
    db.refresh(l)
    return _livraison_dict(l)


def _colis_au_depot(db: Session, produit_id: int) -> Decimal:
    """Unités au dépôt déjà rattachées à un colis étiqueté (AU_DEPOT ou en
    préparation) : elles se livrent par scan, pas en vrac."""
    rows = db.query(BarColis).filter(BarColis.produit_id == produit_id,
                                     BarColis.statut.in_(("AU_DEPOT", "EN_PREPARATION"))).all()
    return sum((Decimal(str(c.quantite)) for c in rows), Decimal("0"))


@router.delete("/livraisons/{livraison_id}/lignes/{ligne_id}")
def retirer_ligne(livraison_id: int, ligne_id: int, db: Session = Depends(get_db),
                  user=Depends(_require_depot)):
    l = _livraison_en_preparation(db, livraison_id)
    ligne = db.get(BarLivraisonLigne, ligne_id)
    if not ligne or ligne.livraison_id != l.id:
        raise HTTPException(404, "Ligne introuvable.")
    if ligne.colis:
        ligne.colis.statut = "AU_DEPOT"
        ligne.colis.livraison_id = None
    db.delete(ligne)
    db.commit()
    db.refresh(l)
    return _livraison_dict(l)


@router.post("/livraisons/{livraison_id}/valider")
def valider_livraison(livraison_id: int, db: Session = Depends(get_db), user=Depends(_require_depot)):
    """Mise à jour des stocks : chaque ligne sort du dépôt et entre au bar."""
    l = _livraison_en_preparation(db, livraison_id)
    if not l.lignes:
        raise HTTPException(422, "La livraison est vide : scannez au moins un colis.")
    # Contrôle du vrac : le dépôt doit encore l'avoir (hors colis étiquetés)
    besoins: dict[int, Decimal] = {}
    for x in l.lignes:
        if x.colis_id is None:
            besoins[x.produit_id] = besoins.get(x.produit_id, Decimal(0)) + Decimal(str(x.quantite))
    for pid, qte in besoins.items():
        dispo = (_stock_depot(db, pid) - _colis_au_depot(db, pid)
                 - _quantite_reservee(db, pid, sauf_livraison=l.id))
        if qte > dispo:
            nom = db.get(BarProduit, pid).nom
            raise HTTPException(409, f"Stock en vrac insuffisant au dépôt pour « {nom} » "
                                     f"(disponible : {float(max(dispo, 0)):g}, demandé : {float(qte):g}).")
    dest = LIEUX[l.lieu]
    for x in l.lignes:
        ref = f"colis {x.colis.code}" if x.colis else "vrac"
        motif = f"Livraison {l.numero} — {ref} → {dest}"
        db.add(BarMouvementStock(produit_id=x.produit_id, type_mouvement="TRANSFERT",
                                 quantite=-Decimal(str(x.quantite)), lieu=None,
                                 motif=motif, utilisateur_id=user.id))
        db.add(BarMouvementStock(produit_id=x.produit_id, type_mouvement="TRANSFERT",
                                 quantite=Decimal(str(x.quantite)), lieu=l.lieu,
                                 motif=motif, utilisateur_id=user.id))
        if x.colis:
            x.colis.statut = "LIVRE"
            x.colis.lieu = l.lieu
    l.statut = "VALIDEE"
    l.valide_le = datetime.now(timezone.utc)
    l.valide_par_id = user.id
    db.commit()
    db.refresh(l)
    return _livraison_dict(l)


@router.post("/livraisons/{livraison_id}/annuler")
def annuler_livraison(livraison_id: int, db: Session = Depends(get_db), user=Depends(_require_depot)):
    l = _livraison_en_preparation(db, livraison_id)
    for x in l.lignes:
        if x.colis:
            x.colis.statut = "AU_DEPOT"
            x.colis.livraison_id = None
    l.statut = "ANNULEE"
    db.commit()
    return _livraison_dict(l)


# ══════════════════════════════════════════════════════════════════════════
# Inventaire physique (dépôt ou bar)
# ══════════════════════════════════════════════════════════════════════════

class LigneInventaireIn(BaseModel):
    produit_id: int
    quantite:   float = Field(ge=0)


class InventaireIn(BaseModel):
    lieu:   str                      # DEPOT | DEVANT | PISCINE
    motif:  str
    lignes: List[LigneInventaireIn]


@router.post("/inventaire")
def inventaire(data: InventaireIn, db: Session = Depends(get_db), user=Depends(_require_depot)):
    """Aligne le stock d'un lieu sur le comptage physique : un AJUSTEMENT par
    produit dont la quantité comptée diffère du stock calculé."""
    lieu_in = (data.lieu or "").upper()
    if lieu_in not in ("DEPOT", *LIEUX):
        raise HTTPException(422, "Lieu invalide : DEPOT, DEVANT ou PISCINE.")
    if len((data.motif or "").strip()) < 5:
        raise HTTPException(422, "Indiquez le motif de l'inventaire (5 caractères minimum).")
    lieu = None if lieu_in == "DEPOT" else lieu_in
    st = stock_par_lieu_tous_produits(db)
    ajustes = []
    for li in data.lignes:
        p = db.get(BarProduit, li.produit_id)
        if not p:
            raise HTTPException(404, f"Produit #{li.produit_id} introuvable.")
        s = st.get(p.id, {"devant": Decimal(0), "piscine": Decimal(0), "total": Decimal(0)})
        actuel = (s["total"] - s["devant"] - s["piscine"]) if lieu is None else s[lieu.lower()]
        compte = Decimal(str(li.quantite))
        ecart = compte - actuel
        if ecart == 0:
            continue
        db.add(BarMouvementStock(produit_id=p.id, type_mouvement="AJUSTEMENT", quantite=ecart, lieu=lieu,
                                 motif=f"Inventaire {LIEUX.get(lieu, 'Dépôt')} — {data.motif.strip()}"[:300],
                                 utilisateur_id=user.id))
        ajustes.append({"produit_id": p.id, "nom": p.nom, "avant": float(actuel),
                        "compte": float(compte), "ecart": float(ecart)})
    db.commit()
    return {"lieu": lieu_in, "nb_ajustes": len(ajustes), "ajustements": ajustes}
