"""
Dépôt central — préfixe /api/depot

  PRODUITS / INVENTAIRE
    GET  /produits                         stock dépôt + bars, prix de vente, seuil
    PUT  /produits/{id}/config             prix de vente et quantité minimum
  ACHATS & RÉCEPTION
    GET  /achats   POST /achats   PUT /achats/{id}
    POST /achats/{id}/reception            quantités reçues → stock dépôt (ou écart)
    POST /achats/{id}/annuler
  TRANSFERTS / VENTES (factures)
    GET  /transactions   GET /transactions/{id}   POST /transactions
    POST /transactions/{id}/valider        stock déplacé
    POST /transactions/{id}/annuler
  CAISSE DÉPÔT
    POST /transactions/{id}/paiements      gourdes ou dollars
    GET  /paiements                        journal de caisse (HTG / USD séparés)
    GET  /caisse                           balance
  RAPPORT
    GET  /rapport                          inventaire, ventes, achats, caisse
"""
from __future__ import annotations

from datetime import date as date_type, datetime, timezone
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db
from models import (
    BarProduit, BarMouvementStock, BarPrixHistorique, Utilisateur,
    DepotAchat, DepotAchatLigne, DepotTransaction, DepotTransactionLigne, DepotPaiement,
)
from pos_service import stock_courant, prix_actif, cmup
from tz_utils import today_haiti, bounds_haiti
import devise as dv

router = APIRouter(prefix="/api/depot", tags=["Dépôt central"])

DEPOT = "DEPOT"
DESTINATIONS = {"DEVANT": "Bar Devant", "PISCINE": "Bar Piscine", "CLIENT": "Client externe"}
METHODES = ("CASH", "CARTE", "ZELLE", "VIREMENT", "CHEQUE")
_CENT = Decimal("0.01")


def _d(v) -> Decimal:
    return Decimal(str(v)) if v is not None else Decimal("0")


def _uid(request: Request) -> Optional[int]:
    u = getattr(request.state, "user", None)
    return u.id if u else None


def _require_pdg_ou_admin(request: Request, db: Session = Depends(get_db)) -> Utilisateur:
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


def _numero(db: Session, modele, prefixe: str) -> str:
    """Numéro lisible et unique : DA-2026-0001 (achat), FD-… (facture), RD-… (reçu)."""
    col = modele.numero_recu if modele is DepotPaiement else modele.numero
    base = f"{prefixe}-{today_haiti().year}-"
    n = db.query(modele).filter(col.like(base + "%")).count() + 1
    while db.query(modele).filter(col == f"{base}{n:04d}").first():
        n += 1
    return f"{base}{n:04d}"


def _periode(date_debut: Optional[str], date_fin: Optional[str]):
    try:
        d1 = date_type.fromisoformat(date_debut) if date_debut else None
        d2 = date_type.fromisoformat(date_fin) if date_fin else None
    except ValueError:
        raise HTTPException(422, "Dates invalides (AAAA-MM-JJ).")
    return (bounds_haiti(d1)[0] if d1 else None, bounds_haiti(d2)[1] if d2 else None, d1, d2)


# ══════════════════════════════════════════════════════════════════
# PRODUITS / INVENTAIRE
# ══════════════════════════════════════════════════════════════════

def _stocks_par_lieu(db: Session) -> dict[int, dict[str, Decimal]]:
    rows = (db.query(BarMouvementStock.produit_id, BarMouvementStock.lieu, func.sum(BarMouvementStock.quantite))
            .group_by(BarMouvementStock.produit_id, BarMouvementStock.lieu).all())
    out: dict[int, dict[str, Decimal]] = {}
    for pid, lieu, q in rows:
        out.setdefault(pid, {})[lieu or "_"] = _d(q)
    return out


