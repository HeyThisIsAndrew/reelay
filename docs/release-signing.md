# Signing and notarizing Reelay

How release builds are signed, what was verified, and what's still needed from accounts only you can create.

## macOS

**What `packaging/build_mac.sh` does**

| Identity in the keychain | Signature | Hardened runtime | Notarized |
|---|---|---|---|
| none (or `REELAY_SIGN_IDENTITY=-`) | ad-hoc | no (library validation would refuse the bundled libraries without a Team ID) | no |
| "Apple Development" (free, Xcode) | stable, so macOS remembers "Allow" across rebuilds | **yes** | no (Apple only notarizes Developer ID) |
| "Developer ID Application" (paid program) | distributable | **yes** | when `REELAY_NOTARY_PROFILE` is set |

With `REELAY_NOTARY_PROFILE`, the script signs with a secure timestamp, notarizes the app (zip) and staples
it, builds the `.dmg`, signs and notarizes that, staples it, and checks both with `spctl`.

**Preflight, before anything is built** (so a wrong setup fails in seconds): the script picks the identity,
refuses one that isn't in the keychain, refuses `REELAY_NOTARY_PROFILE` without a Developer ID identity, and
refuses a notarization profile that `xcrun notarytool` can't use; then it prints what it will do.
`REELAY_PREFLIGHT_ONLY=1 ./packaging/build_mac.sh` runs just that (`tests/test_release_signing.py` does).
Verified here: the default build (no credentials) signs with the hardened runtime and isn't notarized; ad-hoc
skips the hardened runtime; notarizing without Developer ID and an unknown identity both fail clearly. The
"profile can't be used" branch needs a Developer ID certificate to reach, so it's untested until there is one.
The installed app's signature was checked with `codesign --verify --deep --strict` (valid; hardened runtime
on the app and on nested code such as the bundled ffmpeg; no entitlements). `spctl` rejects it, as expected
for an Apple Development signature that isn't notarized.

**Entitlements: none.** Verified on 2026-10-11 with Reelay 0.12.0 (PyInstaller, PySide6 6.x with QtMultimedia
and its FFmpeg backend, yt-dlp 2026.08.19 with curl_cffi, imageio-ffmpeg, keyring): the packaged app was
signed with `--options runtime` and **no entitlements**, then

- `Reelay --self-test` passed every check (yt-dlp extractors, TikTok browser impersonation, ffmpeg, caption
  burning with the bundled ffmpeg, Keychain, SQLite with FTS5, Qt image plugins, the video player's codecs,
  the agent API server, the offline help page);
- the window opened and ran normally against a fresh data folder;
- the bundled ffmpeg encoded a test clip.

So none of `allow-jit`, `allow-unsigned-executable-memory`, `disable-library-validation` or
`allow-dyld-environment-variables` is needed. Don't add one without a failing case that proves it. If a later
dependency needs one (e.g. an optional local AI library loaded from outside the bundle), put it in a plist,
pass it with `REELAY_ENTITLEMENTS=packaging/entitlements.plist`, and record the failing case here.

**One-time setup for notarized releases**

1. Join the Apple Developer Program and create a "Developer ID Application" certificate (Xcode → Settings →
   Accounts → Manage Certificates).
2. Make an app-specific password at appleid.apple.com, then store it once:
   `xcrun notarytool store-credentials reelay-notary --apple-id <you> --team-id <TEAMID> --password <app-specific>`
3. Build: `REELAY_NOTARY_PROFILE=reelay-notary ./packaging/build_mac.sh`

Not done yet: there's no Developer ID certificate on this Mac (only "Apple Development"), so builds are signed
with the hardened runtime but not notarized; Gatekeeper still warns on other Macs until they are.

## Local builds (this Mac)

