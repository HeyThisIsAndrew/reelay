# Reelay v0.5.0: implementation report and local test handoff

**For:** Meta Muse  **From:** Claude Code  **Date:** 2026-10-07
**Repo:** `/Users/thisisandrew/Documents/UpSkill_Projects/Content_Downloader`, single branch `main` (all PRs merged, all other branches deleted)
**Installed app:** `/Applications/Reelay.app`, v0.5.0, running now

---

## 1. Status

| Item | State |
|---|---|
| Branches | **One branch: `main`.** PRs #1–#4 are merged; `test-handoff` and `download-sources` are deleted locally and on GitHub. No open PRs. |
| CI | PRs #3 and #4 passed tests on macOS, Windows and Linux before merging. |
| Automated tests on `main` | **147 passed**, 2 skipped (opt-in live TikTok tests) |
| API smoke test (`scripts/smoke_test_api.py`) | **28/28 checks passed** against an isolated server |
| Packaged-app self-test | Passed |
| Local install | v0.5.0 built from `main` and installed. The old app quit cleanly; one instance is running. The API reports v0.5.0 at `127.0.0.1:8471`. |
| Real database | Upgraded to schema v4. Profiles: "My Channel" and **"Lydia Livingood" (active)**, which has 2 downloaded videos in Backlog and 1 saved source: "Lydia's TikTok" (`https://www.tiktok.com/@livingood.lifestyle`, marked mine). No posts, no dispatches, no connected accounts. |
| Publishing flags | Bluesky, Threads and Facebook are off. YouTube is always on, but no channel is connected. |

---

## 2. What's in the build (by release)