@router.get("/produits")
def produits_depot(actif: bool = True, db: Session = Depends(get_db)):
    q = db.query(BarProduit)
    if actif:
        q = q.filter(BarProduit.actif == True)
    produits = q.order_by(BarProduit.categorie, BarProduit.nom).all()
    stocks = _stocks_par_lieu(db)
    out = []
    for p in produits:
        s = stocks.get(p.id, {})
        stock_depot = s.get(DEPOT, Decimal("0"))
        seuil = _d(p.seuil_alerte_stock)
        prix = prix_actif(p.id, db) or Decimal("0")
        cout = cmup(p.id, db)
        out.append({
            "id": p.id, "nom": p.nom, "categorie": p.categorie, "unite": p.unite,
            "stock_depot":   float(stock_depot),
            "stock_devant":  float(s.get("DEVANT", 0)),
            "stock_piscine": float(s.get("PISCINE", 0)),
            "prix_vente":    float(prix),
            "cout_moyen":    float(cout),
            "seuil_min":     float(seuil),
            "alerte":        seuil > 0 and stock_depot <= seuil,
            "valeur_stock":  float((max(stock_depot, Decimal("0")) * cout).quantize(_CENT)),
        })
    return out


class ConfigProduitIn(BaseModel):
    prix_vente: Optional[float] = Field(default=None, gt=0)
    seuil_min:  Optional[float] = Field(default=None, ge=0)


@router.put("/produits/{produit_id}/config")
def configurer_produit(produit_id: int, data: ConfigProduitIn, request: Request, db: Session = Depends(get_db)):
    p = db.get(BarProduit, produit_id)
    if not p:
        raise HTTPException(404, "Produit introuvable.")
    if data.seuil_min is not None:
        p.seuil_alerte_stock = Decimal(str(data.seuil_min))
    if data.prix_vente is not None:
        nouveau = Decimal(str(data.prix_vente)).quantize(_CENT)
        if (prix_actif(p.id, db) or Decimal("-1")) != nouveau:
            now = datetime.now(timezone.utc)
            ancien = (db.query(BarPrixHistorique)
                      .filter(BarPrixHistorique.produit_id == p.id, BarPrixHistorique.date_fin.is_(None)).first())
            if ancien:
                ancien.date_fin = now
            db.add(BarPrixHistorique(produit_id=p.id, prix=nouveau, date_debut=now, utilisateur_id=_uid(request)))
    db.commit()
    return {"ok": True, "prix_vente": float(prix_actif(p.id, db) or 0), "seuil_min": float(p.seuil_alerte_stock)}


# ══════════════════════════════════════════════════════════════════
# ACHATS & RÉCEPTION
# ══════════════════════════════════════════════════════════════════

class LigneAchatIn(BaseModel):
    produit_id: int
    quantite:   float = Field(gt=0)
    prix_achat: float = Field(ge=0)


class AchatIn(BaseModel):
    fournisseur:    str
    numero_facture: Optional[str] = None
    date_facture:   Optional[date_type] = None
    notes:          Optional[str] = None
    lignes:         List[LigneAchatIn]


class ReceptionLigneIn(BaseModel):
    ligne_id:       int
    quantite_recue: float = Field(ge=0)


class ReceptionIn(BaseModel):
    lignes: List[ReceptionLigneIn]
    notes:  Optional[str] = None
    # Valider malgré un écart : le stock reçoit les quantités RÉELLEMENT
    # comptées (les écarts restent notés sur l'achat).
    forcer: bool = False


