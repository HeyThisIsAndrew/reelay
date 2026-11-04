# Meta Global AI Developer Hackathon Submission: Reelay — Repurpose Engine

* **Hackathon:** Meta Global AI Developer Hackathon
* **Track:** Startup Track ("Build for Your World")
* **Representative:** Andrew Baxter (Solo Entry)
* **Project Title:** Reelay — Repurpose Engine
* **One-Liner:** Reelay turns one of your own videos into platform-ready copy across 6 networks using Meta Muse Spark, requiring your approval before landing in a ticket.
* **Meta Model Used:** `muse-spark-1.3` (Standard non-contributor tier via Meta Model API `https://api.meta.ai/v1`)
* **Public Repository:** https://github.com/HeyThisIsAndrew/reelay
* **Official Hackathon Build Diff:** https://github.com/HeyThisIsAndrew/reelay/compare/pre-hackathon...hackathon-all-nov4

---

## 1. The Problem ("Build for Your World")

Independent creators and solo founders record one original video, then face the tedious bottleneck of rewriting titles, descriptions, captions, and tags across six different networks—YouTube, TikTok, Instagram Reels, Bluesky, Threads, and Facebook. Each platform enforces strict, incompatible measurement rules (UTF-8 bytes on YouTube descriptions, 300 graphemes on Bluesky, zero hashtags in Threads body copy), and generic copy-pasting either sounds robotic, invents unsupported claims, or gets mechanically chopped off mid-sentence.

---

## 2. What Reelay's Repurpose Engine Does

Built on November 4, 2026 for the Meta Global AI Developer Hackathon, the **Repurpose Engine** (`reelay/repurpose_engine.py`) takes the title, description, notes, spoken transcript, and creator brand voice from one of Andrew's own videos and sends them to **`muse-spark-1.3`** via the official Meta Model API (`https://api.meta.ai/v1/chat/completions`):

1. **Six-Platform Structured Proposal (9 String Fields):**
   * **YouTube:** `title` ($\le 100$ chars), `description` ($\le 5,000$ UTF-8 bytes), and comma-separated `tags` ($\le 500$ tag chars, no `#`).
   * **TikTok:** `tiktok_text` ($\le 4,000$ chars).
   * **Instagram:** `instagram_text` ($\le 2,200$ chars).
   * **Bluesky:** `bluesky_text` ($\le 300$ graphemes).
   * **Threads:** `threads_text` ($\le 500$ chars, strictly **zero hashtags** in the body; any hashtags are automatically migrated to the shared `hashtags` block).
   * **Facebook:** `facebook_text` ($\le 63,206$ chars).
   * **Shared Hashtags:** `hashtags` (space-separated tags).
2. **Zero-Truncation Guarantee:**
   * Every field is validated against `reelay.platform_specs.FIELD_LIMITS`.
   * Copy is **never mechanically truncated**. If a generated field exceeds its platform limit, the engine rejects the over-limit draft and triggers a targeted refinement prompt asking Muse Spark to rewrite that specific field shorter within the exact limit.
3. **Creator Voice & Factuality Safeguards:**
   * Preserves Andrew's direct, technical builder voice using the profile's brand voice guidance.
   * System prompt constraints instruct `muse-spark-1.3` to rely strictly on the provided source fields rather than inventing unsupported facts, quotes, products, people, or claims, paired with mandatory human review before saving.
   * Untrusted transcript text is isolated inside `<input_transcript>` data markers with closing-tag escape sanitization (`</input_` &rarr; `< /input_`) to mitigate delimiter breakout and frame spoken text as source data rather than instructions.
   * Copy-only: zero AI-generated imagery or synthetic photos.
4. **Privacy & Local-First Security (`reelay/privacy.py` & `reelay/tokens.py`):**
   * **Opt-In by Default:** Cloud AI defaults to **OFF** (`False`) for every new user (`privacy.cloud_ai_opt_in`). No data leaves the Mac without explicit creator opt-in.
   * **Standard Non-Contributor Tier:** Uses `muse-spark-1.3` (standard tier, not a contributor model) so creator transcripts are not used to train Meta's models.
   * **OS Keychain Storage:** Reuses `reelay/tokens.py` (`KeyringTokenStore`, service `"Reelay"`, key `"META_API_KEY"`). The API key is set once via a no-echo prompt (`python -m reelay.repurpose_engine --setup`) and never stored in `.env` files, repo files, or logs.
5. **Proposal Only — Human Approval Gate (Desktop GUI & CLI):**
   * `generate_repurpose_proposal` returns an in-memory proposal dictionary only and never writes ticket copy to SQLite, schedules, or auto-publishes.
   * **Desktop GUI Integration (`reelay/gui/post_panel.py` & `reelay/gui/repurpose_dialog.py`):** Clicking **"Repurpose with Muse Spark"** on a ticket opens `RepurposeDialog`, displaying the privacy disclosure, an explicit opt-in checkbox, and a read-only **Source Snapshot** of the exact title, description, notes, transcript, and brand voice being sent. Toggling the opt-in checkbox persists only the `privacy.cloud_ai_opt_in` preference setting, while generated copy remains in memory: clicking **"Apply to Ticket"** populates the in-memory `PostPanel` editor blocks (after verifying the target ticket is unchanged and unlocked), requiring the creator to explicitly click **"Save Changes"** to persist ticket copy to SQLite.

