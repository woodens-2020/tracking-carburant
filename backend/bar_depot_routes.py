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
    6. Bar devant / Bar piscine      → destination choisie à l'étape 4
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
lieu NULL = dépôt, DEVANT = Bar devant, PISCINE = Bar piscine. Une
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
from sqlalchemy import func
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

LIEUX = {"DEVANT": "Bar devant", "PISCINE": "Bar piscine"}
MAX_COLIS_PAR_RECEPTION = 500

STATUTS = {
    "A_VERIFIER":     "À vérifier au dépôt",
    "AU_DEPOT":       "Au dépôt",
    "EN_PREPARATION": "En préparation de transfert",
    "LIVRE":          "Transféré au bar",
    "ANNULE":         "Annulé",
    "OUVERT":         "Ouvert au dépôt (vrac)",
    "RETOURNE":       "Rendu au fournisseur",
}
EVENEMENTS = {
    "CREE":              "Colis créé à la réception",
    "VERIFIE":           "Vérifié au dépôt — entré en stock",
    "AJOUTE_LIVRAISON":  "Scanné dans un bon de transfert",
    "RETIRE_LIVRAISON":  "Retiré du bon de transfert",
    "LIVRE":             "Transféré au bar (sortie du dépôt)",
    "LIVRE_MANUEL":      "Transféré sans scan (transfert manuel)",
    "DESTINATION":       "Bar de destination du transfert changé",
    "RETOUR_DEPOT":      "Transfert annulé — remis en stock au dépôt",
    "ANNULE":            "Colis annulé",
    "CONSULTATION":      "Fiche consultée",
    "SCAN_REFUSE":       "Scan refusé",
    "REJET":             "Code rejeté",
    "OUVERT":            "Colis ouvert au dépôt — contenu en vrac",
    "RETOUR_FOURNISSEUR": "Rendu au fournisseur",
    "ETIQUETTE_REMPLACEE": "Étiquette remplacée — nouveau code",
    "ETIQUETE_STOCK":    "Étiqueté à partir du stock existant du dépôt",
    "RETOUR_BAR":        "Retourné du bar au dépôt",
    "TRANSFERT_INTERBAR": "Transféré d'un bar à l'autre",
}


# ── Droits ────────────────────────────────────────────────────────────────

def _require_depot(request: Request, db: Session = Depends(get_db)) -> Utilisateur:
    """Administrateur, PDG, ou rôle avec la permission « Dépôt Bar »
    (opérationnel ou complet). Pour un rôle qui n'a pas encore cette
    permission réglée, repli sur l'ancien usage : Manager, rôle contenant
    « dépôt », poste Magasinier / Gestionnaire de stock."""
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
        if "depot" in perms:                     # permission explicite : elle seule décide
            if perms.get("depot") in ("operationnel", "complet"):
                return u
            raise HTTPException(403, "Votre rôle n'a pas accès aux opérations du Dépôt Bar.")
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


_AZERTY_CHIFFRES = str.maketrans("&é\"'(-è_çà", "1234567890")


def normaliser_code(db: Session, texte: str) -> str:
    """Valide le texte lu par le scanner (liste blanche stricte) et vérifie
    la clé de contrôle. Lève CodeRejete sinon. Accepte le code seul, avec
    tirets ou espaces, ou une URL qui se termine par le code."""
    brut = (texte or "")[:200]
    t = brut.strip()
    # Douchette QWERTY sur clavier AZERTY : « &é"'(-è_çà » au lieu de « 1234567890 »
    if any(ch in t for ch in "&é\"'(è_çà"):
        t = t.translate(_AZERTY_CHIFFRES)
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
        from models import BarCodeRevoque
        r = db.query(BarCodeRevoque).filter(BarCodeRevoque.code == code).first()
        if r:
            _journaliser(db, "REJET", r.colis, utilisateur_id=user_id, code_saisi=code, details="revoque")
            db.commit()
            raise HTTPException(409, {"message": f"Ancienne étiquette {code_lisible(code)} : remplacée le "
                                                 f"{_iso(r.revoque_le)[:10]} par {code_lisible(r.colis.code)}. "
                                                 "Utilisez la nouvelle étiquette.",
                                      "colis_id": r.colis_id, "statut": r.colis.statut, "deja_livre": False})
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
        txt = f"déjà transféré au {LIEUX.get(c.lieu, c.lieu)}"
        if quand:
            q = quand if quand.tzinfo else quand.replace(tzinfo=timezone.utc)
            from tz_utils import HAITI_TZ
            txt += f" le {q.astimezone(HAITI_TZ):%d/%m/%Y à %H:%M}"
        if liv and liv.mode == "MANUELLE":
            txt += " (transfert manuel, sans scan) — ce code n'est plus valable"
        return txt
    return {"AU_DEPOT": "déjà vérifié au dépôt", "EN_PREPARATION": "dans un bon de transfert en préparation",
            "A_VERIFIER": "pas encore vérifié au dépôt", "ANNULE": "annulé",
            "OUVERT": "ouvert au dépôt — son contenu est en vrac, ce code n'est plus utilisable",
            "RETOURNE": "rendu au fournisseur — ce code n'est plus valable"}.get(c.statut, c.statut)


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
    "revoque": "Ancienne étiquette (remplacée)",
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
        "type_bon":    l.type_bon or "TRANSFERT",
        "origine":     l.origine,
        "origine_nom": ("Dépôt" if (l.type_bon or "TRANSFERT") == "TRANSFERT" else LIEUX.get(l.origine, l.origine)),
        "destination_nom": ("Dépôt" if l.type_bon == "RETOUR" else LIEUX.get(l.lieu, l.lieu)),
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
    """Fiche de traçabilité complète d'un colis."""
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


def _quantite_reservee(db: Session, produit_id: int, sauf_livraison: Optional[int] = None,
                       origine: Optional[str] = None, avec_colis: bool = False) -> Decimal:
    """Unités déjà réservées par des bons en préparation qui prennent dans le
    stock d'origine donné (None = dépôt : bons dépôt → bar ; sinon bons de
    retour / entre bars partant de ce bar). Vrac seulement, sauf avec_colis."""
    q = (db.query(BarLivraisonLigne)
         .join(BarLivraisonInterne, BarLivraisonLigne.livraison_id == BarLivraisonInterne.id)
         .filter(BarLivraisonInterne.statut == "PREPARATION",
                 BarLivraisonLigne.produit_id == produit_id))
    if not avec_colis:
        q = q.filter(BarLivraisonLigne.colis_id.is_(None))
    if origine is None:
        q = q.filter((BarLivraisonInterne.type_bon == "TRANSFERT") | BarLivraisonInterne.type_bon.is_(None))
    else:
        q = q.filter(BarLivraisonInterne.type_bon.in_(("RETOUR", "INTERBAR")), BarLivraisonInterne.origine == origine)
    if sauf_livraison:
        q = q.filter(BarLivraisonInterne.id != sauf_livraison)
    return sum((Decimal(str(x.quantite)) for x in q.all()), Decimal("0"))


