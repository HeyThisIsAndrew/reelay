# Reelay security model

Status: v0.12 (schema v9). This covers the agent boundary, the approval queue, input handling, and what the code
can't enforce. It applies to every external client: the REST agent API today, and the MCP server and cloud AI
connections that follow.

## Who is trusted

| Actor | Trust | Path into Reelay |
|---|---|---|
| The person at the Mac (the desktop app) | Full authority: a click is the decision | `Pipeline` directly |
| Automations (rules the person made) | The person's authority, with one exception (see "Automations") | `Pipeline`, logged as `automation` |
| AI agents and any other external client | **Untrusted** | `ScopedPipeline` only (`reelay/scoped_pipeline.py`) |
| A person deciding agent requests | Full authority | `reelay/approvals.py` (the app's Approvals window, or the terminal) |

## The boundary: `ScopedPipeline`

Every operation an external client can reach falls into one class:

| Class | Operations | What happens |
|---|---|---|
| **Read** | board, card, readiness, publishing history, platform states, archived list, sheet dry run, search, sources, approval status | Passed through |
| **Draft edit** | ticket copy and fields (`PATCH`), restore a field's revision (same checks as `PATCH`), fill from original post, fetch transcript, add or rename a source | Allowed. Token bucket per profile (120 burst, 2/s; cost = videos touched, a transcript costs 5); at most 200 videos per call; `429 rate_limited` |
| **Needs approval** | schedule (incl. `force=true`), start an upload, archive, unarchive (also by restoring an `archived` revision: `202`), bulk edit, apply a sheet import, mark a platform posted or scheduled, record a manual post, resolve an interrupted send, delete a source | **Never executed by the wrapper.** Returns `ApprovalPending` (REST: `202` with `approval_id`) |

For approval-gated operations the wrapper:

1. validates every id strictly. An id must be an `int` (never a `bool` or `float`; Python treats `True == 1.0 == 1`, so types are checked before de-duplicating), positive, at most 2⁶³−1, an existing video, inside the client's profiles (`profile_ids`), and all ids in one request must belong to one profile;
2. runs the same checks the real operation would (for scheduling: `Pipeline.schedule_plan`, including the Ready gate), so an impossible request fails at once with the usual 4xx and never reaches a person;
3. stores the request in `approval_requests` with a **target hash** and returns.

The REST app (`reelay/api.py`) only receives the wrapper: inside `create_app` the names `pipeline` and `store` are rebound to the `ScopedPipeline` and a `ReadOnlyStore` view. That view exposes only read methods and `origin`; anything else is an `AttributeError`.

## The approval queue (`approval_requests`, schema v9)

States are enforced by a `CHECK` constraint and atomic conditional updates (`Store.transition_approval`):

```
pending → approved → executed | failed | stale
pending → rejected | expired | stale
```

- **Target hash:** SHA-256 of a canonical snapshot of every target video: post copy and targets, ticket fields, column, archived flag, dispatches, manual posts, and the size and modification time of the file that would be posted (including an edited copy from the Video tools). Notes and card position are excluded. Source deletions hash the source row; resolutions hash the dispatch.
- **Deciding** (`approvals.approve` / `reject`): only from this computer, never over the API. Approving recomputes the hash. Any difference makes the request `stale`, and nothing happens.
- **Carrying out:** the running app's dispatcher (`approvals.execute_approved`, every pass) re-checks the hash right before acting. It writes a `started_at` marker first, so a crash mid-way can never repeat the action (the next run marks it `failed` with an explanation). Actions run through a fixed table (`OPERATIONS`), so a tampered payload can't name another method. The revision log shows `approval` as the origin.
- **Expiry:** 24 h (`REELAY_APPROVAL_TTL_HOURS`), and a schedule request expires 10 minutes before its posting time.
- **Abuse limits:** an identical pending request is reused instead of duplicated, and each profile can have at most 100 pending requests (`429 too_many_pending`).
- **View-only computers** (a shared database opened by someone else) run no dispatcher and no API, so they never carry anything out.

## Automations

Rules run with the person's authority, which could make them a way around approval: an agent's text reaches the copy (directly, or through a field a rule's template uses) and a rule schedules it. Provenance is tracked **per field**, from the revision log, so it survives later rule runs and restarts:

- A field "came from an agent" when its newest revision's origin is an agent (`api`, `mcp`) or `automation:agent-input`.
- A rule's edit is logged as `automation:agent-input` when anything it used came from an agent: the template's placeholders that actually supplied text (`{title}`, `{hashtags}`, `{topic}`, `{creator}`; `{caption}` and `{hook}` are the original post), and the field's old value when it appends (Add hashtags). Otherwise it's plain `automation`.
- A person's later edit to that field, or an approved change (`approval`), clears it. An edit to a different field doesn't.

At the boundary, `Pipeline.schedule` refuses (`needs_approval`) any scheduling done under an `automation` origin while any copy field came from an agent; a rule's **Schedule** action files an approval request (`requested_by = "automation: <rule>"`) instead.

## Input handling (downloader and links)

| Item | Risk | Status |
|---|---|---|
| Output file names | Path traversal or reserved names from captions or custom names | **OK.** `naming.sanitize_stem` strips `/ \ : * ? " < > \|` and control characters, trims dots and spaces, caps the length and prefixes Windows reserved names. The folder is fixed via yt-dlp `paths.home`, and yt-dlp validates extensions |
| Final file path | yt-dlp reports `filepath` from its metadata; it was trusted | **Fixed.** `download_item` refuses a file outside the chosen folder (`urlsafety.inside`) |
| Every fetch (listing, details, download, products, transcripts) | A link from the database (e.g. an agent-edited `source_url` fetched by the app's own Get Transcript) reaching the local network or `file://` through yt-dlp | **Fixed.** `urlsafety.fetch_url_problem` runs before any network activity: http(s) only, a TikTok, Instagram, YouTube or Snapchat host, no user name, default ports, no spaces, backslashes or control characters; unparseable links are refused. The platforms' own redirectors (`l.instagram.com`, `lm.instagram.com`, `youtube.com/redirect`, `youtube.com/attribution_link`, `tiktok.com/link/`) are refused too. `ytdl.guarded_ydl` applies the same check to every link an extractor hands on (yt-dlp resolves them through `self.extract_info`; a bare id is only accepted for a named platform extractor, never the generic one) and keeps `file://` off. Playlist entries with other links are dropped. Reelay's own fetches load only the platform extractors (`urlsafety.app_ydl`); yt-dlp's generic extractor is added only for Instagram `/share/` links, TikTok `/photo/` posts and non-Spotlight Snapchat links. Agent fetches never get it (`restrict_ydl`) |
| Caption tracks and cover images | These live on CDNs with many names, so they can't be limited to the platform hosts | **Fixed, with a residual.** `urlsafety.asset_url_problem`: http(s), no user name, and the host read the way the system reads it: numeric forms (`127.1`, `2130706433`, `0x7f000001`, IPv4-mapped IPv6) count as the address they stand for, and a name is looked up and every address it points to must be public; a name that can't be looked up is refused |
| Agent-triggered fetches (transcripts) | yt-dlp's generic extractor fetches any http(s) address (SSRF to the local network); `file://` | **Fixed for agents.** Only TikTok, Instagram and YouTube hosts over http(s), with no credentials in the URL; yt-dlp limited to those extractors and `enable_file_urls=False`. The check covers the URL actually fetched, including the agent-editable `source_url` |
| Downloads | Not reachable from the API | **Keep it that way**, or approval-gate downloads in the MCP phase |
| Cover image path (`thumbnail_path`) | Any local path settable by an agent; would exfiltrate a file if covers were ever uploaded | **Fixed for agents.** Must be a .jpg, .png or .webp inside the profile's video folder (resolved, so `..` and symlinks can't escape) |
| Browser cookies | Transcript fetches use the profile's browser cookies (`cookiesfrombrowser`), so an agent-triggered fetch is an authenticated request to the platform | **Accepted, documented.** It's limited to the three platforms and rate-limited |
| Files posted or served (`Pipeline.video_path`, `/media/{token}`) | The database (possibly a shared or synced copy) decides which file is uploaded or served publicly: the edited copy, `item.folder`, `item.filename` | **Fixed.** `urlsafety.media_file_problem`: after resolving links and `..`, the file must be inside the profile's folder or the folder the video was downloaded to, have a video extension and start like an MP4/QuickTime or WebM/Matroska file; otherwise it counts as missing. Media links are only issued for such files and stay bound to that exact file (resolved path plus device and inode): a swapped file or symlink makes the link 404 |
| Database migrations | `executescript` committed each statement, so a failed migration left a half-made schema that blocked every later launch | **Fixed.** One `BEGIN IMMEDIATE` transaction per migration, with its `user_version`; statements split with `sqlite3.complete_statement` (trigger bodies intact); the version is re-read inside the lock; a failure rolls back and closes the connection. Before the first migration of an existing database, a consistent copy is made with SQLite's backup API next to it (`<name>-backup-v<N>-<time>.db`, never overwriting) |

## Authentication

- Bearer token, compared with `secrets.compare_digest`. It's stored in the system keychain (service "Reelay", account `agent_api_token`) or set with `REELAY_API_TOKEN`.
- The server binds to `127.0.0.1` by default, with no CORS. `REELAY_HOST` is only honoured for a loopback address unless `REELAY_ALLOW_NETWORK=1` is also set (a warning is logged); otherwise it falls back to `127.0.0.1` (`api.safe_bind_host`).
- **Host check** (`create_app` middleware): every request, `/media` included, must be addressed to `127.0.0.1`, `localhost` or `::1`, to the public URL from Settings (the tunnel), or to a name in `REELAY_ALLOWED_HOSTS`; when listening on the network (opted in) a bare IP address is accepted too, for phones. Anything else gets `400 bad_host` before any route runs. This defeats DNS rebinding (a web page pointing its own domain at 127.0.0.1 sends that domain as Host). The token is still required on every `/v1` route; `/media/{token}` is GET/HEAD only.
- **Private files** (`paths.make_private`): on macOS and Linux the data folder (database, caches, logs, backups) is `0700` and the database and its WAL files `0600`, applied at every start, so existing installs are fixed in place. Synced folders (Google Drive, Dropbox…) and Windows are left alone; failures are logged, never fatal.
- Secrets never appear in responses, logs or error messages, or in the database. Platform credentials live in the keychain.

## Signing in to platforms

- **Loopback receiver** (`reelay/loopback.py`), used by Google and TikTok sign-in and by the Meta bounce:
  127.0.0.1 only, on a random free port; only the registered path with the exact `state` (constant-time
  compare) and a local `Host` header count, so another program or a web page (DNS rebinding) can't end or
  spoof a sign-in; it stops listening right after the real redirect; responses are `no-store` /
  `no-referrer` and never echo the code; URLs are never logged.
- **Meta (Instagram, Threads, Facebook):** the app secret stays on the sign-in server (broker). The state is
  `<port>.<random>`; the broker exchanges the code, sends the browser back to `127.0.0.1:<port>/callback` with
  a one-time key, and the app claims the tokens once with state + key (`POST /v1/oauth/session/claim`).
  There is no "poll by state" route: the state travels through the browser and the platform, so it's never
  enough on its own. A callback whose state doesn't lead back to an app on this computer is refused before
  the code is exchanged, so no tokens are made that nobody can safely claim; a refused or failed sign-in
  leaves nothing to claim.
- **Google:** PKCE on the device; the client ID ships as a placeholder until Reelay's Google app is approved,
  and sign-in says so instead of failing at Google. Tokens record which Google app issued them and only
  refresh with that app.
- **Disconnect** revokes Reelay-app access where the platform allows (Google, TikTok through the broker,
  Facebook) and deletes the keychain entry.

## Remaining gaps: reassessed (2026-10-11)

| Gap | Realistic attack | Severity | Status | Before a public release? |
|---|---|---|---|---|
| **DNS rebinding on cover and caption hosts** | A cover or caption link (from platform metadata or an edited database) names a host that first resolves to a public address, then to 127.0.0.1 or a LAN device, so the download reaches something private. | Low–medium | **Fixed.** `safefetch.fetch_public` looks the name up once per hop, requires every address to be public, connects to that same address (TLS still verifies the name's certificate), and re-checks each redirect (at most 3). Covers always use it; caption files on CDNs use it; caption files on the platforms' own sites (YouTube timedtext) go through yt-dlp, whose DNS is the platform's. Tests: one lookup, the checked address used, private answers never connected, redirects to private/numeric/`file://` targets refused, TLS name kept. | Done |
| **The database choosing the folders** | Someone who can edit a shared (Google Drive) or restored database points a profile or video at another folder on your Mac, schedules it, and your Mac uploads a private video from there to your own account or serves it through a public media link. | Medium | **Fixed, with one residual.** Reelay only posts and serves from folders this computer trusts (`trusted_folders.py`, kept in the private data folder, never synced): a folder chosen in the app (new profile, Download Settings → Where videos go), or the folders a database names the first time this computer opens that database file. Later edits to the database add nothing; a refusal says which folder and how to choose it. **Residual:** a database that was already tampered with before this computer first opened it (or first ran this version) is trusted. | Done |
| **Redirects inside a platform request** | A platform's own server (or an open redirector an extractor itself requests) answers with a redirect to an internal address; yt-dlp follows it and sends one request there. Nothing comes back to the attacker except side effects of that one request. Agents can't trigger it with a link: the first link, handed-on links, the known redirector links and the generic extractor are all restricted. | Low | **Not fixed.** yt-dlp follows redirects inside its network handlers (urllib, requests, and curl_cffi, where libcurl does it in C) with no supported hook; patching each handler's internals would break with yt-dlp updates. Covers and CDN caption files (the fetches that use other hosts) are covered by the pinned download above. | No: it needs a platform's server to misbehave |
| **A file swapped after it was checked** | A process that can write to your video folder swaps a checked video for another file (or a symlink to one) in the moment before the upload or `/media` response opens it. | Low | **Not fixed.** Anything with that access can already change the database, the settings or the video itself (see below), so closing the window would mean passing open files through every publisher for little gain. Media links are bound to the file's identity and stop working when it changes. | No: outside the threat model |

Also accepted: **any person's edit counts as review.** After an agent writes a field, any edit to that field in the app (even one character) makes it the person's again, and an approved change counts as the person's.

## Untrusted text and AI (SEC-08)

Captions, transcripts, product titles and creator names come from the internet. In the API they're labelled (`x-reelay-untrusted: true`, and "Untrusted: …" in the description) so agents treat them as data. The local writing helper (`assist.py`) puts every instruction in the system prompt (the person's brand voice and Reelay's fixed editing rules) and the caption alone in the user message between `<caption>` markers it can't close early. Neither of those is the security boundary: the model's reply is only ever a suggestion string, shown to a person, who clicks Use This to put it in the editor; the helper imports nothing that can save, schedule or approve (static test). Automations and approvals don't read model output at all.

## What code can't enforce

- **Processes on the same Mac with file access.** Anything that can read or write `~/Library/Application Support/Reelay/reelay.db` can bypass every check, including editing `approval_requests`. Agents must not be given shell or file access to Reelay's data folder. The boundary is the API surface, not the operating system.
- **Code in the same process.** `ScopedPipeline` keeps the raw pipeline in a private attribute. The static test makes sure no route uses it, but Python can't prevent in-process code from reaching it.
- **A stolen token.** Today's single agent token reaches every profile. The pairing design (per-client tokens, scopes, revocation, tunnel exposure) is in the AI connection proposal.

## Tests that guard this

- `tests/test_authorization.py`:
  - every route that changes state, over real HTTP with a spy pipeline: no mutating method runs, no table changes, every call answers 202;
  - a static check that `create_app` only uses the wrapper's public methods and the read-only store view (verified to catch an injected `pipeline._pipeline` call);
  - strict ids, profile scope, duplicates, the pending cap, rate limits, expiry, stale, the started marker, the dispatcher executing approved requests, the Automations guard, link and path rules, and the downloader confinement.
- `tests/test_security_remediation.py` (with the network disabled: name lookups and connections fail the test): revision restores (archive state needs approval; fields restore with `PATCH`'s checks; the REST status codes), agent provenance through rule templates and across separate rule runs, the scheduling boundary, link rules at every fetch entry point (refused before yt-dlp starts, with the network disabled), the installed yt-dlp's behaviour, media file confinement (custom folders, `..`, symlinks, non-videos, swapped files), atomic migrations from every older schema (including a failure inside the real search-index migration, recovery, and a backup that includes changes still in the WAL), the unmark-posted locking fix, numeric and looked-up hosts for covers and captions, platform redirector links, which link formats get the generic extractor, agent writes tagged even when the caller forgets, a static check that every API write runs as the agent, and publishing refusing a file outside the folders.
- `tests/test_security_remediation.py` also covers the folder trust list (database-only folders refused with a reason, first-open trust, private and fail-closed list) and the pinned download (one lookup, checked address, redirects, TLS name); `tests/test_api.py` starts real servers for the network opt-in (listening address, reachability on this Mac's LAN address, Host and token still enforced) and checks web pages get no CORS access; `tests/test_release_signing.py` runs the build script's signing preflight.
- `tests/test_security_phase2.py`: private files and folders (and what's left alone), untrusted text kept out of the instructions, model output only a suggestion, the AI helper's imports, the API's untrusted labels, and no raw database connection outside the store. `tests/test_api.py` covers the bind and Host rules; `tests/test_oauth.py` covers the sign-in server (no polling route, nothing exchanged for a foreign state, nothing to claim after a refusal).
- `tests/test_api*.py`, `tests/test_workflow.py` and others follow requests through `approve_and_run`, as a person would.
