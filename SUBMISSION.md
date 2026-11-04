# Meta Global AI Developer Hackathon Submission: Reelay — Repurpose Engine

* **Hackathon:** Meta Global AI Developer Hackathon
* **Track:** Startup Track ("Build for Your World")
* **Representative:** Andrew Baxter (Solo Entry)
* **Project Title:** Reelay — Repurpose Engine
* **One-Liner:** Reelay turns one of your own videos into platform-ready copy for six networks with Meta Muse Spark, checked against platform limits and saved only after your approval.
* **Meta Model Used:** `muse-spark-1.3` via the Meta Model API (`https://api.meta.ai/v1`, Standard non-contributor tier)
* **Public Repository:** https://github.com/HeyThisIsAndrew/reelay
* **Official Hackathon Build Diff:** https://github.com/HeyThisIsAndrew/reelay/compare/pre-hackathon...hackathon-all-nov4

---

## 1. The Problem ("Build for Your World")

Recording a video finishes only part of a creator's publishing work. The same ideas still need different titles, descriptions, captions, and tags across six networks (YouTube, TikTok, Instagram, Bluesky, Threads, and Facebook), each with its own measurement rules: UTF-8 bytes for YouTube descriptions, graphemes for Bluesky, characters elsewhere. That means six rewrites under conflicting limits. Doing them by hand takes time, while generic AI copy loses the creator's voice, invents unsupported claims, or gets cut off mid-sentence.

---

## 2. Solution & Startup Thesis

Reelay brings Meta Muse Spark (`muse-spark-1.3`) directly into a local-first desktop video workflow for independent educators and technical creators.

* **The ticket is the unit of work.** Each video in Reelay already has a ticket with its title, description, notes, transcript, and the creator's brand voice.
* **`RepurposeDialog` is the human approval surface.** It shows a read-only Source Snapshot of exactly what will be sent, and a double approval gate: *Apply to Ticket* fills the ticket editor in memory, then *Save Changes* writes it to SQLite.
* **The same engine scales across a creator's entire video library**, one ticket at a time, always with the creator approving.

