# Reelay

Move a creator's short-form video archive between platforms. Reelay downloads every video from a TikTok, Instagram or YouTube profile (watermark-free, full audio), lets you review and rename them, then stages and schedules them to YouTube from a Kanban board.

It's a native desktop app for macOS (Windows builds included) in a monotone Light, Dark or Black theme.

## What it does

**Download**
- Paste a profile link and click **Fetch Videos** to see every video first: cover, date, hook, length and views.
- Choose what to download: tick boxes, **Include Only the Newest…**, search, or press Space on selected rows.
- Name videos with a template (`{platform}_{date}_{id}` by default, or `{date}_{custom}` and others). Use **Auto-Name** to fill custom names from captions, or type them yourself.
- TikTok downloads are always the clean, watermark-free stream with the creator's own audio. The post's separate "sound" track is never merged in.
- **Apply Names** batch-renames files you've already downloaded, with a preview first.
- Each download writes `manifest.md` (a review table) and a JSON metadata file per video.

**Board**
- A Kanban pipeline: **Backlog → Staging → Scheduled → Published**. Drag cards between columns.
- The post editor sets the YouTube channel, title, description, tags, visibility, and a scheduled date and time.
- Dropping a card on **Scheduled** uploads it to YouTube (resumable, with progress) with a `publishAt` time, so YouTube publishes it even when Reelay is closed.
- **Sync with YouTube** moves cards to Published once they go live.

**Profiles and accounts**
- Each creator gets a profile with their own library folder, board and connected channels. Switch profiles from the sidebar avatar (or ⌘⌥1–9).
- You can connect several YouTube channels per profile through Google sign-in. Tokens are kept in the macOS Keychain or Windows Credential Manager, never in files.
- **Channel Schedule** shows a channel's latest uploads, including private and scheduled ones.

## Running from source

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python run_app.py
```

The command-line version uses the same engine:

```bash
python -m reelay.cli "https://www.tiktok.com/@handle" --limit 5 -o downloads
```

## Connecting YouTube (one-time)

Reelay uses your own free Google API project, so your quota and channels stay yours. The **Accounts** page walks through these steps:

1. In the [Google Cloud Console](https://console.cloud.google.com/), create a project and enable **YouTube Data API v3**.
2. Set up the **OAuth consent screen** as External and add each Google account you'll connect as a test user.
3. Create an **OAuth client ID** of type **Desktop app** and download the JSON.
4. In Reelay, go to **Accounts → Import client_secret.json…**, then **Connect YouTube Channel**.

Limits to know about:
- **Videos stay private until Google audits your project.** Uploads from a Google project that hasn't passed YouTube's [API compliance audit](https://support.google.com/youtube/contact/yt_api_form) are locked to private. Scheduled videos won't go public until that audit is approved. Reelay flags this on the card if it happens.
- **Testing mode signs you out weekly.** While the project is in Testing mode, Google expires sign-ins every 7 days, so you'll need to reconnect.
- **There's a daily upload limit.** YouTube's default API quota caps how many uploads you can do per day, and you can ask Google for more.

## Platform status

| Platform  | Download | Publish | Notes |
|-----------|----------|---------|-------|
| TikTok    | ✅ | Planned | Content Posting API needs an approved TikTok developer app. Unaudited apps can only post privately. |
| Instagram | ✅ (log in with browser cookies) | Planned | Needs a Professional account linked to a Facebook Page, plus Meta App Review. The API has no native scheduling, so Reelay will publish at the set time itself. |
| YouTube   | ✅ | ✅ Upload + schedule | See the limits above. |

## Tests

```bash
python -m pytest                              # 57 tests: engine, database, YouTube client, headless GUI
python -m pytest --run-network -m network     # optional live checks against TikTok
python run_app.py --self-test                 # environment check (also works on packaged builds)
```

The YouTube tests use a fake HTTP session and a real local sign-in server, so they never contact Google. The GUI tests drive the app headlessly with fake downloads and uploads.

## Building the apps

```bash
./packaging/build_mac.sh          # dist/Reelay.app and dist/Reelay-<version>-mac-arm64.dmg
```
```powershell
.\packaging\build_windows.ps1     # dist\Reelay\Reelay.exe and a .zip (run on Windows)
```

PyInstaller can't cross-compile, so each platform builds on its own OS. Pushing a `v*` tag, or running the CI workflow manually, builds both on GitHub Actions and uploads the `.dmg` and `.zip`. Builds are ad-hoc signed: on first launch, right-click the app and choose **Open**. Shipping to other people without that warning needs an Apple Developer ID and notarization.

## Project layout

```
reelay/            engine: fetcher, downloader, naming, library/manifest, store (SQLite),
                   youtube (OAuth + uploads), publishing, accounts, tokens (keychain), cli
reelay/gui/        PySide6 app: main_window (sidebar + profiles), download_page, board_page,
                   post_panel, accounts_page, settings_page, theme (Light/Dark/Black)
tests/             pytest + pytest-qt
packaging/         PyInstaller spec, icon generator, build scripts
```

## Roadmap

- **Bulk scheduling.** Spread a selection across days, for example one video a day at 9 AM.
- **Calendar view** of everything scheduled across channels.
- **Task cards.** Notes, checklists and non-video tasks on the board, a personal ticket system.
- **AI help with titles and descriptions**, using each video's caption and stats.
- **Performance stats** from the YouTube Analytics API, on the cards.
- **TikTok and Instagram publishing**, once developer apps are approved.
- **Signed, notarized builds** with auto-update.

Only download and republish content you own or have permission to use.
