"""
Service OTP — génération, envoi Gmail et vérification.

Sécurité :
  - Code 6 chiffres via secrets.randbelow (CSPRNG, jamais random)
  - Stockage SHA-256 uniquement — le code en clair n'est jamais persisté
  - Rate limit : max 3 demandes par utilisateur sur 10 minutes
  - Max 3 tentatives de vérification par code
  - pending_token aléatoire lie le cookie étape-1 à l'enregistrement DB
  - Invalidation immédiate après succès ou épuisement des tentatives
"""
import hashlib
import logging
import os
import secrets
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from sqlalchemy.orm import Session

from models import OTPCode, Utilisateur

log = logging.getLogger("otp")

# ── Configuration (chargée depuis .env via database.py au démarrage) ─────────
OTP_ENABLED          = os.getenv("OTP_ENABLED", "true").lower() in ("1", "true", "yes")
OTP_DURATION_MIN     = int(os.getenv("OTP_DURATION_MINUTES", "5"))
OTP_MAX_ATTEMPTS     = int(os.getenv("OTP_MAX_ATTEMPTS", "3"))
OTP_RATE_WINDOW_MIN  = int(os.getenv("OTP_RATE_WINDOW_MINUTES", "10"))
OTP_RATE_LIMIT       = int(os.getenv("OTP_RATE_LIMIT", "3"))

EMAIL_HOST      = os.getenv("EMAIL_HOST",      "smtp.gmail.com")
EMAIL_PORT      = int(os.getenv("EMAIL_PORT",  "587"))
EMAIL_USER      = os.getenv("EMAIL_HOST_USER",     "")
EMAIL_PASSWORD  = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_FROM_NAME = os.getenv("EMAIL_FROM_NAME", "Konekta · Bon Prix")

OTP_PENDING_COOKIE  = "otp_pending"
OTP_PENDING_MAX_AGE = (OTP_RATE_WINDOW_MIN + 2) * 60  # léger délai tampon


# ── Primitives cryptographiques ───────────────────────────────────────────────

def _generate_code() -> str:
    """Code 6 chiffres uniforme via CSPRNG (100 000 – 999 999)."""
    return f"{secrets.randbelow(900_000) + 100_000}"


def _hash_code(code: str) -> str:
    """SHA-256 hex — ne stocke et ne compare jamais le code en clair."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _mask_email(email: str) -> str:
    """Masque partiel pour l'UI : t***@gmail.com."""
    try:
        local, domain = email.split("@", 1)
        return f"{local[0]}***@{domain}"
    except Exception:
        return "***"


# ── Rate limiting ─────────────────────────────────────────────────────────────

def _check_rate_limit(db: Session, user_id: int) -> None:
    """Lève ValueError si trop de demandes OTP récentes pour cet utilisateur."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=OTP_RATE_WINDOW_MIN)
    count = (
        db.query(OTPCode)
        .filter(OTPCode.user_id == user_id, OTPCode.created_at >= cutoff)
        .count()
    )
    if count >= OTP_RATE_LIMIT:
        raise ValueError(
            f"Trop de codes demandés — attendez {OTP_RATE_WINDOW_MIN} minutes avant de réessayer."
        )


# ── Création ──────────────────────────────────────────────────────────────────

def create_otp(db: Session, user_id: int) -> tuple[str, str]:
    """
    Génère un OTP pour l'utilisateur donné.

    Retourne (code_clair, pending_token).
    Le code_clair doit être envoyé par email et jamais logué.
    Le pending_token est placé dans un cookie court et sert de clé de recherche.
    """
    _check_rate_limit(db, user_id)

    # Invalider tout OTP en cours pour cet utilisateur
    db.query(OTPCode).filter(
        OTPCode.user_id == user_id,
        OTPCode.used.is_(False),
    ).update({"used": True}, synchronize_session=False)

    code          = _generate_code()
    pending_token = secrets.token_urlsafe(32)
    expires_at    = datetime.now(timezone.utc) + timedelta(minutes=OTP_DURATION_MIN)

    otp = OTPCode(
        user_id       = user_id,
        code_hash     = _hash_code(code),
        pending_token = pending_token,
        expires_at    = expires_at,
        attempts      = 0,
        used          = False,
    )
    db.add(otp)
    db.commit()
    return code, pending_token


# ── Email ─────────────────────────────────────────────────────────────────────

def _build_email_html(nom: str, code: str) -> str:
    digits = "&nbsp;".join(code)
    return f"""<!DOCTYPE html>
