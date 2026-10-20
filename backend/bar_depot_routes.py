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

Traçabilité : chaque étape d'un colis est inscrite dans
bar_colis_evenements (horodatage du serveur de base, utilisateur, lieu,
livraison). Rescanner un colis déjà livré renvoie sa fiche de livraison
(POST /colis/consulter, GET /colis/{id}/fiche).

Sécurité des codes :
  - Format : BC + année (2 chiffres) + n° du colis (6 chiffres) + clé de
    contrôle (4 chiffres) = HMAC-SHA256 du début avec un secret propre à
    l'installation (table secrets_application). Un code inventé ou dont un
    chiffre est modifié est rejeté avant toute lecture en base, et le rejet
    est journalisé. Les codes générés avant la clé (BC + 8 chiffres)
    restent acceptés uniquement s'ils existent en base.
  - Le texte lu par le scanner est validé par liste blanche (lettres BC et
    chiffres uniquement, longueur bornée) : rien d'autre n'atteint la base,
    et les requêtes sont paramétrées (SQLAlchemy).
  - Concurrence : chaque changement d'état est un UPDATE conditionnel sur
    l'état attendu (« seulement si encore A_VERIFIER ») ; le premier scan
    gagne, le second reçoit 409 — jamais de double entrée de stock.

Le stock reste calculé à partir de BarMouvementStock (source unique) :
lieu NULL = dépôt, DEVANT = Bar avant, PISCINE = Bar piscine. Une
livraison crée deux mouvements TRANSFERT de même quantité : le stock total
ne change pas, seule sa localisation change.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import re
import secrets
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db
from models import (
    BarAchat, BarColis, BarColisEvenement, BarLivraisonInterne, BarLivraisonLigne,
    BarMouvementStock, BarProduit, SecretApplication, Utilisateur,
)
from pos_service import stock_par_lieu_tous_produits
from tz_utils import now_haiti

router = APIRouter(prefix="/api/pos/depot", tags=["Dépôt Bar"])

LIEUX = {"DEVANT": "Bar avant", "PISCINE": "Bar piscine"}
MAX_COLIS_PAR_RECEPTION = 500

STATUTS = {
    "A_VERIFIER":     "À vérifier au dépôt",
    "AU_DEPOT":       "Au dépôt",
    "EN_PREPARATION": "En préparation de livraison",
    "LIVRE":          "Déjà livré",
    "ANNULE":         "Annulé",
}
EVENEMENTS = {
    "CREE":              "Colis créé à la réception",
    "VERIFIE":           "Vérifié au dépôt — entré en stock",
    "AJOUTE_LIVRAISON":  "Scanné dans une livraison",
    "RETIRE_LIVRAISON":  "Retiré de la livraison",
    "LIVRE":             "Livré",
    "LIVRE_MANUEL":      "Livré sans scan (livraison manuelle)",
    "RETOUR_DEPOT":      "Livraison annulée — retour au dépôt",
    "ANNULE":            "Colis annulé",
    "CONSULTATION":      "Fiche consultée",
    "SCAN_REFUSE":       "Scan refusé",
    "REJET":             "Code rejeté",
}


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


def _require_connecte(request: Request):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(403, "Non autorisé.")
    return user


# ── Codes : génération signée et validation stricte ──────────────────────

_RE_CODE_SIGNE = re.compile(r"^BC(\d{2})(\d{6})(\d{4})$")
_RE_CODE_ANCIEN = re.compile(r"^BC\d{8}$")
_SECRET_CLE = "bar_colis_hmac"
_secret_cache: dict = {}


def _secret_codes(db: Session) -> bytes:
    """Clé de signature des codes, créée une fois par installation et
    conservée en base (jamais renvoyée par l'API). Créée dans sa PROPRE
    transaction, validée immédiatement : l'annulation de la requête en
    cours ne peut jamais l'effacer après qu'elle a servi à signer un code."""
    if "v" in _secret_cache:
        return _secret_cache["v"]
    from database import SessionLocal
    with SessionLocal() as s:
        row = s.query(SecretApplication).filter_by(cle=_SECRET_CLE).first()
        if not row:
            try:
                s.add(SecretApplication(cle=_SECRET_CLE, valeur=secrets.token_hex(32)))
                s.commit()
            except IntegrityError:
                s.rollback()        # créée au même moment par une autre requête
            row = s.query(SecretApplication).filter_by(cle=_SECRET_CLE).first()
        _secret_cache["v"] = row.valeur.encode()
    return _secret_cache["v"]