def _achat_dict(a: DepotAchat) -> dict:
    lignes = []
    for l in a.lignes:
        ecart = (float(_d(l.quantite_recue) - _d(l.quantite)) if l.quantite_recue is not None else None)
        lignes.append({
            "id": l.id, "produit_id": l.produit_id,
            "produit_nom": l.produit.nom if l.produit else str(l.produit_id),
            "unite": l.produit.unite if l.produit else None,
            "quantite": float(l.quantite), "prix_achat": float(l.prix_achat),
            "sous_total": float(l.sous_total),
            "quantite_recue": float(l.quantite_recue) if l.quantite_recue is not None else None,
            "ecart": ecart,
        })
    return {
        "id": a.id, "numero": a.numero, "fournisseur": a.fournisseur,
        "numero_facture": a.numero_facture,
        "date_facture": str(a.date_facture) if a.date_facture else None,
        "statut": a.statut, "total": float(a.total), "notes": a.notes, "ecart_notes": a.ecart_notes,
        "cree_par": a.cree_par.nom_complet if a.cree_par else None,
        "receptionne_par": a.receptionne_par.nom_complet if a.receptionne_par else None,
        "date_reception": a.date_reception.isoformat() if a.date_reception else None,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "lignes": lignes,
    }


def _remplir_lignes(db: Session, a: DepotAchat, lignes: List[LigneAchatIn]):
    if not lignes:
        raise HTTPException(422, "Ajoutez au moins un produit.")
    a.lignes.clear()
    total = Decimal("0")
    for l in lignes:
        if not db.get(BarProduit, l.produit_id):
            raise HTTPException(404, f"Produit #{l.produit_id} introuvable.")
        q, px = Decimal(str(l.quantite)), Decimal(str(l.prix_achat)).quantize(_CENT)
        st = (q * px).quantize(_CENT)
        total += st
        a.lignes.append(DepotAchatLigne(produit_id=l.produit_id, quantite=q, prix_achat=px, sous_total=st))
    a.total = total


@router.get("/achats")
def liste_achats(date_debut: Optional[str] = None, date_fin: Optional[str] = None,
                 statut: Optional[str] = None, db: Session = Depends(get_db)):
    dt1, dt2, _, _ = _periode(date_debut, date_fin)
    q = db.query(DepotAchat)
    if dt1: q = q.filter(DepotAchat.created_at >= dt1)
    if dt2: q = q.filter(DepotAchat.created_at <= dt2)
    if statut: q = q.filter(DepotAchat.statut == statut.upper())
    return [_achat_dict(a) for a in q.order_by(DepotAchat.created_at.desc()).limit(500).all()]


@router.post("/achats", status_code=201)
def creer_achat(data: AchatIn, request: Request, db: Session = Depends(get_db)):
    if not data.fournisseur.strip():
        raise HTTPException(422, "Fournisseur requis.")
    a = DepotAchat(numero=_numero(db, DepotAchat, "DA"), fournisseur=data.fournisseur.strip(),
                   numero_facture=(data.numero_facture or "").strip() or None,
                   date_facture=data.date_facture, notes=(data.notes or "").strip() or None,
                   cree_par_id=_uid(request))
    db.add(a)
    _remplir_lignes(db, a, data.lignes)
    db.commit(); db.refresh(a)
    return _achat_dict(a)


@router.put("/achats/{achat_id}")
def modifier_achat(achat_id: int, data: AchatIn, db: Session = Depends(get_db)):
    """Correction d'un achat pas encore réceptionné (ex. après un écart)."""
    a = db.get(DepotAchat, achat_id)
    if not a:
        raise HTTPException(404, "Achat introuvable.")
    if a.statut in ("RECEPTIONNE", "ANNULE"):
        raise HTTPException(409, "Achat déjà réceptionné ou annulé.")
    a.fournisseur    = data.fournisseur.strip() or a.fournisseur
    a.numero_facture = (data.numero_facture or "").strip() or None
    a.date_facture   = data.date_facture
    a.notes          = (data.notes or "").strip() or None
    _remplir_lignes(db, a, data.lignes)
    a.statut = "EN_ATTENTE"
    db.commit(); db.refresh(a)
    return _achat_dict(a)


