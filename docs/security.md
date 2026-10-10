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
| A person deciding agent requests | Full authority | `reelay/approvals.py` (terminal now, the app later) |

## The boundary: `ScopedPipeline`

Every operation an external client can reach falls into one class:

| Class | Operations | What happens |
|---|---|---|
| **Read** | board, card, readiness, publishing history, platform states, archived list, sheet dry run, search, sources, approval status | Passed through |
| **Draft edit** | ticket copy and fields (`PATCH`), restore a revision, fill from original post, fetch transcript, add or rename a source | Allowed. Token bucket per profile (120 burst, 2/s; cost = videos touched, a transcript costs 5); at most 200 videos per call; `429 rate_limited` |
| **Needs approval** | schedule (incl. `force=true`), start an upload, archive, unarchive, bulk edit, apply a sheet import, mark a platform posted or scheduled, record a manual post, resolve an interrupted send, delete a source | **Never executed by the wrapper.** Returns `ApprovalPending` (REST: `202` with `approval_id`) |

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

Rules run with the person's authority, which could make them a way around approval: an agent edits a ticket's copy so it matches a rule that schedules. So a rule's **Schedule** action checks the ticket's latest copy edit, ignoring rules' own edits (logged as `automation`). If that edit came from an agent (`api` or `mcp`), the rule files an approval request (`requested_by = "automation: <rule>"`) instead of scheduling.

## Input handling (downloader and links)

| Item | Risk | Status |
|---|---|---|
| Output file names | Path traversal or reserved names from captions or custom names | **OK.** `naming.sanitize_stem` strips `/ \ : * ? " < > \|` and control characters, trims dots and spaces, caps the length and prefixes Windows reserved names. The folder is fixed via yt-dlp `paths.home`, and yt-dlp validates extensions |
| Final file path | yt-dlp reports `filepath` from its metadata; it was trusted | **Fixed.** `download_item` refuses a file outside the chosen folder (`urlsafety.inside`) |
| Agent-triggered fetches (transcripts) | yt-dlp's generic extractor fetches any http(s) address (SSRF to the local network); `file://` | **Fixed for agents.** Only TikTok, Instagram and YouTube hosts over http(s), with no credentials in the URL; yt-dlp limited to those extractors and `enable_file_urls=False`. The check covers the URL actually fetched, including the agent-editable `source_url` |
| Downloads | Not reachable from the API | **Keep it that way**, or approval-gate downloads in the MCP phase |
| Cover image path (`thumbnail_path`) | Any local path settable by an agent; would exfiltrate a file if covers were ever uploaded | **Fixed for agents.** Must be a .jpg, .png or .webp inside the profile's video folder (resolved, so `..` and symlinks can't escape) |
| Browser cookies | Transcript fetches use the profile's browser cookies (`cookiesfrombrowser`), so an agent-triggered fetch is an authenticated request to the platform | **Accepted, documented.** It's limited to the three platforms and rate-limited |
| Expiring media links (`/media/{token}`) | Serve only files Reelay issued a token for | OK |

## Authentication

- Bearer token, compared with `secrets.compare_digest`. It's stored in the system keychain (service "Reelay", account `agent_api_token`) or set with `REELAY_API_TOKEN`.
- The server binds to `127.0.0.1` by default, with no CORS.
- Secrets never appear in responses, logs or error messages, or in the database. Platform credentials live in the keychain.

## What code can't enforce

- **Processes on the same Mac with file access.** Anything that can read or write `~/Library/Application Support/Reelay/reelay.db` can bypass every check, including editing `approval_requests`. Agents must not be given shell or file access to Reelay's data folder. The boundary is the API surface, not the operating system.
- **Code in the same process.** `ScopedPipeline` keeps the raw pipeline in a private attribute. The static test makes sure no route uses it, but Python can't prevent in-process code from reaching it.
- **A stolen token.** Today's single agent token reaches every profile. The pairing design (per-client tokens, scopes, revocation, tunnel exposure) is in the AI connection proposal.

## Tests that guard this

- `tests/test_authorization.py`:
  - every route that changes state, over real HTTP with a spy pipeline: no mutating method runs, no table changes, every call answers 202;
  - a static check that `create_app` only uses the wrapper's public methods and the read-only store view (verified to catch an injected `pipeline._pipeline` call);
  - strict ids, profile scope, duplicates, the pending cap, rate limits, expiry, stale, the started marker, the dispatcher executing approved requests, the Automations guard, link and path rules, and the downloader confinement.
- `tests/test_api*.py`, `tests/test_workflow.py` and others follow requests through `approve_and_run`, as a person would.
