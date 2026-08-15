"""
Routes API génériques — Pièces jointes (factures, reçus…) attachées à
n'importe quelle dépense/achat du système, quel que soit le département.
Préfixe /api/pieces-jointes
"""
from __future__ import annotations

import base64
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from database import get_db
from models import (
    PieceJointe, Depense, Achat, CuisineDepense, CuisineAchat, ZelleDepense, BarAchat,
)

router = APIRouter(prefix="/api/pieces-jointes", tags=["pieces-jointes"])

# Type d'entité -> modèle SQLAlchemy correspondant, pour vérifier que
# l'entité référencée existe vraiment avant d'accepter une pièce jointe.
_TYPES_AUTORISES = {
    "depense":         Depense,
    "achat":           Achat,
    "cuisine_depense": CuisineDepense,
    "cuisine_achat":   CuisineAchat,
    "zelle_depense":   ZelleDepense,
    "bar_achat":       BarAchat,
}

_MIME_AUTORISES     = {"application/pdf", "image/jpeg", "image/jpg", "image/png"}
_TAILLE_MAX_OCTETS  = 5 * 1024 * 1024  # 5 Mo par fichier


def _uid(request: Request) -> Optional[int]:
    u = getattr(request.state, "user", None)
    return u.id if u else None


def _pj_dict(p: PieceJointe) -> dict:
    return {
        "id":               p.id,
        "type_entite":      p.type_entite,
        "entite_id":        p.entite_id,
        "nom_fichier":      p.nom_fichier,
        "type_mime":        p.type_mime,
        "taille_octets":    p.taille_octets,
        "uploaded_par_nom": p.uploaded_par.nom_complet if p.uploaded_par else None,
        "created_at":       p.created_at.isoformat() if p.created_at else None,
    }


@router.get("")
def lister_pieces_jointes(type_entite: str, entite_id: int, db: Session = Depends(get_db)):
    if type_entite not in _TYPES_AUTORISES:
        raise HTTPException(400, "Type d'entité invalide.")
    pjs = (
        db.query(PieceJointe)
        .filter_by(type_entite=type_entite, entite_id=entite_id)
        .order_by(PieceJointe.created_at.desc())
        .all()
    )
    return [_pj_dict(p) for p in pjs]


@router.post("", status_code=201)
async def uploader_piece_jointe(
    request: Request,
    type_entite: str = Form(...),
    entite_id:   int = Form(...),
    fichier:     UploadFile = File(...),
    db: Session = Depends(get_db),
):
    modele = _TYPES_AUTORISES.get(type_entite)
    if not modele:
        raise HTTPException(400, "Type d'entité invalide.")
    entite = db.query(modele).filter_by(id=entite_id).first()
    if not entite:
        raise HTTPException(404, "Dépense/achat introuvable.")

    mime = (fichier.content_type or "").lower()
    if mime not in _MIME_AUTORISES:
        raise HTTPException(400, "Format non supporté — PDF, JPG ou PNG uniquement.")

    contenu = await fichier.read()
    if len(contenu) > _TAILLE_MAX_OCTETS:
        raise HTTPException(413, "Fichier trop volumineux (max 5 Mo).")
    if not contenu:
        raise HTTPException(400, "Fichier vide.")

    pj = PieceJointe(
        type_entite=type_entite,
        entite_id=entite_id,
        nom_fichier=(fichier.filename or "document")[:255],
        type_mime=mime,
        taille_octets=len(contenu),
        contenu_base64=base64.b64encode(contenu).decode("ascii"),
        uploaded_par_id=_uid(request),
    )
    db.add(pj)
    db.commit()
    db.refresh(pj)
    return _pj_dict(pj)


@router.get("/{piece_id}/telecharger")
def telecharger_piece_jointe(piece_id: int, db: Session = Depends(get_db)):
    pj = db.query(PieceJointe).filter_by(id=piece_id).first()
    if not pj:
        raise HTTPException(404, "Pièce jointe introuvable.")
    contenu = base64.b64decode(pj.contenu_base64)
    return Response(
        content=contenu,
        media_type=pj.type_mime,
        headers={"Content-Disposition": f'inline; filename="{pj.nom_fichier}"'},
    )


@router.delete("/{piece_id}")
def supprimer_piece_jointe(piece_id: int, db: Session = Depends(get_db)):
    pj = db.query(PieceJointe).filter_by(id=piece_id).first()
    if not pj:
        raise HTTPException(404, "Pièce jointe introuvable.")
    db.delete(pj)
    db.commit()
    return {"ok": True}
