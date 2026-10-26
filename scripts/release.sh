#!/usr/bin/env bash
# Build an installable release archive: the source at HEAD plus the built
# frontend (dist/), so an install needs Python only — no Node.
#
#   npm run release            -> release/web_canvas-<version>.tar.gz (+ .sha256)
#
# The build runs in a clean export of the committed tree with an emptied
# environment. That matters: Vite bakes every VITE_* value it can see — the
# .env / .env.local files and the process environment — into the bundle, and
# those hold provider API keys. The finished bundle is scanned for key-shaped
# strings and the release is refused if one is found.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
VERSION="$(node -p "require('./package.json').version")"
NAME="web_canvas-${VERSION}"
OUT="$ROOT/release"

if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "release: commit or stash your changes first (the archive is built from HEAD)." >&2
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
git archive --format=tar --prefix="${NAME}/" HEAD | tar -x -C "$WORK"
cd "$WORK/$NAME"

# No env files, and no VITE_* (or anything else) from this shell.
rm -f .env .env.*
env -i PATH="$PATH" HOME="$HOME" npm ci --no-audit --no-fund
env -i PATH="$PATH" HOME="$HOME" npm run build

if grep -rEl 'sk-[A-Za-z0-9_-]{20,}|sk-ant-[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{30,}|xai-[A-Za-z0-9]{20,}' dist >/dev/null; then
  echo "release: the built bundle contains something shaped like an API key; refusing." >&2
  exit 1
fi

rm -rf node_modules
mkdir -p "$OUT"
tar -C "$WORK" -czf "$OUT/${NAME}.tar.gz" "$NAME"
(cd "$OUT" && sha256sum "${NAME}.tar.gz" > "${NAME}.tar.gz.sha256")
echo "release: $OUT/${NAME}.tar.gz"
cat "$OUT/${NAME}.tar.gz.sha256"
