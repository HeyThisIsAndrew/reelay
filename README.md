# Reelay

Move a creator's short-form video archive between platforms. Reelay downloads every video from a TikTok, Instagram or YouTube profile (watermark-free, full audio), lets you review and rename them, and turns each one into a **content ticket**: a card on a Kanban board holding ready-to-paste copy for every platform. You paste the copy into each platform yourself. YouTube can also be uploaded and scheduled automatically, and Bluesky, Threads and Facebook can be turned on later (see [Automatic posting](#automatic-posting)).

It's a native desktop app for macOS (Windows builds included) in a monotone Light, Dark or Black theme.

## What it does

**Download**
- Paste a profile link and click **Fetch Videos** to see every video first: cover, date, hook, length and views.
- Choose what to download: tick boxes, **Include Only the Newest…**, search, or press Space on selected rows.
- Name videos with a template (`{platform}_{date}_{id}` by default, or `{date}_{custom}` and others). Use **Auto-Name** to fill custom names from captions, or type them yourself.
- TikTok downloads are always the clean, watermark-free stream with the creator's own audio. The post's separate "sound" track is never merged in.
- **Apply Names** batch-renames files you've already downloaded, with a preview first.
- Each download writes `manifest.md` (a review table) and a JSON metadata file per video.

**Board (content tickets)**
- A Kanban pipeline: **Backlog → Staging → Scheduled → Published**. Drag cards between columns.
- Each card is a ticket with **copy blocks**:
  - YouTube title, description and tags
  - TikTok caption and Instagram caption
  - Bluesky text, Threads text and Facebook text
  - one shared hashtag block
- Every block has a **Copy** button and a live count that uses that platform's own rules:

  | Block | Limit |
  |---|---|
  | YouTube title | 100 characters |
  | YouTube description | 5,000 bytes |
  | YouTube tags | 500 characters in total |
  | TikTok | 4,000 characters |
  | Instagram | 2,200 characters and 30 hashtags |
  | Bluesky | 300 characters (an emoji counts once) and 3,000 bytes |
  | Threads | 500 characters (an emoji counts as its UTF-8 bytes) |
  | Facebook | 63,206 characters |

  Over-limit text is reported and the save is refused. Nothing is ever cut short.
- Copying an empty block gives what Reelay would use instead: the title (plus the description if it fits) for Bluesky and Threads, and the description for TikTok, Instagram and Facebook.
- **Auto-post** sends the video to YouTube (resumable, with progress) with a `publishAt` time, so YouTube publishes it even when Reelay is closed. Only platforms with automatic posting turned on are offered.
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
| YouTube   | ✅ | ✅ Upload + schedule on YouTube (always on) | See the limits above. |
| Facebook Pages | — | Copy-paste; automatic posting **off by default** (native scheduling when on) | Meta app + App Review (see below). |
| Bluesky   | — | Copy-paste; automatic posting **off by default** (Reelay posts at the scheduled time when on) | App Password. |
| Threads   | — | Copy-paste; automatic posting **off by default** (Reelay posts at the scheduled time when on) | Meta app + App Review, and a public URL (Cloudflare Tunnel). |
| TikTok    | ✅ | Planned | Content Posting API needs an approved TikTok developer app. Unaudited apps can only post privately. |
| Instagram | ✅ (log in with browser cookies) | Planned | Needs a Professional account linked to a Facebook Page, plus Meta App Review. |

## Automatic posting

Reelay is a ticket system first: the copy on each card is meant to be pasted by hand. YouTube posting is always available. Bluesky, Threads and Facebook publishers are built and tested, but stay **off** until you turn them on:

| Flag | Environment variable | Setting |
|---|---|---|
| `publish.bluesky` | `REELAY_PUBLISH_BLUESKY=1` | Settings → Automatic posting |
| `publish.threads` | `REELAY_PUBLISH_THREADS=1` | Settings → Automatic posting |
| `publish.facebook` | `REELAY_PUBLISH_FACEBOOK=1` | Settings → Automatic posting |