def initialiser_secret_codes() -> None:
    """Appelé au démarrage de l'application : crée la clé de signature si
    elle n'existe pas encore, avant toute requête (évite qu'elle soit créée
    pendant une transaction d'écriture)."""
    _secret_cache.pop("v", None)
    _secret_codes(None)


def _cle_controle(db: Session, base: str) -> str:
    mac = hmac.new(_secret_codes(db), base.encode(), hashlib.sha256).hexdigest()
    return f"{int(mac, 16) % 10000:04d}"


def generer_code(db: Session, colis_id: int) -> str:
    base = f"BC{now_haiti():%y}{colis_id:06d}"
    return base + _cle_controle(db, base)


def code_lisible(code: str) -> str:
    """BC26000123 4817 → « BC26-000123-4817 » pour la lecture humaine."""
    m = _RE_CODE_SIGNE.match(code or "")
    return f"BC{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else (code or "")


class CodeRejete(Exception):
    def __init__(self, message: str, motif: str):
        super().__init__(message)
        self.message, self.motif = message, motif


def normaliser_code(db: Session, texte: str) -> str:
    """Valide le texte lu par le scanner (liste blanche stricte) et vérifie
    la clé de contrôle. Lève CodeRejete sinon. Accepte le code seul, avec
    tirets ou espaces, ou une URL qui se termine par le code."""
    brut = (texte or "")[:200]
    t = brut.strip()
    if "/" in t:
        t = t.rstrip("/").rsplit("/", 1)[-1]
    t = re.sub(r"[\s\-]", "", t).upper()
    if not t:
        raise CodeRejete("Aucun code lu.", "vide")
    if len(t) > 20 or not re.fullmatch(r"[A-Z0-9]+", t):
        raise CodeRejete("Ce code n'est pas un code de colis du dépôt.", "format")
    m = _RE_CODE_SIGNE.match(t)
    if m:
        base = t[:-4]
        if not hmac.compare_digest(_cle_controle(db, base), m.group(3)):
            raise CodeRejete("Code invalide ou falsifié : la clé de contrôle ne correspond pas.", "cle")
        return t
    if _RE_CODE_ANCIEN.match(t):
        return t        # ancien format : accepté seulement s'il existe en base
    raise CodeRejete("Ce code n'est pas un code de colis du dépôt.", "format")


def _journaliser(db: Session, type_: str, colis: Optional[BarColis] = None, *, lieu: Optional[str] = None,
                 livraison_id: Optional[int] = None, utilisateur_id: Optional[int] = None,
                 details: Optional[str] = None, code_saisi: Optional[str] = None) -> None:
    db.add(BarColisEvenement(
        colis_id=colis.id if colis else None, type=type_, lieu=lieu, livraison_id=livraison_id,
        utilisateur_id=utilisateur_id, details=(details or None) and details[:300],
        code_saisi=(code_saisi or None) and re.sub(r"[^A-Za-z0-9\-]", "?", code_saisi)[:40],
    ))


def _trouver_colis(db: Session, texte: str, user_id: Optional[int]) -> BarColis:
    """Code validé puis colis existant ; tout rejet est journalisé."""
    try:
        code = normaliser_code(db, texte)
    except CodeRejete as e:
        _journaliser(db, "REJET", utilisateur_id=user_id, code_saisi=texte, details=e.motif)
        db.commit()
        raise HTTPException(400, e.message)
    c = db.query(BarColis).filter(BarColis.code == code).first()
    if not c:
        _journaliser(db, "REJET", utilisateur_id=user_id, code_saisi=code, details="inconnu")
        db.commit()
        raise HTTPException(404, f"Colis {code_lisible(code)} inconnu.")
    return c


def _transition(db: Session, colis_id: int, de: tuple, valeurs: dict) -> bool:
    """Change l'état d'un colis seulement s'il est encore dans un des états
    attendus (UPDATE conditionnel, atomique) : de deux scans simultanés,
    un seul réussit."""
    n = (db.query(BarColis)
         .filter(BarColis.id == colis_id, BarColis.statut.in_(de))
         .update(valeurs, synchronize_session=False))
    return n == 1


def _refus(db: Session, c: BarColis, user_id: Optional[int], message: str, code_http: int = 409):
    """Refus d'une action sur un colis : journalisé, et la réponse indique
    si une fiche de livraison existe (colis déjà livré)."""
    db.rollback()
    db.refresh(c)
    _journaliser(db, "SCAN_REFUSE", c, lieu=c.lieu, livraison_id=c.livraison_id,
                 utilisateur_id=user_id, details=message)
    db.commit()
    raise HTTPException(code_http, {"message": message, "colis_id": c.id, "statut": c.statut,
                                    "deja_livre": c.statut == "LIVRE"})


