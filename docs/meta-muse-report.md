# Reelay v0.4.0: implementation report and local test handoff

**For:** Meta Muse  **From:** Claude Code  **Date:** 2026-10-07
**Repo:** `/Users/thisisandrew/Documents/UpSkill_Projects/Content_Downloader`, branch `main` at `2cd997f` (tag `v0.4.0`)
**Installed app:** `/Applications/Reelay.app`, v0.4.0, running now

---

## 1. Status

| Item | State |
|---|---|
| Ticket-first pivot (PR #2) | Merged to `main` (squash). CI passed on macOS, Windows and Linux. |
| Automated tests | **136 passed**, 2 skipped (opt-in live TikTok tests) |
| Packaged-app self-test | Passed (bundled ffmpeg, keychain, database, agent API server) |
| API smoke test (`scripts/smoke_test_api.py`) | **23/23 checks passed** against an isolated server |
| Local install | v0.4.0 built from the merged code (the Git trees are identical) and installed. The old app quit cleanly first. |
| Real database | Upgraded to schema v3 on launch. Profiles: "My Channel" and "Lydia Livingood" (active, 2 videos in Backlog). No posts or dispatches exist yet. |
| Publishing flags | All off (Bluesky, Threads, Facebook). YouTube is always on but has no channel connected. |
| GitHub release build for `v0.4.0` | Was still running when this report was written. |

---

## 2. What was implemented (against your pivot prompt)

| Requirement | Done | Where |
|---|---|---|
| 1. Flags `publish.bluesky`, `publish.threads`, `publish.facebook`, default OFF, env overrides `REELAY_PUBLISH_<PLATFORM>` | ✅ | `reelay/flags.py`. The env var wins (`1`/`true`/`on` → on; `0`/`false`/`off` → off; anything else is ignored). Then the setting `publish.<platform>` (Settings → Automatic posting), then off. YouTube is unchanged and always on. |
| 2. Gate publishers in Services; dispatcher harmless with an empty set; disabled platform → 422 "platform not enabled"; GUI hides disabled targets | ✅ | `reelay/services.py` builds publishers from `default_publishers()` filtered by the flags, read **once at startup**, so the dispatcher and scheduling always agree. The dispatcher only claims platforms it has a publisher for, so items for a disabled platform stay `pending`, untouched. API: `422 {"code": "platform_disabled"}`. GUI: the Auto-post checkboxes show only enabled platforms. |
| 3. Ticket UI: copy blocks with copy button, live count, reject over-limit on save | ✅ | `reelay/gui/post_panel.py`. Blocks: YouTube title/description/tags, TikTok, Instagram, Bluesky, Threads, Facebook, Hashtags. Counts use each platform's rules. Over-limit saves are refused with nothing written. The YouTube title field's `maxLength` was removed: it silently cut pasted text. |
| 4. Keep schema, publishers, tests, dispatcher, API, token, contract | ✅ | No files deleted, and no test removed. The only publisher change is a one-line Facebook edit (it uses `facebook_text` when set). |
| 5. README: flags, enabling, ticket workflow, Facebook native scheduling, Bluesky/Threads need Reelay awake | ✅ | README → "Automatic posting", "Board (content tickets)" and the API examples, re-run against the headless server. |
| 6. Tests for flags | ✅ | `tests/test_dispatcher.py`, `tests/test_api.py`, `tests/test_gui.py`, `tests/test_publishers.py`. 12 new tests. |

### Decisions taken with the user before building
1. **Schema v3 (additive only).** New columns `posts.tiktok_text`, `instagram_text`, `facebook_text`, `hashtags`. Your prompt said "keep the schema as-is", but the copy blocks need somewhere to live. Nothing existing changed. The v1 → v3 upgrade is tested and ran on the real database.
2. **The API exposes the new copy.** PATCH and `GET /v1/board` include `tiktok_text`, `instagram_text`, `facebook_text` and `hashtags`. `GET /v1/platforms` adds `auto_post`. `api/openapi.yaml` was regenerated, and the contract test passes.
3. **Disabled targets.** `POST /schedule` **without** `platforms` schedules only the card's enabled targets and returns the rest as `manual_platforms`. With an explicit disabled platform it returns `422 platform_disabled`. If no enabled target is left, it also returns `422 platform_disabled`.
4. **Hashtags** are one shared block per card.

### Limits enforced (validate, never truncate)
| Block | Rule |
|---|---|
| YouTube title | 1–100 characters, no `<` or `>` |
| YouTube description | ≤ 5,000 UTF-8 bytes, no `<` or `>` |
| YouTube tags | ≤ 500 characters in total |
| TikTok caption | ≤ 4,000 characters ⚠️ from model knowledge, not checked against current docs |
| Instagram caption | ≤ 2,200 characters and ≤ 30 hashtags ⚠️ same caveat |
| Bluesky text | ≤ 300 graphemes **and** ≤ 3,000 UTF-8 bytes (from the `app.bsky.feed.post` schema) |
| Threads text | ≤ 500 characters, with emoji counted as their UTF-8 byte length (from the Threads docs) |
| Facebook text | ≤ 63,206 characters ⚠️ same caveat |

---

## 3. Local environment facts

| What | Value |
|---|---|
| App | `/Applications/Reelay.app` (ad-hoc signed, arm64) |
| Database | `~/Library/Application Support/Reelay/reelay.db` (SQLite, schema 3) |
| Logs | `~/Library/Logs/Reelay/app.log` |
| Agent API | `http://127.0.0.1:8471` (loopback only), OpenAPI at `/openapi.json` |
| Agent token | macOS Keychain, service `Reelay`, account `agent_api_token`. Read it with `security find-generic-password -s Reelay -a agent_api_token -w` (macOS may ask once to allow access), or copy it from **Settings → Agent API → Copy Token**. |
| Lydia's library | `lydia_archive/` in the repo (2 downloaded TikToks with metadata) |
| Env vars | `REELAY_HOME`, `REELAY_HOST`, `REELAY_PORT`, `REELAY_API_TOKEN`, `REELAY_API_DISABLED`, `REELAY_PUBLIC_URL`, `REELAY_PUBLISH_BLUESKY` / `_THREADS` / `_FACEBOOK`, `REELAY_DISPATCH_INTERVAL`, `REELAY_DISPATCH_STALE_SECONDS`, `REELAY_BUNDLED_FFMPEG` |

---

## 4. Safety rules for testing

- **Prefer isolated runs.** `REELAY_HOME=<temp folder>` gives a separate database, logs and cache. `REELAY_API_TOKEN=<value>` keeps the keychain out of it. `REELAY_PORT=<port>` avoids clashing with the running app on 8471.
- **Writes to the real app change Lydia's real tickets.** `PATCH /v1/videos/{id}` against `127.0.0.1:8471` edits her cards. Only do read-only calls against the real instance unless the user says otherwise.
- **Nothing can post anywhere today.** No YouTube channel, Bluesky, Threads or Facebook account is connected, and the three flags are off. Don't connect real accounts or turn flags on as part of testing without the user's go-ahead: turning a flag on plus connecting an account makes posting real.
- **Don't run** `pytest --run-network`. It downloads from TikTok, which is fine but slow and unnecessary.

---

## 5. How to test

All commands run from the repo root.

### 5.1 Automated suite (about 30 seconds)
```bash
.venv/bin/python -m pytest -q
```
Expected: `136 passed, 2 skipped`.

### 5.2 Isolated API smoke test (about 20 seconds)
```bash
.venv/bin/python scripts/smoke_test_api.py
```
Expected: `23/23 checks passed`. It seeds a temporary home from `lydia_archive/`, starts `python -m reelay.api` on a free port with a throwaway token, and checks:
- **Auth:** 401 without a token or with a wrong one.
- **Board:** a missing profile gives 400; a valid one returns every copy block.
- **PATCH:** saves the copy blocks, rejects each over-limit case and an unknown field with 422, writes nothing when it rejects, and returns 404 for an unknown video.
- **Schedule:** a disabled platform gives 422 `platform_disabled`, a past time gives 422, and no YouTube channel gives 422 listing the problem.
- **Platforms:** only YouTube has `auto_post: true`, and no secrets appear.
- **Shutdown:** SIGINT stops it cleanly, the port is released, and the token never reaches the log.

### 5.3 Packaged-app self-test
```bash
REELAY_BUNDLED_FFMPEG=1 /Applications/Reelay.app/Contents/MacOS/Reelay --self-test
```
Expected: 7 PASS lines and `Self-test passed`.

### 5.4 GUI checklist (the running app, read-only)

| # | Action | Expected |
|---|---|---|
| G1 | Open Reelay | It opens on **Lydia Livingood**; the sidebar shows an "LL" avatar. |
| G2 | **Board** | Backlog has 2 cards: "all my designs are hand drawn…" (Sep 29, 0:08) and "the loneliness in the middle…" (Oct 2, 0:47). |
| G3 | Click a card | The right panel shows copy blocks: YouTube Title / Description / Tags, TikTok, Instagram, Bluesky, Threads, Facebook, Hashtags. Each has a counter and a **Copy** button. |
| G4 | Click **Copy** on an empty Bluesky block | The clipboard gets the title plus the description, if that fits in 300 characters. The panel says "Copied bluesky text (N characters)". |
| G5 | Type 301 characters in Bluesky text | The counter shows `301/300` in red. |
| G6 | Click **Save Draft** with an over-limit block | A red error lists the problem. The text stays as typed (not cut), and nothing is saved. Fix it and save: the error clears and the card moves to Staging. |
| G7 | **Auto-post** section | Only **YouTube** is offered. The note reads "Bluesky, Threads, Facebook: copy-paste only (automatic posting is off)." |
| G8 | **Settings → Automatic posting** | Three unchecked switches. Ticking one adds "(restart to apply)". **Untick it again** afterwards. |
| G9 | **Settings → Agent API** | "Running at http://127.0.0.1:8471…" and "Token is stored in your system keychain…". |
| G10 | **Accounts** | Bluesky, Threads and Facebook cards each say "Copy-paste only (turn on in Settings or REELAY_PUBLISH_…=1)". |
| G11 | Theme | **View → Theme** or Settings switches between Light, Dark and Black live. |

⚠️ Saving a draft in G6 writes a draft post for that card in the real database (it's the normal user flow). Do it only if the user is fine with Lydia's card holding test copy, or edit the text back afterwards.

### 5.5 Flag enablement (isolated, no posting possible)
```bash
REELAY_HOME=/tmp/reelay-flagtest REELAY_PORT=8482 REELAY_API_TOKEN=t REELAY_PUBLISH_BLUESKY=1 \
  /Applications/Reelay.app/Contents/MacOS/Reelay
```
Expected:
- **Log:** `/tmp/reelay-flagtest/logs/app.log` says "Automatic posting is off for: facebook, threads".
- **API:** `curl -s "http://127.0.0.1:8482/v1/platforms?profile=My%20Channel" -H "Authorization: Bearer t"` shows `bluesky.auto_post: true`.
- **GUI:** Settings shows the Bluesky switch as "(REELAY_PUBLISH_BLUESKY=1)", greyed out.
- **Close:** quit that window afterwards.

No account is connected in that home, so nothing can post.

### 5.6 Read-only checks against the real app
```bash
TOKEN=$(security find-generic-password -s Reelay -a agent_api_token -w)
```
```bash
curl -s "http://127.0.0.1:8471/v1/board?profile=Lydia%20Livingood" -H "Authorization: Bearer $TOKEN"
```
```bash
curl -s "http://127.0.0.1:8471/v1/platforms?profile=Lydia%20Livingood" -H "Authorization: Bearer $TOKEN"
```
Expected: board `total: 2`, both `stage: "backlog"`, `draft_saved: false`, every override `null`. Platforms: YouTube `auto_post: true`, the others `false`, all `connected: false`.

---

## 6. Error catalogue (all bodies are `{"detail", "code", "problems"?}`)

| Situation | Status | `code` |
|---|---|---|
| Missing or wrong bearer token | 401 | `unauthorized` |
| Missing or unknown `profile` | 400 | `profile_required` / `unknown_profile` |
| Unknown video id | 404 | `not_found` |
| PATCH unknown field, wrong type, empty body | 422 | `invalid_request` |
| PATCH over a platform limit | 422 | `invalid_post` (`problems` lists each) |
| PATCH a scheduled or published video | 409 | `locked` |
| Schedule a disabled platform, or no enabled targets left | 422 | `platform_disabled` |
| Schedule with a past, naive or too-soon (< 10 min) time | 422 | `invalid_time` / `invalid_request` / `invalid_post` |
| Schedule when setup is missing (channel, file, credentials) | 422 | `invalid_post` |
| Schedule a platform that's already scheduled or published | 409 | `already_scheduled` |

---

## 7. Not verified / known limitations

1. **No real platform post has ever been made.** YouTube, Bluesky, Threads and Facebook publishers are verified only against fakes and the platforms' documented flows.
2. **TikTok, Instagram and Facebook copy limits** come from model knowledge, not checked against current platform docs.
3. **The GitHub `v0.4.0` release build** (`.dmg` and Windows `.exe`) was still running when this report was written.
4. **The local app is ad-hoc signed.** macOS may ask again for Keychain access after each rebuild.
5. **Flags apply only after a restart** (by design: the dispatcher and scheduling must agree).
6. **Threads user tokens expire after 60 days** and aren't refreshed automatically yet.
7. **Auto-merge is disabled** in the GitHub repository settings, so PRs are merged by hand.

## 8. Suggestions for the next prompt

- **Per-platform "Mark as posted"** on a card (TikTok, Instagram and so on). Copy-paste is now the main workflow, so this is how the board tracks what went out where. It adds no background machinery.
- **Checking the three copy limits** marked ⚠️ against current docs, or the user's Google Sheet.
- **A one-time import from the Google Sheet** (CSV export, mapped to copy blocks per video) to finish replacing the Sheets workflow.