---

## 3. Clear Separation of Pre-Existing vs. Hackathon-Built Work

### Pre-Existing Reelay Foundation (Oct 17, 2026 and earlier — Not Claimed as New)
* Reelay PySide6 macOS desktop application (`reelay/gui/`).
* Content ticket model, SQLite database, and revision history (`reelay/models.py`, `reelay/store.py`, `reelay/pipeline.py`).
* Video downloading, local inspection, and scheduling (`reelay/downloader.py`, `reelay/scheduler.py`).
* Platform limit registry (`reelay/platform_specs.py`, `reelay/platform_text.py`) and OS keychain wrapper (`reelay/tokens.py`).
* Local loopback Agent API at `127.0.0.1:8471` (`reelay/api.py`, `reelay/scoped_pipeline.py`).

### Built Exclusively During the Hackathon (November 4, 2026 — 10 Files in `pre-hackathon...hackathon-all-nov4`)
* `reelay/repurpose_engine.py`: Core Meta Model API (`muse-spark-1.3`) client, prompt isolation, transcript & brand voice injection, Threads hashtag sanitizer, zero-truncation `FIELD_LIMITS` validator/refiner, OS keychain lookup/setup CLI, and formatted human-review runner.
* `reelay/privacy.py`: Cloud AI opt-in gate (`is_cloud_ai_enabled`, `set_cloud_ai_enabled`, `require_cloud_ai_opt_in`) and transparent privacy disclosure (`DISCLOSURE_TEXT`).
* `reelay/gui/repurpose_dialog.py`: Native PySide6 modal review dialog displaying the privacy disclosure, explicit opt-in checkbox, read-only source snapshot preview, non-blocking `CallWorker` lifecycle management, and 9 editable platform fields with live `FIELD_LIMITS` counters and `PASS` badges.
* `reelay/gui/post_panel.py`: Minimal wiring adding the **"Repurpose with Muse Spark"** button to the ticket editor and applying accepted proposals into in-memory blocks (with stale-ticket protection) without auto-saving ticket copy.
* `reelay/__main__.py`: Package entry point enabling direct `python -m reelay` desktop launch.
* `reelay/REPURPOSE_README.md`: Module documentation, CLI reference, and desktop GUI approval workflow guide.
* `tests/test_repurpose_engine.py`: 17 unit tests covering schema normalization, `FIELD_LIMITS` validation, zero-truncation refinement, Threads hashtag migration, `MemoryTokenStore` keychain fallback, hidden-prompt setup, and transcript delimiter escaping.
* `tests/test_privacy.py`: 6 unit tests covering default-off privacy state, blocked calls when disabled, blocked direct calls with no store/opt_in/env var, allowed calls after explicit opt-in, environment overrides, and disclosure completeness.
* `tests/test_repurpose_gui.py`: 11 GUI unit tests verifying the button in `PostPanel`, `python -m reelay` package entry point, privacy disclosure & unchecked consent blocking (even when `REELAY_CLOUD_AI_OPT_IN=1`), read-only source snapshot fidelity, non-blocking cancel during active generation, disabled Apply during regeneration, stale-ticket protection, and persisted SQLite store verification confirming ticket copy is not saved until **Save Changes**.
* `SUBMISSION.md`: Hackathon submission write-up and build diff reference.

---

## 4. Verification & Commands

```bash
# 1. One-time keychain setup (hidden no-echo prompt)
./.venv/bin/python -m reelay.repurpose_engine --setup

# 2. Verify key availability without displaying the key
./.venv/bin/python -m reelay.repurpose_engine --check

# 3. Launch Reelay Desktop GUI
./.venv/bin/python -m reelay

# 4. Run live Muse Spark CLI proposal & review output on one of Andrew's own videos
./.venv/bin/python -m reelay.repurpose_engine \
  --title "Building Local AI Desktop Tools on macOS" \
  --description "Walking through local-first media management and agent APIs." \
  --transcript "Hey everyone, today we are looking at how Reelay runs locally on Mac while using Meta Muse Spark for six-platform copy generation." \
  --brand-voice "Direct, technical builder tone; clear and grounded."

# 5. Run unit test suites (34 tests — no API key or network required)
./.venv/bin/pytest tests/test_repurpose_engine.py tests/test_privacy.py tests/test_repurpose_gui.py -v

# 6. Inspect exact hackathon build diff (10 files)
git diff pre-hackathon...hackathon-all-nov4
```