def _etat_texte(c: BarColis) -> str:
    if c.statut == "LIVRE":
        liv = c.livraison
        quand = liv.date_reelle or liv.valide_le if liv else None
        txt = f"déjà livré au {LIEUX.get(c.lieu, c.lieu)}"
        if quand:
            q = quand if quand.tzinfo else quand.replace(tzinfo=timezone.utc)
            from tz_utils import HAITI_TZ
            txt += f" le {q.astimezone(HAITI_TZ):%d/%m/%Y à %H:%M}"
        if liv and liv.mode == "MANUELLE":
            txt += " (livraison manuelle, sans scan) — ce code n'est plus valable"
        return txt
    return {"AU_DEPOT": "déjà vérifié au dépôt", "EN_PREPARATION": "dans une livraison en préparation",
            "A_VERIFIER": "pas encore vérifié au dépôt", "ANNULE": "annulé"}.get(c.statut, c.statut)


# ── Représentations ───────────────────────────────────────────────────────

def _iso(dt) -> Optional[str]:
    """Date-heure ISO toujours avec fuseau (UTC si la base n'en donne pas),
    pour un affichage exact à l'heure locale dans le navigateur."""
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


_MOTIFS_REJET = {
    "cle": "Clé de contrôle invalide (code falsifié ou mal lu)",
    "format": "Format non reconnu",
    "inconnu": "Code inexistant",
    "vide": "Aucun code lu",
}

def _colis_dict(c: BarColis) -> dict:
    return {
        "id":           c.id,
        "code":         c.code,
        "code_lisible": code_lisible(c.code),
        "produit_id":   c.produit_id,
        "produit_nom":  c.produit.nom if c.produit else None,
        "unite":        c.produit.unite if c.produit else None,
        "achat_id":     c.achat_id,
        "quantite":     float(c.quantite),
        "statut":       c.statut,
        "statut_libelle": STATUTS.get(c.statut, c.statut),
        "lieu":         c.lieu,
        "lieu_nom":     LIEUX.get(c.lieu) if c.lieu else None,
        "livraison_id": c.livraison_id,
        "cree_le":      _iso(c.cree_le),
        "verifie_le":   _iso(c.verifie_le),
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
        "cree_le":     _iso(l.cree_le),
        "cree_par":    l.cree_par.nom_complet if l.cree_par else None,
        "valide_le":   _iso(l.valide_le),
        "valide_par":  l.valide_par.nom_complet if l.valide_par else None,
        "mode":        l.mode or "SCAN",
        "motif_manuel": l.motif_manuel,
        "date_reelle": _iso(l.date_reelle),
        "nb_lignes":   len(l.lignes),
        "total_unites": float(sum(Decimal(str(x.quantite)) for x in l.lignes)),
    }
    if details:
        d["lignes"] = [{
            "id":          x.id,
            "produit_id":  x.produit_id,
            "produit_nom": x.produit.nom if x.produit else None,
            "colis_id":    x.colis_id,
            "colis_code":  code_lisible(x.colis.code) if x.colis else None,
            "colis_code_brut": x.colis.code if x.colis else None,
            "quantite":    float(x.quantite),
        } for x in l.lignes]
    return d


def _fiche(db: Session, c: BarColis) -> dict:
    """Fiche de livraison / traçabilité complète d'un colis."""
    evts = (db.query(BarColisEvenement)
            .filter(BarColisEvenement.colis_id == c.id)
            .order_by(BarColisEvenement.horodatage, BarColisEvenement.id).all())
    liv = db.get(BarLivraisonInterne, c.livraison_id) if c.livraison_id else None
    achat = c.achat
    return {
        "colis": _colis_dict(c),
        "deja_livre": c.statut == "LIVRE",
        "livraison": _livraison_dict(liv, details=False) if liv else None,
        "achat": {
            "id": achat.id,
            "fournisseur": achat.fournisseur,
            "date_achat": _iso(achat.date_achat),
        } if achat else None,
        "historique": [{
            "horodatage": _iso(e.horodatage),
            "type": e.type,
            "libelle": EVENEMENTS.get(e.type, e.type),
            "lieu": e.lieu,
            "lieu_nom": LIEUX.get(e.lieu) if e.lieu else "Dépôt",
            "livraison": e.livraison.numero if e.livraison else None,
            "par": e.utilisateur.nom_complet if e.utilisateur else None,
            "details": e.details,
        } for e in evts],
    }


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


