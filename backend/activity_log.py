"""
Service de journalisation d'activité (connexions, actions admin, OTP).

log_event() est silencieux en cas d'erreur — un audit raté
ne doit jamais bloquer l'opération métier.
"""
import json
import logging

from sqlalchemy.orm import Session

from models import AuditLog

log = logging.getLogger("activity")

# ── Constantes d'action ───────────────────────────────────────────
LOGIN_SUCCESS    = "login_success"
LOGIN_FAILED     = "login_failed"
LOGOUT           = "logout"
OTP_SENT         = "otp_sent"
OTP_VERIFIED     = "otp_verified"
OTP_FAILED       = "otp_failed"
SESSION_REVOKED  = "session_revoked"
SESSIONS_CLEARED = "sessions_cleared"
PASSWORD_RESET   = "password_reset"
PIN_RESET        = "pin_reset"
USER_CREATED     = "user_created"
USER_UPDATED     = "user_updated"
USER_DISABLED    = "user_disabled"
USER_ENABLED     = "user_enabled"


def log_event(
    db: Session,
    action: str,
    *,
    user_id: int | None        = None,
    target_user_id: int | None = None,
    ip_address: str | None     = None,
    details: dict | None       = None,
) -> None:
    """Enregistre un événement dans le journal d'activité."""
    try:
        entry = AuditLog(
            action         = action,
            user_id        = user_id,
            target_user_id = target_user_id,
            ip_address     = ip_address,
            details        = json.dumps(details, ensure_ascii=False) if details else None,
        )
        db.add(entry)
        db.commit()
    except Exception as exc:
        log.error("Erreur activity_log [%s] : %s", action, exc)
        try:
            db.rollback()
        except Exception:
            pass
