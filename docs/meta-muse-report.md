# Reelay v0.11.0: implementation report and agent handoff

**For:** Meta Muse  **From:** Claude Code  **Date:** 2026-10-10
**Repo:** `/Users/thisisandrew/Documents/UpSkill_Projects/Content_Downloader`, single branch `main`, tag `v0.11.0`
**Installed app:** `/Applications/Reelay.app`, v0.11.0

---

## v0.11.0 (2026-10-10): what changed for agents

| Item | State |
|---|---|
| Automated tests on `main` | **324 passed**, 2 skipped (opt-in live TikTok tests) |
| Packaged-app self-test | Passed: 9 checks, now including full-text search and caption burning |
| Real database | Migrated **v7 → v8** (full-text index + brand voices). Backup: `reelay-backup-v7-2026-10-10.db`. 176 videos indexed. |
| Publishing flags | Unchanged: Bluesky, Threads, Facebook off; no accounts connected. |

**New and changed API (all in `api/openapi.yaml`):**
- `GET /v1/search?profile=…&q=…`: full-text search over title, original caption, **transcript**, every platform's copy, tags, topic, keywords and notes. Each result has `spoken` (matched what was said), a `snippet` with «matches», and the full `video`. Write transcripts with `PATCH /v1/videos/{id}` `{"transcript": "…"}` to make spoken words searchable.
- Every video now has **`coverage`**: per platform `published | scheduled | ready | needs_work | not_applicable`, with a `reason` for needs_work. It's the same map as the cards.
- `POST /v1/videos/{id}/platforms/{platform}` accepts **`url`** (the live post's link; must be on that platform's site). This is how a Prepare to Publish hand-off is recorded.
- Publications can be **`interrupted`**: Reelay stopped mid-send (crash, power loss) or the time passed while it was closed. Nothing more is sent until someone decides. Resolve with `POST /v1/videos/{id}/publications/{platform}/resolve` `{"action": "retry" | "posted" | "cancel", "url": "…"}` (`409` if it isn't interrupted). **Don't choose `retry` without checking the platform first**: the whole point is to avoid double posts.

**New in the app (no API yet):**
- **Prepare to Publish** for TikTok and Instagram: checks, copies the caption, opens the uploader, shows the file in Finder, records the link.
- **Automations** page: when/if/then rules (templates, hashtags, platforms, column moves, schedule for the next weekday). Stored in settings `rules.<profile_id>`; they run when the Board loads. Agents editing tickets through the API won't trigger them until the Board next reloads.
- **Brand voice** (Settings): the local AI helper's system prompt. Table `brand_profiles`.
- **Video tools**: remove or replace the sound and burn in captions locally; the edited copy (`<name>.edited.mp4`) is what gets posted. The `file_path` in the API is still the original download.
- **Shared database**: Settings → Data can move the database into Google Drive, Dropbox or iCloud. A computer that opens it **view only** runs **no agent API** (and no dispatcher), so point agents at the editing computer.
- **Central sign-in** buttons (Phase 1) only appear once Reelay's own platform apps are registered and the auth broker is deployed; until then the bring-your-own setup is unchanged.


## Earlier report (v0.8.0)

## 1. Status

| Item | State |
|---|---|
| Branches | **One branch: `main`.** It has two new commits: `13b0bb6` (UX clarity pass) and `85ae4e9` (Ticket v2). The `v0.6.0` tag is pushed. |
| Automated tests on `main` | **231 passed**, 2 skipped (the opt-in live TikTok tests) |
| API smoke test (`scripts/smoke_test_api.py`) | **49/49 checks passed** against an isolated server |
| Packaged-app self-test | Passed: 8 checks, including the new offline Help page check |
| Local install | v0.6.0 built from `main`, installed and launched. The log shows no errors. |
| Agent API on the real app | v0.6.0 served it on 127.0.0.1:8471 once the prompt was allowed. v0.7.0 is a new build, so it's **waiting on the same macOS Keychain prompt again.** The rebuilt app has a new ad-hoc signature, so macOS asks once more before Reelay may read `agent_api_token`. After the user clicks **Always Allow**, the API comes up on `127.0.0.1:8471`. |
| Real database | Migrated **v4 → v5 → v6 → v7**. Backups are `reelay-backup-v4-2026-10-07.db`, `reelay-backup-v5-2026-10-07.db` and `reelay-backup-v6-2026-10-09.db` in `~/Library/Application Support/Reelay/`.<br>• **Default profile:** **My Channel** (id 1, empty) is now the active one.<br>• **"Lydia Livingood"** (id 2) is unchanged: 174 videos in Backlog, provenance backfilled, and the saved source "Lydia's TikTok".<br>• No posts, dispatches or connected accounts. |
| Publishing flags | Bluesky, Threads and Facebook are off. YouTube is always on; no channel is connected. |

---

## 2. What's in the build

**v0.8.0: platform tags, collapsible ticket, audit fixes**
- **Platform tags.** Each ticket has a row per platform (YouTube, TikTok, Instagram, Bluesky, Threads, Facebook), with a drawn logo and **Going** / **Scheduled** (asks when) / **Posted** boxes.
  - `target_platforms` now accepts all six. TikTok and Instagram are always hand-posted.
  - Hand marks are stored in `manual_posts.kind` (schema v7).
  - Cards move on their own: to Scheduled once every target is scheduled or live, to Published once every target is live. A hand-scheduled time passing counts. Unticking moves a card back; the timer only moves cards forward.
  - Platforms Reelay posts itself are read-only rows.
  - A card waiting on hand platforms stays in Scheduled even after Reelay's own posts go live.
- **Board cards show the logos:** faded = to do, solid = scheduled, green dot = live, red dot = failed. The card tooltip lists every platform's state.
- **Collapsible ticket.** PLATFORMS, then one section per platform's copy (logo, count / "empty" / "⚠ too long" in the header; only YouTube open at first; open state remembered), POST AUTOMATICALLY, Details and provenance, and WHERE IT'S BEEN POSTED.
- **API:** `POST /v1/videos/{id}/platforms/{platform}` (`scheduled` with `at`, `posted`, or `none`). Videos now include `platform_states`.
- **Download page (v0.7.1):**
  - explicit **Select All** (follows the search) and **Unselect All**;
  - newly listed videos start unselected;
  - selections survive reloads;
  - clearer tooltips (a test checks that a real hover shows the tip);
  - right-click → Select / Unselect / Open Original Post / Archive.
- **Audit fixes:**
  1. Facebook posts Reelay scheduled stayed "scheduled" forever, so their cards never reached Published. The dispatcher now marks them live once the time passes (Facebook schedules natively). YouTube is still confirmed by syncing with YouTube.
  2. Time-based moves ran only while the Board was open. They now run every minute in the dispatcher, app open or headless.
  3. Adding a platform to a published card didn't move it back. Re-evaluation now allows a backward move after any tag change.
  4. Collapsible headers and long platform rows made the ticket wider than its panel; they now wrap or clip.

**v0.7.0: the workflow items Meta Muse approved, plus small improvements**
- **Spreadsheet import (the priority item).**
  - **Where:** Board → **Import Sheet…** (also File → Import from Spreadsheet…, ⌘⇧I), and `POST /v1/import`.
  - **Input:** a CSV/TSV file, or a Google Sheets link shared as "Anyone with the link". The link is turned into its CSV export URL.
  - **Matching:** each row is matched to a video by link, video ID, file name or custom name.
  - **Mapping:** columns map automatically from their headers (about 70 aliases) and can be changed.
  - **Preview first:** shows each row's video, the fields it would set, the fields it keeps, and why a row is skipped.
  - **Rules:**
    - empty cells never clear anything;
    - fill-only by default, with an opt-in "replace";
    - over-limit or invalid rows are skipped whole, never truncated;
    - copy of scheduled or published videos is kept.
  - **One transaction**, with each change logged with origin `import` (`api` through the API). The API defaults to `dry_run=true`.
- **Fill from the original post.** Empty copy blocks are filled from the original caption, within each platform's limits. A block that doesn't fit stays empty. It's in the ticket, the right-click menu and the bulk panel; the API is `POST /v1/videos/fill-from-original`.
- **Transcripts from captions.** yt-dlp fetches creator subtitles first, then automatic captions, and the WebVTT is cleaned into plain text. It's in the ticket, the menu and the bulk panel; the API is `POST /v1/videos/{id}/transcript`, which returns `422 no_transcript` when a post has no captions. There's no local speech-to-text.
- **Bulk edit.** ⌘- or Shift-click cards in a column, and the side panel sets permission, type, topic, keywords, creator or hashtags on all of them in one transaction. The API is `POST /v1/videos/bulk-update`, all or nothing.
- **Archive** (schema v6, `archived_at`). Hides videos from the Board and the Download page, and they're never re-imported from the folder. Files, tickets and history are kept, and **Archived…** restores them. The API has `…/archive`, `…/unarchive` and `GET /v1/board?archived=true`. Scheduled videos can't be archived (409).
- **Undo from history.** **Restore the Earlier Value** in Change History, or `POST …/revisions/{id}/restore`. The restore is itself logged.
- **Ready filter.** Show: All / Ready / Not ready / Blocking <platform>, with "3 of 12" column counts.
- **Sources API:** `PATCH /v1/sources/{id}` (name, mine) and `DELETE /v1/sources/{id}` (204).
- **Small improvements:**
  - background-task errors now read in plain words;
  - import problems use the sheet's labels, not internal field names;
  - unsaved suggested drafts count as empty, so a sheet fills them.

**v0.6.0, Ticket v2 (`85ae4e9`)**
- **Schema v5 (additive).** New nullable per-video fields:
  - `transcript`, `thumbnail_path`, `content_type` (short / long-form / clip / other), `topic`, `keywords`, `creator`, `original_publish_date`;
  - provenance: `source_platform`, `source_url`, `imported_at`, `authorization`;
  - `derived_from` (a foreign key with `ON DELETE SET NULL`).

  The migration backfills provenance from data already recorded and never invents an authorization. Cards that were already Scheduled or Published get `ready_gate_exempt = 1`.
- **Readiness engine** (`reelay/readiness.py`). For each target platform, it checks that the platform's own copy is written, within that platform's limits, and that the video file is present.
  - Flag-off platforms come out as `not_checked` and count as ready.
  - The ticket is READY when any target platform is ready.
  - Board API videos carry `ready` and `readiness`.
- **Ready gate.**
  - `POST /v1/videos/{id}/schedule` returns `422 ticket_not_ready` with prefixed `problems` and writes nothing.
  - `?force=true` bypasses the gate, but never the hard platform rules: those still return `invalid_post`, together with any readiness problems.
  - On the Board, scheduling a ticket that isn't ready shows the checklist and retries with force only after an explicit Yes.
  - Grandfathered cards and their dispatches are untouched.
- **Revision log** (`revisions` table).
  - Written in the same SQLite transaction as each logical commit: a GUI field blur or save, an API PATCH, or an agent write. A logging failure rolls the write back.
  - Typing is never logged, and neither are publishing-state saves.
  - Readable through `GET /v1/videos/{id}/revisions` and the ticket's **Change History…**.
- **Unified publishing history.** Dispatches and the new `manual_posts` table are combined per platform. Add records with **Mark as Posted…** in the ticket or `POST /v1/videos/{id}/manual-posts` (201). It doesn't move the card.
- **PATCH** accepts the ticket fields. They stay editable after scheduling, while copy locks as before; `imported_at` can't be edited.
- **Provenance on import.** The Download note now says to import media you own or have permission to manage. Imports from the profile's own sources (marked mine, or a connected channel) record `authorization = "Own content"`; other imports leave it null.

**v0.6.0, UX clarity pass (`13b0bb6`)**
- **Plain wording everywhere.** Download is **Bring videos in**, with **List Videos**, **Save This Link…**, **Rename This Source…**, **Use browser sign-in**, **Rename Files…** and **Open Video List**.
- **Naming** uses a friendly picker with a live example. Patterns are under **Custom pattern…**.
- **The search box grows** to three lines, then scrolls.
- **Tooltips** on every control and section header. **Settings → Show helpful tips** (default on) turns them all off.
- **Guided account setup:**
  - **YouTube:** a five-step Google Cloud wizard (paste a client ID and secret, or load the JSON), then a **Sign in with Google** modal with progress, a way to reopen the page, and Cancel.
  - **Bluesky:** an App Password guide.
  - **Threads and Facebook:** own-Meta-app guides. Each verifies the details before saving.
  - **TikTok and Instagram:** wording that's truthful about posting.
- **Offline Help** is pinned in the sidebar (`reelay/gui/help.md`, bundled).
- **My Channel** (the lowest-id profile) is the default.

---

## 3. Local environment facts

| What | Value |
|---|---|
| App | `/Applications/Reelay.app` (ad-hoc signed, arm64) |
| Database | `~/Library/Application Support/Reelay/reelay.db` (SQLite, **schema 5**); v4 backup next to it |
| Logs | `~/Library/Logs/Reelay/app.log` |
| Agent API | `http://127.0.0.1:8471` (loopback only); OpenAPI at `/openapi.json` |
| Agent token | Keychain, service `Reelay`, account `agent_api_token`, or **Settings → AI agents → Copy Access Key** |
| Lydia's library folder | `lydia_archive/` in the repo |

---

## 4. Safety rules for testing

- **Prefer isolated runs.** Use `REELAY_HOME=<temp folder>`, `REELAY_API_TOKEN=<value>` and `REELAY_PORT=<other port>`; the smoke test does all three.
- **Writes against the real app change real data.** That includes PATCH, `manual-posts`, sources, and ticket fields edited in the GUI. Do only read-only calls there unless the user approves.
- **Nothing can post anywhere:** no accounts are connected and the flags are off. Don't connect real accounts or turn flags on without the user's go-ahead.
- Don't run `pytest --run-network`. Don't answer the Keychain prompt for the user.

---

## 5. How to test

All commands run from the repo root.

### 5.1 Automated suite (about 75 seconds)
```bash
.venv/bin/python -m pytest -q
```
Expected: `215 passed, 2 skipped`. `tests/test_workflow.py` (22 tests) covers v0.7.0:
- import: reading, mapping, matching, fill-only versus overwrite, invalid rows, scheduled copy, and the one-transaction rollback;
- Google Sheets links;
- fill-from-original and its limits;
- captions to text and fetching transcripts with a fake yt-dlp;
- bulk all-or-nothing;
- archive (including the Download page not re-importing archived videos);
- restore;
- every new API endpoint;
- GUI: the filter, multi-select bulk edit, the import dialog, the Archived dialog, and restore from history.

The v0.6.0 files:
- `tests/test_ticket_v2.py` (30 tests):
  - the v4→v5 migration, and that grandfathered cards keep dispatching;
  - readiness: the ready, not-ready and flag-off cases;
  - the 422 path, the `force` bypass, and that force can't skip platform rules;
  - revision logs and rollback on a log failure;
  - `derived_from` nulling;
  - Mark as Posted and the publishing history;
  - provenance;
  - the API;
  - GUI blur commits and the gate dialog;
  - the full 8-combination flags matrix.
- `tests/test_ux.py` (15 tests):
  - first launch;
  - a tooltip audit of every control;
  - the tips switch;
  - wording;
  - the naming picker and the search box;
  - the YouTube wizard and the Google sign-in modal (with cancel);
  - the Bluesky, Threads and Facebook guides;
  - Help.

### 5.2 Isolated API smoke test (about 20 seconds)
```bash
.venv/bin/python scripts/smoke_test_api.py
```
Expected: `47/47 checks passed`. New in v0.7.0: source PATCH/DELETE, an import dry run and then the real import, fill-from-original, bulk update, archive/unarchive with `?archived=true`, and restoring a revision. New in v0.6.0:
- the board's `ready`, `readiness` (flag-off platforms come back `not_checked`), `ticket` and `provenance`;
- PATCHing ticket fields, and a 422 for `imported_at`;
- revisions with `origin: "api"`;
- readiness problems listed in the schedule rejection;
- `force=true` still refused for a missing channel;
- `manual-posts` (201), shown in `publishing_history`.

### 5.3 Packaged-app self-test
```bash
/Applications/Reelay.app/Contents/MacOS/Reelay --self-test
```
Expected: 8 PASS lines (including `Help page`) and `Self-test passed`.

### 5.4 GUI checklist (the running app)

| # | Action | Expected |
|---|---|---|
| G1 | Open Reelay | It opens on **My Channel** (an "MC" avatar), with an empty Download page and no sources. |
| G2 | Switch to **Lydia Livingood** | The Download page lists 174 videos (2 downloaded), with the "Lydia's TikTok" chip showing **List Videos** and **⋯**. The Board shows the 2 downloaded ones. |
| G3 | Hover anything | A plain-language tip. **Settings → Show helpful tips** off makes them all stop. |
| G4 | **Download**, top | **Bring videos in**, with the note "Import media you own or have permission to manage…". |
| G5 | **Name files** | The choice reads "Platform, date and video ID", with "Example: TikTok_….mp4" under it. Choose **Custom pattern…** to see the pattern box. |
| G6 | **Board** → drag a Backlog card to Staging, then click it | The ticket opens with a **READY / NOT READY** badge and a ✓ / ⚠ line per target. Bluesky, Threads and Facebook say "posted by hand… not checked". |
| G7 | Clear the YouTube description | After a moment, the header says YouTube is blocking: "Add a description." |
| G8 | **Details and Provenance** | Shows the provenance (TikTok · original post · Imported …). Type a Topic and click away; **Change History…** shows it, newest first, by "You (Reelay app)". |
| G9 | **Mark as Posted…** → TikTok | **Where it's been posted** lists "TikTok: posted by hand · …", with **Remove**. |
| G10 | **Accounts** | **Set Up YouTube…** opens a 5-step guide. **Connect Bluesky…**, **Connect Threads…** and **Connect Facebook…** open guides. TikTok and Instagram say "Copy and paste" and explain why. |
| G11 | **Help** (bottom of the sidebar) | The full guide, offline. |

| G12 | Board → **Show: Not ready** | Only blocking tickets remain; the counts read "n of N". |
| G13 | ⌘-click both Backlog cards | The side panel turns into "2 tickets selected" with Quick fill and "Set on every selected ticket". |
| G14 | **Import Sheet…** → choose a CSV with a `Video ID` column | The preview lists each row; the button says "Import N Tickets". Cancel to write nothing. |
| G15 | Right-click a card | Fill, Get Transcript, Mark as Posted…, Change History…, Archive. |
| G16 | Open a ticket → PLATFORMS → tick TikTok **Going**, then **Posted** | The TikTok logo shows on the card. Once every Going platform is posted, the card moves to Published by itself. |
| G17 | Tick **Scheduled** on a platform | It asks when the post goes live; the card moves to Published within a minute of that time. |
| G18 | Click a copy section's name (e.g. TIKTOK) | It opens or closes; the header shows its count or "empty". |
| G19 | Download page after **List Videos** | Nothing is selected; **Select All** and **Unselect All** work, and hovering explains each control. |

⚠️ G6–G9 and G13–G15 (if confirmed) write to the real database (Lydia's tickets). They're normal user flows; undo or skip them as the user prefers.

### 5.5 Read-only checks against the real app (after the Keychain prompt is allowed)
```bash
TOKEN=$(security find-generic-password -s Reelay -a agent_api_token -w)
```
```bash
curl -s "http://127.0.0.1:8471/v1/board?profile=Lydia%20Livingood&limit=1" -H "Authorization: Bearer $TOKEN"
```
Expected:
- `total: 2`: only downloaded videos are on the board; the other 172 are on the Download page;
- `ready: true`;
- `readiness.youtube.state: "ready"` (title and description suggested from the caption, file present);
- `provenance.source_platform: "tiktok"`.

---

## 6. Error catalogue (bodies are `{"detail", "code", "problems"?}`)

| Situation | Status | `code` |
|---|---|---|
| Missing or wrong bearer token | 401 | `unauthorized` |
| Missing or unknown `profile` | 400 | `profile_required` / `unknown_profile` |
| Unknown video id | 404 | `not_found` |
| PATCH unknown field (including `imported_at`), wrong type, empty body | 422 | `invalid_request` |
| PATCH over a limit, bad `content_type` / date / `derived_from` | 422 | `invalid_post` |
| PATCH copy on a scheduled or published video (ticket fields are fine) | 409 | `locked` |
| **Schedule a platform whose ticket isn't ready** | **422** | **`ticket_not_ready`** (retry with `?force=true`) |
| Schedule a disabled platform, or no enabled targets left | 422 | `platform_disabled` |
| Schedule with a past, naive or too-soon time | 422 | `invalid_time` / `invalid_request` / `invalid_post` |
| Schedule when setup is missing (force can't skip it) | 422 | `invalid_post` |
| Schedule an already scheduled or published platform | 409 | `already_scheduled` |
| Mark as posted: unknown platform / future time / non-http URL | 422 | `invalid_request` / `invalid_time` / `invalid_post` |
| POST /v1/sources with an unsupported URL | 422 | `invalid_source` |
| Import CSV unreadable or empty / unknown mapping field | 422 | `invalid_sheet` / `invalid_request` |
| Bulk update where any video can't take a value | 422 | `invalid_post` (nothing written) |
| Transcript: the post has no captions / one exists and replace is false | 422 | `no_transcript` / `invalid_post` |
| Archive a scheduled or uploading video | 409 | `locked` |

---

## 7. Not verified / known limitations

1. **No real platform sign-in or post has been made.** Google, Bluesky and Meta sign-ins, and every publish, are verified only against fakes, because they need the user's own credentials.
2. **The Google Cloud console links** are current paths, but Google reorganizes that console often. The wizard text names each screen, so it still makes sense if a link moves.
3. **Readiness covers copy and media, not scheduling setup.** A connected account, a public URL and the schedule time are still checked at schedule time, as `invalid_post`.
4. **Deleting a video** exists in the store (`delete_video`) but has no GUI or API action yet; `derived_from` nulling is tested at the store level.
5. **TikTok, Instagram and Facebook copy limits** come from model knowledge and haven't been checked against current docs.
6. **Transcripts depend on the platform publishing captions**: there's no local speech-to-text. A Google Sheet must be link-shared; private sheets have to be downloaded as CSV.
7. **The app is ad-hoc signed**, so Keychain prompts return after every rebuild. Flags apply after a restart. Threads tokens aren't refreshed automatically.

## 8. Next

Meta Muse's next prompt: the **repurpose engine** (transcript → platform packages in the creator's voice). The ticket now has what it needs as input: a transcript (fetched from captions or imported), the original caption, topic and keywords, per-platform limits, readiness checks to validate output, and a revision log so generated copy is attributable and undoable.
