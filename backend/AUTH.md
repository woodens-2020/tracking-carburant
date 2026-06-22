# Authentification — Konekta

Deux mécanismes coexistent. Les deux donnent accès aux mêmes routes protégées.

---

## 1. Session cookie (navigateur web)

Flux standard pour l'interface web.

| Étape | Détail |
|-------|--------|
| Login | `POST /api/login` — corps JSON `{username, password}` |
| Cookie | `session_token` httponly, durée 7 jours |
| Logout | `POST /api/logout` — supprime le cookie et la session en base |

Le cookie est envoyé automatiquement par le navigateur sur chaque requête same-origin.

---

## 2. Clé API (header X-API-Key)

Pour les scripts, CI/CD, ou appels programmatiques.

### Obtenir une clé

La clé est générée automatiquement à chaque connexion (`POST /api/login`) et retournée dans le corps de la réponse :

```json
{
  "id": 1,
  "username": "admin",
  "nom_complet": "Administrateur",
  "role": "admin",
  "api_key": "knt_AbCdEf..."
}
```

**Important :** la valeur brute n'est visible qu'une seule fois. Seul son hash SHA-256 est stocké en base.

### Utiliser la clé

```bash
curl -H "X-API-Key: knt_AbCdEf..." https://votre-domaine.com/api/produits
```

### Renouveler la clé (rotation)

```bash
curl -X POST -H "X-API-Key: knt_AbCdEf..." https://votre-domaine.com/api/auth/api-key
# Retourne {"api_key": "knt_NewKey..."}
```

L'ancienne clé est immédiatement invalidée.

### Révoquer la clé

```bash
curl -X DELETE -H "X-API-Key: knt_AbCdEf..." https://votre-domaine.com/api/auth/api-key
# Retourne {"ok": true}
```

---

## 3. Clé admin statique (.env)

Une clé de secours pour les scripts de déploiement / CI peut être définie dans `.env` :

```
ADMIN_API_KEY=votre-cle-secrete-longue
```

Cette clé agit comme l'utilisateur `admin`. Elle ne tourne pas et n'est pas stockée en base — à n'utiliser que pour les accès systèmes (pipelines, migrations).

---

## Endpoints /api/auth/*

| Méthode | Route | Description | Auth requise |
|---------|-------|-------------|-------------|
| `POST` | `/api/login` | Connexion — retourne cookie + clé API | Non |
| `POST` | `/api/logout` | Déconnexion — supprime la session | Oui |
| `GET` | `/api/me` | Infos utilisateur courant | Oui |
| `GET` | `/api/auth/me` | Idem + champ `has_api_key` | Oui |
| `POST` | `/api/auth/api-key` | Rotation de la clé API | Oui |
| `DELETE` | `/api/auth/api-key` | Révocation de la clé API | Oui |

---

## Sécurité

- Mots de passe : PBKDF2-HMAC-SHA256, 200 000 itérations, sel aléatoire par utilisateur.
- Clés API : SHA-256 (pas de sel — la clé brute est déjà un secret à haute entropie de 256 bits).
- Clé admin statique : comparaison timing-safe via `hmac.compare_digest`.
- Cookies : `httponly`, `samesite=lax` — inaccessibles depuis JavaScript.
- Les clés API sont stockées dans `sessionStorage` du navigateur (effacé à la fermeture de l'onglet).

---

## Lancer les tests

```bash
cd backend
pytest tests/test_auth.py -v
```
