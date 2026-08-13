"""
Service de notifications — événements clés du système, partagés entre
utilisateurs avec un statut lu/non-lu/supprimé individuel.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from models import Notification, NotificationEtat

# Délai anti-spam : n'émet pas deux notifications identiques (même module+type+titre)
# à moins que ce délai (minutes) ne soit écoulé depuis la dernière.
DEDUPE_MINUTES_PAR_DEFAUT = 360  # 6h


def creer_notification(
    db: Session, module: str, type_: str, titre: str,
    message: str | None = None, lien: str | None = None,
    dedupe_minutes: int | None = DEDUPE_MINUTES_PAR_DEFAUT,
) -> Notification | None:
    """Crée une notification. Si dedupe_minutes est fourni et qu'une notification
    identique (module+type+titre) existe déjà dans cette fenêtre, ne recrée rien
    (évite le bruit — ex: alerte stock bas répétée à chaque relevé)."""
    if dedupe_minutes:
        seuil = datetime.now(timezone.utc) - timedelta(minutes=dedupe_minutes)
        existe = (
            db.query(Notification)
            .filter(
                Notification.module == module,
                Notification.type == type_,
                Notification.titre == titre,
                Notification.created_at >= seuil,
            )
            .first()
        )
        if existe:
            return None

    notif = Notification(module=module, type=type_, titre=titre, message=message, lien=lien)
    db.add(notif)
    db.flush()
    return notif


def lister_notifications(
    db: Session, utilisateur_id: int,
    seulement_non_lues: bool = False,
    seulement_resolues: bool = False,
    limite: int = 200,
) -> list[dict]:
    q = (
        db.query(Notification, NotificationEtat)
        .outerjoin(
            NotificationEtat,
            (NotificationEtat.notification_id == Notification.id)
            & (NotificationEtat.utilisateur_id == utilisateur_id),
        )
        .filter((NotificationEtat.supprime.is_(None)) | (NotificationEtat.supprime.is_(False)))
        .order_by(Notification.created_at.desc())
        .limit(limite)
    )
    resultats = []
    for notif, etat in q.all():
        lu = bool(etat.lu) if etat else False
        if seulement_non_lues and lu:
            continue
        if seulement_resolues and not notif.resolu:
            continue
        resultats.append({
            "id": notif.id,
            "module": notif.module,
            "type": notif.type,
            "titre": notif.titre,
            "message": notif.message,
            "lien": notif.lien,
            "created_at": notif.created_at,
            "lu": lu,
            "resolu": bool(notif.resolu),
            "resolu_par_nom": notif.resolu_par.nom_complet if notif.resolu_par else None,
            "resolu_at": notif.resolu_at,
        })
    return resultats


def compter_non_lues(db: Session, utilisateur_id: int) -> int:
    total_notifs = db.query(func.count(Notification.id)).scalar() or 0
    etats = (
        db.query(NotificationEtat)
        .filter(NotificationEtat.utilisateur_id == utilisateur_id)
        .all()
    )
    supprimees = sum(1 for e in etats if e.supprime)
    lues_non_supprimees = sum(1 for e in etats if e.lu and not e.supprime)
    return max(0, total_notifs - supprimees - lues_non_supprimees)


def _get_or_create_etat(db: Session, notification_id: int, utilisateur_id: int) -> NotificationEtat:
    etat = (
        db.query(NotificationEtat)
        .filter_by(notification_id=notification_id, utilisateur_id=utilisateur_id)
        .first()
    )
    if not etat:
        etat = NotificationEtat(notification_id=notification_id, utilisateur_id=utilisateur_id)
        db.add(etat)
    return etat


def marquer_lu(db: Session, notification_id: int, utilisateur_id: int, lu: bool = True) -> None:
    etat = _get_or_create_etat(db, notification_id, utilisateur_id)
    etat.lu = lu
    etat.lu_at = datetime.now(timezone.utc) if lu else None
    db.commit()


def marquer_tout_lu(db: Session, utilisateur_id: int) -> None:
    notif_ids = [n.id for n in db.query(Notification.id).all()]
    for nid in notif_ids:
        etat = _get_or_create_etat(db, nid, utilisateur_id)
        etat.lu = True
        etat.lu_at = datetime.now(timezone.utc)
    db.commit()


def supprimer_pour_utilisateur(db: Session, notification_id: int, utilisateur_id: int) -> None:
    etat = _get_or_create_etat(db, notification_id, utilisateur_id)
    etat.supprime = True
    db.commit()


def marquer_resolu(db: Session, notification_id: int, utilisateur_id: int, resolu: bool = True) -> Notification | None:
    """Statut global (partage entre tous) — resoudre un probleme est un fait
    objectif sur le systeme, pas une preference de lecture personnelle."""
    notif = db.query(Notification).filter_by(id=notification_id).first()
    if not notif:
        return None
    notif.resolu = resolu
    notif.resolu_par_id = utilisateur_id if resolu else None
    notif.resolu_at = datetime.now(timezone.utc) if resolu else None
    db.commit()
    return notif
