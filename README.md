# Reelay

Get everything ready to publish everywhere. Cross-posting is a packaging problem, not a scheduling problem: Reelay turns one finished video into a complete, platform-ready publishing package, and knows whether it's actually ready to leave the building.

Drop in a video file fresh from your editor, or bring in every video from a TikTok, Instagram or YouTube profile you own or manage (watermark-free, full audio). Each one becomes a **content ticket**: a card on a Kanban board holding:
- ready-to-paste copy for every platform, drafted from the video's transcript in your own voice (**Repurpose**);
- a transcript, cover, topic and keywords;
- where the video came from;
- a readiness checklist;
- a change history;
- one record of everywhere it has been posted.

Reelay uploads and schedules YouTube itself, and posts to Bluesky, Threads and Facebook once you connect them and turn automatic posting on (see [Automatic posting](#automatic-posting)). TikTok and Instagram are posted by hand with **Prepare to Publish**, which copies the caption, opens the uploader and selects the file.

It's a native desktop app for macOS (Windows builds included) in a Light, Dark, Black or Custom theme (each profile's own brand colours, with its logo in the sidebar). The Library and Board are media-first grids of thumbnails (hover a card to scrub through the video), and selecting a video plays it in the right-hand panel above its captions and readiness. Hover over anything for a plain-language tip (turn them off in **Settings → Show helpful tips**), and **Help** at the bottom of the sidebar has the full guide, offline.

## What it does

**Bring videos in** (import media you own or have permission to manage)
- **A finished video from this computer:** drop the file anywhere on the window, or **Add Video Files…** in the Library (`local_import.py`). It's copied into the profile's library folder (the export is never moved or changed), recorded as Own content, and its ticket opens on the Board. A caption file next to it with the same name (`My video.srt`, `.vtt` or `.txt`, as DaVinci Resolve, Premiere and Final Cut export) becomes the transcript; with the optional faster-whisper add-on, a file without one is transcribed on this Mac. The same file added twice is one ticket.
- **Download Sources** (in the Library, and on the Accounts page) holds the profile links you use often, one section per platform; **List Videos** on one shows its videos, with nothing to paste again.
  - **Connected YouTube channels** (from Accounts) appear automatically and list their videos through the YouTube Data API, with no link needed. Private and scheduled videos are shown but left unticked: downloading them needs **Use browser sign-in** set to a browser signed into that channel.
  - **Your whole Instagram profile** lists through Meta's official Instagram API once it's connected under **Accounts → Instagram → Advanced Setup…** (a Professional account and a token from your own Meta app; `instagram_media.py`). yt-dlp's Instagram profile reader is marked broken by its developers, so without that, paste reel or post links instead; Reelay says so rather than showing yt-dlp's error.
  - To add a source, type the username (`@khaby.lame` or `khaby.lame`) or paste the profile link. **Mine** marks the profile's own accounts; the **⋯** menu renames or removes a source. Adding an account that's already saved is caught.
- Or paste a profile link (or several video links) and click **List Videos** to see every video first: cover, date, caption, length and views.
- Downloading pulls videos **in**. Posting pushes videos **out** to the accounts you connect under **Accounts**, from the Board.
- **Tagged products (TikTok Shop):** each TikTok video's tagged products are read from its public page in the background.
  - **All products ▾** filters to every video for one product, even with different captions, and search matches product names and brands.
  - Products also appear in the side panel, on the ticket, and in the API (`source.products`).
  - YouTube's Data API has no product tags, and Instagram's need the owner's Meta app, so those aren't read yet.
- **Search and hashtag categories:** the search matches captions and hashtags, and **Filter → All hashtags** narrows the list to one hashtag (with counts). You can also click a hashtag in the side panel. The Board has the same search and hashtag filter.
- Choose what to download in the **selection strip** above the list, which always says what's selected. Newly listed videos start **unselected**, and choices you've made are kept when the list refreshes. Then:
  - **Select All** (every video shown, so a search narrows it) or **Unselect All**;
  - tick boxes;
  - **More ▾** (the newest few, what the search shows, or the highlighted rows);
  - or press Space on highlighted rows.
- **Name files** offers plain choices with a live example: *Platform, date and video ID* (the default), *Date and your name*, and others. Patterns like `{date}_{custom}` are still available under **Custom pattern…**. **Suggest Names ▾** fills names from captions, or you can type your own.
- TikTok downloads are always the clean, watermark-free stream with the creator's own audio. The post's separate "sound" track is never merged in.
- **Rename Files…** batch-renames files you've already downloaded, with a preview first.
- Each download writes `manifest.md` (**Open Video List**) and a JSON metadata file per video.
- **Provenance:** every imported video records its source platform, the original link, when it was imported, the original publish date and creator, and its permission. Links to the profile's own accounts are recorded as "Own content"; anything else stays unrecorded until you set it on the ticket.

**Board (content tickets)**
- A Kanban pipeline: **Inbox → In Prep → Scheduled → Posted** (stored as backlog / staging / scheduled / published). Drag cards between columns.
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
- **Custom columns:** the Board's **Columns** button (or a column's ⋯ menu) renames, adds, removes and reorders columns per profile, 2 to 6 of them, each with a title and a one-line subtitle. Double-click a title or subtitle to edit it in place. The default is Inbox / In Prep / Scheduled / Posted; a saved board still using the old default names gets the new ones.
  - Reelay's four steps always live in some column; removing one hands its steps and cards to the neighbour, so automatic moves keep working.
  - Added columns are your own manual steps (`custom:…`, also in the API's `columns`).
- **Platform tags.** Every ticket has a **Posting to** row of chips, one per platform (YouTube, TikTok, Instagram, Bluesky, Threads, Facebook), each with its logo. Tick a platform and it's going (Preparing), and its caption box appears below; untick it and it's skipped (a platform marked Scheduled or Posted asks first). Each caption box's header says where that platform stands and has a **⋯** menu: **Mark as Scheduled…** (asks when it goes live), **Mark as Posted…** (asks when, and for the link), **Back to Preparing** and **Publish…**.
  - **Cards move themselves:** to Scheduled once every ticked platform is scheduled or live, and to Published once every one is live. A scheduled time passing counts as live, and unticking moves a card back.
  - **When it's checked:** every minute, by the background dispatcher, whether the app is open or running headless. Facebook posts that Reelay scheduled count as live once their time passes; YouTube is still confirmed with YouTube.
  - **Coverage map:** every card shows all six platforms in a fixed order: green ✓ published, blue clock scheduled, amber ready to publish, red ! needs work (missing caption or file, failed or interrupted), faint = not going there. The card's status line names what needs work (`coverage.py`; the API returns `coverage` per platform).
- **Prepare to Publish** (TikTok, Instagram and any platform Reelay isn't posting itself): checks the file and caption, copies the caption, opens the platform's uploader, shows the .mp4 selected in Finder/Explorer, then records the pasted post link and time (`handoff.py`). The person posts in the real uploader, which keeps sounds, tags and Shop links.
- **Collapsible ticket sections:** each platform's copy has its own section with its logo, its count against the limit in the header, and remembered open/closed state.
- **Readiness.** The top of every ticket shows a per-platform checklist (✓ ready or ⚠ blocking) and a **READY / NOT READY** badge. The checks:
  - each platform's own copy is written: YouTube's title and description, and the Bluesky, Threads and Facebook posts;
  - the copy is within that platform's limits;
  - the video file is downloaded.

  A ticket is READY when at least one of its target platforms is ready. Platforms with automatic posting off are *not checked*: you post those by hand, so they never block.
- **The Ready gate.** Scheduling refuses any platform that isn't ready, and lists why. You can still schedule after an explicit confirmation (the API's `force=true`). Copying and pasting is never blocked. Cards that were already Scheduled or Published before the gate existed carry on untouched.
- **Ticket details:** transcript, cover image, type (short, long-form, clip, other), topic, keywords, creator, first-published date, "cut from" (another video; cleared if that video is deleted), and provenance. Each field saves when you leave it.
- **Change history.** Every saved change is logged with the time, who made it (the app or an AI agent), the field, and the old and new values, newest first. Typing isn't logged; saves are. The log is written in the same database transaction as the change, so if logging fails, nothing is saved.
- **Where it's been posted:** one list per platform combining Reelay's automatic posts and your manual ones. Click **Mark as Posted…** after posting by hand.
- **Import Sheet…** fills tickets from a spreadsheet (a CSV file, or a Google Sheets link shared as "Anyone with the link").
  - Rows are matched to videos by link, video ID, file name or your name.
  - Columns are mapped from their headers, and you can change the mapping.
  - Every row is previewed before anything is written. Empty cells never erase anything, only empty fields are filled unless you choose to replace, and invalid rows are skipped with the reason.
  - The whole import is one transaction, logged as "Sheet import".
- **Fill from Original Post** fills a ticket's empty copy from the original caption, within every platform's limits; text that doesn't fit is left empty, never shortened. **Get from Original Post** turns the post's captions into the transcript.
- **Bulk edit:** ⌘/Shift-click cards to set permission, type, topic, keywords, creator or hashtags on all of them in one transaction, or to fill, fetch transcripts or archive them together. Right-click a card for the same actions.
- **Filter** on the Board shows Ready, Not ready or Blocking <platform> tickets.
- **Archive** hides videos without deleting anything; **Archived…** brings them back.
- **Change History** can restore any earlier value. The restore is logged as a new change.
- **Post automatically to** sends the video to YouTube (resumable, with progress) with a `publishAt` time, so YouTube publishes it even when Reelay is closed. Only platforms with automatic posting turned on are offered.
- **Check YouTube for Updates** moves cards to Published once they go live.
- **Archive search** (schema v8): an SQLite FTS5 index over every video's title, original caption, **transcript**, every platform's copy, tags, topic, keywords and notes, kept current by triggers. The Board's search uses it (prefix matching, "exact phrases"), and a card found by spoken words shows the quote. `GET /v1/search`.
- **Writing help, locally:** an **Assist ▾** menu on every caption: spelling and grammar (built-in, or LanguageTool when installed), rewrites through a local model (Ollama or a llama.cpp `.gguf`), and **brand voice** profiles sent as the model's system prompt. **Get SEO Suggestions** uses YouTube's search suggestions and scores the title and tags. These run on this computer.
- **Repurpose** (side panel → **Repurpose with Muse Spark**, or **Batch Repurpose…** for several tickets with a status per ticket): drafts every platform's copy with Meta's Muse Spark model, using **your own Meta API key** (Keychain; `python -m reelay.repurpose_engine --setup`). It sends that video's title, description, notes, transcript and voice profile to Meta's Model API, only after you tick "Allow Muse Spark" and click Generate. When a ticket has no transcript, it fetches the original post's captions first; the transcript is the draft's source of truth. The result is a proposal: you review and edit it, and nothing is saved until **Apply to Ticket**. Drafts over a platform's limit, with an em dash, a banned phrase or a placeholder are rewritten once and otherwise refused, never cut short. A full draft takes about a minute.
- **Voice profiles** (Settings → Brand voice): tone, words to avoid, emoji level (none, sparing, natural), default hashtags (used only when they fit the video), hashtag style, call to action, and a **YouTube footer** added exactly as written to every drafted description (socials, gear and affiliate links). **Import…** / **Export…** move a voice as a file; `python -m reelay.voices import voices/hey-this-is-andrew.json --profile "Hey This Is Andrew" --use` does it from a terminal.
- **Video tools** (ffmpeg, on this computer): remove the sound, replace it with a track you have rights to, burn in captions from a .srt/.vtt/.ass (or from faster-whisper when installed). Edits write `<name>.edited.mp4`; the original is never changed, and the edited copy is what gets posted until **Use Original**.
- **Automations:** when/if/then rules (Automations page; `rules.py`). Triggers: added to the board, moved to a column. Conditions: caption, title, hashtag, product/brand, length, source. Actions: fill an empty field from a template (with Insert Variable chips), add hashtags, set platforms (logo toggles), set a ticket field, move, schedule for the next weekday at a time (weekday + time picker). Recipes open in the rule editor filled in (**Use Recipe…**) and are only added when saved. Once per video per rule, never on existing videos unless asked, templates never overwrite, scheduling respects the Ready gate, and everything is logged.
- **Durable sends:** every send is recorded before it runs. Closing mid-upload pauses it (continues next launch). A send cut off by a crash or power loss, or one whose time passed while Reelay was closed or asleep, becomes **interrupted** and waits for a person (**Try Again / It Was Posted / Cancel**), never retried blindly. Bluesky and Threads resume by checkpoint.

**Profiles and accounts**
- Each creator gets a profile with their own library folder, board and connected channels. Switch profiles from the sidebar avatar (or ⌘⌥1–9). **My Channel** (the first profile) opens by default; other profiles stay as they are.
- You can connect several YouTube channels per profile through Google sign-in. Tokens are kept in the macOS Keychain or Windows Credential Manager, never in files.
- **Central sign-in** (`oauth.py`, `oauth_config.py`, `server/auth_broker.py`): once Reelay's own apps are registered with each platform (client IDs via `REELAY_*_CLIENT_ID` env or `oauth_config.DEFAULTS`) and the small auth broker is deployed, **Sign in with Google / TikTok / Instagram / Facebook / Threads** buttons appear, with analytics scopes included. Until then the existing bring-your-own-project setup (YouTube setup guide, Meta tokens, Bluesky App Password) stays exactly as it was.
- **Shared database:** Settings → Data can move the database into Google Drive, Dropbox, iCloud Drive or OneDrive (SQLite backup API, checked copy) or open one a teammate shares. In synced folders it uses a single-file journal, and a heartbeat lock file allows one editor at a time; everyone else is **view only** (no dispatcher, no API, so only one computer posts) and reloads as the file syncs (`dblocation.py`, `gui/shared_db.py`).
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

Google requires apps like Reelay to connect through a free Google Cloud project that you own, so your quota and channels stay yours. **Accounts → Set Up YouTube…** is a five-step guide with a button for each Google page:

1. Create a project.
2. Enable the **YouTube Data API v3**.
3. Set up the sign-in screen: **External**, with each Google account you'll connect added as a test user.
4. Create a **Desktop app** client ID.
5. Paste the client ID and secret, or load the downloaded JSON.

After that, connecting a channel is one click: **Sign in with Google** opens Google's page in your browser, and the app shows progress with a **Cancel** button.

Limits to know about:
- **Videos stay private until Google audits your project.** Uploads from a Google project that hasn't passed YouTube's [API compliance audit](https://support.google.com/youtube/contact/yt_api_form) are locked to private. Scheduled videos won't go public until that audit is approved. Reelay flags this on the card if it happens.
- **Testing mode signs you out weekly.** While the project is in Testing mode, Google expires sign-ins every 7 days, so you'll need to reconnect.
- **There's a daily upload limit.** YouTube's default API quota caps how many uploads you can do per day, and you can ask Google for more.

## Platform status

| Platform  | Download | Publish | Notes |
|-----------|----------|---------|-------|
| YouTube   | ✅ | ✅ Upload + schedule on YouTube (always on) | See the limits above. |
| Facebook Pages | No | Copy-paste; automatic posting **off by default** (native scheduling when on) | Meta app + App Review (see below). |
| Bluesky   | No | Copy-paste; automatic posting **off by default** (Reelay posts at the scheduled time when on) | App Password. |
| Threads   | No | Copy-paste; automatic posting **off by default** (Reelay posts at the scheduled time when on) | Meta app + App Review, and a public URL (Cloudflare Tunnel). |
| TikTok    | ✅ | Copy-paste only | TikTok only allows automatic posting from developer apps it has reviewed and approved; without approval, an app can only send private ("Only me") posts or drafts. |
| Instagram | ✅ Your own profile through the official API (Accounts → Instagram → Advanced Setup); reel and post links for any account | Copy-paste only | Automatic posting needs a Meta app with instagram_business_content_publish that passes App Review. |

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
- It isn't offered under **Post automatically to**, and asking the API to schedule to it returns `422` with code `platform_disabled`.
- Its readiness is `not_checked` and never blocks the ticket.
- A card's `target_platforms` can still list it; scheduling without a `platforms` list posts only to the enabled ones and returns the rest in `manual_platforms`.
- Anything already queued for that platform (scheduled while it was on) waits untouched until it's turned back on.

## Agent API

A local HTTP API for AI agents and automations: read the board, apply approved metadata edits, and schedule posts. It runs inside Reelay, and agent changes show up on the desktop board straight away. The machine-readable description is at `/openapi.json` (OpenAPI 3.1), which agent frameworks (Gemini, Antigravity, Claude) can turn into tools directly. The same contract is checked into [`api/openapi.yaml`](api/openapi.yaml).

**MCP** (`mcp_server.py`, at `/mcp` on the same server): MCP clients such as Claude Code, Claude Desktop or Cursor connect with **Settings → AI agents (advanced) → Pair AI Client…**, which shows a one-time code (`RLY-XXXX-XXXX`). The paired client gets its own key, stored only as a hash, that works only for `/mcp`; its edits are logged as `mcp:<client name>`, and anything consequential (scheduling, posting, archiving) waits for your approval exactly like the REST API's. Five wrong codes in five minutes lock pairing for a minute.

### Starting it

- **With the app:** the API starts when Reelay opens and stops when it quits. **Settings → AI agents** shows its address and has **Copy Access Key** and **Make a New Key…**.
- **Without the window** (on a server, or for testing):
  ```bash
  python -m reelay.api
  ```
  Press Ctrl-C to stop. The scheduler that posts to Bluesky and Threads runs too.
- **Turn it off:** set `REELAY_API_DISABLED=1`.

| Setting | Default | Purpose |
|---|---|---|
| `REELAY_HOST` | `127.0.0.1` | Address to bind. Only set `0.0.0.0` for phone apps on your LAN, and then also `REELAY_ALLOW_NETWORK=1` (without it, Reelay stays on `127.0.0.1`). It's never needed for agents or the tunnel. |
| `REELAY_ALLOW_NETWORK` | unset | `1` lets `REELAY_HOST` be a network address. |
| `REELAY_ALLOWED_HOSTS` | unset | Extra host names (comma-separated) the agent API answers to, besides `127.0.0.1`, `localhost` and the tunnel's public URL. |
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

**Approvals.** Anything that posts, archives, or changes many tickets at once **needs a person's approval**. The request is checked straight away (impossible ones get the usual 4xx), stored, and answered with `202` and an `approval_id`. Nothing happens until a person approves it on the Mac (`python -m reelay.approvals list | show | approve | reject` for now). The running app then carries it out. If the video changed in the meantime, it becomes `stale` and isn't done. Draft edits (`PATCH`, restore, fill, transcript, sources) apply at once but are rate-limited (`429`). See `docs/security.md`.

| Endpoint | What it does |
|---|---|
| `GET /v1/board?profile=…&stage=…&limit=…&offset=…` | A profile's board in order. Each video has:<br>• its copy: title, description, tags, target platforms, overrides;<br>• the channel, visibility, schedule and file path;<br>• per-platform status, and the original caption and stats;<br>• **`ready`** and **`readiness`** (a per-platform checklist with `state`: `ready`, `not_ready` or `not_checked`);<br>• **`ticket`** fields and **`provenance`**;<br>• **`publishing_history`** (automatic and manual posts per platform).<br>`profile` is the sidebar name (not case-sensitive). |
| `PATCH /v1/videos/{id}` | Partial edit of the ticket. Two kinds of field:<br>• **Copy:** `title`, `description`, `tags`, `target_platforms`, the five `*_text` fields and `hashtags`. Locked once the video is scheduled or published.<br>• **Ticket fields** (editable any time): `transcript`, `thumbnail_path`, `content_type`, `topic`, `keywords`, `creator`, `original_publish_date`, `source_platform`, `source_url`, `authorization` and `derived_from`.<br>Everything is checked before anything is saved. Over-limit text is rejected, never cut short. Each change is logged as a revision. |
| `POST /v1/videos/{id}/schedule?force=…` | **Needs approval (202).** Asks to schedule automatic posting through the same pipeline as the Board's button. Optional `scheduled_at` (ISO 8601 with a timezone, at least 10 minutes ahead) and `platforms` (each must be enabled). Returns the queued jobs and `manual_platforms`. If a platform isn't ready it returns `422 ticket_not_ready` with the problems; `force=true` schedules anyway. |
| `POST /v1/import?profile=…&dry_run=true` | Imports a sheet (applying it, `dry_run=false`, **needs approval**, 202): `{"csv": "…", "mapping": {header: field} (optional, guessed from headers), "overwrite": false}`. Returns a per-row report (`video_id`, `matched_by`, `fields`, `kept`, `problems`). It's a dry run unless `dry_run=false`; then every changing row is written in one transaction. |
| `POST /v1/videos/bulk-update` | **Needs approval (202).** `{"video_ids": […], "changes": {…same fields as PATCH…}}`. All or nothing when carried out; the result is `{"changed": n}`. |
| `POST /v1/videos/fill-from-original` | `{"video_ids": […]}`. Fills empty copy from the original captions, within limits. Returns the fields filled per video. |
| `POST /v1/videos/{id}/transcript` | Gets the transcript from the original post's captions (network). `{"replace": false}`. `422 no_transcript` if the post has none. |
| `POST /v1/videos/{id}/archive`, `…/unarchive` | **Needs approval (202).** Hides or restores a video. `GET /v1/board?archived=true` lists archived ones. `409` while it's scheduled or uploading. |
| `POST /v1/videos/{id}/revisions/{revision_id}/restore` | Puts a field back to its value before that change (logged as a new change). |
| `PATCH /v1/sources/{id}`, `DELETE /v1/sources/{id}` | Rename a saved source or mark it as yours; remove it (**needs approval**, 202). Connected channels are managed in the app. |
| `POST /v1/videos/{id}/platforms/{platform}` | **Needs approval (202).** Ticks a platform's tag: `{"state": "scheduled", "at": "…"}`, `{"state": "posted", "url": "…"}` or `{"state": "none"}`. A `url` must be on that platform's site (Prepare to Publish). The card then moves on its own. Videos carry `platform_states` and `coverage`, and `target_platforms` accepts all six platforms. |
| `GET /v1/search?profile=…&q=…` | Full-text search of the archive, including transcripts: results best first, each with `spoken` (found in what was said), a `snippet` with «matches», and the `video`. |
| `POST /v1/videos/{id}/publications/{platform}/resolve` | **Needs approval (202).** For an `interrupted` publication: `{"action": "retry" \| "posted" \| "cancel", "url": "…"}`. `409` if it isn't interrupted. |
| `GET /v1/videos/{id}/revisions` | The ticket's change log, newest first: `at`, `origin` (`local` = the app, `api` = an agent), `field`, `old_value` and `new_value`. |
| `POST /v1/videos/{id}/manual-posts` | **Needs approval (202).** Records a post made by hand: `{"platform": "tiktok", "posted_at": "…" (optional, not in the future), "url": "…", "note": "…"}`. It doesn't post anything or move the card. |
| `GET /v1/approvals?profile=…&status=…`, `GET /v1/approvals/{id}` | What agents asked for and what happened: `status` (`pending`, `approved`, `executed`, `failed`, `stale`, `rejected`, `expired`), `payload`, `result`. Read-only: agents can't approve. |
| `GET /v1/sources?profile=…` | The profile's download sources: connected YouTube channels (`kind: "account"`, with an `account` reference) and saved profile links (`kind: "saved"`, with `url`, `mine`, `last_fetched_at`). Duplicates are listed as stored. |
| `POST /v1/sources?profile=…` | Saves a TikTok, Instagram or YouTube profile link: `{"url": "…", "name": "…" (optional), "mine": false}`. Returns `201` with the new source; `422` for unsupported links. |
| `GET /v1/platforms?profile=…` | Per platform: `auto_post` (whether automatic posting is on), whether a credential is in the keychain (`connected`), account names, and `last_verified_at`. "Connected" means a credential is present, not that it worked recently. Secrets are never returned. |

Errors always come back as `{"detail": "…", "code": "…", "problems": […]}`:

| Status | Meaning |
|---|---|
| 400 | Missing or unknown profile |
| 401 | Missing or invalid token |
| 404 | Unknown video |
| 409 | Already scheduled or published, or not in a state that allows it (e.g. resolving a send that isn't interrupted) |
| 429 | `rate_limited` (too many draft edits too quickly) or `too_many_pending` (100 requests already waiting) |
| 422 | Invalid input; nothing was changed. `ticket_not_ready` means the ticket isn't ready for a platform being scheduled. Fix the listed problems, or repeat the request with `?force=true`. Other 422 codes can't be forced. |

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
- **Ticket not ready:** `422 {"detail": "This ticket isn't ready for Bluesky. Fix the problems listed, or schedule anyway with force.", "code": "ticket_not_ready", "problems": ["Bluesky: Write the Bluesky post (otherwise Reelay would fall back to the title)."]}`. Repeat with `?force=true` to schedule anyway.
- **Missing setup:** `422 invalid_post`, listing every problem, readiness included (e.g. `"Choose a YouTube channel to post to."`, `"YouTube: Add a description."`). `force` can't skip these.

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
| `agent_api_token` | Agent API bearer token (made on first run) | **Settings → AI agents → Make a New Key…** (copies the new one). Or delete the item (`security delete-generic-password -s Reelay -a agent_api_token`) and restart Reelay. `REELAY_API_TOKEN` overrides it. |
| `youtube:<channel id>` | Google OAuth tokens | **Accounts → Disconnect**, then **Sign in with Google**. |
| `bluesky:<did>` | Handle + App Password | Revoke the App Password in Bluesky, create a new one, then **Accounts → Connect Bluesky…** (replaces the old one). |
| `threads:<user id>` | Threads user access token | Generate a new long-lived token, then **Accounts → Connect Threads…**. |
| `facebook:<page id>` | Page ID + Page access token | Generate a new Page token, then **Accounts → Connect Facebook…**. |

**Check Connection** on each account sends a live check and updates `last_verified_at`. A successful post updates it too. Each **Connect…** guide checks the details with the platform before saving anything.

### Getting each platform's credentials (one-time manual steps)

- **YouTube:** the Google Cloud steps under *Connecting YouTube* above.
- **Bluesky:**
  1. In Bluesky, go to **Settings → Privacy and security → App passwords** and create one.
  2. In Reelay, click **Accounts → Connect Bluesky…** and enter your handle and the App Password. Don't use your main password.
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
python -m pytest -n auto                      # about 830 tests (pytest-xdist; drop -n auto to run serially):
                                              #   engine, database + migrations, pipeline, readiness,
                                              #   agent API + contract, dispatcher, platform publishers,
                                              #   YouTube client, headless GUI (incl. setup guides)
python scripts/smoke_test_api.py              # 49 end-to-end API checks in a throwaway data folder
python -m pytest --run-network -m network     # optional live checks against TikTok
python run_app.py --self-test                 # environment check (also works on packaged builds)
```

Tests never post to real platforms:
- **YouTube** uses a fake HTTP session and a real local sign-in server.
- **Bluesky, Threads and Facebook** check their exact request flows against scripted fake transports.
- **The agent API** runs on a real local server.
- **The GUI** is driven headlessly with fake downloads and uploads.

## Updating screenshots

To regenerate all application, Help Center, and marketing website screenshots after UI changes (using an isolated temporary `REELAY_HOME` and in-memory token store, never touching real data or credentials):

```bash
.venv/bin/python scripts/capture_screens.py --all        # full workflow: Help Center + website slots + 36 app route PNGs + website sync
.venv/bin/python scripts/capture_screens.py --routes-only # only docs/screenshots/app_routes/ (36 route PNGs)
.venv/bin/python scripts/capture_screens.py --help-shots  # only reelay/gui/help_shots/ (34 WebPs + marks.json)
```

## Building the apps

```bash
./scripts/build_local.sh          # one command: build, back up the database, install to /Applications, self-test
./packaging/build_mac.sh          # just dist/Reelay.app and dist/Reelay-<version>-mac-arm64.dmg
```
```powershell
.\packaging\build_windows.ps1     # dist\Reelay\Reelay.exe and a .zip (run on Windows)
```

`build_local.sh` works from a clean clone (it makes `.venv`), and the installed app needs none of this folder. PyInstaller can't cross-compile, so each platform builds on its own OS. GitHub Actions workflows exist but are switched off; builds run locally. Mac builds are signed with the keychain's Apple Development identity (ad-hoc when there's none), with the hardened runtime, but not notarized, so other Macs warn on first launch. [docs/release-signing.md](docs/release-signing.md) has the Developer ID and notarization checklist.

## Project layout

```
reelay/            engine: fetcher, downloader, naming, library/manifest, store (SQLite + change feed + FTS),
                   pipeline (every board/post/schedule/sync rule), uploads (app-wide upload queue),
                   scheduler (dispatcher + start-up reconciliation), publishers/ (Bluesky, Threads, Facebook),
                   platform_text, api (agent API), services (wiring), media (expiring links), youtube,
                   publishing, accounts + tokens (keychain), oauth (+ oauth_config), handoff, coverage,
                   rules (automations), assist + seo + vidiq_service (writing help), video_tools (ffmpeg),
                   dblocation (database location + lock), cli
server/            auth_broker: the small service that holds platform app secrets for central sign-in
api/openapi.yaml   agent API contract (generated)
reelay/gui/        PySide6 app: main_window (sidebar + profiles), download_page, board_page,
                   post_panel, accounts_page, settings_page, theme (Light/Dark/Black/Custom; template styles/app.qss), branding, inspector
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
- **Performance stats** (YouTube Analytics, Instagram and Threads insights) on the cards, using the analytics scopes central sign-in already requests.
- **Bluesky sign-in** with atproto OAuth (PAR + DPoP) instead of App Passwords.
- **TikTok and Instagram publishing**, once developer apps are approved.
- **Signed, notarized builds** with auto-update.

Only download and republish content you own or have permission to use.