@router.post("/achats/{achat_id}/reception")
def receptionner_achat(achat_id: int, data: ReceptionIn, request: Request, db: Session = Depends(get_db)):
    """Contrôle à la réception : quantités comptées ligne par ligne. Conforme
    (ou `forcer`) → stock du dépôt augmenté des quantités reçues. Sinon →
    statut ECART, différences enregistrées, correction demandée."""
    a = db.get(DepotAchat, achat_id)
    if not a:
        raise HTTPException(404, "Achat introuvable.")
    if a.statut in ("RECEPTIONNE", "ANNULE"):
        raise HTTPException(409, "Achat déjà réceptionné ou annulé.")
    recues = {l.ligne_id: Decimal(str(l.quantite_recue)) for l in data.lignes}
    ecarts = []
    for l in a.lignes:
        if l.id not in recues:
            raise HTTPException(422, f"Quantité reçue manquante pour « {l.produit.nom if l.produit else l.produit_id} ».")
        l.quantite_recue = recues[l.id]
        if recues[l.id] != _d(l.quantite):
            ecarts.append(f"{l.produit.nom if l.produit else l.produit_id} : facturé {float(l.quantite):g}, reçu {float(recues[l.id]):g}")
    if ecarts and not data.forcer:
        a.statut = "ECART"
        a.ecart_notes = ("; ".join(ecarts) + (f" — {data.notes.strip()}" if data.notes else ""))[:500]
        from notifications_service import creer_notification
        creer_notification(db, module="pos", type_="depot_ecart",
                           titre=f"Écart à la réception — {a.numero}",
                           message=a.ecart_notes[:280], lien="depot-achats", dedupe_minutes=None)
        db.commit(); db.refresh(a)
        return {"conforme": False, "ecarts": ecarts, "achat": _achat_dict(a)}

    now = datetime.now(timezone.utc)
    for l in a.lignes:
        if _d(l.quantite_recue) > 0:
            db.add(BarMouvementStock(
                produit_id=l.produit_id, type_mouvement="ENTREE", quantite=l.quantite_recue,
                motif=f"Réception dépôt {a.numero} — {a.fournisseur}", lieu=DEPOT,
                date_mouvement=now, utilisateur_id=_uid(request),
            ))
    if ecarts:
        a.ecart_notes = ("Validé avec écart : " + "; ".join(ecarts))[:500]
    a.statut = "RECEPTIONNE"
    a.receptionne_par_id = _uid(request)
    a.date_reception = now
    db.commit(); db.refresh(a)
    return {"conforme": not ecarts, "ecarts": ecarts, "achat": _achat_dict(a)}


@router.post("/achats/{achat_id}/annuler")
def annuler_achat(achat_id: int, db: Session = Depends(get_db),
                  _u: Utilisateur = Depends(_require_pdg_ou_admin)):
    a = db.get(DepotAchat, achat_id)
    if not a:
        raise HTTPException(404, "Achat introuvable.")
    if a.statut == "RECEPTIONNE":
        raise HTTPException(409, "Achat déjà réceptionné : le stock a été mis à jour.")
    a.statut = "ANNULE"
    db.commit()
    return _achat_dict(a)


# ══════════════════════════════════════════════════════════════════
# TRANSFERTS / VENTES — factures
# ══════════════════════════════════════════════════════════════════

class LigneTxIn(BaseModel):
    produit_id: int
    quantite:   float = Field(gt=0)


class TransactionIn(BaseModel):
    destination:    str                 # DEVANT | PISCINE | CLIENT
    client_nom:     Optional[str] = None
    client_contact: Optional[str] = None
    notes:          Optional[str] = None
    lignes:         List[LigneTxIn]
    valider:        bool = True         # valider tout de suite (stock déplacé)


