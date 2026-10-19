# Staging bpccp — circuit de mise en production

Tout changement destiné à **app.konekta-bpccp.com** passe d'abord par le
staging. Rien ne va en production sans avoir été vérifié automatiquement
puis à la main.

| | Staging | Production |
|---|---|---|
| Adresse | https://tracking-carburant-staging.up.railway.app | https://app.konekta-bpccp.com |
| Branche Git | `staging-bpccp` | `railway-migration` |
| Railway | projet *victorious-creativity*, env. `staging` | même projet, env. `production` |
| Base | Postgres du staging (séparée) | Postgres de production |
| Bandeau | « STAGING · environnement de test » | aucun |

## Le circuit

1. **Développer** sur `staging-bpccp` (jamais directement sur `railway-migration`).
2. **Pousser** : `git push origin staging-bpccp`.
3. **Vérification automatique** (GitHub Actions, `verifier-bpccp.yml`) :
   syntaxe Python, démarrage de l'app sur une base vierge, routes principales
   sans erreur 500, cycle cargaison → relevé → stock cohérent, syntaxe
   JavaScript de l'interface. Railway attend ce résultat avant de déployer.
4. **Vérifier à la main** sur l'adresse du staging.
5. **Mettre en production** : `bash scripts/promouvoir-bpccp.sh`.
   Le script refuse si la CI n'est pas verte ou si la production a divergé,
   affiche la liste des commits et demande de taper `OUI`.

## Sécurités propres au staging

- `APP_ENV=staging` : bandeau orange visible sur toutes les pages.
- `OTP_ENABLED=false` : connexion sans code.
- Pas d'email (`EMAIL_HOST_PASSWORD` vide) ni de WhatsApp (`TWILIO_AUTH_TOKEN`
  vide) : le staging ne peut pas écrire aux vrais employés.
- `ADMIN_API_KEY` différente de celle de la production.
- Base séparée : une erreur sur le staging ne touche jamais les vraies données.

## Rafraîchir les données du staging

La base du staging est une copie de la production à une date donnée. Pour la
remettre à jour, il faut une copie `pg_dump` (PostgreSQL 18) de la production
restaurée dans le Postgres du staging. Cette opération écrase entièrement les
données du staging.

## Si la production a reçu un correctif direct

Ramener le correctif dans le staging avant toute nouvelle promotion :

```
git checkout staging-bpccp
git merge origin/railway-migration
git push origin staging-bpccp
```
