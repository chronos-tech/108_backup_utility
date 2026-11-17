#!/usr/bin/env bash
# Commit data/ and cache/ changes and push without ever force-pushing.
# If the branch moved, rebase onto it; on any conflict, fail loudly instead.
set -euo pipefail

MESSAGE="${1:-chore: update data}"
BRANCH="${GITHUB_REF_NAME:-$(git rev-parse --abbrev-ref HEAD)}"

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

git add -A -- data cache
if git diff --cached --quiet; then
  echo "No changes to commit."
  exit 0
fi
git commit -q -m "$MESSAGE"

for attempt in 1 2 3 4 5; do
  if git push origin "HEAD:${BRANCH}"; then
    echo "Pushed."
    exit 0
  fi
  if [ "${COMMIT_BEST_EFFORT:-0}" = "1" ]; then
    # Mid-run checkpoint: the working tree may still be changing, so don't rebase here.
    # The commit stays local and the final step pushes it (rebasing if needed).
    echo "Checkpoint push rejected; the final step will retry."
    exit 0
  fi
  echo "Push rejected (attempt ${attempt}); rebasing onto origin/${BRANCH}."
  if ! git pull --rebase origin "${BRANCH}"; then
    git rebase --abort || true
    echo "ERROR: rebase conflict. Resolve manually; nothing was force-pushed." >&2
    exit 1
  fi
  sleep $((attempt * 3))
done
echo "ERROR: could not push after 5 attempts." >&2
exit 1
