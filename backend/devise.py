"""
Devises — gourdes (HTG) et dollars US (USD), pour tous les départements.

Règle commune : une vente / facture reste valorisée en gourdes (montant
comptable), mais l'argent RÉELLEMENT reçu est enregistré dans sa devise :
  devise          HTG | USD
  montant_devise  montant reçu dans cette devise (ex. 20 $)
  taux_change     HTG pour 1 USD (USD seulement)
Les caisses et rapports comptent toujours les gourdes et les dollars à part :
les dollars ne sont jamais additionnés aux gourdes comme s'ils en étaient.

Le taux du jour proposé par défaut est commun à toute l'institution (stocké
dans hotel_config.taux_usd, historique : l'hôtel a été le premier à recevoir
des dollars) — modifiable par le PDG/admin via /api/devise/taux.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db

DEVISES = ("HTG", "USD")
_CENT = Decimal("0.01")

router = APIRouter(prefix="/api/devise", tags=["Devises"])


def _d(v) -> Decimal:
    return Decimal(str(v)) if v is not None else Decimal("0")


def taux_defaut(db: Session) -> Decimal:
    from models import HotelConfig
    cfg = db.get(HotelConfig, 1)
    return _d(cfg.taux_usd) if cfg and cfg.taux_usd else Decimal("130")


def valider_devise(devise: Optional[str], taux, db: Session) -> tuple[str, Optional[Decimal]]:
    """('HTG', None) ou ('USD', taux) — taux par défaut : taux du jour."""
    devise = (devise or "HTG").strip().upper()
    if devise not in DEVISES:
        raise HTTPException(422, "Devise invalide — HTG (gourdes) ou USD (dollars).")
    if devise == "HTG":
        return devise, None
    t = _d(taux) if taux not in (None, "", 0) else taux_defaut(db)
    if t <= 0:
        raise HTTPException(422, "Taux du dollar invalide (HTG pour 1 USD).")
    return devise, t


def en_gourdes(devise: str, montant, taux: Optional[Decimal]) -> Decimal:
    m = _d(montant)
    return (m * taux).quantize(_CENT, rounding=ROUND_HALF_UP) if devise == "USD" else m.quantize(_CENT)


def en_dollars(montant_htg, taux: Decimal) -> Decimal:
    return (_d(montant_htg) / taux).quantize(_CENT, rounding=ROUND_HALF_UP)


def paiement_vente(total_htg, devise: Optional[str], taux, db: Session) -> dict:
    """Pour une vente payée comptant : colonnes devise / montant_devise /
    taux_change. En dollars, le client paie l'équivalent du total au taux."""
    devise, t = valider_devise(devise, taux, db)
    if devise == "HTG":
        return {"devise": "HTG", "montant_devise": _d(total_htg).quantize(_CENT), "taux_change": None}
    return {"devise": "USD", "montant_devise": en_dollars(total_htg, t), "taux_change": t}


def ventiler(lignes) -> dict:
    """lignes : itérable de (devise, montant_dans_la_devise, montant_htg).
    Renvoie les totaux séparés gourdes / dollars."""
    htg = usd = usd_eq = Decimal("0")
    for devise, montant, montant_htg in lignes:
        if devise == "USD":
            usd    += _d(montant)
            usd_eq += _d(montant_htg)
        else:
            htg += _d(montant_htg if montant_htg is not None else montant)
    return {"htg": float(htg), "usd": float(usd), "usd_equiv_htg": float(usd_eq)}


# ── Taux du jour (commun à tous les départements) ─────────────────

class TauxIn(BaseModel):
    taux_usd: float = Field(gt=0)


def _require_pdg_ou_admin(request: Request, db: Session = Depends(get_db)):
    from models import Utilisateur
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


@router.get("/taux")
def lire_taux(db: Session = Depends(get_db)):
    from models import HotelConfig
    cfg = db.get(HotelConfig, 1)
    return {
        "taux_usd":   float(taux_defaut(db)),
        "updated_at": cfg.updated_at.isoformat() if cfg and cfg.updated_at else None,
        "updated_by": cfg.updated_by if cfg else None,
    }


@router.put("/taux")
def modifier_taux(data: TauxIn, db: Session = Depends(get_db), user=Depends(_require_pdg_ou_admin)):
    from models import HotelConfig
    cfg = db.get(HotelConfig, 1)
    if not cfg:
        cfg = HotelConfig(id=1)
        db.add(cfg)
    cfg.taux_usd   = Decimal(str(data.taux_usd))
    cfg.updated_at = datetime.now(tz=timezone.utc)
    cfg.updated_by = user.nom_complet
    db.commit()
    return lire_taux(db)
