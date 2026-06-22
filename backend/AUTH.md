# Authentification & Gestion des comptes — Konekta v2

## Mécanismes d'authentification

Le système accepte **deux mécanismes simultanément**, vérifiés par `AuthMiddleware` à chaque requête :

| Mécanisme | En-tête / Cookie | Porteur |
|---|---|---|
| Session cookie | `session_token` (httponly) | Utilisateur humain après `/api/login` ou OAuth |
| Clé API | `X-API-Key: knt_…` | Intégrations, scripts, employés |

Une clé maître `ADMIN_API_KEY` (variable d'env) court-circuite la base : elle charge directement l'administrateur et n'est jamais stockée en DB.

---

## Système de rôles

| Rôle | Valeur DB | Accès |
|---|---|---|
| Administrateur | `"admin"` | Tout, y compris la gestion des comptes |
| Opérateur | `"operateur"` | Métier uniquement (relevés, livraisons, audit…) |

La dépendance FastAPI `require_admin` lève **HTTP 403** si `request.state.user.role != "admin"`.

---

## Gestion des comptes employés

### Endpoints (tous sous `/api/auth/utilisateurs`, requis `require_admin`)

| Méthode | Chemin | Description |
|---|---|---|
| `POST` | `/api/auth/utilisateurs` | Crée un compte, retourne la clé API **une seule fois** |
| `GET` | `/api/auth/utilisateurs` | Liste tous les comptes (sans hashes) |
| `POST` | `/api/auth/utilisateurs/{id}/revoquer` | Désactive + révoque la clé |
| `POST` | `/api/auth/utilisateurs/{id}/reactiver` | Réactive le compte |
| `POST` | `/api/auth/utilisateurs/{id}/regenerer` | Nouvelle clé API, retourne **une seule fois** |
| `DELETE` | `/api/auth/utilisateurs/{id}` | Suppression définitive |

### Garde-fou anti-verrouillage

La révocation et la suppression vérifient `_count_active_admins(db) > 1` avant d'agir sur un admin. Si l'admin est le dernier actif, l'opération est rejetée avec **HTTP 409**.

### Création depuis l'interface

Dans `page-admin` (accessible après connexion admin), la carte **Gestion des employés** permet de créer, lister, révoquer, réactiver, régénérer et supprimer des comptes. La clé API est affichée dans un modal **une seule fois** — à communiquer à l'employé par voie sécurisée.

---

## Authentification Google OAuth 2.0

### Flux (Authorization Code Flow)

```
Navigateur → GET /api/auth/oauth/google/login
          ← 302 → accounts.google.com?state=<csrf>&…
          ← 302 → /api/auth/oauth/google/callback?code=…&state=…
          → vérifie state, échange code, récupère userinfo
          → cherche email en DB → crée session → 302 /
```

### Variables d'environnement

| Variable | Description |
|---|---|
| `GOOGLE_CLIENT_ID` | Client ID depuis Google Cloud Console |
| `GOOGLE_CLIENT_SECRET` | Secret depuis Google Cloud Console |
| `MICROSOFT_CLIENT_ID` | (optionnel) Azure App registration |
| `MICROSOFT_CLIENT_SECRET` | (optionnel) Azure secret |

### Configuration dans Google Cloud Console

1. Créer un projet → **APIs & Services → Credentials**
2. Type : **Web application**
3. URI de redirection autorisé : `https://votre-domaine.com/api/auth/oauth/google/callback`
4. Copier `client_id` et `client_secret` dans `.env`

### Politique de compte

**Aucune création automatique.** L'email retourné par Google doit exister en base (champ `utilisateurs.email`). Si inconnu → `account_not_found` (HTTP 302 vers `/login?oauth_error=account_not_found`).

Workflow pour activer OAuth sur un compte existant :
1. Créer le compte via la carte **Gestion des employés** avec le champ `email` renseigné
2. L'employé clique sur **Se connecter avec Google** sur la page de connexion

### Après la connexion OAuth

Le flux appelle `create_session()` → pose le cookie `session_token` (identique à la connexion par mot de passe). Le compte local est lié via `oauth_sub` (identifiant unique Google) pour les reconnexions suivantes.

---

## Risques et points d'attention

| Risque | Statut | Mitigation |
|---|---|---|
| Credentials par défaut (`admin`/`admin123`) | ⚠️ **À changer en production** | Changer via `POST /api/auth/change-password` |
| `backend/.env` contient `GEMINI_API_KEY` | ⚠️ **Ne jamais committer** | Ajouté à `.gitignore` |
| `ADMIN_API_KEY` en clair dans `.env` | ⚠️ Rotation recommandée | Utiliser un gestionnaire de secrets en prod |
| Tokens OAuth en mémoire (`_OAUTH_STATES`) | ℹ️ TTL 10 min, perdu au redémarrage | Acceptable pour un seul worker ; utiliser Redis en multi-worker |
| Pas de rate-limiting sur `/api/login` | ⚠️ Brute-force possible | Ajouter `slowapi` ou un reverse proxy limitant les tentatives |
| Sessions en DB sans nettoyage automatique | ℹ️ Sessions expirées restent en table | Ajouter une tâche cron `DELETE FROM sessions WHERE expires_at < now()` |