- **Precedence:** the environment variable wins when set (`1` turns the platform on, `0` forces it off), then the Settings switch, then the default (off).
- **Restart to apply:** flags are read once at startup, so restart Reelay (or `python -m reelay.api`) after changing one.

To turn a platform on:
1. Connect its account under **Accounts**. The card there shows whether automatic posting is on.
2. Turn its flag on.
3. Restart Reelay.

No code changes are needed.

What changes when a platform is on:
- **Facebook** uses its own scheduling (`scheduled_publish_time`): the video is uploaded right away and Facebook publishes it at the set time.
- **Bluesky and Threads** have no scheduling of their own. Reelay posts them at the set time, so Reelay must be running and the Mac awake then.

While a platform is off:
- It isn't offered as an Auto-post target, and asking the API to schedule to it returns `422` with code `platform_disabled`.
- A card's `target_platforms` can still list it; scheduling without a `platforms` list posts only to the enabled ones and returns the rest in `manual_platforms`.
- Anything already queued for that platform (scheduled while it was on) waits untouched until it's turned back on.

## Agent API

A local HTTP API for AI agents and automations: read the board, apply approved metadata edits, and schedule posts. It runs inside Reelay, and agent changes show up on the desktop board straight away. The machine-readable description is at `/openapi.json` (OpenAPI 3.1), which agent frameworks (Gemini, Antigravity, Claude) can turn into tools directly. The same contract is checked into [`api/openapi.yaml`](api/openapi.yaml).

### Starting it

- **With the app:** the API starts when Reelay opens and stops when it quits. **Settings → Agent API** shows its address and has **Copy Token** and **Rotate Token**.
- **Without the window** (on a server, or for testing):
  ```bash
  python -m reelay.api
  ```
  Press Ctrl-C to stop. The scheduler that posts to Bluesky and Threads runs too.
- **Turn it off:** set `REELAY_API_DISABLED=1`.

| Setting | Default | Purpose |
|---|---|---|
| `REELAY_HOST` | `127.0.0.1` | Address to bind. Only set `0.0.0.0` for phone apps on your LAN. It's never needed for agents or the tunnel. |
| `REELAY_PORT` | `8471` | Port. |
| `REELAY_API_TOKEN` | *(keychain)* | Use this token instead of the keychain one (tests, headless servers). |
| `REELAY_PUBLIC_URL` | *(Settings → Public URL)* | Your Cloudflare Tunnel address. Threads downloads videos from expiring links under it. |
| `REELAY_DISPATCH_INTERVAL` | `60` | Seconds between scheduler checks. |
| `REELAY_DISPATCH_STALE_SECONDS` | `1800` | A post stuck mid-send longer than this is retried. |

**Remote agents through a Cloudflare Tunnel.** Point the tunnel at the API on the Mac:
```bash
cloudflared tunnel --url http://127.0.0.1:8471
```
For a stable address, create a named tunnel in the Cloudflare dashboard. Every `/v1` call needs the bearer token. Only `/openapi.json` (no secrets) and the one-hour `/media/<token>` links for Threads are open without it. For a second lock on the public hostname, add a Cloudflare Access service-token policy. Reelay sends no CORS headers: it's meant for server-to-server calls.

### Endpoints

| Endpoint | What it does |
|---|---|
| `GET /v1/board?profile=…&stage=…&limit=…&offset=…` | A profile's board in order, with title, description, tags, target platforms, overrides, channel, visibility, schedule, file path, per-platform status, and the original caption and stats. `profile` is the sidebar name (not case-sensitive). |
| `PATCH /v1/videos/{id}` | Partial edit of the ticket's copy: `title`, `description`, `tags`, `target_platforms`, `tiktok_text`, `instagram_text`, `bluesky_text`, `threads_text`, `facebook_text` and `hashtags`. Everything is checked before anything is saved. Over-limit text is rejected, never cut short. |
| `POST /v1/videos/{id}/schedule` | Schedules automatic posting through the same pipeline as the Board's button. Optional `scheduled_at` (ISO 8601 with a timezone, at least 10 minutes ahead) and `platforms` (each must be enabled). Returns the queued jobs and `manual_platforms`. |
| `GET /v1/platforms?profile=…` | Per platform: `auto_post` (whether automatic posting is on), whether a credential is in the keychain (`connected`), account names, and `last_verified_at`. "Connected" means a credential is present, not that it worked recently. Secrets are never returned. |