`./scripts/build_local.sh` is the one command: from a clean clone of `main` it makes `.venv` if needed, runs
`packaging/build_mac.sh`, self-tests the new build, quits a running Reelay, backs up the database
(`reelay-backup-<date>-<commit>.db` next to `reelay.db`, a consistent SQLite backup), installs to
`/Applications/Reelay.app`, verifies the signature and self-tests the installed copy. `--open` opens it after,
`--no-dmg` skips the disk image. It signs with whatever identity the keychain has (today the free "Apple
Development" one, so macOS remembers "Allow" across rebuilds), or ad-hoc when there is none. Both are fine for
this Mac; neither is fine for anyone else's.

## Developer ID release checklist (not done yet: do this before the first public download)

Everything below needs the owner's Apple account; none of it can be scripted from here. Steps 1 to 4 happen
once; 5 to 10 happen for every release.

**Once**

1. **Apple Developer Program** ($99 a year) at developer.apple.com/programs, enrolled as an individual or as
   the business that will appear on the certificate ("Developer ID Application: <name> (TEAMID)"). The name
   is what Gatekeeper and the website's download page show, so enroll under the name you sell under.
2. **Developer ID Application certificate.** Xcode → Settings → Accounts → (your team) → Manage Certificates →
   + → Developer ID Application. It lands in the login keychain. Check it with
   `security find-identity -v -p codesigning` (it should list "Developer ID Application: …").
   Export a backup (.p12 with a strong password) to a password manager, never to the repository.
3. **App-specific password** at account.apple.com → Sign-In and Security → App-Specific Passwords, named
   "reelay-notary".
4. **Notary profile in the keychain** (stores the password there, not in a file):
   `xcrun notarytool store-credentials reelay-notary --apple-id <apple id> --team-id <TEAMID> --password <app-specific password>`
   Check it: `xcrun notarytool history --keychain-profile reelay-notary`.

**Every release**

5. Clean tree on `main`, version bumped in `reelay/__init__.py` (the only place it lives), tests green:
   `.venv/bin/python -m pytest -q -n auto`.
6. Build, sign, notarize and staple in one go:
   `REELAY_NOTARY_PROFILE=reelay-notary ./packaging/build_mac.sh`
   The preflight refuses to start without a Developer ID identity and a usable profile. The script signs the app
   with the hardened runtime and a secure timestamp, submits it, staples the ticket, builds the .dmg, signs,
   notarizes and staples that too, then runs `spctl` on both.
7. If notarization is rejected: `xcrun notarytool log <submission id> --keychain-profile reelay-notary` names
   every file Apple didn't accept. Fix the cause (usually an unsigned nested binary or a missing timestamp);
   never add an entitlement to make it pass without recording the failing case in this file.
8. Check it as a stranger would, on a Mac (or a fresh user account) that has never run Reelay:
   download the .dmg through a browser (so it is quarantined), open it, drag to Applications, double-click.
   There must be no "can't be opened" or "unidentified developer" dialog. Then `Reelay --self-test`.
   `spctl --assess --type execute -vv /Applications/Reelay.app` must say "accepted, source=Notarized Developer ID".
9. Record the .dmg's SHA-256 (`shasum -a 256 dist/Reelay-<version>-mac-arm64.dmg`) on the download page.
10. Publish the GitHub release with that .dmg and the release notes. Only then point the website's download
    button at it.

**What can go wrong**

- A certificate that expires (Developer ID certificates last five years) stops new builds from signing, not
  old downloads from opening: stapled, notarized builds keep working.
- Signing on a Mac without the private key: export the .p12 from step 2 and import it there first.
- Windows is separate (Authenticode, below); a Mac certificate does nothing for it.

## Windows

`packaging/build_windows.ps1` signs `Reelay.exe` and the bundled `.exe`/`.dll`/`.pyd` files that aren't signed
yet (SHA-256, RFC 3161 timestamp), then verifies `Reelay.exe`, when either is set:

- `REELAY_WIN_SIGN_THUMBPRINT`: a code-signing certificate in the user's certificate store (including a USB
  token or a cloud HSM that exposes one there, which current CA rules require for new certificates);
- `REELAY_WIN_PFX` and `REELAY_WIN_PFX_PASSWORD`: a `.pfx` file (never in the repository; `*.pfx` is ignored).

`REELAY_WIN_TIMESTAMP_URL` overrides the timestamp server (default DigiCert's). Without either variable the
build is unsigned, as before. Not verified on Windows yet: there's no Windows machine or certificate here, so
run it once on Windows with a certificate before the first signed release.

## Keys and secrets

Signing certificates live in the macOS Keychain or the Windows certificate store, notarization credentials in
a `notarytool` keychain profile. Nothing secret goes in the repository, the build scripts or their output.
