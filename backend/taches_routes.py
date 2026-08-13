"""
Routes API Module Tâches — préfixe /api/taches
Planification de tâches individuelles ou collectives entre employés.
"""
from __future__ import annotations

from datetime import date as date_type, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from database import get_db
from models import Tache, TacheParticipant, Utilisateur

router = APIRouter(prefix="/api/taches", tags=["taches"])

_STATUTS_VALIDES = ("A_FAIRE", "EN_COURS", "TERMINEE", "ANNULEE")


class TacheIn(BaseModel):
    titre:           str
    description:     Optional[str] = None
    date_echeance:   Optional[str] = None   # AAAA-MM-JJ
    participant_ids: list[int] = []


class TachePatch(BaseModel):
    titre:           Optional[str]       = None
    description:     Optional[str]       = None
    date_echeance:   Optional[str]       = None
    participant_ids: Optional[list[int]] = None


class StatutIn(BaseModel):
    statut: str


def _uid(request: Request) -> Optional[int]:
    u = getattr(request.state, "user", None)
    return u.id if u else None


def _tache_dict(t: Tache, aujourdhui: date_type) -> dict:
    participants = [
        {"id": p.utilisateur.id, "nom_complet": p.utilisateur.nom_complet or p.utilisateur.username}
        for p in t.participants if p.utilisateur
    ]
    en_retard = (
        t.date_echeance is not None
        and t.date_echeance < aujourdhui
        and t.statut not in ("TERMINEE", "ANNULEE")
    )
    return {
        "id":            t.id,
        "titre":         t.titre,
        "description":   t.description,
        "createur_id":   t.createur_id,
        "createur_nom":  (t.createur.nom_complet or t.createur.username) if t.createur else None,
        "date_echeance": t.date_echeance.isoformat() if t.date_echeance else None,
        "statut":        t.statut,
        "en_retard":     en_retard,
        "type":          "collective" if len(participants) >= 1 else "individuelle",
        "participants":  participants,
        "created_at":    t.created_at.isoformat() if t.created_at else None,
        "updated_at":    t.updated_at.isoformat() if t.updated_at else None,
    }


@router.get("/utilisateurs")
def lister_utilisateurs_pour_taches(db: Session = Depends(get_db)):
    """Liste légère des comptes actifs pour choisir des participants —
    accessible à tout utilisateur connecté (pas réservé admin)."""
    users = db.query(Utilisateur).filter(Utilisateur.actif == True).order_by(Utilisateur.nom_complet).all()
    return [
        {"id": u.id, "nom_complet": u.nom_complet or u.username, "username": u.username}
        for u in users
    ]


@router.get("")
def lister_taches(request: Request, statut: Optional[str] = None, db: Session = Depends(get_db)):
    uid = _uid(request)
    # Sous-requête plutôt qu'un JOIN + DISTINCT : un JOIN produirait une ligne
    # Tache dupliquée par participant (rejeté par Postgres si DISTINCT est
    # combiné à un ORDER BY sur une expression absente du SELECT).
    mes_tache_ids = db.query(TacheParticipant.tache_id).filter(TacheParticipant.utilisateur_id == uid)
    q = db.query(Tache).filter(or_(Tache.createur_id == uid, Tache.id.in_(mes_tache_ids)))
    if statut:
        q = q.filter(Tache.statut == statut)
    taches = q.order_by(Tache.date_echeance.is_(None), Tache.date_echeance.asc(), Tache.created_at.desc()).all()

    aujourdhui = date_type.today()

    # Rappels d'échéance — vérification opportuniste à chaque chargement
    # (pas de scheduler dédié dans cette appli).
    from notifications_service import creer_notification
    for t in taches:
        if t.statut in ("TERMINEE", "ANNULEE") or not t.date_echeance:
            continue
        if t.date_echeance < aujourdhui:
            creer_notification(
                db, module="systeme", type_="tache_en_retard",
                titre=f"Tâche en retard — {t.titre}",
                message=f"Échéance dépassée depuis le {t.date_echeance.isoformat()}.",
                lien="taches", dedupe_minutes=1440,
            )
        elif t.date_echeance <= aujourdhui + timedelta(days=1):
            creer_notification(
                db, module="systeme", type_="tache_echeance_proche",
                titre=f"Échéance proche — {t.titre}",
                message=f"À rendre pour le {t.date_echeance.isoformat()}.",
                lien="taches", dedupe_minutes=1440,
            )
    db.commit()

    return [_tache_dict(t, aujourdhui) for t in taches]