def _colis_au_depot(db: Session, produit_id: int) -> Decimal:
    """Unités au dépôt déjà rattachées à un colis étiqueté (AU_DEPOT ou en
    préparation) : elles se livrent par scan, pas en vrac."""
    # En préparation : seulement les colis encore au dépôt (lieu vide) — un
    # colis d'un bon de retour ou entre bars est physiquement dans un bar.
    rows = db.query(BarColis).filter(BarColis.produit_id == produit_id,
                                     (BarColis.statut == "AU_DEPOT") |
                                     ((BarColis.statut == "EN_PREPARATION") & BarColis.lieu.is_(None))).all()
    return sum((Decimal(str(c.quantite)) for c in rows), Decimal("0"))


# ══════════════════════════════════════════════════════════════════════════
# 1-2. RÉCEPTION → génération des colis et des codes
# ══════════════════════════════════════════════════════════════════════════

class ReceptionIn(BaseModel):
    # Unités par colis. Par défaut : unités par caisse du produit (une
    # étiquette par caisse), sinon un seul colis pour toute la quantité.
    unites_par_colis: Optional[int] = Field(default=None, gt=0)
    # Réception partielle : unités réellement arrivées (vide = tout l'achat)
    quantite_recue:   Optional[float] = Field(default=None, gt=0)
    motif_ecart:      Optional[str] = Field(default=None, max_length=250)


def receptionner_achat(db: Session, achat: BarAchat, unites_par_colis: Optional[int],
                       utilisateur_id: Optional[int], quantite_recue: Optional[float] = None,
                       motif_ecart: Optional[str] = None) -> List[BarColis]:
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
    if quantite_recue is not None and Decimal(str(quantite_recue)) != qte:
        recue = Decimal(str(quantite_recue))
        if recue > qte:
            raise HTTPException(422, f"Reçu plus que l'achat ({float(qte):g}) : enregistrez un autre achat pour le surplus.")
        if len((motif_ecart or "").strip()) < 5:
            raise HTTPException(422, "Indiquez la raison de l'écart de réception (5 caractères minimum).")
        achat.quantite_recue = recue
        achat.motif_ecart_reception = motif_ecart.strip()[:255]
        qte = recue
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
    colis = receptionner_achat(db, achat, data.unites_par_colis, user.id, data.quantite_recue, data.motif_ecart)
    db.commit()
    return {"achat_id": achat.id, "nb_colis": len(colis), "colis": [_colis_dict(c) for c in colis],
            "quantite_recue": float(achat.quantite_recue) if achat.quantite_recue is not None else float(achat.quantite)}


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
    # Scan fait sans réseau, envoyé au retour d'internet : heure réelle du scan
    hors_ligne_le: Optional[datetime] = None
    # Bon de transfert : un colis pas encore vérifié est vérifié puis ajouté
    verifier_si_besoin: bool = False


def _details_hors_ligne(data: "ScanIn") -> Optional[str]:
    if not data.hors_ligne_le:
        return None
    h = data.hors_ligne_le if data.hors_ligne_le.tzinfo else data.hors_ligne_le.replace(tzinfo=timezone.utc)
    return f"Scanné hors ligne le {h.astimezone(now_haiti().tzinfo):%d/%m/%Y à %H:%M}, envoyé au retour du réseau"


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


def _stock_produit_lieux(db: Session, produit_id: int) -> dict:
    st = stock_par_lieu_tous_produits(db).get(produit_id, {"devant": Decimal(0), "piscine": Decimal(0), "total": Decimal(0)})
    return {"DEPOT": float(st["total"] - st["devant"] - st["piscine"]),
            "DEVANT": float(st["devant"]), "PISCINE": float(st["piscine"])}


def _actions_possibles(c: BarColis) -> list:
    """Actions proposées au scan selon l'état du colis (scan intelligent)."""
    if c.statut == "A_VERIFIER":
        return ["VERIFIER", "DEVANT", "PISCINE"]
    if c.statut == "AU_DEPOT":
        return ["DEVANT", "PISCINE", "OUVRIR"]
    if c.statut == "LIVRE" and c.lieu in LIEUX:
        autre = "PISCINE" if c.lieu == "DEVANT" else "DEVANT"
        return ["DEPOT", autre]
    return []


class ScanIntelIn(BaseModel):
    code: str = Field(max_length=200)


@router.post("/colis/scan")
def scan_intelligent(data: ScanIntelIn, request: Request, db: Session = Depends(get_db)):
    """Scan intelligent : reconnaît le colis et dit ce qu'on peut en faire
    (vérifier, envoyer au Bar devant / Bar piscine, retour au dépôt, vers
    l'autre bar), avec le stock du produit dans chaque lieu. Ne modifie rien."""
    u = _require_connecte(request)
    c = _trouver_colis(db, data.code, u.id)
    liv = db.get(BarLivraisonInterne, c.livraison_id) if c.livraison_id else None
    return {
        "colis": _colis_dict(c),
        "etat": _etat_texte(c),
        "actions": _actions_possibles(c),
        "stock": _stock_produit_lieux(db, c.produit_id),
        "bon": _livraison_dict(liv, details=False) if liv and c.statut == "EN_PREPARATION" else None,
        "livraison": _livraison_dict(liv, details=False) if liv and c.statut == "LIVRE" else None,
    }


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


def _verifier(db: Session, c: BarColis, user_id: int, details: Optional[str] = None) -> bool:
    """Le colis entre dans le stock du dépôt (A_VERIFIER → AU_DEPOT, mouvement
    ENTREE, journal). Atomique ; False s'il n'était plus « à vérifier »."""
    maintenant = datetime.now(timezone.utc)
    if not _transition(db, c.id, ("A_VERIFIER",),
                       {"statut": "AU_DEPOT", "verifie_le": maintenant, "verifie_par_id": user_id}):
        return False
    db.add(BarMouvementStock(
        produit_id=c.produit_id, type_mouvement="ENTREE", quantite=c.quantite, lieu=None,
        motif=f"Colis {code_lisible(c.code)} vérifié au dépôt" + (f" (achat #{c.achat_id})" if c.achat_id else ""),
        utilisateur_id=user_id,
    ))
    _journaliser(db, "VERIFIE", c, utilisateur_id=user_id, details=details)
    return True


@router.post("/colis/verifier")
def verifier_colis(data: ScanIn, request: Request, db: Session = Depends(get_db),
                   user=Depends(_require_depot)):
    """Scan au dépôt : le colis est physiquement présent → son contenu entre
    dans le stock du dépôt. Atomique : un seul des scans simultanés réussit."""
    c = _trouver_colis(db, data.code, user.id)
    if not _verifier(db, c, user.id, _details_hors_ligne(data)):
        db.refresh(c)
        _refus(db, c, user.id, f"Colis {code_lisible(c.code)} {_etat_texte(c)}.")
    db.commit()
    db.refresh(c)
    restants = (db.query(BarColis).filter(BarColis.achat_id == c.achat_id,
                                          BarColis.statut == "A_VERIFIER").count()
                if c.achat_id else 0)
    return {**_colis_dict(c), "restants_achat": restants}


