#!/usr/bin/env bash
# Build "Reelay.app" and a drag-to-Applications .dmg.
# Usage (from anywhere):  ./packaging/build_mac.sh
set -euo pipefail
cd "$(dirname "$0")/.."

# ---- signing preflight: before anything is built, so a wrong setup fails in seconds
# Sign with a stable identity when there is one, so macOS remembers "Allow"
# (folder access, Keychain) across rebuilds. An ad-hoc signature changes with
# every build, which makes macOS ask again each time.
#   REELAY_SIGN_IDENTITY=<name or SHA-1>  pick one explicitly ("-" = ad-hoc)
#   otherwise: the first "Developer ID Application" or "Apple Development"
#   identity in the keychain (free with an Apple ID in Xcode), else ad-hoc.
#
# Real identities get the hardened runtime (required for notarization). Reelay
# needs NO entitlements under it: verified by signing the packaged app with
# --options runtime and no entitlements, then running --self-test (yt-dlp,
# TikTok impersonation via curl_cffi, bundled ffmpeg, keychain, SQLite FTS5,
# Qt image plugins, the QtMultimedia/FFmpeg player, the API server) and
# launching the window. See docs/release-signing.md before adding any.
# Ad-hoc signatures don't get it: without a Team ID, library validation would
# refuse the bundled libraries.
#
# Notarization (needs a "Developer ID Application" identity and an Apple
# Developer Program membership), off unless asked for:
#   REELAY_NOTARY_PROFILE=<keychain profile>  from:
#     xcrun notarytool store-credentials <profile> --apple-id … --team-id … --password <app-specific>
IDENTITY=${REELAY_SIGN_IDENTITY:-}
if [ -z "$IDENTITY" ]; then
  IDENTITY=$(security find-identity -v -p codesigning 2>/dev/null \
    | awk '/"Developer ID Application:/ {print $2; exit}')
fi
if [ -z "$IDENTITY" ]; then
  IDENTITY=$(security find-identity -v -p codesigning 2>/dev/null \
    | awk '/"Apple Development:/ {print $2; exit}')
fi
IDENTITY=${IDENTITY:--}
IDENTITY_NAME=""
if [ "$IDENTITY" != "-" ]; then
  IDENTITY_NAME=$(security find-identity -v -p codesigning \
    | awk -v id="$IDENTITY" '$2==id || index($0, id) {$1="";$2=""; print; exit}' | sed 's/^ *//')
fi
if [ "$IDENTITY" != "-" ] && [ -z "$IDENTITY_NAME" ]; then
  echo "Signing identity \"$IDENTITY\" isn't in this keychain (see: security find-identity -v -p codesigning)." >&2
  exit 1
fi
NOTARIZE=${REELAY_NOTARY_PROFILE:-}
if [ -n "$NOTARIZE" ]; then
  if [[ "$IDENTITY_NAME" != *"Developer ID Application"* ]]; then
    echo "REELAY_NOTARY_PROFILE is set, but notarization needs a \"Developer ID Application\" identity" >&2
    echo "(found: ${IDENTITY_NAME:-ad-hoc}). See docs/release-signing.md." >&2
    exit 1
  fi
  if ! xcrun notarytool history --keychain-profile "$NOTARIZE" >/dev/null 2>&1; then
    echo "Notarization profile \"$NOTARIZE\" couldn't be used (not stored in this keychain, or no network)." >&2
    echo "Store it with: xcrun notarytool store-credentials $NOTARIZE --apple-id … --team-id … --password …" >&2
    exit 1
  fi
fi
if [ "$IDENTITY" = "-" ]; then
  echo "Preflight: ad-hoc signature, no hardened runtime, not notarized"
else
  echo "Preflight: $IDENTITY_NAME, hardened runtime, $([ -n "$NOTARIZE" ] && echo "notarized ($NOTARIZE)" || echo "not notarized")"
fi
if [ -n "${REELAY_PREFLIGHT_ONLY:-}" ]; then exit 0; fi   # checks only (used by the tests)


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

SIGN_OPTS=(--force --deep --sign "$IDENTITY")
if [ "$IDENTITY" = "-" ]; then
  echo "Signing: ad-hoc (macOS will ask for permissions again after each rebuild)"
  SIGN_OPTS+=(--timestamp=none)
else
  echo "Signing: $IDENTITY_NAME (hardened runtime)"
  SIGN_OPTS+=(--options runtime)
  if [ -n "$NOTARIZE" ]; then SIGN_OPTS+=(--timestamp); else SIGN_OPTS+=(--timestamp=none); fi
  if [ -n "${REELAY_ENTITLEMENTS:-}" ]; then SIGN_OPTS+=(--entitlements "$REELAY_ENTITLEMENTS"); fi
fi
codesign "${SIGN_OPTS[@]}" "$APP"
codesign --verify --deep --strict "$APP"

if [ -n "$NOTARIZE" ]; then
  echo "Notarizing the app (this can take a few minutes)…"
  ZIP=$(mktemp -d)/Reelay.zip
  ditto -c -k --keepParent "$APP" "$ZIP"
  xcrun notarytool submit "$ZIP" --keychain-profile "$NOTARIZE" --wait
  xcrun stapler staple "$APP"
  rm -f "$ZIP"
fi

if [ -n "${REELAY_SKIP_DMG:-}" ]; then   # scripts/build_local.sh --no-dmg: the .app is all /Applications needs
  echo "Built: $APP (no .dmg: REELAY_SKIP_DMG is set)"
  exit 0
fi

# Stage the .app next to an Applications shortcut, then compress into a .dmg.
STAGE=$(mktemp -d)
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
rm -f "$DMG"
hdiutil create -volname "Reelay" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
rm -rf "$STAGE"

if [ -n "$NOTARIZE" ]; then
  codesign --force --timestamp --sign "$IDENTITY" "$DMG"
  echo "Notarizing the disk image…"
  xcrun notarytool submit "$DMG" --keychain-profile "$NOTARIZE" --wait
  xcrun stapler staple "$DMG"
  spctl --assess --type execute -vv "$APP"
  spctl --assess --type open --context context:primary-signature -vv "$DMG"
fi

echo "Built: $APP"
echo "Built: $DMG"