@router.post("", status_code=201)
def creer_tache(data: TacheIn, request: Request, db: Session = Depends(get_db)):
    if not data.titre.strip():
        raise HTTPException(400, "Le titre est requis.")
    uid = _uid(request)
    date_ech = None
    if data.date_echeance:
        try:
            date_ech = date_type.fromisoformat(data.date_echeance)
        except ValueError:
            raise HTTPException(400, "Date d'échéance invalide.")

    t = Tache(
        titre=data.titre.strip(),
        description=(data.description or "").strip() or None,
        createur_id=uid,
        date_echeance=date_ech,
        statut="A_FAIRE",
    )
    db.add(t)
    db.flush()

    ids_participants = set(data.participant_ids or [])
    ids_participants.discard(uid)  # le créateur voit déjà sa tâche
    noms = []
    for pid in ids_participants:
        u = db.query(Utilisateur).filter_by(id=pid, actif=True).first()
        if u:
            db.add(TacheParticipant(tache_id=t.id, utilisateur_id=pid))
            noms.append(u.nom_complet or u.username)

    if noms:
        from notifications_service import creer_notification
        creer_notification(
            db, module="systeme", type_="nouvelle_tache",
            titre=f"Nouvelle tâche assignée — {t.titre}",
            message=f"Pour : {', '.join(noms)}" + (f" · Échéance {date_ech.isoformat()}" if date_ech else ""),
            lien="taches", dedupe_minutes=None,
        )

    db.commit()
    db.refresh(t)
    return _tache_dict(t, date_type.today())


@router.put("/{tache_id}")
def modifier_tache(tache_id: int, data: TachePatch, request: Request, db: Session = Depends(get_db)):
    t = db.query(Tache).filter_by(id=tache_id).first()
    if not t:
        raise HTTPException(404, "Tâche introuvable.")
    uid = _uid(request)
    if t.createur_id != uid:
        raise HTTPException(403, "Seul le créateur peut modifier cette tâche.")

    if data.titre is not None:
        if not data.titre.strip():
            raise HTTPException(400, "Le titre est requis.")
        t.titre = data.titre.strip()
    if data.description is not None:
        t.description = data.description.strip() or None
    if data.date_echeance is not None:
        if data.date_echeance:
            try:
                t.date_echeance = date_type.fromisoformat(data.date_echeance)
            except ValueError:
                raise HTTPException(400, "Date d'échéance invalide.")
        else:
            t.date_echeance = None
    if data.participant_ids is not None:
        db.query(TacheParticipant).filter_by(tache_id=t.id).delete()
        for pid in set(data.participant_ids):
            if pid == uid:
                continue
            u = db.query(Utilisateur).filter_by(id=pid, actif=True).first()
            if u:
                db.add(TacheParticipant(tache_id=t.id, utilisateur_id=pid))

    db.commit()
    db.refresh(t)
    return _tache_dict(t, date_type.today())


@router.patch("/{tache_id}/statut")
def changer_statut_tache(tache_id: int, data: StatutIn, request: Request, db: Session = Depends(get_db)):
    if data.statut not in _STATUTS_VALIDES:
        raise HTTPException(400, f"Statut invalide. Valeurs : {_STATUTS_VALIDES}")
    t = db.query(Tache).filter_by(id=tache_id).first()
    if not t:
        raise HTTPException(404, "Tâche introuvable.")
    uid = _uid(request)
    est_participant = db.query(TacheParticipant).filter_by(tache_id=t.id, utilisateur_id=uid).first() is not None
    if t.createur_id != uid and not est_participant:
        raise HTTPException(403, "Vous ne participez pas à cette tâche.")
    t.statut = data.statut
    db.commit()
    return {"ok": True}


@router.delete("/{tache_id}")
def supprimer_tache(tache_id: int, request: Request, db: Session = Depends(get_db)):
    t = db.query(Tache).filter_by(id=tache_id).first()
    if not t:
        raise HTTPException(404, "Tâche introuvable.")
    uid = _uid(request)
    if t.createur_id != uid:
        raise HTTPException(403, "Seul le créateur peut supprimer cette tâche.")
    db.delete(t)
    db.commit()
    return {"ok": True}