class VerificationManuelleIn(BaseModel):
    colis_ids: List[int] = Field(min_length=1, max_length=MAX_COLIS_PAR_RECEPTION)
    motif:     Optional[str] = Field(default=None, max_length=200)


@router.post("/colis/verifier-manuel")
def verifier_manuel(data: VerificationManuelleIn, db: Session = Depends(get_db), user=Depends(_require_depot)):
    """Vérification à la main (sans scanner) : la personne confirme que ces
    colis sont bien au dépôt. Même effet que le scan, inscrit au journal comme
    « vérifié à la main ». Les colis qui ne sont plus « à vérifier » sont ignorés."""
    detail = "Vérifié à la main (sans scan)" + (f" — {data.motif.strip()}" if (data.motif or "").strip() else "")
    faits, ignores = [], []
    for cid in dict.fromkeys(data.colis_ids):
        c = db.get(BarColis, cid)
        if c and _verifier(db, c, user.id, detail[:300]):
            faits.append(c)
        else:
            ignores.append(cid)
    db.commit()
    return {"verifies": len(faits), "ignores": len(ignores),
            "unites": float(sum(Decimal(str(c.quantite)) for c in faits))}


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
    """Stock de chaque produit au dépôt, au Bar devant et au Bar piscine
    (dépôt + Bar devant + Bar piscine = total)."""
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
    lieu:     str = Field(max_length=20)              # bar de destination (ou bar d'origine d'un RETOUR)
    notes:    Optional[str] = Field(default=None, max_length=300)
    type_bon: str = Field(default="TRANSFERT", max_length=20)   # TRANSFERT | RETOUR | INTERBAR
    origine:  Optional[str] = Field(default=None, max_length=20)  # bar d'origine (RETOUR, INTERBAR)


class VracIn(BaseModel):
    produit_id: int
    quantite:   float = Field(gt=0, le=100000)


TYPES_BON = {
    "TRANSFERT": "Transfert dépôt → bar",
    "RETOUR":    "Retour bar → dépôt",
    "INTERBAR":  "Transfert entre bars",
}


def _type_bon(l: BarLivraisonInterne) -> str:
    return l.type_bon or "TRANSFERT"


def _origine_stock(l: BarLivraisonInterne) -> Optional[str]:
    """Lieu dont le stock sort (None = dépôt)."""
    return None if _type_bon(l) == "TRANSFERT" else l.origine


def _destination_stock(l: BarLivraisonInterne) -> Optional[str]:
    """Lieu dont le stock entre (None = dépôt)."""
    return None if _type_bon(l) == "RETOUR" else l.lieu


def _nom_lieu(lieu: Optional[str]) -> str:
    return LIEUX.get(lieu, "Dépôt") if lieu else "Dépôt"


def _livraison_en_preparation(db: Session, livraison_id: int) -> BarLivraisonInterne:
    l = db.get(BarLivraisonInterne, livraison_id)
    if not l:
        raise HTTPException(404, "Bon de transfert introuvable.")
    if l.statut != "PREPARATION":
        raise HTTPException(409, f"Bon de transfert {l.numero} déjà {l.statut.lower()}.")
    return l


def _livraison_ouverte(db: Session, lieu: str, type_bon: str = "TRANSFERT",
                       origine: Optional[str] = None) -> Optional[BarLivraisonInterne]:
    q = (db.query(BarLivraisonInterne)
         .filter(BarLivraisonInterne.lieu == lieu, BarLivraisonInterne.statut == "PREPARATION"))
    if type_bon == "TRANSFERT":
        q = q.filter((BarLivraisonInterne.type_bon == "TRANSFERT") | BarLivraisonInterne.type_bon.is_(None))
    else:
        q = q.filter(BarLivraisonInterne.type_bon == type_bon, BarLivraisonInterne.origine == origine)
    return q.order_by(BarLivraisonInterne.id.desc()).first()


def _dernier_bon_valide(db: Session, colis_id: int) -> Optional[int]:
    """Dernier bon validé contenant ce colis (pour remettre sa fiche à jour
    quand un retour ou un transfert entre bars est annulé)."""
    row = (db.query(BarLivraisonInterne.id)
           .join(BarLivraisonLigne, BarLivraisonLigne.livraison_id == BarLivraisonInterne.id)
           .filter(BarLivraisonLigne.colis_id == colis_id, BarLivraisonInterne.statut == "VALIDEE")
           .order_by(BarLivraisonInterne.id.desc()).first())
    return row[0] if row else None


def _vrac_disponible(db: Session, produit_id: int, origine: Optional[str],
                     sauf_livraison: Optional[int] = None) -> Decimal:
    """Unités en vrac qu'un bon peut encore prendre dans le stock d'origine.
    Dépôt : stock du dépôt − colis fermés au dépôt − vrac réservé par les bons
    dépôt → bar. Bar : stock du bar − ce que ses bons de retour / entre bars
    ont déjà réservé (colis et vrac)."""
    if origine is None:
        return _stock_depot(db, produit_id) - _colis_au_depot(db, produit_id) \
            - _quantite_reservee(db, produit_id, sauf_livraison=sauf_livraison)
    from pos_service import stock_courant
    return stock_courant(produit_id, db, lieu=origine) \
        - _quantite_reservee(db, produit_id, sauf_livraison=sauf_livraison, origine=origine, avec_colis=True)


class DestinationIn(BaseModel):
    lieu: str = Field(max_length=20)


@router.post("/livraisons", status_code=201)
def creer_livraison(data: LivraisonIn, request: Request, db: Session = Depends(get_db),
                    user=Depends(_require_depot)):
    """Ouvre un bon en préparation :
      - TRANSFERT (dépôt → bar) : un seul ouvert par bar, les deux bars en parallèle ;
      - RETOUR (bar → dépôt) : `origine` = bar qui rend la marchandise ;
      - INTERBAR (bar → bar) : `origine` → `lieu`.
    Si le même bon est déjà ouvert, il est renvoyé tel quel (double clic, deux postes)."""
    type_bon = (data.type_bon or "TRANSFERT").upper()
    if type_bon not in TYPES_BON:
        raise HTTPException(422, "Type de bon invalide : TRANSFERT, RETOUR ou INTERBAR.")
    lieu = (data.lieu or "").upper()
    origine = (data.origine or "").upper() or None
    if type_bon == "RETOUR":
        lieu = origine or lieu
        origine = lieu
    if lieu not in LIEUX:
        raise HTTPException(422, "Bar invalide : DEVANT (Bar devant) ou PISCINE (Bar piscine).")
    if type_bon == "INTERBAR":
        if origine not in LIEUX or origine == lieu:
            raise HTTPException(422, "Transfert entre bars : choisissez deux bars différents.")
    elif type_bon == "TRANSFERT":
        origine = None
    existante = _livraison_ouverte(db, lieu, type_bon, origine)
    if existante:
        return _livraison_dict(existante)
    l = BarLivraisonInterne(numero="TMP-" + uuid.uuid4().hex[:20], lieu=lieu, statut="PREPARATION",
                            notes=(data.notes or "").strip()[:300] or None, cree_par_id=user.id,
                            type_bon=type_bon, origine=origine)
    db.add(l)
    db.flush()
    prefixe = {"TRANSFERT": "TRF", "RETOUR": "RET", "INTERBAR": "TIB"}[type_bon]
    l.numero = f"{prefixe}{now_haiti():%y%m%d}-{l.id:04d}"
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
        raise HTTPException(404, "Bon de transfert introuvable.")
    return _livraison_dict(l)