**v0.5.0: download sources (PR #4)**
- **Saved sources** per profile (new `sources` table, schema v4): name, platform (TikTok, Instagram, YouTube), URL, "mine".
  - **Manage them** from the Download page: **Save as Source…**, or the **⋯** menu on a source (Rename, mark as mine, Remove).
  - **One-click Fetch** runs the original fetch-and-choose flow and records `last_fetched_at`.
  - **Duplicates are allowed.** The Download page merges them by normalized URL; the first-saved name wins.
- **Connected YouTube channels** are sources too. They're listed through the YouTube Data API (uploads playlist, paginated), with no link needed. Private and scheduled videos are shown, labeled "Private on YouTube" / "Scheduled on YouTube", and left unticked: downloading them needs Login cookies from the owner's browser.
- **TikTok and Instagram own accounts** are saved sources marked mine. Accounts stays about publishing only.
- **The Download page shows two doors.** **Download from** lists the sources (accounts first), then the paste field, labeled "Paste any public profile link, yours or someone else's", with Save as Source and Fetch Videos. A note explains that downloading pulls videos in, while posting pushes them out to accounts connected under Accounts.
- **API:** `GET /v1/sources` and `POST /v1/sources` (201). The contract was regenerated and the contract test extended; existing endpoints are unchanged.

**v0.4.0: ticket-first (PR #2)**
- **Copy blocks** on each card: YouTube title, description and tags; TikTok, Instagram, Bluesky, Threads and Facebook copy; shared hashtags. Each has Copy and a live count by that platform's rules, and over-limit text is rejected, never truncated.
- **Auto-posting** to Bluesky, Threads and Facebook is behind flags (`REELAY_PUBLISH_<PLATFORM>=1` or Settings → Automatic posting; default off). Scheduling a disabled platform returns `422 platform_disabled`.

**v0.3.0: agent API and dispatcher (PR #1)**
- The **FastAPI agent API** (bearer token in the Keychain), the **multi-platform dispatcher**, and the **Bluesky, Threads and Facebook publishers** (dormant).

**Smoke test and this report (PR #3)**, extended for v0.5.0 in a direct commit to `main`.

---

## 3. Local environment facts

| What | Value |
|---|---|
| App | `/Applications/Reelay.app` (ad-hoc signed, arm64) |
| Database | `~/Library/Application Support/Reelay/reelay.db` (SQLite, schema 4) |
| Logs | `~/Library/Logs/Reelay/app.log` |
| Agent API | `http://127.0.0.1:8471` (loopback only); OpenAPI at `/openapi.json` |
| Agent token | Keychain, service `Reelay`, account `agent_api_token`. Read it with `security find-generic-password -s Reelay -a agent_api_token -w` (macOS may ask once), or **Settings → Agent API → Copy Token**. |
| Lydia's library folder | `lydia_archive/` in the repo |
| Env vars | `REELAY_HOME`, `REELAY_HOST`, `REELAY_PORT`, `REELAY_API_TOKEN`, `REELAY_API_DISABLED`, `REELAY_PUBLIC_URL`, `REELAY_PUBLISH_BLUESKY` / `_THREADS` / `_FACEBOOK`, `REELAY_DISPATCH_INTERVAL`, `REELAY_DISPATCH_STALE_SECONDS`, `REELAY_BUNDLED_FFMPEG` |

---

## 4. Safety rules for testing

- **Prefer isolated runs.** Use `REELAY_HOME=<temp folder>`, `REELAY_API_TOKEN=<value>` and `REELAY_PORT=<other port>`; the smoke test does all three.
- **Writes against the real app change Lydia's real data.** Against `127.0.0.1:8471`, `PATCH /v1/videos/{id}` edits her tickets and `POST /v1/sources` adds to her sources. Do only read-only calls there unless the user approves.
- **Nothing can post anywhere:** no accounts are connected and the flags are off. Don't connect real accounts or turn flags on without the user's go-ahead.
- **Fetching on the real app is safe but uses the network.** The "Lydia's TikTok" Fetch button lists her ~174 TikToks; the list is saved to her profile, and nothing downloads until **Download** is clicked.
- Don't run `pytest --run-network`.

---

## 5. How to test

All commands run from the repo root.

### 5.1 Automated suite (about 30 seconds)
```bash
.venv/bin/python -m pytest -q
```
Expected: `147 passed, 2 skipped`.

### 5.2 Isolated API smoke test (about 20 seconds)
```bash
.venv/bin/python scripts/smoke_test_api.py
```
Expected: `28/28 checks passed`. It covers:
- **Auth:** 401 without a token or with a wrong one.
- **Board:** a missing profile gives 400; a valid one returns every copy block.
- **PATCH:** saves the copy blocks, rejects each over-limit case and an unknown field with 422 while writing nothing, and returns 404 for an unknown video.
- **Schedule:** rejections for a disabled platform, a past time, and no YouTube channel.
- **Sources:** none to start; POST saves one (201) with a default name; the same URL can be saved twice; an unsupported URL gives 422; GET lists both as stored.
- **Platforms:** only YouTube auto-posts, and no secrets appear.
- **Shutdown:** SIGINT stops it cleanly, the port is released, and the token never reaches the log.

### 5.3 Packaged-app self-test
```bash
REELAY_BUNDLED_FFMPEG=1 /Applications/Reelay.app/Contents/MacOS/Reelay --self-test
```
Expected: 7 PASS lines and `Self-test passed`.

### 5.4 GUI checklist (the running app)

| # | Action | Expected |
|---|---|---|
| G1 | Open Reelay | It opens on **Lydia Livingood** (an "LL" avatar). |
| G2 | **Download** page, top | The heading **Download from**, followed by the note "Downloading pulls videos in from these sources. Posting pushes videos out to the accounts you connect under Accounts." |
| G3 | Source chips | One chip: **Lydia's TikTok · TikTok · mine**, with **Fetch** and **⋯**. |
| G4 | Paste field | Labeled "Paste any public profile link, yours or someone else's (or several video links)", with **Save as Source…** and **Fetch Videos** beside it. |
| G5 | Click **Fetch** on Lydia's TikTok *(network; read-only)* | The paste field fills with her URL, the status shows fetching, and then about 174 videos are listed. The 2 already downloaded show "✓ Downloaded"; the rest are ticked "Ready". **Don't click Download** unless the user wants all of them. |
| G6 | **⋯ → Rename…** on the chip, then rename it back | The chip label updates straight away. |
| G7 | Paste `https://www.tiktok.com/@livingood.lifestyle/` and **Save as Source…** | It saves a duplicate, and the page still shows one chip (hover: "Saved 2 times"). Clean up with **⋯ → Remove**, which removes both copies; then re-add "Lydia's TikTok" (mine) to restore the starting state. |
| G8 | **Board** → click a card | The ticket editor shows copy blocks with counters and Copy buttons. Auto-post offers only YouTube, with the note "Bluesky, Threads, Facebook: copy-paste only". |
| G9 | Type 301 characters in Bluesky text and click **Save Draft** | The counter shows `301/300` in red, the error lists the problem, and nothing is saved or cut. |
| G10 | **Settings** | Under Automatic posting, three switches are off. Under Agent API: "Running at http://127.0.0.1:8471…". |
| G11 | **Accounts** | The YouTube setup steps, then Bluesky, Threads and Facebook cards saying "Copy-paste only (…)". |

⚠️ G6, G7 and G9 write to the real database: these are the normal user flows. Undo them as described, or skip them if the user prefers.

### 5.5 Isolated sources and flags (no posting possible)
```bash
REELAY_HOME=/tmp/reelay-test REELAY_PORT=8482 REELAY_API_TOKEN=t REELAY_PUBLISH_BLUESKY=1 \
  /Applications/Reelay.app/Contents/MacOS/Reelay
```
Then:
```bash
curl -s -X POST "http://127.0.0.1:8482/v1/sources?profile=My%20Channel" -H "Authorization: Bearer t" \
  -H "Content-Type: application/json" -d '{"url": "https://www.instagram.com/livingood.lifestyle/"}'
```
Expected: `201`, `"name": "@livingood.lifestyle on Instagram"`, and the chip appears on that window's Download page. `GET /v1/platforms?profile=My%20Channel` shows `bluesky.auto_post: true`. Quit that window afterwards.

### 5.6 Read-only checks against the real app
```bash
TOKEN=$(security find-generic-password -s Reelay -a agent_api_token -w)
```
```bash
curl -s "http://127.0.0.1:8471/v1/sources?profile=Lydia%20Livingood" -H "Authorization: Bearer $TOKEN"
```
```bash
curl -s "http://127.0.0.1:8471/v1/board?profile=Lydia%20Livingood" -H "Authorization: Bearer $TOKEN"
```
Expected:
- **Sources:** one saved source, "Lydia's TikTok", with `kind: "saved"`, `platform: "tiktok"` and `mine: true`.
- **Board:** `total: 2`, both in `backlog`.

---

## 6. Error catalogue (bodies are `{"detail", "code", "problems"?}`)

| Situation | Status | `code` |
|---|---|---|
| Missing or wrong bearer token | 401 | `unauthorized` |
| Missing or unknown `profile` | 400 | `profile_required` / `unknown_profile` |
| Unknown video id | 404 | `not_found` |
| PATCH unknown field, wrong type, empty body | 422 | `invalid_request` |
| PATCH over a platform limit | 422 | `invalid_post` |
| PATCH a scheduled or published video | 409 | `locked` |
| Schedule a disabled platform, or no enabled targets left | 422 | `platform_disabled` |
| Schedule with a past, naive or too-soon time | 422 | `invalid_time` / `invalid_request` / `invalid_post` |
| Schedule when setup is missing | 422 | `invalid_post` |
| Schedule an already scheduled or published platform | 409 | `already_scheduled` |
| POST /v1/sources with an unsupported URL | 422 | `invalid_source` |
| POST /v1/sources with an extra field or an over-long name | 422 | `invalid_request` |

---

## 7. Not verified / known limitations

1. **No real platform post** has ever been made: YouTube, Bluesky, Threads and Facebook are verified only against fakes.
2. **The YouTube channel source** has only run against a fake API session (no channel is connected). It costs about 2 quota units per 50 videos.
3. **The TikTok, Instagram and Facebook copy limits** come from model knowledge, not checked against current docs.
4. **There's no API endpoint to rename or delete a source** (the spec asked only for GET and POST). That's GUI only for now.
5. **A saved YouTube link that duplicates a connected channel** is merged into the channel's chip and can't be removed from the GUI (an edge case).
6. **The app is ad-hoc signed** (Keychain prompts may come back after rebuilds). **Flags apply after a restart.** **Threads tokens aren't auto-refreshed.**
7. **GitHub auto-merge is disabled** in the repository settings.

## 8. Suggestions for the next prompt

- **Per-platform "Mark as posted"** on cards, to track the manual copy-paste workflow.
- **`PATCH` / `DELETE /v1/sources/{id}`**, so agents can rename and remove sources.
- **A one-time import from the user's Google Sheet** (CSV) into the copy blocks.
- **Checking the three copy limits** marked as unverified.