Errors always come back as `{"detail": "…", "code": "…", "problems": […]}`:

| Status | Meaning |
|---|---|
| 400 | Missing or unknown profile |
| 401 | Missing or invalid token |
| 404 | Unknown video |
| 409 | Already scheduled or published |
| 422 | Invalid input; nothing was changed |

Each change is regenerated from the code, not edited by hand:
```bash
python -m reelay.api --write-contract
```
`tests/test_api_contract.py` fails if the live spec and `api/openapi.yaml` differ.

### Examples

These were run against `python -m reelay.api` with the default flags (only YouTube posts automatically). Output is abbreviated.

```bash
TOKEN=$(security find-generic-password -s Reelay -a agent_api_token -w)   # macOS keychain
API=http://127.0.0.1:8471
```

**Read the board**
```bash
curl -s "$API/v1/board?profile=Lydia%20Livingood&limit=1" -H "Authorization: Bearer $TOKEN"
```
```json
{"profile": {"id": 1, "name": "Lydia Livingood"}, "total": 2, "limit": 1, "offset": 0, "next_offset": 1,
 "videos": [{"id": 1, "stage": "backlog", "title": "All my designs are hand drawn stickers are",
   "tags": ["artistsoftiktok", "handdrawnart", "vinylstickers"], "target_platforms": ["youtube"],
   "overrides": {"tiktok_text": null, "instagram_text": null, "bluesky_text": null, "threads_text": null,
                 "facebook_text": null},
   "hashtags": null, "draft_saved": false, "file_available": true, "publications": [],
   "source": {"platform": "TikTok", "caption": "all my designs are hand drawn ✨️ …", "uploaded_on": "2026-09-29",
              "stats": {"views": 99, "likes": 1, "comments": 0, "duration_seconds": 8.0}}}]}
```

**Write the ticket's copy** (after you've approved it)
```bash
curl -s -X PATCH "$API/v1/videos/2" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"title": "Letting Go of Who You Were", "tags": ["growth", "healing"],
       "tiktok_text": "the loneliness in the middle of who you'"'"'ve been and who you'"'"'re becoming 🦋",
       "instagram_text": "Growth doesn'"'"'t always feel like winning. 🦋",
       "bluesky_text": "Growth doesn'"'"'t always feel like winning 🦋 #growthjourney",
       "facebook_text": "A note on letting go.", "hashtags": "#growthjourney #healing #transformation"}'
```
```json
{"id": 2, "title": "Letting Go of Who You Were", "tags": ["growth", "healing"], "draft_saved": true,
 "overrides": {"tiktok_text": "the loneliness in the middle of who you've been and who you're becoming 🦋",
               "instagram_text": "Growth doesn't always feel like winning. 🦋",
               "bluesky_text": "Growth doesn't always feel like winning 🦋 #growthjourney",
               "threads_text": null, "facebook_text": "A note on letting go."},
 "hashtags": "#growthjourney #healing #transformation"}
```
Over-limit copy is rejected and nothing is saved:
`422 {"detail": "Those changes aren't valid.", "code": "invalid_post", "problems": ["instagram_text: Instagram caption has 31 hashtags; the limit is 30."]}`

**Schedule** (posts to the enabled targets; the rest come back as manual)
```bash
curl -s -X POST "$API/v1/videos/1/schedule" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"scheduled_at": "2026-10-20T09:00:00-07:00"}'
```
```json
{"video_id": 1, "stage": "scheduled", "scheduled_at": "2026-10-20T16:00:00Z",
 "target_platforms": ["youtube"], "manual_platforms": ["bluesky", "threads"],
 "jobs": [{"platform": "youtube", "job_id": 1, "status": "dispatching", "due_at": "2026-10-07T22:41:08Z",
           "publish_at": "2026-10-20T16:00:00Z", "queue_position": 0}]}
```
The test suite checks this response's shape (`test_disabled_targets_are_reported_as_manual`); the timestamps here are illustrative. On a real machine it needs a connected YouTube channel.