def _tx_dict(t: DepotTransaction, avec_lignes: bool = True) -> dict:
    tot_dev = dv.ventiler((p.devise, p.montant, p.montant_htg) for p in t.paiements)
    d = {
        "id": t.id, "numero": t.numero, "type": t.type, "destination": t.destination,
        "destination_libelle": DESTINATIONS.get(t.destination, t.destination),
        "client_nom": t.client_nom, "client_contact": t.client_contact,
        "statut": t.statut, "statut_paiement": t.statut_paiement,
        "total": float(t.total), "montant_paye": float(t.montant_paye),
        "reste": float(max(_d(t.total) - _d(t.montant_paye), Decimal("0"))),
        "paye_htg": tot_dev["htg"], "paye_usd": tot_dev["usd"],
        "notes": t.notes,
        "responsable": t.responsable.nom_complet if t.responsable else None,
        "valide_par": t.valide_par.nom_complet if t.valide_par else None,
        "date_validation": t.date_validation.isoformat() if t.date_validation else None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "nb_lignes": len(t.lignes),
    }
    if avec_lignes:
        d["lignes"] = [{
            "produit_id": l.produit_id, "produit_nom": l.produit.nom if l.produit else str(l.produit_id),
            "unite": l.produit.unite if l.produit else None,
            "quantite": float(l.quantite), "prix_unitaire": float(l.prix_unitaire), "sous_total": float(l.sous_total),
        } for l in t.lignes]
        d["paiements"] = [_paiement_dict(p) for p in t.paiements]
    return d


def _verifier_stock(db: Session, lignes) -> list[str]:
    besoin: dict[int, Decimal] = {}
    for l in lignes:
        besoin[l.produit_id] = besoin.get(l.produit_id, Decimal("0")) + _d(l.quantite)
    manques = []
    for pid, q in besoin.items():
        dispo = stock_courant(pid, db, lieu=DEPOT)
        if dispo < q:
            p = db.get(BarProduit, pid)
            manques.append(f"{p.nom if p else pid} : disponible {float(dispo):g}, demandé {float(q):g}")
    return manques


def _valider(db: Session, t: DepotTransaction, uid: Optional[int]):
    manques = _verifier_stock(db, t.lignes)
    if manques:
        raise HTTPException(422, "Stock insuffisant au dépôt — " + "; ".join(manques))
    now = datetime.now(timezone.utc)
    for l in t.lignes:
        motif = f"{'Transfert' if t.type == 'TRANSFERT' else 'Vente'} {t.numero} → {DESTINATIONS.get(t.destination)}"
        db.add(BarMouvementStock(produit_id=l.produit_id, type_mouvement="TRANSFERT" if t.type == "TRANSFERT" else "SORTIE_VENTE",
                                 quantite=-_d(l.quantite), motif=motif, lieu=DEPOT,
                                 date_mouvement=now, utilisateur_id=uid))
        if t.type == "TRANSFERT":
            db.add(BarMouvementStock(produit_id=l.produit_id, type_mouvement="TRANSFERT",
                                     quantite=_d(l.quantite), motif=f"Reçu du dépôt — {t.numero}",
                                     lieu=t.destination, date_mouvement=now, utilisateur_id=uid))
    t.statut = "VALIDEE"
    t.valide_par_id = uid
    t.date_validation = now


@router.post("/transactions", status_code=201)
def creer_transaction(data: TransactionIn, request: Request, db: Session = Depends(get_db)):
    dest = (data.destination or "").strip().upper()
    if dest not in DESTINATIONS:
        raise HTTPException(422, "Destination : DEVANT, PISCINE ou CLIENT.")
    if dest == "CLIENT" and not (data.client_nom or "").strip():
        raise HTTPException(422, "Nom du client requis pour une vente externe.")
    if not data.lignes:
        raise HTTPException(422, "Ajoutez au moins un produit.")
    t = DepotTransaction(
        numero=_numero(db, DepotTransaction, "FD"),
        type="VENTE" if dest == "CLIENT" else "TRANSFERT", destination=dest,
        client_nom=(data.client_nom or "").strip() or None,
        client_contact=(data.client_contact or "").strip() or None,
        notes=(data.notes or "").strip() or None, responsable_id=_uid(request),
    )
    total = Decimal("0")
    for l in data.lignes:
        p = db.get(BarProduit, l.produit_id)
        if not p:
            raise HTTPException(404, f"Produit #{l.produit_id} introuvable.")
        if dest in ("DEVANT", "PISCINE") and p.lieu and p.lieu != dest:
            raise HTTPException(422, f"« {p.nom} » est réservé à {DESTINATIONS[p.lieu]}.")
        prix = prix_actif(p.id, db)
        if prix is None:
            raise HTTPException(422, f"Aucun prix de vente pour « {p.nom} » (Dépôt › Produits).")
        q = Decimal(str(l.quantite))
        st = (q * prix).quantize(_CENT)
        total += st
        t.lignes.append(DepotTransactionLigne(produit_id=p.id, quantite=q, prix_unitaire=prix, sous_total=st))
    t.total = total
    manques = _verifier_stock(db, t.lignes)
    if manques:
        raise HTTPException(422, "Stock insuffisant au dépôt — " + "; ".join(manques))
    db.add(t)
    db.flush()
    if data.valider:
        _valider(db, t, _uid(request))
    db.commit(); db.refresh(t)
    return _tx_dict(t)