@router.post("/livraisons/{livraison_id}/scan")
def scanner_colis_livraison(livraison_id: int, data: ScanIn, db: Session = Depends(get_db),
                            user=Depends(_require_depot)):
    l = _livraison_en_preparation(db, livraison_id)
    c = _trouver_colis(db, data.code, user.id)
    tb = _type_bon(l)
    if tb == "TRANSFERT":
        verifie_ici = False
        if c.statut == "A_VERIFIER" and data.verifier_si_besoin:
            verifie_ici = _verifier(db, c, user.id, f"Vérifié au moment du transfert {l.numero}")
            if verifie_ici:
                db.flush()
        if not _transition(db, c.id, ("AU_DEPOT",), {"statut": "EN_PREPARATION", "livraison_id": l.id}):
            db.refresh(c)
            if c.statut == "EN_PREPARATION":
                ou = "ce bon de transfert" if c.livraison_id == l.id else "un autre bon de transfert"
                _refus(db, c, user.id, f"Colis {code_lisible(c.code)} déjà scanné dans {ou}.")
            if c.statut == "A_VERIFIER":
                _refus(db, c, user.id, f"Colis {code_lisible(c.code)} pas encore vérifié au dépôt : scannez-le d'abord en réception.")
            _refus(db, c, user.id, f"Colis {code_lisible(c.code)} {_etat_texte(c)}.")
    else:
        # Retour ou transfert entre bars : colis fermé, encore au bar d'origine
        if c.statut != "LIVRE" or c.lieu != l.origine or \
                not _transition(db, c.id, ("LIVRE",), {"statut": "EN_PREPARATION", "livraison_id": l.id}):
            db.refresh(c)
            if c.statut == "EN_PREPARATION":
                _refus(db, c, user.id, f"Colis {code_lisible(c.code)} déjà scanné dans un bon en préparation.")
            if c.statut == "LIVRE":
                _refus(db, c, user.id, f"Colis {code_lisible(c.code)} est au {_nom_lieu(c.lieu)}, pas au {_nom_lieu(l.origine)}.")
            _refus(db, c, user.id, f"Colis {code_lisible(c.code)} n'est pas dans un bar ({_etat_texte(c)}) : "
                                   "seul un colis fermé, déjà transféré à ce bar, peut repartir.")
    db.add(BarLivraisonLigne(livraison_id=l.id, produit_id=c.produit_id, colis_id=c.id, quantite=c.quantite))
    _journaliser(db, "AJOUTE_LIVRAISON", c, lieu=l.lieu, livraison_id=l.id, utilisateur_id=user.id,
                 details=_details_hors_ligne(data))
    db.commit()
    db.refresh(l)
    db.refresh(c)
    return {**_livraison_dict(l), "colis_scanne": _colis_dict(c)}


@router.post("/livraisons/{livraison_id}/destination")
def changer_destination(livraison_id: int, data: DestinationIn, db: Session = Depends(get_db),
                        user=Depends(_require_depot)):
    """Corrige le bar de destination d'un bon dépôt → bar encore en
    préparation. Rien n'a encore bougé dans les stocks : seule la destination
    change, et le changement est journalisé."""
    l = _livraison_en_preparation(db, livraison_id)
    if _type_bon(l) != "TRANSFERT":
        raise HTTPException(409, "Seul un bon dépôt → bar peut changer de destination.")
    lieu = (data.lieu or "").upper()
    if lieu not in LIEUX:
        raise HTTPException(422, "Destination invalide : DEVANT (Bar devant) ou PISCINE (Bar piscine).")
    if lieu == l.lieu:
        return _livraison_dict(l)
    autre = _livraison_ouverte(db, lieu)
    if autre:
        raise HTTPException(409, f"Un transfert vers le {LIEUX[lieu]} est déjà en préparation ({autre.numero}) : "
                                 "validez-le ou annulez-le d'abord.")
    ancien = LIEUX.get(l.lieu, l.lieu)
    l.lieu = lieu
    for x in l.lignes:
        if x.colis:
            _journaliser(db, "DESTINATION", x.colis, lieu=lieu, livraison_id=l.id, utilisateur_id=user.id,
                         details=f"{ancien} → {LIEUX[lieu]}")
    db.commit()
    db.refresh(l)
    return _livraison_dict(l)


@router.post("/livraisons/{livraison_id}/vrac")
def ajouter_vrac(livraison_id: int, data: VracIn, db: Session = Depends(get_db),
                 user=Depends(_require_depot)):
    """Ajoute une quantité sans étiquette, prise dans le stock d'origine du
    bon (dépôt pour un transfert, bar pour un retour ou un transfert entre bars)."""
    l = _livraison_en_preparation(db, livraison_id)
    p = db.get(BarProduit, data.produit_id)
    if not p:
        raise HTTPException(404, "Produit introuvable.")
    qte = Decimal(str(data.quantite))
    dispo = _vrac_disponible(db, p.id, _origine_stock(l))
    if qte > dispo:
        raise HTTPException(409, f"Stock en vrac insuffisant au {_nom_lieu(_origine_stock(l))} pour « {p.nom} » "
                                 f"(disponible : {float(max(dispo, 0)):g}).")
    db.add(BarLivraisonLigne(livraison_id=l.id, produit_id=p.id, quantite=qte))
    db.commit()
    db.refresh(l)
    return _livraison_dict(l)


def _remettre_colis(db: Session, l: BarLivraisonInterne, colis_id: int) -> None:
    """Colis retiré d'un bon ou bon annulé : il revient là où il était."""
    if _type_bon(l) == "TRANSFERT":
        _transition(db, colis_id, ("EN_PREPARATION",), {"statut": "AU_DEPOT", "livraison_id": None})
    else:
        _transition(db, colis_id, ("EN_PREPARATION",),
                    {"statut": "LIVRE", "lieu": l.origine, "livraison_id": _dernier_bon_valide(db, colis_id)})


@router.delete("/livraisons/{livraison_id}/lignes/{ligne_id}")
def retirer_ligne(livraison_id: int, ligne_id: int, db: Session = Depends(get_db),
                  user=Depends(_require_depot)):
    l = _livraison_en_preparation(db, livraison_id)
    ligne = db.get(BarLivraisonLigne, ligne_id)
    if not ligne or ligne.livraison_id != l.id:
        raise HTTPException(404, "Ligne introuvable.")
    if ligne.colis_id:
        _remettre_colis(db, l, ligne.colis_id)
        _journaliser(db, "RETIRE_LIVRAISON", ligne.colis, livraison_id=l.id, utilisateur_id=user.id)
    db.delete(ligne)
    db.commit()
    db.refresh(l)
    return _livraison_dict(l)


