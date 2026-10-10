#!/usr/bin/env bash
# Build "Reelay.app" and a drag-to-Applications .dmg.
# Usage (from anywhere):  ./packaging/build_mac.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PY=${PYTHON:-.venv/bin/python}
if [ ! -x "$PY" ]; then
  python3 -m venv .venv
  PY=.venv/bin/python
fi

"$PY" -m pip install -q -r requirements.txt -r requirements-dev.txt
"$PY" packaging/make_icons.py
"$PY" -m PyInstaller --noconfirm --clean packaging/Reelay.spec

APP="dist/Reelay.app"
VERSION=$("$PY" -c "import reelay; print(reelay.__version__)")
ARCH=$(uname -m)
DMG="dist/Reelay-${VERSION}-mac-${ARCH}.dmg"

# Sign with a stable identity when there is one, so macOS remembers "Allow"
# (folder access, Keychain) across rebuilds. An ad-hoc signature changes with
# every build, which makes macOS ask again each time.
#   REELAY_SIGN_IDENTITY=<name or SHA-1>  pick one explicitly ("-" = ad-hoc)
#   otherwise: the first "Apple Development" or "Developer ID Application"
#   identity in the keychain (free with an Apple ID in Xcode), else ad-hoc.
IDENTITY=${REELAY_SIGN_IDENTITY:-}
if [ -z "$IDENTITY" ]; then
  IDENTITY=$(security find-identity -v -p codesigning 2>/dev/null \
    | awk '/"(Developer ID Application|Apple Development):/ {print $2; exit}')
fi
IDENTITY=${IDENTITY:--}
if [ "$IDENTITY" = "-" ]; then
  echo "Signing: ad-hoc (macOS will ask for permissions again after each rebuild)"
else
  echo "Signing: $(security find-identity -v -p codesigning | awk -v id="$IDENTITY" '$2==id {$1="";$2=""; print}' | sed 's/^ *//')"
fi
codesign --force --deep --timestamp=none --sign "$IDENTITY" "$APP"
codesign --verify --deep --strict "$APP"

# Stage the .app next to an Applications shortcut, then compress into a .dmg.
STAGE=$(mktemp -d)
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
rm -f "$DMG"
hdiutil create -volname "Reelay" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
rm -rf "$STAGE"

echo "Built: $APP"
echo "Built: $DMG"