When a platform is on, its job shows up too. Bluesky's `due_at` is the publish time: YouTube and Facebook are sent right away and scheduled on the platform, while Reelay sends Bluesky and Threads when they're due. `queue_position` is a job's place among waiting jobs.

Errors:
- **Asking for a turned-off platform:** `422 {"detail": "Automatic posting to Bluesky is not enabled (platform not enabled). Post it manually from the card's copy, or turn it on with REELAY_PUBLISH_BLUESKY=1.", "code": "platform_disabled"}`
- **Scheduling again:** `409 {"detail": "Already scheduled or published on YouTube.", "code": "already_scheduled"}`
- **Missing setup:** `422`, listing every problem (e.g. `"Choose a YouTube channel to post to."`).

**Check platforms**
```bash
curl -s "$API/v1/platforms?profile=Lydia%20Livingood" -H "Authorization: Bearer $TOKEN"
```
```json
{"profile": {"id": 1, "name": "Lydia Livingood"},
 "platforms": [{"platform": "youtube", "connected": false, "auto_post": true, "accounts": [],
                "setup_issue": "Import Google API credentials in Reelay → Accounts."},
               {"platform": "bluesky", "connected": false, "auto_post": false, "accounts": [],
                "setup_issue": "Automatic posting is off (copy-paste only). Turn it on with REELAY_PUBLISH_BLUESKY=1 or Reelay → Settings → Automatic posting, then restart Reelay."}]}
```

### How publishing works