@router.post("/livraisons/{livraison_id}/valider")
def valider_livraison(livraison_id: int, db: Session = Depends(get_db), user=Depends(_require_depot)):
    """Mise à jour des stocks : chaque ligne sort du lieu d'origine et entre
    au lieu de destination (deux mouvements TRANSFERT de même quantité). Le
    passage PREPARATION → VALIDEE est conditionnel : une double validation
    ne crée jamais deux transferts."""
    l = _livraison_en_preparation(db, livraison_id)
    if not l.lignes:
        raise HTTPException(422, "Le bon est vide : scannez au moins un colis.")
    origine, destination, tb = _origine_stock(l), _destination_stock(l), _type_bon(l)
    # Contrôle du vrac : le lieu d'origine doit encore l'avoir
    besoins: dict[int, Decimal] = {}
    for x in l.lignes:
        if x.colis_id is None:
            besoins[x.produit_id] = besoins.get(x.produit_id, Decimal(0)) + Decimal(str(x.quantite))
    for pid, qte in besoins.items():
        dispo = _vrac_disponible(db, pid, origine, sauf_livraison=l.id)
        if qte > dispo:
            nom = db.get(BarProduit, pid).nom
            raise HTTPException(409, f"Stock en vrac insuffisant au {_nom_lieu(origine)} pour « {nom} » "
                                     f"(disponible : {float(max(dispo, 0)):g}, demandé : {float(qte):g}).")
    maintenant = datetime.now(timezone.utc)
    n = (db.query(BarLivraisonInterne)
         .filter(BarLivraisonInterne.id == l.id, BarLivraisonInterne.statut == "PREPARATION")
         .update({"statut": "VALIDEE", "valide_le": maintenant, "valide_par_id": user.id},
                 synchronize_session=False))
    if n != 1:
        db.rollback()
        raise HTTPException(409, f"Bon {l.numero} déjà validé ou annulé.")
    sens = f"{_nom_lieu(origine)} → {_nom_lieu(destination)}"
    evt = {"TRANSFERT": "LIVRE", "RETOUR": "RETOUR_BAR", "INTERBAR": "TRANSFERT_INTERBAR"}[tb]
    for x in l.lignes:
        ref = f"colis {code_lisible(x.colis.code)}" if x.colis else "vrac"
        motif = f"{TYPES_BON[tb]} {l.numero} — {ref} — {sens}"
        db.add(BarMouvementStock(produit_id=x.produit_id, type_mouvement="TRANSFERT",
                                 quantite=-Decimal(str(x.quantite)), lieu=origine,
                                 motif=motif[:300], utilisateur_id=user.id))
        db.add(BarMouvementStock(produit_id=x.produit_id, type_mouvement="TRANSFERT",
                                 quantite=Decimal(str(x.quantite)), lieu=destination,
                                 motif=motif[:300], utilisateur_id=user.id))
        if x.colis_id:
            if tb == "RETOUR":
                _transition(db, x.colis_id, ("EN_PREPARATION",), {"statut": "AU_DEPOT", "lieu": None})
            else:
                _transition(db, x.colis_id, ("EN_PREPARATION",), {"statut": "LIVRE", "lieu": destination})
            _journaliser(db, evt, x.colis, lieu=destination, livraison_id=l.id, utilisateur_id=user.id,
                         details=f"{TYPES_BON[tb]} {l.numero} : {sens}")
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
        raise HTTPException(409, f"Bon {l.numero} déjà validé ou annulé.")
    for x in l.lignes:
        if x.colis_id:
            _remettre_colis(db, l, x.colis_id)
            _journaliser(db, "RETOUR_DEPOT" if _type_bon(l) == "TRANSFERT" else "RETIRE_LIVRAISON",
                         x.colis, livraison_id=l.id, utilisateur_id=user.id,
                         details=None if _type_bon(l) == "TRANSFERT" else f"Bon {l.numero} annulé — colis resté au {_nom_lieu(l.origine)}")
    db.commit()
    db.refresh(l)
    return _livraison_dict(l)


# ══════════════════════════════════════════════════════════════════════════
# Transfert direct d'un colis scanné (scan intelligent)
# ══════════════════════════════════════════════════════════════════════════

class TransfertDirectIn(BaseModel):
    code:        str = Field(max_length=200)
    destination: str = Field(max_length=20)        # DEPOT | DEVANT | PISCINE
    verifier_si_besoin: bool = False                # colis « à vérifier » : vérifié puis envoyé


@router.post("/colis/transferer")
def transferer_colis(data: TransfertDirectIn, db: Session = Depends(get_db), user=Depends(_require_depot)):
    """Un scan, un choix, c'est fait : le colis part vers la destination
    choisie et les stocks sont à jour immédiatement.
      - colis au dépôt (ou à vérifier, avec verifier_si_besoin) → Bar devant / Bar piscine ;
      - colis dans un bar → retour au dépôt, ou vers l'autre bar.
    Un bon d'une ligne est créé et validé dans la même transaction (numéro et
    journal comme un transfert normal). Atomique : deux scans simultanés du
    même colis ne peuvent pas réussir tous les deux."""
    dest = (data.destination or "").upper()
    if dest not in ("DEPOT", *LIEUX):
        raise HTTPException(422, "Destination invalide : DEPOT, DEVANT ou PISCINE.")
    c = _trouver_colis(db, data.code, user.id)
    if c.statut == "A_VERIFIER" and dest != "DEPOT":
        if not data.verifier_si_besoin:
            _refus(db, c, user.id, f"Colis {code_lisible(c.code)} pas encore vérifié au dépôt.")
        if not _verifier(db, c, user.id, "Vérifié au scan, avant envoi direct"):
            db.refresh(c)
            _refus(db, c, user.id, f"Colis {code_lisible(c.code)} {_etat_texte(c)}.")
        db.flush()
        db.refresh(c)
    if c.statut == "AU_DEPOT":
        if dest == "DEPOT":
            _refus(db, c, user.id, f"Colis {code_lisible(c.code)} déjà au dépôt.")
        type_bon, origine, lieu_bon, de = "TRANSFERT", None, dest, ("AU_DEPOT",)
    elif c.statut == "LIVRE" and c.lieu in LIEUX:
        if dest == c.lieu:
            _refus(db, c, user.id, f"Colis {code_lisible(c.code)} déjà au {LIEUX[c.lieu]}.")
        if dest == "DEPOT":
            type_bon, origine, lieu_bon = "RETOUR", c.lieu, c.lieu
        else:
            type_bon, origine, lieu_bon = "INTERBAR", c.lieu, dest
        de = ("LIVRE",)
    else:
        _refus(db, c, user.id, f"Colis {code_lisible(c.code)} {_etat_texte(c)} : il ne peut pas être déplacé.")

    maintenant = datetime.now(timezone.utc)
    l = BarLivraisonInterne(numero="TMP-" + uuid.uuid4().hex[:20], lieu=lieu_bon, statut="VALIDEE",
                            type_bon=type_bon, origine=origine, cree_par_id=user.id,
                            valide_par_id=user.id, valide_le=maintenant, notes="Transfert direct par scan")
    db.add(l)
    db.flush()
    prefixe = {"TRANSFERT": "TRF", "RETOUR": "RET", "INTERBAR": "TIB"}[type_bon]
    l.numero = f"{prefixe}{now_haiti():%y%m%d}-{l.id:04d}"
    src = None if type_bon == "TRANSFERT" else origine
    dst = None if type_bon == "RETOUR" else lieu_bon
    nouveau = {"statut": "AU_DEPOT", "lieu": None, "livraison_id": l.id} if type_bon == "RETOUR" \
        else {"statut": "LIVRE", "lieu": dst, "livraison_id": l.id}
    if not _transition(db, c.id, de, nouveau):
        db.refresh(c)
        _refus(db, c, user.id, f"Colis {code_lisible(c.code)} vient de changer d'état ({_etat_texte(c)}).")
    sens = f"{_nom_lieu(src)} → {_nom_lieu(dst)}"
    motif = f"{TYPES_BON[type_bon]} {l.numero} — colis {code_lisible(c.code)} — {sens}"
    db.add(BarLivraisonLigne(livraison_id=l.id, produit_id=c.produit_id, colis_id=c.id, quantite=c.quantite))
    q = Decimal(str(c.quantite))
    db.add(BarMouvementStock(produit_id=c.produit_id, type_mouvement="TRANSFERT", quantite=-q, lieu=src,
                             motif=motif[:300], utilisateur_id=user.id))
    db.add(BarMouvementStock(produit_id=c.produit_id, type_mouvement="TRANSFERT", quantite=q, lieu=dst,
                             motif=motif[:300], utilisateur_id=user.id))
    evt = {"TRANSFERT": "LIVRE", "RETOUR": "RETOUR_BAR", "INTERBAR": "TRANSFERT_INTERBAR"}[type_bon]
    _journaliser(db, evt, c, lieu=dst, livraison_id=l.id, utilisateur_id=user.id,
                 details=f"Transfert direct par scan {l.numero} : {sens}")
    db.commit()
    db.refresh(c)
    return {"colis": _colis_dict(c), "bon": _livraison_dict(l, details=False), "sens": sens,
            "stock": _stock_produit_lieux(db, c.produit_id), "actions": _actions_possibles(c)}