Reelay is a **local desktop workflow with optional, explicit cloud generation**. Ticket text is sent to Meta only after the creator ticks the Cloud AI consent box and clicks Generate Proposal. (Reelay's pre-existing optional YouTube SEO keyword ideas also use the network, only when the creator clicks "Get SEO Suggestions".)

---

## 3. What the Repurpose Engine Does

The **Repurpose Engine** (`reelay/repurpose_engine.py`) sends a ticket's title, description, notes, transcript, and brand voice to **`muse-spark-1.3`** through the Meta Model API (`https://api.meta.ai/v1/chat/completions`):

1. **Six-platform structured proposal (9 string fields):**
   * **YouTube:** `title` (≤ 100 chars), `description` (≤ 5,000 UTF-8 bytes), comma-separated `tags` (≤ 500 chars, no `#`).
   * **TikTok:** `tiktok_text` (≤ 4,000 chars).
   * **Instagram:** `instagram_text` (≤ 2,200 chars).
   * **Bluesky:** `bluesky_text` (≤ 300 graphemes).
   * **Threads:** `threads_text` (≤ 500 chars, with no hashtags in the body under Reelay's Threads editorial policy).
   * **Facebook:** `facebook_text` (≤ 63,206 chars).
   * **Shared hashtags:** `hashtags` (space-separated).
2. **Limit checks without truncation:**
   * Every field is validated against Reelay's configured platform-limit registry, `reelay.platform_specs.FIELD_LIMITS`.
   * Copy is never mechanically cut. If a field is over its limit, the engine asks Muse Spark for a targeted shorter rewrite of that field. If the rewrite is still over the limit, the engine stops with a clear validation error instead of returning over-limit or chopped copy.
3. **Threads editorial policy:** Any `#tags` in `threads_text` are moved automatically into the shared `hashtags` field, both after the first draft and again after any shorter rewrite.
4. **Voice and source safeguards:**
   * The creator's brand voice guidance is sent with every request.
   * The system prompt instructs `muse-spark-1.3` to rely only on the provided source fields and not to invent facts, quotes, products, people, or claims.
   * As a **prompt-isolation safeguard**, the transcript is wrapped in `<input_transcript>` tags, and any closing tag inside it is escaped (`</input_` → `< /input_`) so spoken text is framed as source data, not instructions. This reduces risk; it does not make hallucination or prompt injection impossible, which is why every proposal goes through human review before it is saved.
   * Copy only: no AI-generated images.
5. **Privacy and key storage (`reelay/privacy.py`, `reelay/tokens.py`):**
   * **Off by default:** the Cloud AI setting (`privacy.cloud_ai_opt_in`) starts off for every user, and generation is blocked until the creator opts in. In the dialog, an unchecked consent box always blocks generation, even if `REELAY_CLOUD_AI_OPT_IN=1` is set.
   * **Standard non-contributor tier:** requests go to `muse-spark-1.3` on `https://api.meta.ai/v1`. Per Meta's documentation for this tier, Standard-mode requests are not used to train Meta's models.
   * **OS keychain storage:** the API key is stored with Reelay's existing `KeyringTokenStore` (service `"Reelay"`, account `"META_API_KEY"`), set once through a no-echo prompt (`python -m reelay.repurpose_engine --setup`), and never written to `.env` files, repo files, or logs.
6. **Proposal only, with a double approval gate:**
   * `generate_repurpose_proposal` returns an in-memory proposal. It never writes ticket copy to SQLite, schedules, or publishes.
   * In the desktop app, **"Repurpose with Muse Spark"** opens `RepurposeDialog`: privacy disclosure, consent checkbox, read-only Source Snapshot, and 9 editable fields with live limit counters and `PASS` / `⚠ OVER LIMIT` badges. Generation runs on a background thread, so the dialog stays responsive and can be cancelled.
   * **Apply to Ticket** fills the ticket editor in memory, after checking the ticket is still the same one, unlocked, and unedited since the dialog opened. Nothing is saved until the creator clicks **Save Changes**. (Ticking the consent box saves only that preference.)

---

## 4. Pre-Existing Foundation vs. Hackathon Build

### Pre-existing Reelay foundation (not claimed as new)
Everything up to and including the `pre-hackathon` tag (`f93d360`): feature work through October 17, 2026, plus a screenshot and help-image refresh committed at 7:33 AM PT on November 4, before the first hackathon commit.
* Reelay PySide6 desktop application (`reelay/gui/`).
* Ticket model, SQLite store, and revision history (`reelay/models.py`, `reelay/store.py`, `reelay/pipeline.py`).
* Video downloading, inspection, and scheduling (`reelay/downloader.py`, `reelay/scheduler.py`).
* Platform-limit registry (`reelay/platform_specs.py`, `reelay/platform_text.py`) and OS keychain wrapper (`reelay/tokens.py`).
* YouTube SEO help (`reelay/seo.py`, the **"Get SEO Suggestions"** button): a local 0–100 title/tags/description checklist plus keyword ideas from YouTube search suggestions. Because the Muse Spark proposal lands in the same ticket editor, the creator can run this existing checklist on the new copy right away.
* Local loopback Agent API at `127.0.0.1:8471` (`reelay/api.py`, `reelay/scoped_pipeline.py`).

### Built on November 4, 2026 (`pre-hackathon...hackathon-all-nov4`)
The diff touches **10 files** (+2,472 lines): **9 net-new files** plus **1 minimally modified pre-existing file**.

Net-new:
* `reelay/repurpose_engine.py`: Meta Model API client for `muse-spark-1.3`, prompt construction and transcript isolation, Threads hashtag migration, `FIELD_LIMITS` validation with targeted rewrites (no truncation), keychain key lookup/setup CLI, and a review runner.
* `reelay/privacy.py`: Cloud AI opt-in gate (`is_cloud_ai_enabled`, `set_cloud_ai_enabled`, `require_cloud_ai_opt_in`) and `DISCLOSURE_TEXT`.
* `reelay/gui/repurpose_dialog.py`: The review dialog (disclosure, consent checkbox, Source Snapshot, background `CallWorker` lifecycle, 9 editable fields with live counters).
* `reelay/__main__.py`: Lets `python -m reelay` launch the desktop app.
* `reelay/REPURPOSE_README.md`: Module documentation, CLI reference, and approval workflow.
* `tests/test_repurpose_engine.py`: 18 tests (schema normalization, `FIELD_LIMITS` validation, rewrite-instead-of-truncate, Threads hashtag migration including after a rewrite, keychain fallback, hidden-prompt setup, transcript tag escaping).
* `tests/test_privacy.py`: 6 tests (off by default, blocked when disabled, blocked with no store/opt-in/env var, allowed after opt-in, environment override, disclosure completeness).
* `tests/test_repurpose_gui.py`: 12 tests (button present, `python -m reelay` entry point, disclosure and unchecked-consent blocking, Source Snapshot fidelity, non-blocking cancel, Apply disabled during regeneration, stale-ticket protection, SQLite check that nothing is saved before Save Changes, and edit/apply/regenerate after a background run completes).
* `SUBMISSION.md`: This write-up.

Modified:
* `reelay/gui/post_panel.py` (+58 lines): only adds the **"Repurpose with Muse Spark"** button and its `_repurpose()` handler, which applies an accepted proposal to the in-memory editor with stale-ticket protection and never auto-saves.

**Total: 36 unit tests**, no API key or network required.

---

## 5. Dependencies

The engine uses `requests` for HTTP. `requests` is **not listed directly** in `pyproject.toml` or `requirements.txt`; it is installed indirectly through `yt-dlp[default]`, which Reelay already depends on. Reelay's pre-existing `reelay/seo.py` relies on it the same way.

---

## 6. Verification & Commands

```bash
# 1. One-time keychain setup (hidden no-echo prompt)
./.venv/bin/python -m reelay.repurpose_engine --setup

# 2. Check the key is available without displaying it
./.venv/bin/python -m reelay.repurpose_engine --check

# 3. Launch the Reelay desktop app
./.venv/bin/python -m reelay

# 4. Live CLI proposal on one of your own videos
./.venv/bin/python -m reelay.repurpose_engine \
  --title "Building Local AI Desktop Tools on macOS" \
  --description "Walking through local-first media management and agent APIs." \
  --transcript "Hey everyone, today we are looking at how Reelay runs locally on Mac while using Meta Muse Spark for six-platform copy generation." \
  --brand-voice "Direct, technical builder tone; clear and grounded."

# 5. Run the hackathon tests (36 tests, no API key or network required)
./.venv/bin/pytest tests/test_repurpose_engine.py tests/test_privacy.py tests/test_repurpose_gui.py -v

# 6. Inspect the hackathon build diff (10 files)
git diff --stat pre-hackathon...hackathon-all-nov4
```
