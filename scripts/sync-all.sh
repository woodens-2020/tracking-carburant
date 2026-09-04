#!/usr/bin/env bash
#
# sync-all.sh — pousse le commit courant sur les DEUX branches distantes
# (`master` et `railway-migration`, qui doivent rester identiques) et met à
# jour la copie de travail jumelle si elle est présente sur la machine.
#
# Appelé automatiquement par le hook scripts/git-hooks/post-commit dès qu'un
# commit est fait (Claude, IDE, ligne de commande — n'importe quelle session).
# Peut aussi être lancé à la main : bash scripts/sync-all.sh
#
# Idempotent. Ne crée jamais de commit. Fast-forward uniquement : si les deux
# branches distantes ont divergé, le push est refusé (protection voulue).

set -uo pipefail

ROOT=$(git rev-parse --show-toplevel) || exit 0
cd "$ROOT" || exit 0

HEAD_SHA=$(git rev-parse HEAD)
SHORT=${HEAD_SHA:0:9}

# Ne rien faire pendant un rebase / merge / cherry-pick en cours.
if [ -d "$ROOT/.git/rebase-merge" ] || [ -d "$ROOT/.git/rebase-apply" ] \
   || [ -f "$ROOT/.git/MERGE_HEAD" ] || [ -f "$ROOT/.git/CHERRY_PICK_HEAD" ]; then
  echo "[sync] opération git en cours — synchro reportée"
  exit 0
fi

# 1) Pousser HEAD sur master ET railway-migration (fast-forward only).
if git push origin "HEAD:refs/heads/master" "HEAD:refs/heads/railway-migration" 2>&1; then
  echo "[sync] origin master & railway-migration -> $SHORT"
else
  echo "[sync] ÉCHEC du push (réseau / non-fast-forward / auth) — à pousser manuellement"
fi

# 2) Mettre à jour la copie de travail jumelle si elle existe.
for SIB in \
  "$HOME/tracking-carburant" \
  "$HOME/Downloads/Tracking-Carburant/Tracking-Carburant"
do
  [ -d "$SIB/.git" ] || continue
  SIB_ROOT=$(cd "$SIB" 2>/dev/null && git rev-parse --show-toplevel 2>/dev/null) || continue
  [ "$SIB_ROOT" = "$ROOT" ] && continue   # c'est nous

  BR=$(cd "$SIB_ROOT" && git rev-parse --abbrev-ref HEAD)
  if ( cd "$SIB_ROOT" \
       && [ -z "$(git status --porcelain)" ] \
       && git fetch origin --quiet \
       && git merge --ff-only "origin/$BR" >/dev/null 2>&1 ); then
    echo "[sync] copie jumelle $SIB_ROOT ($BR) -> $SHORT"
  else
    echo "[sync] ATTENTION : $SIB_ROOT non synchronisée (changements locaux ou branche divergente)"
  fi
done

echo "[sync] terminé @ $SHORT"
