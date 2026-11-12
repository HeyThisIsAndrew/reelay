#!/usr/bin/env bash
# One command: build Reelay.app from this checkout and install it to /Applications.
#
#   ./scripts/build_local.sh            build, back up the database, install, verify
#   ./scripts/build_local.sh --open     ...then open the installed app
#   ./scripts/build_local.sh --no-dmg   skip the .dmg (faster; the .app is all /Applications needs)
#
# Works from a clean clone of main: it makes .venv if there isn't one (Python 3.11 or newer), installs
# the build requirements into it, and runs packaging/build_mac.sh (PyInstaller, icons, signing).
# Signing: the first "Developer ID Application" or "Apple Development" identity in the keychain,
# else ad-hoc. Ad-hoc is fine for this Mac (macOS asks for folder and Keychain access again after
# each rebuild). REELAY_SIGN_IDENTITY overrides; see docs/release-signing.md.
#
# The installed app needs none of this folder: no terminal, no .venv, no run_app.py.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)

OPEN_AFTER=0
NO_DMG=0
for arg in "$@"; do
  case "$arg" in
    --open) OPEN_AFTER=1 ;;
    --no-dmg) NO_DMG=1 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

if [ "$(uname -s)" != "Darwin" ]; then
  echo "build_local.sh builds the macOS app; on Windows use packaging/build_windows.ps1." >&2
  exit 1
fi

APP_SRC="$ROOT/dist/Reelay.app"
APP_DST="/Applications/Reelay.app"
DATA_DIR="$HOME/Library/Application Support/Reelay"

# ---- 1. a Python for the build (the built app bundles its own; this one is only for PyInstaller)
pick_python() {
  for candidate in "${PYTHON:-}" python3.13 python3.12 python3.11 python3; do
    [ -n "$candidate" ] || continue
    if command -v "$candidate" >/dev/null 2>&1 && \
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      command -v "$candidate"; return 0
    fi
  done
  return 1
}
if [ ! -x .venv/bin/python ]; then
  BASE_PY=$(pick_python) || { echo "Python 3.11 or newer is needed to build (brew install python@3.12)." >&2; exit 1; }
  echo "==> Making .venv with $BASE_PY"
  "$BASE_PY" -m venv .venv
fi
export PYTHON="$ROOT/.venv/bin/python"
"$PYTHON" -m pip install -q --upgrade pip

VERSION=$("$PYTHON" -c "import reelay; print(reelay.__version__)")
COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)
echo "==> Building Reelay $VERSION ($COMMIT)"

# ---- 2. build and sign (packaging/build_mac.sh installs requirements, runs PyInstaller, signs, makes the .dmg)
if [ "$NO_DMG" = 1 ]; then
  REELAY_SKIP_DMG=1 ./packaging/build_mac.sh
else
  ./packaging/build_mac.sh
fi
[ -d "$APP_SRC" ] || { echo "Build finished without $APP_SRC" >&2; exit 1; }

# ---- 3. check the fresh build before touching /Applications
echo "==> Self-test of the new build"
REELAY_HOME=$(mktemp -d) "$APP_SRC/Contents/MacOS/Reelay" --self-test

# ---- 4. quit a running Reelay so the bundle isn't replaced under it
if pgrep -f "/Applications/Reelay.app/Contents/MacOS/Reelay" >/dev/null 2>&1; then
  echo "==> Quitting the running Reelay"
  osascript -e 'tell application "Reelay" to quit' >/dev/null 2>&1 || true
  for _ in $(seq 1 20); do
    pgrep -f "/Applications/Reelay.app/Contents/MacOS/Reelay" >/dev/null 2>&1 || break
    sleep 0.5
  done
  if pgrep -f "/Applications/Reelay.app/Contents/MacOS/Reelay" >/dev/null 2>&1; then
    echo "Reelay is still running (an upload or a dialog may be open). Quit it, then run this again." >&2
    exit 1
  fi
fi

# ---- 5. back up the database (the new version may migrate it on first launch)
DB="$DATA_DIR/reelay.db"
if [ -f "$DB" ]; then
  BACKUP="$DATA_DIR/reelay-backup-$(date +%Y-%m-%d-%H%M%S)-$COMMIT.db"
  echo "==> Backing up the database to $(basename "$BACKUP")"
  "$PYTHON" -I - "$DB" "$BACKUP" <<'PY'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)          # consistent copy, including anything still in the WAL
dst.close(); src.close()
PY
  chmod 600 "$BACKUP"
fi

# ---- 6. install
echo "==> Installing to $APP_DST"
STAGE="/Applications/.Reelay-installing.app"
rm -rf "$STAGE"
ditto "$APP_SRC" "$STAGE"
rm -rf "$APP_DST"
mv "$STAGE" "$APP_DST"
codesign --verify --deep --strict "$APP_DST"

# ---- 7. verify the installed copy
echo "==> Self-test of the installed app"
REELAY_HOME=$(mktemp -d) "$APP_DST/Contents/MacOS/Reelay" --self-test
INSTALLED=$(defaults read "$APP_DST/Contents/Info.plist" CFBundleShortVersionString)
echo "Installed: $APP_DST (version $INSTALLED, commit $COMMIT)"

if [ "$OPEN_AFTER" = 1 ]; then
  open "$APP_DST"
fi