# ══════════════════════════════════════════════════════════════════════════
# Opérations sur un colis au dépôt : ouvrir, rendre au fournisseur,
# remplacer l'étiquette ; étiqueter le stock déjà présent
# ══════════════════════════════════════════════════════════════════════════

class MotifIn(BaseModel):
    motif: Optional[str] = Field(default=None, max_length=250)


@router.post("/colis/{colis_id}/ouvrir")
def ouvrir_colis(colis_id: int, data: MotifIn, db: Session = Depends(get_db), user=Depends(_require_depot)):
    """Ouvre un colis fermé au dépôt : son contenu reste au dépôt mais devient
    du vrac, transférable par quantité (ex. 6 bouteilles d'une caisse de 24).
    Le code du colis n'est plus utilisable."""
    c = db.get(BarColis, colis_id)
    if not c:
        raise HTTPException(404, "Colis introuvable.")
    if not _transition(db, c.id, ("AU_DEPOT",), {"statut": "OUVERT"}):
        db.refresh(c)
        _refus(db, c, user.id, f"Seul un colis fermé au dépôt peut être ouvert (ce colis est {_etat_texte(c)}).")
    _journaliser(db, "OUVERT", c, utilisateur_id=user.id,
                 details=(data.motif or "").strip()[:250] or f"{float(c.quantite):g} unités passées en vrac au dépôt")
    db.commit()
    db.refresh(c)
    return _colis_dict(c)


class RetourFournisseurIn(BaseModel):
    colis_ids: List[int] = Field(default_factory=list, max_length=MAX_COLIS_PAR_RECEPTION)
    vrac:      List[VracIn] = Field(default_factory=list, max_length=200)
    motif:     str = Field(min_length=5, max_length=250)


@router.post("/retour-fournisseur", status_code=201)
def retour_fournisseur(data: RetourFournisseurIn, db: Session = Depends(get_db), user=Depends(_require_depot)):
    """Rend de la marchandise au fournisseur (casse à la livraison, produit
    périmé, erreur de commande). Colis fermés au dépôt et/ou vrac du dépôt.
    Tout ou rien. Les colis rendus sont déduits du coût moyen de leur achat."""
    if not data.colis_ids and not data.vrac:
        raise HTTPException(422, "Choisissez au moins un colis ou une quantité en vrac.")
    motif = " ".join(data.motif.split())
    refuses, total = [], Decimal("0")
    for cid in dict.fromkeys(data.colis_ids):
        c = db.get(BarColis, cid)
        if not c:
            refuses.append(f"#{cid} inconnu")
            continue
        if not _transition(db, c.id, ("AU_DEPOT",), {"statut": "RETOURNE"}):
            db.refresh(c)
            refuses.append(f"{code_lisible(c.code)} {_etat_texte(c)}")
            continue
        q = Decimal(str(c.quantite))
        db.add(BarMouvementStock(produit_id=c.produit_id, type_mouvement="RETOUR_FOURNISSEUR", quantite=-q,
                                 lieu=None, achat_id=c.achat_id, utilisateur_id=user.id,
                                 motif=f"Retour fournisseur — colis {code_lisible(c.code)} — {motif}"[:300]))
        _journaliser(db, "RETOUR_FOURNISSEUR", c, utilisateur_id=user.id, details=motif)
        total += q
    for v in data.vrac:
        p = db.get(BarProduit, v.produit_id)
        if not p:
            refuses.append(f"produit #{v.produit_id} inconnu")
            continue
        q = Decimal(str(v.quantite))
        dispo = _vrac_disponible(db, p.id, None)
        if q > dispo:
            refuses.append(f"{p.nom} : vrac disponible {float(max(dispo, 0)):g}")
            continue
        db.add(BarMouvementStock(produit_id=p.id, type_mouvement="RETOUR_FOURNISSEUR", quantite=-q, lieu=None,
                                 utilisateur_id=user.id, motif=f"Retour fournisseur — vrac — {motif}"[:300]))
        total += q
    if refuses:
        db.rollback()
        raise HTTPException(409, "Retour refusé, rien n'a été rendu : " + " ; ".join(refuses[:10]))
    db.commit()
    return {"unites_rendues": float(total), "colis": len(set(data.colis_ids)), "motif": motif}


def _nouveau_code(db: Session, revoque_id: int) -> str:
    """Nouveau code signé pour un colis dont l'étiquette est remplacée :
    numéro pris en partant de 999999 vers le bas (jamais celui d'un colis)."""
    num = 999999 - revoque_id
    while True:
        base = f"BC{now_haiti():%y}{num:06d}"
        code = base + _cle_controle(db, base)
        if not db.query(BarColis.id).filter(BarColis.code == code).first():
            return code
        num -= 1