@router.post("/transactions/{tx_id}/valider")
def valider_transaction(tx_id: int, request: Request, db: Session = Depends(get_db)):
    t = db.get(DepotTransaction, tx_id)
    if not t:
        raise HTTPException(404, "Transaction introuvable.")
    if t.statut != "PREPAREE":
        raise HTTPException(409, "Seule une facture préparée peut être validée.")
    _valider(db, t, _uid(request))
    db.commit(); db.refresh(t)
    return _tx_dict(t)


@router.post("/transactions/{tx_id}/annuler")
def annuler_transaction(tx_id: int, request: Request, db: Session = Depends(get_db),
                        _u: Utilisateur = Depends(_require_pdg_ou_admin)):
    """Annule une facture : une facture validée remet le stock à sa place
    (dépôt) — refusé si un paiement a déjà été reçu."""
    t = db.get(DepotTransaction, tx_id)
    if not t:
        raise HTTPException(404, "Transaction introuvable.")
    if t.statut == "ANNULEE":
        raise HTTPException(409, "Déjà annulée.")
    if t.paiements:
        raise HTTPException(409, "Des paiements ont été reçus pour cette facture.")
    if t.statut == "VALIDEE":
        now = datetime.now(timezone.utc)
        for l in t.lignes:
            db.add(BarMouvementStock(produit_id=l.produit_id, type_mouvement="TRANSFERT" if t.type == "TRANSFERT" else "ENTREE",
                                     quantite=_d(l.quantite), motif=f"Annulation {t.numero}", lieu=DEPOT,
                                     date_mouvement=now, utilisateur_id=_uid(request)))
            if t.type == "TRANSFERT":
                db.add(BarMouvementStock(produit_id=l.produit_id, type_mouvement="TRANSFERT",
                                         quantite=-_d(l.quantite), motif=f"Annulation {t.numero}",
                                         lieu=t.destination, date_mouvement=now, utilisateur_id=_uid(request)))
    t.statut = "ANNULEE"
    db.commit(); db.refresh(t)
    return _tx_dict(t)


@router.get("/transactions")
def liste_transactions(date_debut: Optional[str] = None, date_fin: Optional[str] = None,
                       destination: Optional[str] = None, statut: Optional[str] = None,
                       statut_paiement: Optional[str] = None, responsable: Optional[str] = None,
                       db: Session = Depends(get_db)):
    dt1, dt2, _, _ = _periode(date_debut, date_fin)
    q = db.query(DepotTransaction)
    if dt1: q = q.filter(DepotTransaction.created_at >= dt1)
    if dt2: q = q.filter(DepotTransaction.created_at <= dt2)
    if destination: q = q.filter(DepotTransaction.destination == destination.upper())
    if statut: q = q.filter(DepotTransaction.statut == statut.upper())
    if statut_paiement: q = q.filter(DepotTransaction.statut_paiement == statut_paiement.upper())
    rows = q.order_by(DepotTransaction.created_at.desc()).limit(1000).all()
    if responsable:
        r = responsable.strip().lower()
        rows = [t for t in rows if t.responsable and r in (t.responsable.nom_complet or "").lower()]
    return [_tx_dict(t, avec_lignes=False) for t in rows]


