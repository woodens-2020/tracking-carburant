# État des opérations infra — reprise de travail

> Document de passation, **sans secret** (dépôt public). Les identifiants
> (mots de passe d'application Gmail, mot de passe admin généré, token API
> Hostinger) sont détenus par l'exploitant — les redemander au besoin.
>
> Dernière mise à jour : 2026-09-03 (soir).

## Vérification pipeline — 2026-09-03

Contrôle de bout en bout (commit → push GitHub → auto-déploiement Railway) :

- `master` et `railway-migration` sur le même commit, synchro avec `origin`.
- `vertieres`, `victorious-creativity`, `carribean` : déploiement `SUCCESS`,
  service `Online`, **0 erreur SMTP `535`** dans les logs récents.
- carribean : `[bootstrap-admin] compte admin cree` confirmé dans les logs.
- konekta-crb.com + www : HTTPS `307 → /login`, certificats valides.

## Contexte

Konekta est déployé en **multi-tenant** : un projet Railway par institution.
Projets Railway (compte GitHub `woodens-2020`, org Railway `woodens-2020's Projects`) :

| Projet Railway | Service app | Domaine | Env |
|---|---|---|---|
| `vertieres` | `vertieres-app` | konekta-cpv.com | production |
| `victorious-creativity` | `tracking-carburant` | app.konekta-bpccp.com | staging + **production** |
| `carribean` | `carribean-app` | konekta-crb.com + www | production |
| `complexenativite` | ? | ? | ? |
| `affectionate-light` | ? | ? | ? |

Chaque projet a aussi un service `Postgres`.

## Branches Git

`master` et `railway-migration` ont été **réconciliées** : elles pointent sur
le **même commit** et doivent le rester. Tous les projets Railway se
redéploient automatiquement depuis GitHub sur push.

Deux copies de travail locales à garder synchro :
- `C:\Users\Pilla\tracking-carburant` → branche `master`
- `C:\Users\Pilla\Downloads\Tracking-Carburant\Tracking-Carburant` → branche `railway-migration`

Procédure : commit sur une copie → `git merge --ff-only` l'autre → push les deux.

## Changements de code déjà faits et déployés

1. **`backend/otp_service.py`**
   - Logs SMTP détaillés : code/réponse Gmail exacts (535 vs 534…), résumé
     non sensible de la config (`_smtp_diagnostics()`), distinction
     auth / serveur injoignable.
   - `EMAIL_HOST_PASSWORD` nettoyé (espaces du format `xxxx xxxx xxxx xxxx`,
     guillemets d'un copier-coller) avant `smtp.login()`.

2. **`backend/database.py`** — `_ensure_bootstrap_admin()`
   - Crée un compte admin depuis `BOOTSTRAP_ADMIN_EMAIL` / `_PASSWORD` /
     `_PIN` (+ `_USERNAME` / `_NAME`) au démarrage. Inerte si les variables
     ne sont pas définies ; idempotent.
   - **Bloc temporaire** — à retirer avec les variables une fois l'amorçage
     terminé partout où c'est utile.

## Diagnostic OTP (récurrent sur tous les déploiements)

Symptôme : « impossible d'envoyer le code » → HTTP 503.
Cause : `535 5.7.8 BadCredentials` — le **mot de passe d'application Gmail**
du compte expéditeur (partagé par tous les projets) avait été révoqué.
Correctif : régénérer le mot de passe d'application (2FA du compte Google
active) et mettre à jour `EMAIL_HOST_PASSWORD` sur chaque projet.

## État par déploiement

| Déploiement | OTP Gmail | Domaine | Compte admin |
|---|---|---|---|
| konekta-cpv.com (`vertieres`) | ✅ corrigé + confirmé (`/api/login` 200) | en place | ⏳ passer l'email admin en `complexevertieres@gmail.com` via **Admin → Utilisateurs** |
| konekta-bpccp.com (`victorious-creativity`) | ✅ corrigé (session active constatée) — confirmer par un login email | en place | ⏳ passer l'email admin en `whomilus@gmail.com` via l'UI |
| konekta-crb.com (`carribean`) | ✅ corrigé | ✅ apex + www en ligne, certs Let's Encrypt valides | ✅ admin `complexekonekta@gmail.com` / username `complexekonekta` créé via bootstrap (identifiants remis à l'exploitant) |
| `complexenativite` | ❌ pas encore traité | à faire | — |
| `affectionate-light` | ❌ pas encore traité | à faire | — |

## À FAIRE (reste)

- [ ] **Google OAuth** : ajouter dans Google Cloud Console (client
      `154413795531-gqe03qv37rll9hhp0caifu0oiisfob00.apps.googleusercontent.com`)
      les **URI de redirection autorisés** :
      - `https://konekta-crb.com/api/auth/oauth/google/callback`
      - `https://www.konekta-crb.com/api/auth/oauth/google/callback`
      et les **origines JS** `https://konekta-crb.com`, `https://www.konekta-crb.com`.
      Erreur observée tant que non fait : « Access blocked: This app's request is invalid ».
      Le `redirect_uri` est construit dynamiquement depuis le host appelé
      (`_oauth_callback_url`, `backend/main.py`).
- [ ] Après retour du `/callback`, on a aussi vu `oauth_error=invalid_state` :
      le `state` OAuth est en **mémoire process** (`_OAUTH_STATES`,
      `backend/main.py`) → cassé par tout redéploiement/redémarrage entre le
      clic et le retour. Si ça persiste hors redéploiement : rendre le
      `state` persistant (cookie signé).
- [ ] Changer l'email admin : `vertieres` → `complexevertieres@gmail.com`,
      `victorious-creativity` → `whomilus@gmail.com` (via l'UI Admin, après
      login OK).
- [ ] Appliquer le correctif OTP (`EMAIL_HOST_PASSWORD`) à `complexenativite`
      et `affectionate-light`.
- [ ] Une fois l'amorçage admin terminé : retirer `_ensure_bootstrap_admin()`
      de `backend/database.py` et les variables `BOOTSTRAP_ADMIN_*` restantes.
- [ ] carribean : la copie B (`railway-migration`) contenait aussi ce
      travail — vérifier qu'aucune divergence ne se recrée.

## Notes outillage (utile pour reprendre)

- `railway variables --set "K=V"` **sans** `--skip-deploys` → déclenche un
  redéploiement **propre** (builder Nixpacks). C'est la bonne méthode.
- `railway redeploy` (CLI) **échoue** : il passe par le builder « Railpack »
  qui ne sait pas construire ce repo. Ne pas l'utiliser ; préférer un
  changement de variable ou un commit vide poussé sur GitHub.
- `railway variable delete <KEY>` : pas de `--yes` ni `--skip-deploys` ;
  répondre `y`. Ne déclenche pas de redéploiement.
- Depuis Claude Code, le garde-fou bloque : `railway run`, `railway connect`,
  `railway variables --kv` (lecture brute de secrets), et les requêtes
  **DELETE** vers l'API Hostinger. Les `railway variables --set`,
  `railway domain`, et les `PUT`/`GET` Hostinger passent.
- API DNS Hostinger : `GET|PUT|DELETE https://developers.hostinger.com/api/dns/v1/zones/<domaine>`,
  header `Authorization: Bearer <token>`. Le PUT prend
  `{"overwrite":true,"zone":[{"name","type","ttl","records":[{"content"}]}]}`.
  CNAME sur `@` → Hostinger le convertit en `ALIAS` mais **refuse** s'il
  reste un `A` sur `@` (à supprimer d'abord).
