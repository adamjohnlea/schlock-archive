#!/bin/bash
# Build the static site from schlock.db and publish it to the gh-pages branch.
#
# The build runs locally because schlock.db (your archive + TMDB key) is private and
# gitignored, so GitHub can't build it for you. This script builds, then force-pushes
# the generated build/ folder to the gh-pages branch of your remote.
#
# One-time setup is in DEPLOY.md. Then just run:  ./deploy.sh         (hotlinked images)
#                                        or:       ./deploy.sh --images  (self-hosted images)
set -euo pipefail
cd "$(dirname "$0")"

# ---- configure (or override with env vars) -------------------------------
BASE_URL="${BASE_URL:-https://adamjohnlea.github.io/schlock-archive}"
REMOTE="${REMOTE:-origin}"
BRANCH="${BRANCH:-gh-pages}"
# --------------------------------------------------------------------------

if [[ "$BASE_URL" == *USERNAME* ]]; then
  echo "Set BASE_URL first: edit it at the top of deploy.sh, or run" >&2
  echo "  BASE_URL=https://you.github.io/your-repo ./deploy.sh" >&2
  exit 1
fi

REMOTE_URL="$(git remote get-url "$REMOTE" 2>/dev/null || true)"
if [ -z "$REMOTE_URL" ]; then
  echo "No git remote '$REMOTE'. See DEPLOY.md, step 2:" >&2
  echo "  git remote add origin https://github.com/you/your-repo.git" >&2
  exit 1
fi

IMAGES=()
[ "${1:-}" = "--images" ] && IMAGES=(--images)

echo "==> Building static site  (base: $BASE_URL)"
rm -rf build
python3 app.py --export build --base-url "$BASE_URL" "${IMAGES[@]}"
test -f build/index.html || { echo "Build produced no index.html; aborting." >&2; exit 1; }

echo "==> Publishing build/ to $REMOTE/$BRANCH"
rm -rf build/.git
git -C build init -q
git -C build checkout -q -b "$BRANCH"
git -C build add -A
git -C build commit -q -m "Deploy $(date -u +%Y-%m-%dT%H:%M:%SZ)"
git -C build push -f "$REMOTE_URL" "$BRANCH"
rm -rf build/.git

echo "==> Done. Once Pages rebuilds (a minute or two) it will be live at:"
echo "    $BASE_URL/"
