"""
Garde-fou d'isolation de module (GAZ / DEPO).

Voir Utilisateur.espace_autorise, OTPCode.target_module et SessionToken.scope
(models.py). Module séparé de main.py pour être importable par les routeurs
(pos_routes.py, bar_caisses_routes.py) sans import circulaire — même
contrainte que require_admin, dupliqué dans ces fichiers pour la même
raison (voir leurs commentaires « dupliquée ici pour éviter un import
circulaire »).

RÈGLE CENTRALE : un scope de session NULL — compte non concerné par ce
cloisonnement (espace_autorise NULL), admin/pdg (jamais restreints par
cette fonctionnalité), ou session ouverte avant son déploiement — garde
l'accès complet. Ces dépendances bloquent UNIQUEMENT les sessions dont le
scope est explicitement l'espace opposé.
"""
from fastapi import Request, HTTPException


def require_gaz_scope(request: Request) -> None:
    """À ajouter en dépendance sur les routes du module Carburant (GAZ)."""
    user  = getattr(request.state, "user", None)
    scope = getattr(user, "scope", None) if user else None
    if scope == "DEPO":
        raise HTTPException(403, "Cette session est réservée au module Dépôt — "
                                  "reconnectez-vous en choisissant Carburant pour accéder à cette page.")


def require_depot_scope(request: Request) -> None:
    """À ajouter en dépendance sur les routes du module Dépôt (DEPO)."""
    user  = getattr(request.state, "user", None)
    scope = getattr(user, "scope", None) if user else None
    if scope == "GAZ":
        raise HTTPException(403, "Cette session est réservée au module Carburant — "
                                  "reconnectez-vous en choisissant Dépôt pour accéder à cette page.")
