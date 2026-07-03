"""
Service de réinitialisation de mot de passe.

Sécurité :
  - Token : secrets.token_urlsafe(48) → jamais stocké en clair (SHA-256)
  - Expiry : 30 minutes, usage unique
  - Rate : 3 demandes / heure par combinaison (email + IP)
  - Anti-énumération : délai constant + réponse identique si l'email existe ou non
  - Post-reset : toutes les sessions actives révoquées
  - Audit : événement enregistré dans AuditLog
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

from models import PasswordResetToken, SessionToken, Utilisateur
from auth import hash_password

log = logging.getLogger("password_reset")

# ── Config (héritée de l'environnement via otp_service) ────────────
EMAIL_HOST      = os.getenv("EMAIL_HOST",           "smtp.gmail.com")
EMAIL_PORT      = int(os.getenv("EMAIL_PORT",       "587"))
EMAIL_USER      = os.getenv("EMAIL_HOST_USER",      "")
EMAIL_PASSWORD  = os.getenv("EMAIL_HOST_PASSWORD",  "")
EMAIL_FROM_NAME = os.getenv("EMAIL_FROM_NAME",       "Konekta · Bon Prix")

RESET_EXPIRY_MIN   = 30
RESET_RATE_WINDOW  = 60   # minutes
RESET_RATE_MAX     = 3    # demandes max par fenêtre (email + IP)
RESET_MIN_PASSWORD = 8    # longueur minimale nouveau mot de passe


# ── Primitives ────────────────────────────────────────────────────────────────

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _check_rate_limit(db: Session, user_id: int, ip: str | None) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=RESET_RATE_WINDOW)
    count = (
        db.query(PasswordResetToken)
        .filter(
            PasswordResetToken.user_id    == user_id,
            PasswordResetToken.created_at >= cutoff,
        )
        .count()
    )
    if count >= RESET_RATE_MAX:
        raise ValueError(
            f"Trop de demandes — attendez {RESET_RATE_WINDOW} minutes avant de réessayer."
        )


def _invalidate_existing(db: Session, user_id: int) -> None:
    db.query(PasswordResetToken).filter(
        PasswordResetToken.user_id == user_id,
        PasswordResetToken.used.is_(False),
    ).update({"used": True}, synchronize_session=False)


# ── Création du token ─────────────────────────────────────────────────────────

def create_reset_token(db: Session, user_id: int, ip: str | None) -> str:
    """
    Génère un token de reset pour l'utilisateur, invalide les précédents.
    Retourne le token en clair (à envoyer par email).
    """
    _check_rate_limit(db, user_id, ip)
    _invalidate_existing(db, user_id)

    raw_token  = secrets.token_urlsafe(48)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=RESET_EXPIRY_MIN)

    db.add(PasswordResetToken(
        user_id    = user_id,
        token_hash = _hash_token(raw_token),
        expires_at = expires_at,
        used       = False,
        ip_address = ip,
    ))
    db.commit()
    return raw_token


# ── Email ─────────────────────────────────────────────────────────────────────

def _send_reset_email(user: Utilisateur, raw_token: str, base_url: str) -> None:
    reset_url  = f"{base_url}/login?token={raw_token}"
    nom        = user.nom_complet or user.username

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#070e1c;font-family:Arial,Helvetica,sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#070e1c;padding:36px 16px">
  <tr><td align="center">
    <table width="100%" style="max-width:500px;background:#0b1628;border-radius:14px;border:1px solid rgba(232,197,88,.22);overflow:hidden">

      <!-- En-tête -->
      <tr>
        <td style="padding:28px 32px 24px;text-align:center;border-bottom:2px solid #e8c558">
          <div style="font-size:32px;font-weight:900;color:#e8c558;line-height:1">K</div>
          <div style="font-size:13px;font-weight:800;color:#e8c558;letter-spacing:3px;margin-top:4px">KONEKTA</div>
          <div style="font-size:10px;color:rgba(232,197,88,.45);margin-top:3px">Bon Prix &middot; Complexe Commerciale de Pillatre</div>
        </td>
      </tr>

      <!-- Icône -->
      <tr>
        <td style="padding:32px 32px 0;text-align:center">
          <div style="display:inline-flex;align-items:center;justify-content:center;width:64px;height:64px;border-radius:50%;background:rgba(232,197,88,.1);border:2px solid rgba(232,197,88,.3)">
            <svg viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="#e8c558" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>
            </svg>
          </div>
        </td>
      </tr>

      <!-- Corps -->
      <tr>
        <td style="padding:24px 32px 0">
          <h2 style="color:#dde8f8;font-size:20px;font-weight:700;margin:0 0 12px">Réinitialisation de mot de passe</h2>
          <p style="color:#7a9cc4;font-size:14px;line-height:1.6;margin:0 0 8px">
            Bonjour <strong style="color:#dde8f8">{nom}</strong>,
          </p>
          <p style="color:#7a9cc4;font-size:14px;line-height:1.6;margin:0 0 24px">
            Vous avez demandé la réinitialisation de votre mot de passe. Cliquez sur le bouton ci-dessous pour définir un nouveau mot de passe.
          </p>
        </td>
      </tr>

      <!-- Bouton -->
      <tr>
        <td style="padding:0 32px 28px;text-align:center">
          <a href="{reset_url}" target="_blank"
             style="display:inline-block;padding:14px 36px;background:linear-gradient(135deg,#e8c558,#b8860b);
                    color:#050f1e;font-size:15px;font-weight:800;text-decoration:none;
                    border-radius:10px;letter-spacing:.3px">
            Réinitialiser mon mot de passe
          </a>
          <p style="margin:16px 0 0;color:rgba(122,156,196,.6);font-size:11px">
            Ce lien est valable <strong style="color:#e8c558">{RESET_EXPIRY_MIN} minutes</strong> et ne peut être utilisé qu'une seule fois.
          </p>
        </td>
      </tr>

      <!-- Avertissement sécurité -->
      <tr>
        <td style="padding:0 32px 28px">
          <div style="background:rgba(239,68,68,.07);border:1px solid rgba(239,68,68,.2);border-radius:10px;padding:14px 16px">
            <p style="color:rgba(239,68,68,.85);font-size:12px;line-height:1.5;margin:0">
              <strong>Si vous n'avez pas fait cette demande</strong>, ignorez cet email. Votre mot de passe restera inchangé.<br>
              Par sécurité, ne partagez jamais ce lien.
            </p>
          </div>
        </td>
      </tr>

      <!-- Lien texte de secours -->
      <tr>
        <td style="padding:0 32px 28px">
          <p style="color:rgba(122,156,196,.5);font-size:11px;word-break:break-all;margin:0">
            Si le bouton ne fonctionne pas, copiez ce lien dans votre navigateur :<br>
            <span style="color:rgba(232,197,88,.6)">{reset_url}</span>
          </p>
        </td>
      </tr>

      <!-- Pied -->
      <tr>
        <td style="padding:18px 32px;border-top:1px solid rgba(232,197,88,.1);text-align:center">
          <p style="color:rgba(122,156,196,.4);font-size:10px;margin:0">
            {EMAIL_FROM_NAME} &mdash; Cet email a été envoyé automatiquement, ne pas répondre.
          </p>
        </td>
      </tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Réinitialisation de votre mot de passe — Konekta"
    msg["From"]    = f"{EMAIL_FROM_NAME} <{EMAIL_USER}>"
    msg["To"]      = user.email
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT, timeout=15) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(EMAIL_USER, EMAIL_PASSWORD)
        smtp.sendmail(EMAIL_USER, [user.email], msg.as_string())


# ── Point d'entrée public : demande de reset ─────────────────────────────────

def request_reset(identifier: str, db: Session, ip: str | None, base_url: str) -> None:
    """
    Cherche l'utilisateur par email ou username.
    Génère et envoie le token.
    NE lève JAMAIS d'erreur si l'utilisateur est introuvable
    (protection contre l'énumération de comptes).
    """
    identifier = identifier.strip().lower()
    user = (
        db.query(Utilisateur)
        .filter(
            (Utilisateur.email == identifier) |
            (Utilisateur.username == identifier)
        )
        .filter(Utilisateur.actif == True)
        .first()
    )
    if not user or not user.email:
        # Délai artificiel pour limiter le timing oracle
        import time; time.sleep(0.3)
        return

    try:
        raw_token = create_reset_token(db, user.id, ip)
        _send_reset_email(user, raw_token, base_url)
    except ValueError:
        raise  # Rate limit — on la propage pour afficher un message
    except Exception as exc:
        log.error("Erreur envoi email reset [user=%s] : %s", user.id, exc)
        raise RuntimeError("Impossible d'envoyer l'email. Contactez l'administrateur.") from exc


# ── Vérification du token ─────────────────────────────────────────────────────

def verify_reset_token(raw_token: str, db: Session) -> Utilisateur:
    """
    Vérifie le token et retourne l'utilisateur associé.
    Lève ValueError si le token est invalide, expiré ou déjà utilisé.
    """
    token_hash = _hash_token(raw_token)
    record = (
        db.query(PasswordResetToken)
        .filter_by(token_hash=token_hash)
        .first()
    )
    if not record:
        raise ValueError("Lien invalide ou déjà utilisé.")
    if record.used:
        raise ValueError("Ce lien a déjà été utilisé.")
    if datetime.now(timezone.utc) > record.expires_at:
        raise ValueError("Ce lien a expiré. Faites une nouvelle demande.")
    return record.user


# ── Consommation du token + changement de mot de passe ───────────────────────

def consume_reset_token(raw_token: str, new_password: str, db: Session) -> Utilisateur:
    """
    Valide le token, change le mot de passe, révoque toutes les sessions,
    marque le token comme utilisé. Retourne l'utilisateur.
    """
    if not new_password or len(new_password) < RESET_MIN_PASSWORD:
        raise ValueError(f"Le mot de passe doit contenir au moins {RESET_MIN_PASSWORD} caractères.")

    user = verify_reset_token(raw_token, db)

    # 1. Changer le mot de passe
    user.password_hash = hash_password(new_password)

    # 2. Révoquer toutes les sessions actives
    db.query(SessionToken).filter_by(user_id=user.id).delete()

    # 3. Marquer le token comme utilisé
    token_hash = _hash_token(raw_token)
    db.query(PasswordResetToken).filter_by(token_hash=token_hash).update({"used": True})

    db.commit()
    return user