@router.post("/colis/{colis_id}/remplacer-etiquette")
def remplacer_etiquette(colis_id: int, data: MotifIn, db: Session = Depends(get_db), user=Depends(_require_depot)):
    """Étiquette perdue ou abîmée : le colis reçoit un nouveau code et
    l'ancien est révoqué (refusé au scan avec un message clair)."""
    from models import BarCodeRevoque
    c = db.get(BarColis, colis_id)
    if not c:
        raise HTTPException(404, "Colis introuvable.")
    if c.statut not in ("A_VERIFIER", "AU_DEPOT"):
        raise HTTPException(409, f"Seul un colis au dépôt peut recevoir une nouvelle étiquette (ce colis est {_etat_texte(c)}).")
    ancien = c.code
    r = BarCodeRevoque(code=ancien, colis_id=c.id, revoque_par_id=user.id,
                       motif=(data.motif or "").strip()[:200] or "Étiquette remplacée")
    db.add(r)
    db.flush()
    c.code = _nouveau_code(db, r.id)
    _journaliser(db, "ETIQUETTE_REMPLACEE", c, utilisateur_id=user.id,
                 details=f"{code_lisible(ancien)} → {code_lisible(c.code)} · {r.motif}"[:300])
    db.commit()
    db.refresh(c)
    return {**_colis_dict(c), "ancien_code": code_lisible(ancien)}


class EtiquetageIn(BaseModel):
    produit_id:       int
    nb_colis:         int = Field(gt=0, le=MAX_COLIS_PAR_RECEPTION)
    unites_par_colis: float = Field(gt=0, le=10000)


@router.post("/etiqueter-stock", status_code=201)
def etiqueter_stock(data: EtiquetageIn, db: Session = Depends(get_db), user=Depends(_require_depot)):
    """Étiquette des caisses déjà présentes au dépôt sans code (stock
    d'avant les étiquettes, inventaire d'ouverture) : crée des colis « au
    dépôt » sans mouvement de stock, dans la limite du vrac disponible."""
    p = db.get(BarProduit, data.produit_id)
    if not p:
        raise HTTPException(404, "Produit introuvable.")
    taille = Decimal(str(data.unites_par_colis))
    total = taille * data.nb_colis
    dispo = _vrac_disponible(db, p.id, None)
    if total > dispo:
        raise HTTPException(409, f"Le dépôt n'a que {float(max(dispo, 0)):g} {p.unite or 'unités'} de « {p.nom} » "
                                 f"sans étiquette ; impossible d'en étiqueter {float(total):g}. Faites d'abord l'inventaire du dépôt.")
    maintenant = datetime.now(timezone.utc)
    colis = []
    for _ in range(data.nb_colis):
        c = BarColis(code="TMP-" + uuid.uuid4().hex[:20], produit_id=p.id, quantite=taille, statut="AU_DEPOT",
                     verifie_le=maintenant, verifie_par_id=user.id)
        db.add(c)
        db.flush()
        c.code = generer_code(db, c.id)
        _journaliser(db, "ETIQUETE_STOCK", c, utilisateur_id=user.id,
                     details=f"{float(taille):g} {p.unite or 'u.'} du stock existant du dépôt")
        colis.append(c)
    db.commit()
    return {"nb_colis": len(colis), "colis": [_colis_dict(c) for c in colis]}


# ══════════════════════════════════════════════════════════════════════════
# À transférer aujourd'hui : seuils par bar
# ══════════════════════════════════════════════════════════════════════════

@router.get("/a-transferer")
def a_transferer(db: Session = Depends(get_db)):
    """Pour chaque bar, les produits sous leur seuil (seuil du bar, sinon
    seuil général) et ce qu'il faut sortir du dépôt pour remonter à deux
    fois le seuil : d'abord des caisses fermées (à scanner), puis du vrac
    s'il n'y a plus de caisse. « À racheter » si le dépôt n'a plus rien."""
    import math
    st = stock_par_lieu_tous_produits(db)
    caisses: dict[int, int] = {}
    fermes: dict[int, Decimal] = {}
    for c in db.query(BarColis).filter((BarColis.statut == "AU_DEPOT") |
                                       ((BarColis.statut == "EN_PREPARATION") & BarColis.lieu.is_(None))).all():
        fermes[c.produit_id] = fermes.get(c.produit_id, Decimal(0)) + Decimal(str(c.quantite))
        if c.statut == "AU_DEPOT":
            caisses[c.produit_id] = caisses.get(c.produit_id, 0) + 1
    en_cours: dict[tuple, Decimal] = {}
    vrac_reserve: dict[int, Decimal] = {}
    for x in (db.query(BarLivraisonLigne).join(BarLivraisonInterne, BarLivraisonLigne.livraison_id == BarLivraisonInterne.id)
              .filter(BarLivraisonInterne.statut == "PREPARATION",
                      (BarLivraisonInterne.type_bon == "TRANSFERT") | BarLivraisonInterne.type_bon.is_(None)).all()):
        k = (x.livraison.lieu, x.produit_id)
        en_cours[k] = en_cours.get(k, Decimal(0)) + Decimal(str(x.quantite))
        if x.colis_id is None:
            vrac_reserve[x.produit_id] = vrac_reserve.get(x.produit_id, Decimal(0)) + Decimal(str(x.quantite))
    out = {k: [] for k in LIEUX}
    for p in db.query(BarProduit).filter(BarProduit.actif.is_(True)).order_by(BarProduit.nom).all():
        s = st.get(p.id, {"devant": Decimal(0), "piscine": Decimal(0), "total": Decimal(0)})
        depot = s["total"] - s["devant"] - s["piscine"]
        vrac = max(depot - fermes.get(p.id, Decimal(0)) - vrac_reserve.get(p.id, Decimal(0)), Decimal(0))
        upc = p.unites_par_caisse if p.vendu_par_caisse and p.unites_par_caisse else None
        for lieu in LIEUX:
            seuil = p.seuil_devant if lieu == "DEVANT" else p.seuil_piscine
            seuil = Decimal(str(seuil if seuil is not None else (p.seuil_alerte_stock or 0)))
            if seuil <= 0:
                continue
            prepare = en_cours.get((lieu, p.id), Decimal(0))
            stock_bar = s[lieu.lower()] + prepare
            if stock_bar >= seuil:
                continue
            besoin = seuil * 2 - stock_bar
            nb_caisses = min(math.ceil(float(besoin) / upc), caisses.get(p.id, 0)) if upc else 0
            reste = max(besoin - Decimal(nb_caisses * (upc or 0)), Decimal(0))
            nb_vrac = min(reste, vrac) if reste > 0 else Decimal(0)
            out[lieu].append({
                "produit_id": p.id, "nom": p.nom, "unite": p.unite,
                "stock_bar": float(s[lieu.lower()]), "en_preparation": float(prepare),
                "seuil": float(seuil), "besoin": float(besoin),
                "propose_caisses": nb_caisses, "propose_vrac": float(nb_vrac),
                "propose_unites": float(Decimal(nb_caisses * (upc or 0)) + nb_vrac),
                "unites_par_caisse": upc, "stock_depot": float(depot), "colis_au_depot": caisses.get(p.id, 0),
                "vrac_depot": float(vrac),
                "a_racheter": nb_caisses == 0 and nb_vrac <= 0,
            })
    return {lieu: sorted(v, key=lambda x: (x["a_racheter"], x["stock_bar"] - x["seuil"])) for lieu, v in out.items()}