@router.get("/transactions/{tx_id}")
def detail_transaction(tx_id: int, db: Session = Depends(get_db)):
    t = db.get(DepotTransaction, tx_id)
    if not t:
        raise HTTPException(404, "Transaction introuvable.")
    return _tx_dict(t)


# ══════════════════════════════════════════════════════════════════
# CAISSE DÉPÔT — gourdes et dollars séparés
# ══════════════════════════════════════════════════════════════════

class PaiementIn(BaseModel):
    montant:     float = Field(gt=0)              # dans la devise
    devise:      Optional[str] = "HTG"
    taux_change: Optional[float] = None
    methode:     str = "CASH"
    notes:       Optional[str] = None


def _paiement_dict(p: DepotPaiement) -> dict:
    t = p.transaction
    return {
        "id": p.id, "numero_recu": p.numero_recu, "transaction_id": p.transaction_id,
        "facture": t.numero if t else None,
        "destination_libelle": DESTINATIONS.get(t.destination) if t else None,
        "client_nom": t.client_nom if t else None,
        "date_paiement": p.date_paiement.isoformat() if p.date_paiement else None,
        "devise": p.devise, "montant": float(p.montant),
        "taux_change": float(p.taux_change) if p.taux_change is not None else None,
        "montant_htg": float(p.montant_htg), "methode": p.methode, "notes": p.notes,
        "encaisse_par": p.utilisateur.nom_complet if p.utilisateur else None,
    }


@router.post("/transactions/{tx_id}/paiements", status_code=201)
def payer_transaction(tx_id: int, data: PaiementIn, request: Request, db: Session = Depends(get_db)):
    t = db.get(DepotTransaction, tx_id)
    if not t:
        raise HTTPException(404, "Facture introuvable.")
    if t.statut != "VALIDEE":
        raise HTTPException(409, "La facture doit être validée avant paiement.")
    if t.statut_paiement == "PAYEE":
        raise HTTPException(409, "Facture déjà payée.")
    methode = (data.methode or "CASH").upper()
    if methode not in METHODES:
        raise HTTPException(422, f"Méthode : {', '.join(METHODES)}.")
    devise, taux = dv.valider_devise(data.devise, data.taux_change, db)
    montant = Decimal(str(data.montant)).quantize(_CENT)
    montant_htg = dv.en_gourdes(devise, montant, taux)
    reste = _d(t.total) - _d(t.montant_paye)
    if montant_htg > reste + Decimal("0.5"):
        raise HTTPException(422, f"Le paiement ({montant_htg:,.2f} G) dépasse le reste à payer ({reste:,.2f} G).")
    p = DepotPaiement(transaction_id=t.id, numero_recu=_numero(db, DepotPaiement, "RD"),
                      devise=devise, montant=montant, taux_change=taux,
                      montant_htg=min(montant_htg, reste), methode=methode,
                      notes=(data.notes or "").strip() or None, utilisateur_id=_uid(request))
    db.add(p)
    t.montant_paye = _d(t.montant_paye) + p.montant_htg
    t.statut_paiement = "PAYEE" if t.montant_paye >= _d(t.total) else "PARTIELLE"
    db.commit(); db.refresh(p); db.refresh(t)
    return {"recu": _paiement_dict(p), "facture": _tx_dict(t)}