def _colis_au_depot(db: Session, produit_id: int) -> Decimal:
    """Unités au dépôt déjà rattachées à un colis étiqueté (AU_DEPOT ou en
    préparation) : elles se livrent par scan, pas en vrac."""
    rows = db.query(BarColis).filter(BarColis.produit_id == produit_id,
                                     BarColis.statut.in_(("AU_DEPOT", "EN_PREPARATION"))).all()
    return sum((Decimal(str(c.quantite)) for c in rows), Decimal("0"))


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
        raise HTTPException(422, f"Quantité trop élevée : {float(qte):g} unités = {len(tailles)} colis "
                                 f"(maximum {MAX_COLIS_PAR_RECEPTION} par achat). Probable faute de saisie : "
                                 f"annulez cet achat et ressaisissez-le avec la bonne quantité.")

    achat.statut = "CONFIRME"
    colis = []
    for t in tailles:
        c = BarColis(code="TMP-" + uuid.uuid4().hex[:20], produit_id=achat.produit_id,
                     achat_id=achat.id, quantite=t, statut="A_VERIFIER")
        db.add(c)
        db.flush()
        c.code = generer_code(db, c.id)
        _journaliser(db, "CREE", c, utilisateur_id=utilisateur_id,
                     details=f"Achat #{achat.id} · {float(t):g} {produit.unite if produit else 'u.'}")
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
        "date_achat":  _iso(a.date_achat),
        "statut":      a.statut,
    } for a in achats if a.id not in avec_colis and a.statut == "EN_ATTENTE"]
    a_verifier = db.query(BarColis).filter(BarColis.statut == "A_VERIFIER").order_by(BarColis.id).all()
    return {
        "achats_a_receptionner": a_receptionner,
        "colis_a_verifier": [_colis_dict(c) for c in a_verifier],
    }


# ══════════════════════════════════════════════════════════════════════════
# Étiquettes — format 2 × 1 pouce (50,8 × 25,4 mm), une étiquette par page
# ══════════════════════════════════════════════════════════════════════════

ETIQ_LARGEUR = 2 * 72      # points (1 pouce = 72 pt)
ETIQ_HAUTEUR = 1 * 72


def _texte_ajuste(pdf, texte: str, police: str, taille: float, largeur_max: float) -> str:
    """Raccourcit le texte (avec …) pour qu'il tienne dans la largeur."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    if stringWidth(texte, police, taille) <= largeur_max:
        return texte
    while texte and stringWidth(texte + "…", police, taille) > largeur_max:
        texte = texte[:-1]
    return texte.rstrip() + "…"


def _dessiner_etiquette(pdf, c: BarColis) -> None:
    """Une étiquette 2 × 1 po : code-barres Code 128 sur toute la largeur
    (lisible par une douchette 1D comme par la caméra), puis le code en
    clair, le produit, la quantité et le n° d'achat."""
    from reportlab.graphics.barcode.code128 import Code128

    marge = 5                                   # ≈ 1,8 mm, zone non imprimable des imprimantes thermiques
    largeur_utile = ETIQ_LARGEUR - 2 * marge

    # Code-barres : module de 3 points d'impression à 203 dpi (résolution
    # standard des imprimantes d'étiquettes 2 × 1) pour des barres nettes ;
    # zones blanches de 10 modules de chaque côté (exigées par les lecteurs).
    # Si le code est plus long que prévu, le module est réduit pour tenir.
    module = 3 * 72 / 203
    essai = Code128(c.code, barWidth=module, humanReadable=False, quiet=False)
    if essai.width + 20 * module > largeur_utile + 2 * marge:
        module = (largeur_utile + 2 * marge) / (essai.width / module + 20)
    code_barres = Code128(c.code, barWidth=module, barHeight=31, humanReadable=False,
                          quiet=True, lquiet=10 * module, rquiet=10 * module)
    x_cb = (ETIQ_LARGEUR - code_barres.width) / 2
    code_barres.drawOn(pdf, x_cb, ETIQ_HAUTEUR - marge - 31)

    # Code en clair, centré, sous le code-barres
    pdf.setFont("Helvetica-Bold", 9.5)
    pdf.drawCentredString(ETIQ_LARGEUR / 2, 24.5, code_lisible(c.code))

    # Produit
    nom = (c.produit.nom if c.produit else "") or ""
    pdf.setFont("Helvetica-Bold", 7.5)
    pdf.drawString(marge, 14.5, _texte_ajuste(pdf, nom, "Helvetica-Bold", 7.5, largeur_utile))

    # Quantité (gauche) et achat (droite)
    unite = (c.produit.unite if c.produit else "") or "u."
    pdf.setFont("Helvetica", 7)
    pdf.drawString(marge, 5.5, f"{float(c.quantite):g} {unite}")
    if c.achat_id:
        pdf.drawRightString(ETIQ_LARGEUR - marge, 5.5, f"Achat #{c.achat_id}")