<html lang="fr">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#070e1c;font-family:Arial,Helvetica,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#070e1c;padding:36px 16px">
  <tr><td align="center">
    <table width="100%" style="max-width:480px;background:#0b1628;border-radius:14px;border:1px solid rgba(232,197,88,.22);overflow:hidden">

      <!-- En-tête -->
      <tr>
        <td style="padding:28px 32px 24px;text-align:center;border-bottom:2px solid #e8c558">
          <div style="font-size:32px;font-weight:900;color:#e8c558;line-height:1">K</div>
          <div style="font-size:13px;font-weight:800;color:#e8c558;letter-spacing:3px;margin-top:4px">KONEKTA</div>
          <div style="font-size:10px;color:rgba(232,197,88,.45);margin-top:3px">
            Bon Prix &middot; Complexe Commerciale de Pillatre
          </div>
        </td>
      </tr>

      <!-- Corps -->
      <tr>
        <td style="padding:32px 32px 24px">
          <p style="margin:0 0 6px;color:#dde8f8;font-size:15px;font-weight:600">
            Bonjour <span style="color:#e8c558">{nom}</span>,
          </p>
          <p style="margin:0 0 28px;color:rgba(221,232,248,.65);font-size:13px;line-height:1.65">
            Voici votre code de vérification pour accéder à Konekta.
            Saisissez-le dans l'application pour finaliser votre connexion.
          </p>

          <!-- Bloc code -->
          <div style="background:#050c18;border:2px solid #e8c558;border-radius:12px;padding:26px 20px;text-align:center;margin:0 0 26px">
            <div style="font-size:10px;font-weight:800;color:rgba(232,197,88,.5);letter-spacing:.18em;text-transform:uppercase;margin-bottom:12px">
              Code de vérification
            </div>
            <div style="font-size:42px;font-weight:900;color:#e8c558;letter-spacing:14px;
                        font-family:'Courier New',Courier,monospace">
              {digits}
            </div>
            <div style="font-size:11px;color:rgba(232,197,88,.38);margin-top:14px">
              ⏱ Valide pendant <strong style="color:rgba(232,197,88,.6)">{OTP_DURATION_MIN}&nbsp;minutes</strong>
              &nbsp;&middot;&nbsp; Usage unique
            </div>
          </div>

          <!-- Avertissement -->
          <table cellpadding="0" cellspacing="0" width="100%"
                 style="background:rgba(239,68,68,.07);border:1px solid rgba(239,68,68,.22);
                        border-left:3px solid #ef4444;border-radius:8px;margin:0 0 20px">
            <tr>
              <td style="padding:12px 14px;color:rgba(252,133,133,.9);font-size:12px;line-height:1.5">
                ⚠️ <strong>Ne partagez jamais ce code.</strong>
                L'équipe Konekta ne vous le demandera jamais par téléphone, email ou messagerie.
              </td>
            </tr>
          </table>

          <p style="margin:0;color:rgba(221,232,248,.35);font-size:11px;line-height:1.6">
            Si vous n'êtes pas à l'origine de cette demande, ignorez ce message.
            Votre compte est toujours sécurisé.
          </p>
        </td>
      </tr>

      <!-- Pied -->
      <tr>
        <td style="padding:14px 32px;border-top:1px solid rgba(255,255,255,.06);text-align:center">
          <p style="margin:0;color:rgba(255,255,255,.18);font-size:10px">
            &copy; 2026 Konekta &nbsp;&middot;&nbsp;
            Message automatique &mdash; ne pas répondre.
          </p>
        </td>
      </tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""


