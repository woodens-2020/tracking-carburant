#!/usr/bin/env bash
#
# promouvoir-bpccp.sh — met en production (app.konekta-bpccp.com) ce qui a
# été validé sur le staging (tracking-carburant-staging.up.railway.app).
#
#   bash scripts/promouvoir-bpccp.sh
#
# Refuse si :
#   - la vérification CI du dernier commit de staging-bpccp n'est pas verte ;
#   - la production contient des commits absents du staging (il faudrait
#     d'abord les ramener dans staging-bpccp) ;
#   - la personne ne confirme pas en tapant OUI.
# La promotion est toujours un fast-forward : la production reçoit exactement
# les commits testés sur le staging, rien d'autre.

set -euo pipefail

REPO="woodens-2020/tracking-carburant"
STAGING="staging-bpccp"
PROD="railway-migration"

git fetch -q origin "$STAGING" "$PROD"
SHA=$(git rev-parse "origin/$STAGING")
SHORT=${SHA:0:9}

if ! git merge-base --is-ancestor "origin/$PROD" "origin/$STAGING"; then
  echo "✗ La production a des commits absents du staging."
  echo "  Ramène-les d'abord :  git checkout $STAGING && git merge origin/$PROD && git push"
  exit 1
fi

NB=$(git rev-list --count "origin/$PROD..origin/$STAGING")
if [ "$NB" -eq 0 ]; then
  echo "Rien à promouvoir : la production est déjà au niveau du staging ($SHORT)."
  exit 0
fi

ETAT=$(gh api "repos/$REPO/commits/$SHA/check-runs" \
  --jq '[.check_runs[] | select(.name=="verifier")] | if length==0 then "absent" else (.[0].status + "/" + (.[0].conclusion // "")) end')
if [ "$ETAT" != "completed/success" ]; then
  echo "✗ La vérification CI de $SHORT n'est pas verte (état : $ETAT)."
  echo "  Voir : https://github.com/$REPO/actions"
  exit 1
fi

echo "Commits qui vont partir en production ($NB) :"
git log --oneline "origin/$PROD..origin/$STAGING"
echo
read -r -p "As-tu vérifié ces changements sur le staging ? Tape OUI pour mettre en production : " REP
[ "$REP" = "OUI" ] || { echo "Annulé."; exit 1; }

git push origin "$SHA:refs/heads/$PROD"
echo "✓ $SHORT envoyé sur $PROD — Railway déploie app.konekta-bpccp.com."