@router.get("/paiements")
def journal_caisse(date_debut: Optional[str] = None, date_fin: Optional[str] = None,
                   devise: Optional[str] = None, db: Session = Depends(get_db)):
    dt1, dt2, _, _ = _periode(date_debut, date_fin)
    q = db.query(DepotPaiement)
    if dt1: q = q.filter(DepotPaiement.date_paiement >= dt1)
    if dt2: q = q.filter(DepotPaiement.date_paiement <= dt2)
    if devise and devise.upper() in dv.DEVISES: q = q.filter(DepotPaiement.devise == devise.upper())
    rows = q.order_by(DepotPaiement.date_paiement.desc()).all()
    tot = dv.ventiler((p.devise, p.montant, p.montant_htg) for p in rows)
    par_methode: dict[str, dict] = {}
    for p in rows:
        m = par_methode.setdefault(p.methode, {"methode": p.methode, "htg": 0.0, "usd": 0.0})
        m["usd" if p.devise == "USD" else "htg"] += float(p.montant)
    return {
        "total_htg": tot["htg"], "total_usd": tot["usd"], "total_usd_equiv_htg": tot["usd_equiv_htg"],
        "nb": len(rows), "par_methode": list(par_methode.values()),
        "paiements": [_paiement_dict(p) for p in rows],
    }


@router.get("/caisse")
def balance_caisse(date_debut: Optional[str] = None, date_fin: Optional[str] = None,
                   db: Session = Depends(get_db)):
    j = journal_caisse(date_debut, date_fin, None, db)
    a_recevoir = db.query(func.sum(DepotTransaction.total - DepotTransaction.montant_paye)).filter(
        DepotTransaction.statut == "VALIDEE", DepotTransaction.statut_paiement != "PAYEE").scalar()
    return {
        "balance_htg": j["total_htg"], "balance_usd": j["total_usd"],
        "usd_equiv_htg": j["total_usd_equiv_htg"],
        "a_recevoir": float(a_recevoir or 0),
        "par_methode": j["par_methode"], "nb_paiements": j["nb"],
    }


# ══════════════════════════════════════════════════════════════════
# RAPPORT
# ══════════════════════════════════════════════════════════════════

@router.get("/rapport")
def rapport_depot(date_debut: Optional[str] = None, date_fin: Optional[str] = None,
                  db: Session = Depends(get_db)):
    dt1, dt2, d1, d2 = _periode(date_debut, date_fin)
    produits = produits_depot(True, db)

    q_tx = db.query(DepotTransaction).filter(DepotTransaction.statut == "VALIDEE")
    if dt1: q_tx = q_tx.filter(DepotTransaction.date_validation >= dt1)
    if dt2: q_tx = q_tx.filter(DepotTransaction.date_validation <= dt2)
    txs = q_tx.all()
    par_dest = {k: {"destination": k, "libelle": v, "nb": 0, "total": 0.0, "paye": 0.0} for k, v in DESTINATIONS.items()}
    for t in txs:
        e = par_dest[t.destination]
        e["nb"] += 1; e["total"] += float(t.total); e["paye"] += float(t.montant_paye)

    q_a = db.query(DepotAchat).filter(DepotAchat.statut == "RECEPTIONNE")
    if dt1: q_a = q_a.filter(DepotAchat.date_reception >= dt1)
    if dt2: q_a = q_a.filter(DepotAchat.date_reception <= dt2)
    achats = q_a.all()
    caisse = journal_caisse(date_debut, date_fin, None, db)
    return {
        "periode": {"debut": str(d1) if d1 else None, "fin": str(d2) if d2 else None},
        "inventaire": {
            "nb_produits": len(produits),
            "valeur_stock": round(sum(p["valeur_stock"] for p in produits), 2),
            "en_alerte": [p for p in produits if p["alerte"]],
        },
        "ventes_transferts": {
            "par_destination": list(par_dest.values()),
            "total": round(sum(float(t.total) for t in txs), 2),
            "nb": len(txs),
        },
        "achats": {"nb": len(achats), "total": round(sum(float(a.total) for a in achats), 2)},
        "caisse": {"total_htg": caisse["total_htg"], "total_usd": caisse["total_usd"],
                   "total_usd_equiv_htg": caisse["total_usd_equiv_htg"],
                   "par_methode": caisse["par_methode"]},
    }