# ══════════════════════════════════════════════════════════════════════════
# Inventaire d'ouverture et contrôle du stock par bar
# ══════════════════════════════════════════════════════════════════════════

_CLE_CONTROLE_BAR = "bar_stock_par_lieu"


def controle_par_bar_actif(db: Session) -> bool:
    """Contrôle du stock par bar à la vente : variable BAR_STOCK_PAR_LIEU,
    ou réglage enregistré depuis la page Dépôt Bar."""
    import os
    if os.getenv("BAR_STOCK_PAR_LIEU", "false").lower() == "true":
        return True
    row = db.query(SecretApplication).filter_by(cle=_CLE_CONTROLE_BAR).first()
    return bool(row and row.valeur == "true")


def _dernier_inventaire(db: Session, lieu: Optional[str]):
    q = db.query(func.max(BarMouvementStock.date_mouvement)).filter(
        BarMouvementStock.type_mouvement == "AJUSTEMENT", BarMouvementStock.motif.like("Inventaire%"))
    q = q.filter(BarMouvementStock.lieu.is_(None)) if lieu is None else q.filter(BarMouvementStock.lieu == lieu)
    return q.scalar()


@router.get("/ouverture")
def etat_ouverture(db: Session = Depends(get_db)):
    """Où en est l'inventaire d'ouverture : produits en stock négatif et
    dernier inventaire pour le dépôt et chaque bar, contrôle par bar actif ou non."""
    st = stock_par_lieu_tous_produits(db)
    lieux = []
    for code, nom in (("DEPOT", "Dépôt"), ("DEVANT", "Bar devant"), ("PISCINE", "Bar piscine")):
        negatifs = 0
        for s in st.values():
            v = (s["total"] - s["devant"] - s["piscine"]) if code == "DEPOT" else s[code.lower()]
            negatifs += 1 if v < 0 else 0
        der = _dernier_inventaire(db, None if code == "DEPOT" else code)
        lieux.append({"lieu": code, "nom": nom, "negatifs": negatifs, "dernier_inventaire": _iso(der)})
    pret = all(x["negatifs"] == 0 and x["dernier_inventaire"] for x in lieux if x["lieu"] != "DEPOT")
    return {"lieux": lieux, "controle_par_bar": controle_par_bar_actif(db), "pret": pret}


class ControleIn(BaseModel):
    actif: bool


@router.post("/ouverture/controle")
def regler_controle(data: ControleIn, db: Session = Depends(get_db), user=Depends(_require_depot)):
    """Active (ou coupe) le contrôle du stock par bar à la vente. Activation
    refusée tant qu'un bar n'a pas eu son inventaire ou garde un stock négatif."""
    if data.actif:
        etat = etat_ouverture(db)
        manque = [x["nom"] for x in etat["lieux"] if x["lieu"] != "DEPOT" and (x["negatifs"] or not x["dernier_inventaire"])]
        if manque:
            raise HTTPException(409, "Faites d'abord l'inventaire de : " + ", ".join(manque) +
                                     " (aucun produit ne doit rester en stock négatif).")
    row = db.query(SecretApplication).filter_by(cle=_CLE_CONTROLE_BAR).first()
    if not row:
        row = SecretApplication(cle=_CLE_CONTROLE_BAR, valeur="false")
        db.add(row)
    row.valeur = "true" if data.actif else "false"
    db.commit()
    return {"controle_par_bar": controle_par_bar_actif(db)}


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
        raise HTTPException(422, "Destination invalide : DEVANT (Bar devant) ou PISCINE (Bar piscine).")
    motif = " ".join((data.motif or "").split())
    if len(motif) < MOTIF_MANUEL_MIN:
        raise HTTPException(422, f"Expliquez pourquoi le transfert n'a pas été scanné ({MOTIF_MANUEL_MIN} caractères minimum).")
    reelle = data.date_reelle if data.date_reelle.tzinfo else data.date_reelle.replace(tzinfo=now_haiti().tzinfo)
    maintenant = datetime.now(timezone.utc)
    if reelle > maintenant.replace(microsecond=0) + __import__("datetime").timedelta(minutes=5):
        raise HTTPException(422, "La date du transfert ne peut pas être dans le futur.")
    if (maintenant - reelle).days >= DELAI_MAX_REGULARISATION_JOURS:
        raise HTTPException(422, f"Transfert trop ancien : régularisation possible sur {DELAI_MAX_REGULARISATION_JOURS} jours seulement. Faites un inventaire.")
    ids = list(dict.fromkeys(data.colis_ids))          # sans doublon

    l = BarLivraisonInterne(numero="TMP-" + uuid.uuid4().hex[:20], lieu=lieu, statut="VALIDEE",
                            cree_par_id=user.id, valide_par_id=user.id, valide_le=maintenant,
                            mode="MANUELLE", motif_manuel=motif[:255], date_reelle=reelle,
                            notes="Transfert manuel (sans scan)")
    db.add(l)
    db.flush()
    l.numero = f"TRF{now_haiti():%y%m%d}-{l.id:04d}"
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
        motif_mv = f"Transfert manuel {l.numero} — colis {code_lisible(c.code)} → {dest}"
        if etait_a_verifier:
            # Jamais vérifié au dépôt : il y entre puis en ressort aussitôt
            db.add(BarMouvementStock(produit_id=c.produit_id, type_mouvement="ENTREE", quantite=c.quantite,
                                     lieu=None, motif=f"Colis {code_lisible(c.code)} reçu sans scan ({motif[:120]})",
                                     utilisateur_id=user.id))
            _journaliser(db, "VERIFIE", c, utilisateur_id=user.id, details="Sans scan — transfert manuel")
        db.add(BarMouvementStock(produit_id=c.produit_id, type_mouvement="TRANSFERT",
                                 quantite=-Decimal(str(c.quantite)), lieu=None, motif=motif_mv, utilisateur_id=user.id))
        db.add(BarMouvementStock(produit_id=c.produit_id, type_mouvement="TRANSFERT",
                                 quantite=Decimal(str(c.quantite)), lieu=lieu, motif=motif_mv, utilisateur_id=user.id))
        db.add(BarLivraisonLigne(livraison_id=l.id, produit_id=c.produit_id, colis_id=c.id, quantite=c.quantite))
        _journaliser(db, "LIVRE_MANUEL", c, lieu=lieu, livraison_id=l.id, utilisateur_id=user.id,
                     details=f"Transféré le {reelle.astimezone(now_haiti().tzinfo):%d/%m/%Y %H:%M} · motif : {motif}"[:300])
    if refuses:
        db.rollback()          # tout ou rien : aucune livraison partielle
        raise HTTPException(409, "Transfert manuel refusé, aucun colis n'a quitté le dépôt : " + " ; ".join(refuses[:10]))
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