def send_otp_email(nom: str, email: str, code: str) -> None:
    """
    Envoie le code OTP via Gmail SMTP (STARTTLS, port 587).
    Utilise un mot de passe d'application Gmail — jamais le mot de passe du compte.
    Ne logue jamais le code.
    """
    if not EMAIL_USER or not EMAIL_PASSWORD:
        raise RuntimeError(
            "Email non configuré. Définissez EMAIL_HOST_USER et "
            "EMAIL_HOST_PASSWORD dans backend/.env"
        )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Votre code Konekta : {code[0]}{'*' * 4}{code[-1]}"
    msg["From"]    = f"{EMAIL_FROM_NAME} <{EMAIL_USER}>"
    msg["To"]      = email
    msg["X-Priority"] = "1"

    html_body = _build_email_html(nom, code)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT, timeout=10) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(EMAIL_USER, EMAIL_PASSWORD)
            smtp.sendmail(EMAIL_USER, [email], msg.as_string())
        log.info("OTP email envoyé à %s", _mask_email(email))
    except smtplib.SMTPAuthenticationError:
        log.error("Échec authentification SMTP Gmail — vérifiez le mot de passe d'application")
        raise RuntimeError(
            "Impossible d'envoyer l'email de vérification. "
            "Contactez l'administrateur système."
        )
    except Exception as exc:
        log.error("Erreur envoi email OTP : %s", exc)
        raise RuntimeError(
            "L'email de vérification n'a pas pu être envoyé. Réessayez dans un moment."
        )


# ── Vérification ──────────────────────────────────────────────────────────────

def verify_otp(db: Session, pending_token: str, submitted_code: str) -> Utilisateur:
    """
    Vérifie le code soumis par l'utilisateur.

    Retourne l'objet Utilisateur si le code est correct.
    Lève ValueError avec un message lisible en cas d'échec.
    Ne logue jamais le code soumis.
    """
    if not pending_token:
        raise ValueError("Session OTP expirée — recommencez la connexion.")

    otp = (
        db.query(OTPCode)
        .filter(OTPCode.pending_token == pending_token)
        .first()
    )
    if not otp:
        raise ValueError("Session OTP invalide — recommencez la connexion.")

    now = datetime.now(timezone.utc)

    if otp.used:
        raise ValueError("Ce code a déjà été utilisé. Recommencez la connexion.")

    expires = otp.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < now:
        otp.used = True
        db.commit()
        raise ValueError("Code expiré — demandez un nouveau code.")

    if otp.attempts >= OTP_MAX_ATTEMPTS:
        otp.used = True
        db.commit()
        raise ValueError("Trop de tentatives — recommencez la connexion.")

    # Vérification HMAC-safe (même timing quelle que soit la longueur)
    import hmac as _hmac
    expected = _hash_code(submitted_code.strip())
    if not _hmac.compare_digest(expected, otp.code_hash):
        otp.attempts += 1
        remaining = OTP_MAX_ATTEMPTS - otp.attempts
        if remaining <= 0:
            otp.used = True
            db.commit()
            raise ValueError("Code incorrect — trop de tentatives. Recommencez la connexion.")
        db.commit()
        raise ValueError(
            f"Code incorrect. {remaining} tentative{'s' if remaining > 1 else ''} restante{'s' if remaining > 1 else ''}."
        )

    # Succès : invalider l'OTP immédiatement
    otp.used = True
    db.commit()

    user = db.get(Utilisateur, otp.user_id)
    if not user or not user.actif:
        raise ValueError("Compte introuvable ou désactivé.")

    return user


# ── Maintenance ───────────────────────────────────────────────────────────────

def cleanup_expired_otps(db: Session) -> int:
    """Supprime les OTP expirés ou déjà utilisés. Retourne le nombre de lignes supprimées."""
    cutoff = datetime.now(timezone.utc)
    deleted = (
        db.query(OTPCode)
        .filter((OTPCode.expires_at < cutoff) | (OTPCode.used.is_(True)))
        .delete(synchronize_session=False)
    )
    db.commit()
    return deleted