@router.get("/etiquettes.pdf")
def etiquettes(achat_id: Optional[int] = None, ids: Optional[str] = None,
               db: Session = Depends(get_db)):
    """PDF d'étiquettes 2 × 1 pouce, une par page, dans l'ordre des colis.
    À imprimer sur une imprimante d'étiquettes, à l'échelle 100 % (taille
    réelle), sans « ajuster à la page »."""
    from reportlab.pdfgen import canvas

    q = db.query(BarColis)
    if achat_id:
        q = q.filter(BarColis.achat_id == achat_id)
    elif ids:
        try:
            liste = [int(x) for x in ids.split(",") if x.strip()][:MAX_COLIS_PAR_RECEPTION]
        except ValueError:
            raise HTTPException(400, "ids invalide.")
        q = q.filter(BarColis.id.in_(liste))
    else:
        q = q.filter(BarColis.statut == "A_VERIFIER")
    colis = q.order_by(BarColis.id).all()
    if not colis:
        raise HTTPException(404, "Aucun colis à imprimer.")

    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=(ETIQ_LARGEUR, ETIQ_HAUTEUR))
    pdf.setTitle("Étiquettes colis 2x1")
    for i, c in enumerate(colis):
        if i:
            pdf.showPage()
        _dessiner_etiquette(pdf, c)
    pdf.save()
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/pdf",
                             headers={"Content-Disposition": 'inline; filename="etiquettes-colis-2x1.pdf"'})


# ══════════════════════════════════════════════════════════════════════════
# 3. Mise en dépôt et vérification (scan) · fiche de livraison
# ══════════════════════════════════════════════════════════════════════════

class ScanIn(BaseModel):
    code: str = Field(max_length=200)


@router.get("/colis")
def lister_colis(statut: Optional[str] = None, achat_id: Optional[int] = None,
                 db: Session = Depends(get_db)):
    q = db.query(BarColis)
    if statut:
        if statut.upper() not in STATUTS:
            raise HTTPException(400, "Statut inconnu.")
        q = q.filter(BarColis.statut == statut.upper())
    if achat_id:
        q = q.filter(BarColis.achat_id == achat_id)
    return [_colis_dict(c) for c in q.order_by(BarColis.id.desc()).limit(1000).all()]


@router.get("/colis/code/{code}")
def colis_par_code(code: str, request: Request, db: Session = Depends(get_db)):
    u = _require_connecte(request)
    return _colis_dict(_trouver_colis(db, code, u.id))


@router.post("/colis/consulter")
def consulter_colis(data: ScanIn, request: Request, db: Session = Depends(get_db)):
    """Scan en mode consultation : renvoie la fiche du colis (fiche de
    livraison s'il est déjà livré) et inscrit la consultation au journal."""
    u = _require_connecte(request)
    c = _trouver_colis(db, data.code, u.id)
    _journaliser(db, "CONSULTATION", c, lieu=c.lieu, livraison_id=c.livraison_id, utilisateur_id=u.id)
    db.commit()
    return _fiche(db, c)


@router.get("/colis/{colis_id}/fiche")
def fiche_colis(colis_id: int, request: Request, db: Session = Depends(get_db)):
    _require_connecte(request)
    c = db.get(BarColis, colis_id)
    if not c:
        raise HTTPException(404, "Colis introuvable.")
    return _fiche(db, c)


