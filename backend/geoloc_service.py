"""
Service de géolocalisation des connexions.

Calcule la distance entre la position GPS transmise par le navigateur à la
connexion et les coordonnées de l'institution (formule de Haversine), et
détecte deux anomalies de visibilité (jamais de blocage automatique) :

  CONNEXION_HORS_PERIMETRE — position obtenue, distance > rayon configuré
  GEOLOCALISATION_REFUSEE  — l'employé a refusé l'autorisation navigateur

Tant que l'admin n'a pas configuré de coordonnées d'institution, la
fonctionnalité est inerte : aucune anomalie CONNEXION_HORS_PERIMETRE n'est
émise. La géolocalisation par IP (fallback existant de login-meta) n'est
JAMAIS utilisée pour ce calcul — inutilisable pour les employés distants
(IP visible côté serveur = IP du tunnel Tailscale, pas la position réelle).
"""
from __future__ import annotations

import math
from datetime import date as date_type

from sqlalchemy import func
from sqlalchemy.orm import Session

from models import GeolocalisationConfig, LoginSecurityEvent, Utilisateur

RAYON_TERRE_KM = 6371.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance orthodromique (grand cercle) entre deux points GPS, en km."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmbda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmbda / 2) ** 2
    return RAYON_TERRE_KM * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def get_or_create_geoloc_config(db: Session) -> GeolocalisationConfig:
    """Retourne le singleton GeolocalisationConfig, le crée si absent."""
    cfg = db.query(GeolocalisationConfig).first()
    if not cfg:
        cfg = GeolocalisationConfig(
            institution_latitude=None, institution_longitude=None, rayon_alerte_km=1.0
        )
        db.add(cfg)
        db.commit()
        db.refresh(cfg)
    return cfg


def anomalies_geoloc(db: Session, date_cible: date_type) -> list[dict]:
    """Détecte les anomalies de géolocalisation à la connexion pour une date."""
    cfg = get_or_create_geoloc_config(db)

    events = (
        db.query(LoginSecurityEvent)
        .filter(func.date(LoginSecurityEvent.created_at) == date_cible)
        .all()
    )
    if not events:
        return []

    user_ids = {e.user_id for e in events}
    users = {
        u.id: u for u in db.query(Utilisateur).filter(Utilisateur.id.in_(user_ids)).all()
    }

    resultat: list[dict] = []
    for e in events:
        u = users.get(e.user_id)
        nom = u.nom_complet if u else f"Utilisateur #{e.user_id}"
        heure = e.created_at.isoformat() if e.created_at else None

        if e.statut_geoloc == "refuse":
            resultat.append({
                "type":            "GEOLOCALISATION_REFUSEE",
                "gravite":         "avertissement",
                "utilisateur_id":  e.user_id,
                "utilisateur_nom": nom,
                "date":            str(date_cible),
                "event_id":        e.id,
                "heure":           heure,
                "message": (
                    f"{nom} a refusé l'autorisation de géolocalisation lors de la "
                    f"connexion du {heure or date_cible}. Position non vérifiable."
                ),
            })
        elif (
            e.statut_geoloc == "ok"
            and e.distance_km is not None
            and cfg.institution_latitude is not None
            and cfg.institution_longitude is not None
            and e.distance_km > cfg.rayon_alerte_km
        ):
            resultat.append({
                "type":            "CONNEXION_HORS_PERIMETRE",
                "gravite":         "avertissement",
                "utilisateur_id":  e.user_id,
                "utilisateur_nom": nom,
                "date":            str(date_cible),
                "event_id":        e.id,
                "heure":           heure,
                "distance_km":     round(e.distance_km, 2),
                "rayon_km":        cfg.rayon_alerte_km,
                "latitude":        e.latitude,
                "longitude":       e.longitude,
                "message": (
                    f"{nom} s'est connecté à {round(e.distance_km, 2)} km de "
                    f"l'institution (seuil configuré : {cfg.rayon_alerte_km} km)."
                ),
            })
    return resultat