This applies to YouTube, and to Bluesky, Threads and Facebook once their [flags](#automatic-posting) are on.

- **YouTube** gets the video as soon as you schedule, through Reelay's upload queue (one upload at a time). It's uploaded as private with `publishAt`, and YouTube makes it public at the time you set, even if your Mac is off.
- **Facebook Pages** get the video right away too, with `scheduled_publish_time` (allowed 10 minutes to 6 months ahead). Facebook publishes it on its own.
- **Bluesky and Threads** have no scheduling of their own. Reelay's scheduler posts them at the scheduled time, so Reelay (or `python -m reelay.api`) must be running and the Mac awake then.
- **Statuses.** Each platform has its own: `pending` → `dispatching` → `scheduled` (accepted by the platform) or `published`. A failure on one platform never changes another's.
  - Temporary errors (network problems, 5xx, rate limits) retry after 1, 2, 4, 8 minutes and so on, giving up after 6 attempts.
  - Permanent errors (bad credentials, rejected video) show as `failed`, with the reason on the card and in the API.
- **No double posting.** Each post is claimed in a single database operation before any platform call. A restart or a second worker can't post it twice, and posts left mid-send by a crash are retried. Bluesky reuses its record key and Threads reuses its media container, so a retry finishes the same post instead of creating another. For Facebook, an interrupted upload is marked failed rather than re-uploaded, because it may already be on the Page.

### Credentials: where they live and how to rotate them

Secrets are only ever in the system keychain (macOS Keychain or Windows Credential Manager), under service **Reelay**. Never in the database, logs or API responses.

| Keychain account | Holds | Rotate |
|---|---|---|
| `agent_api_token` | Agent API bearer token (made on first run) | **Settings → Agent API → Rotate Token** (copies the new one). Or delete the item (`security delete-generic-password -s Reelay -a agent_api_token`) and restart Reelay. `REELAY_API_TOKEN` overrides it. |
| `youtube:<channel id>` | Google OAuth tokens | **Accounts → Disconnect**, then **Connect YouTube Channel**. |
| `bluesky:<did>` | Handle + App Password | Revoke the App Password in Bluesky, create a new one, then **Accounts → Bluesky → Connect** (replaces the old one). |
| `threads:<user id>` | Threads user access token | Generate a new long-lived token, then **Accounts → Threads → Connect**. |
| `facebook:<page id>` | Page ID + Page access token | Generate a new Page token, then **Accounts → Facebook → Connect**. |

**Verify** on each account sends a live check and updates `last_verified_at`. A successful post updates it too.

### Getting each platform's credentials (one-time manual steps)

- **YouTube:** the Google Cloud steps under *Connecting YouTube* above.
- **Bluesky:**
  1. In Bluesky, go to **Settings → Privacy and security → App passwords** and create one.
  2. In Reelay, go to **Accounts → Bluesky** and enter your handle and the App Password. Don't use your main password.
  3. The account needs a verified email to post video. Bluesky also has daily video upload limits.
- **Threads:**
  1. Create a Meta app at [developers.facebook.com](https://developers.facebook.com/apps) with the **Threads API** use case.
  2. Request `threads_basic` and `threads_content_publish`. Posting for accounts other than your own needs App Review; for your own account, add yourself as a tester.
  3. Generate a long-lived user access token (valid 60 days; refresh or re-generate it before it expires) and paste it into **Accounts → Threads**.
  4. Set **Settings → Public URL** (or `REELAY_PUBLIC_URL`) to your tunnel address. Threads fetches the video from there, through a one-time link that expires after an hour.
- **Facebook Pages:**
  1. In a Meta app, add the **Pages** permissions `pages_manage_posts`, `pages_read_engagement` and `pages_show_list`. App Review is required for Pages you don't manage.
  2. Using a long-lived user token from someone who can create content on the Page, get the Page's access token (`GET /me/accounts`).
  3. Enter the Page ID and Page access token in **Accounts → Facebook**.

## Tests

```bash
python -m pytest                              # 136 tests: engine, database, pipeline, agent API, dispatcher,
                                              #   platform publishers, YouTube client, headless GUI
python -m pytest --run-network -m network     # optional live checks against TikTok
python run_app.py --self-test                 # environment check (also works on packaged builds)
```

Tests never post to real platforms:
- **YouTube** uses a fake HTTP session and a real local sign-in server.
- **Bluesky, Threads and Facebook** check their exact request flows against scripted fake transports.
- **The agent API** runs on a real local server.
- **The GUI** is driven headlessly with fake downloads and uploads.

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
reelay/            engine: fetcher, downloader, naming, library/manifest, store (SQLite + change feed),
                   pipeline (every board/post/schedule/sync rule), uploads (app-wide upload queue),
                   scheduler (dispatcher), publishers/ (Bluesky, Threads, Facebook), platform_text,
                   api (agent API), services (wiring), media (expiring links), youtube, publishing,
                   accounts + tokens (keychain), cli
api/openapi.yaml   agent API contract (generated)
reelay/gui/        PySide6 app: main_window (sidebar + profiles), download_page, board_page,
                   post_panel, accounts_page, settings_page, theme (Light/Dark/Black)
tests/             pytest + pytest-qt
packaging/         PyInstaller spec, icon generator, build scripts
```

How the parts fit:

- **`Store`** is the only thing that touches SQLite, and it's safe to call from any thread. After every write it announces a `Change` (what changed, which profile, and who made it: `local` or `api`) to subscribers. The screens receive these on the GUI thread through a Qt bridge.
- **`Pipeline`** holds the rules: which board moves are allowed, drafts, metadata validation, scheduling, upload completion and syncing. It has no Qt and shows no dialogs; problems come back as `PipelineError`s with a stable `code` that the API maps to HTTP statuses. The desktop Board, the agent API and the dispatcher all call it.
- **`Dispatcher`** (in `scheduler.py`) is a background thread with no Qt. It claims due posts through the Store and hands them to the platform publishers.
- **`UploadManager`** runs YouTube uploads one at a time for the whole app, whatever screen is open.
- **The screens** collect what you want to do, confirm it with you, then call the pipeline. They redraw from the change feed, so edits made anywhere show up everywhere.

## Roadmap

- **Bulk scheduling.** Spread a selection across days, for example one video a day at 9 AM.
- **Calendar view** of everything scheduled across channels.
- **Task cards.** Notes, checklists and non-video tasks on the board, a personal ticket system.
- **Automatic Threads token refresh** before the 60-day expiry.
- **Performance stats** from the YouTube Analytics API, on the cards.
- **TikTok and Instagram publishing**, once developer apps are approved.
- **Signed, notarized builds** with auto-update.

Only download and republish content you own or have permission to use.