@router.post("/colis/verifier")
def verifier_colis(data: ScanIn, request: Request, db: Session = Depends(get_db),
                   user=Depends(_require_depot)):
    """Scan au dépôt : le colis est physiquement présent → son contenu entre
    dans le stock du dépôt. Atomique : un seul des scans simultanés réussit."""
    c = _trouver_colis(db, data.code, user.id)
    maintenant = datetime.now(timezone.utc)
    if not _transition(db, c.id, ("A_VERIFIER",),
                       {"statut": "AU_DEPOT", "verifie_le": maintenant, "verifie_par_id": user.id}):
        db.refresh(c)
        _refus(db, c, user.id, f"Colis {code_lisible(c.code)} {_etat_texte(c)}.")
    db.add(BarMouvementStock(
        produit_id=c.produit_id, type_mouvement="ENTREE", quantite=c.quantite, lieu=None,
        motif=f"Colis {code_lisible(c.code)} vérifié au dépôt" + (f" (achat #{c.achat_id})" if c.achat_id else ""),
        utilisateur_id=user.id,
    ))
    _journaliser(db, "VERIFIE", c, utilisateur_id=user.id)
    db.commit()
    db.refresh(c)
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
    if not _transition(db, c.id, ("A_VERIFIER",), {"statut": "ANNULE"}):
        db.refresh(c)
        _refus(db, c, user.id, "Seul un colis pas encore vérifié peut être annulé.")
    _journaliser(db, "ANNULE", c, utilisateur_id=user.id)
    db.commit()
    db.refresh(c)
    return _colis_dict(c)


@router.get("/journal")
def journal(limite: int = 200, type: Optional[str] = None, db: Session = Depends(get_db),
            user=Depends(_require_depot)):
    """Derniers événements de traçabilité (dont les scans rejetés)."""
    q = db.query(BarColisEvenement)
    if type:
        q = q.filter(BarColisEvenement.type == type.upper())
    rows = q.order_by(BarColisEvenement.horodatage.desc(), BarColisEvenement.id.desc()).limit(min(limite, 1000)).all()
    return [{
        "horodatage": _iso(e.horodatage),
        "type": e.type, "libelle": EVENEMENTS.get(e.type, e.type),
        "colis_id": e.colis_id, "code": code_lisible(e.colis.code) if e.colis else (e.code_saisi or None),
        "produit_nom": e.colis.produit.nom if e.colis and e.colis.produit else None,
        "lieu_nom": LIEUX.get(e.lieu) if e.lieu else "Dépôt",
        "livraison": e.livraison.numero if e.livraison else None,
        "par": e.utilisateur.nom_complet if e.utilisateur else None,
        "details": _MOTIFS_REJET.get(e.details, e.details) if e.type == "REJET" else e.details,
    } for e in rows]


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
    lieu:  str = Field(max_length=20)
    notes: Optional[str] = Field(default=None, max_length=300)


class VracIn(BaseModel):
    produit_id: int
    quantite:   float = Field(gt=0, le=100000)


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
    c = _trouver_colis(db, data.code, user.id)
    if not _transition(db, c.id, ("AU_DEPOT",), {"statut": "EN_PREPARATION", "livraison_id": l.id}):
        db.refresh(c)
        if c.statut == "EN_PREPARATION":
            ou = "cette livraison" if c.livraison_id == l.id else "une autre livraison"
            _refus(db, c, user.id, f"Colis {code_lisible(c.code)} déjà scanné dans {ou}.")
        if c.statut == "A_VERIFIER":
            _refus(db, c, user.id, f"Colis {code_lisible(c.code)} pas encore vérifié au dépôt : scannez-le d'abord en réception.")
        _refus(db, c, user.id, f"Colis {code_lisible(c.code)} {_etat_texte(c)}.")
    db.add(BarLivraisonLigne(livraison_id=l.id, produit_id=c.produit_id, colis_id=c.id, quantite=c.quantite))
    _journaliser(db, "AJOUTE_LIVRAISON", c, lieu=l.lieu, livraison_id=l.id, utilisateur_id=user.id)
    db.commit()
    db.refresh(l)
    db.refresh(c)
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


@router.delete("/livraisons/{livraison_id}/lignes/{ligne_id}")
def retirer_ligne(livraison_id: int, ligne_id: int, db: Session = Depends(get_db),
                  user=Depends(_require_depot)):
    l = _livraison_en_preparation(db, livraison_id)
    ligne = db.get(BarLivraisonLigne, ligne_id)
    if not ligne or ligne.livraison_id != l.id:
        raise HTTPException(404, "Ligne introuvable.")
    if ligne.colis_id:
        _transition(db, ligne.colis_id, ("EN_PREPARATION",), {"statut": "AU_DEPOT", "livraison_id": None})
        _journaliser(db, "RETIRE_LIVRAISON", ligne.colis, livraison_id=l.id, utilisateur_id=user.id)
    db.delete(ligne)
    db.commit()
    db.refresh(l)
    return _livraison_dict(l)


