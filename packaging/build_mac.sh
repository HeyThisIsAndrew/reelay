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

# Ad-hoc sign the whole bundle so Apple Silicon will launch it.
codesign --force --deep --sign - "$APP"

# Stage the .app next to an Applications shortcut, then compress into a .dmg.
STAGE=$(mktemp -d)
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
rm -f "$DMG"
hdiutil create -volname "Reelay" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
rm -rf "$STAGE"

echo "Built: $APP"
echo "Built: $DMG"