@router.post("/livraisons/{livraison_id}/valider")
def valider_livraison(livraison_id: int, db: Session = Depends(get_db), user=Depends(_require_depot)):
    """Mise à jour des stocks : chaque ligne sort du dépôt et entre au bar.
    Le passage PREPARATION → VALIDEE est conditionnel : une double
    validation (deux clics, deux postes) ne crée jamais deux transferts."""
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
    maintenant = datetime.now(timezone.utc)
    n = (db.query(BarLivraisonInterne)
         .filter(BarLivraisonInterne.id == l.id, BarLivraisonInterne.statut == "PREPARATION")
         .update({"statut": "VALIDEE", "valide_le": maintenant, "valide_par_id": user.id},
                 synchronize_session=False))
    if n != 1:
        db.rollback()
        raise HTTPException(409, f"Livraison {l.numero} déjà validée ou annulée.")
    dest = LIEUX[l.lieu]
    for x in l.lignes:
        ref = f"colis {code_lisible(x.colis.code)}" if x.colis else "vrac"
        motif = f"Livraison {l.numero} — {ref} → {dest}"
        db.add(BarMouvementStock(produit_id=x.produit_id, type_mouvement="TRANSFERT",
                                 quantite=-Decimal(str(x.quantite)), lieu=None,
                                 motif=motif, utilisateur_id=user.id))
        db.add(BarMouvementStock(produit_id=x.produit_id, type_mouvement="TRANSFERT",
                                 quantite=Decimal(str(x.quantite)), lieu=l.lieu,
                                 motif=motif, utilisateur_id=user.id))
        if x.colis_id:
            _transition(db, x.colis_id, ("EN_PREPARATION",), {"statut": "LIVRE", "lieu": l.lieu})
            _journaliser(db, "LIVRE", x.colis, lieu=l.lieu, livraison_id=l.id, utilisateur_id=user.id,
                         details=f"Livraison {l.numero} vers le {dest}")
    db.commit()
    db.refresh(l)
    return _livraison_dict(l)


@router.post("/livraisons/{livraison_id}/annuler")
def annuler_livraison(livraison_id: int, db: Session = Depends(get_db), user=Depends(_require_depot)):
    l = _livraison_en_preparation(db, livraison_id)
    n = (db.query(BarLivraisonInterne)
         .filter(BarLivraisonInterne.id == l.id, BarLivraisonInterne.statut == "PREPARATION")
         .update({"statut": "ANNULEE"}, synchronize_session=False))
    if n != 1:
        db.rollback()
        raise HTTPException(409, f"Livraison {l.numero} déjà validée ou annulée.")
    for x in l.lignes:
        if x.colis_id:
            _transition(db, x.colis_id, ("EN_PREPARATION",), {"statut": "AU_DEPOT", "livraison_id": None})
            _journaliser(db, "RETOUR_DEPOT", x.colis, livraison_id=l.id, utilisateur_id=user.id)
    db.commit()
    db.refresh(l)
    return _livraison_dict(l)


# ══════════════════════════════════════════════════════════════════════════
# Livraison manuelle (sans scan) — internet ou scanner en panne
# ══════════════════════════════════════════════════════════════════════════

DELAI_MAX_REGULARISATION_JOURS = 7
MOTIF_MANUEL_MIN = 10


class LivraisonManuelleIn(BaseModel):
    lieu:          str = Field(max_length=20)
    colis_ids:     List[int] = Field(min_length=1, max_length=MAX_COLIS_PAR_RECEPTION)
    motif:         str = Field(max_length=250)
    date_reelle:   datetime                  # date et heure réelles de la livraison


@router.post("/livraisons/manuelle", status_code=201)
def livraison_manuelle(data: LivraisonManuelleIn, db: Session = Depends(get_db), user=Depends(_require_depot)):
    """Enregistre une livraison faite sans scanner (internet ou scanner en
    panne). Règles strictes :
      - réservé au dépôt (mêmes droits que la livraison par scan) ;
      - motif écrit obligatoire ; date-heure réelle déclarée, ni dans le
        futur ni de plus de 7 jours ;
      - seuls des colis encore au dépôt (ou reçus mais pas encore vérifiés)
        peuvent être livrés ; chacun change d'état de façon atomique ;
      - les colis deviennent LIVRE : leur code n'est plus utilisable, tout
        scan ultérieur est refusé et renvoie la fiche (livré sans scan, quand,
        où, par qui, pourquoi)."""
    lieu = (data.lieu or "").upper()
    if lieu not in LIEUX:
        raise HTTPException(422, "Destination invalide : DEVANT (Bar avant) ou PISCINE (Bar piscine).")
    motif = " ".join((data.motif or "").split())
    if len(motif) < MOTIF_MANUEL_MIN:
        raise HTTPException(422, f"Expliquez pourquoi la livraison n'a pas été scannée ({MOTIF_MANUEL_MIN} caractères minimum).")
    reelle = data.date_reelle if data.date_reelle.tzinfo else data.date_reelle.replace(tzinfo=now_haiti().tzinfo)
    maintenant = datetime.now(timezone.utc)
    if reelle > maintenant.replace(microsecond=0) + __import__("datetime").timedelta(minutes=5):
        raise HTTPException(422, "La date de livraison ne peut pas être dans le futur.")
    if (maintenant - reelle).days >= DELAI_MAX_REGULARISATION_JOURS:
        raise HTTPException(422, f"Livraison trop ancienne : régularisation possible sur {DELAI_MAX_REGULARISATION_JOURS} jours seulement. Faites un inventaire.")
    ids = list(dict.fromkeys(data.colis_ids))          # sans doublon

    l = BarLivraisonInterne(numero="TMP-" + uuid.uuid4().hex[:20], lieu=lieu, statut="VALIDEE",
                            cree_par_id=user.id, valide_par_id=user.id, valide_le=maintenant,
                            mode="MANUELLE", motif_manuel=motif[:255], date_reelle=reelle,
                            notes="Livraison manuelle (sans scan)")
    db.add(l)
    db.flush()
    l.numero = f"LIV{now_haiti():%y%m%d}-{l.id:04d}"
    dest = LIEUX[lieu]
    refuses = []
    for cid in ids:
        c = db.get(BarColis, cid)
        if not c:
            refuses.append(f"#{cid} inconnu")
            continue
        etait_a_verifier = c.statut == "A_VERIFIER"
        if not _transition(db, c.id, ("AU_DEPOT", "A_VERIFIER"),
                           {"statut": "LIVRE", "lieu": lieu, "livraison_id": l.id}):
            db.refresh(c)
            refuses.append(f"{code_lisible(c.code)} {_etat_texte(c)}")
            continue
        motif_mv = f"Livraison manuelle {l.numero} — colis {code_lisible(c.code)} → {dest}"
        if etait_a_verifier:
            # Jamais vérifié au dépôt : il y entre puis en ressort aussitôt
            db.add(BarMouvementStock(produit_id=c.produit_id, type_mouvement="ENTREE", quantite=c.quantite,
                                     lieu=None, motif=f"Colis {code_lisible(c.code)} reçu sans scan ({motif[:120]})",
                                     utilisateur_id=user.id))
            _journaliser(db, "VERIFIE", c, utilisateur_id=user.id, details="Sans scan — livraison manuelle")
        db.add(BarMouvementStock(produit_id=c.produit_id, type_mouvement="TRANSFERT",
                                 quantite=-Decimal(str(c.quantite)), lieu=None, motif=motif_mv, utilisateur_id=user.id))
        db.add(BarMouvementStock(produit_id=c.produit_id, type_mouvement="TRANSFERT",
                                 quantite=Decimal(str(c.quantite)), lieu=lieu, motif=motif_mv, utilisateur_id=user.id))
        db.add(BarLivraisonLigne(livraison_id=l.id, produit_id=c.produit_id, colis_id=c.id, quantite=c.quantite))
        _journaliser(db, "LIVRE_MANUEL", c, lieu=lieu, livraison_id=l.id, utilisateur_id=user.id,
                     details=f"Livré le {reelle.astimezone(now_haiti().tzinfo):%d/%m/%Y %H:%M} · motif : {motif}"[:300])
    if refuses:
        db.rollback()          # tout ou rien : aucune livraison partielle
        raise HTTPException(409, "Livraison manuelle refusée, aucun colis n'a été livré : " + " ; ".join(refuses[:10]))
    db.commit()
    db.refresh(l)
    return _livraison_dict(l)


# ══════════════════════════════════════════════════════════════════════════
# Inventaire physique (dépôt ou bar)
# ══════════════════════════════════════════════════════════════════════════

class LigneInventaireIn(BaseModel):
    produit_id: int
    quantite:   float = Field(ge=0, le=1000000)


class InventaireIn(BaseModel):
    lieu:   str = Field(max_length=20)       # DEPOT | DEVANT | PISCINE
    motif:  str = Field(max_length=250)
    lignes: List[LigneInventaireIn] = Field(max_length=2000)


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
