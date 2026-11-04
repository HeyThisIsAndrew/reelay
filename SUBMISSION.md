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
3. **Creator Voice & Factuality:**
   * Preserves Andrew's direct, technical builder voice using the profile's brand voice guidance.
   * Never invents facts, quotes, products, people, or claims not present in the source input.
   * Untrusted transcript text is isolated inside `<input_transcript>` data markers with tag-escape sanitization so spoken text is treated strictly as data, never instructions.
   * Copy-only: zero AI-generated imagery or synthetic photos.
4. **Privacy & Local-First Security (`reelay/privacy.py` & `reelay/tokens.py`):**
   * **Opt-In by Default:** Cloud AI defaults to **OFF** (`False`) for every new user (`privacy.cloud_ai_opt_in`). No data leaves the Mac without explicit creator opt-in.
   * **Standard Non-Contributor Tier:** Uses `muse-spark-1.3` (standard tier, not a contributor model) so creator transcripts are not used to train Meta's models.
   * **OS Keychain Storage:** Reuses `reelay/tokens.py` (`KeyringTokenStore`, service `"Reelay"`, key `"META_API_KEY"`). The API key is set once via a no-echo prompt (`python -m reelay.repurpose_engine --setup`) and never stored in `.env` files, repo files, or logs.
5. **Proposal Only — Human Approval Gate:**
   * Returns an in-memory proposal dictionary only. Never writes to SQLite by itself, never auto-saves to a ticket, never schedules, and never auto-publishes.

---

## 3. Clear Separation of Pre-Existing vs. Hackathon-Built Work

### Pre-Existing Reelay Foundation (Oct 17, 2026 and earlier — Not Claimed as New)
* Reelay PySide6 macOS desktop application (`reelay/gui/`).
* Content ticket model, SQLite database, and revision history (`reelay/models.py`, `reelay/store.py`, `reelay/pipeline.py`).
* Video downloading, local inspection, and scheduling (`reelay/downloader.py`, `reelay/scheduler.py`).
* Platform limit registry (`reelay/platform_specs.py`, `reelay/platform_text.py`) and OS keychain wrapper (`reelay/tokens.py`).
* Local loopback Agent API at `127.0.0.1:8471` (`reelay/api.py`, `reelay/scoped_pipeline.py`).

### Built Exclusively During the Hackathon (November 4, 2026 — `pre-hackathon...hackathon-all-nov4`)
* `reelay/repurpose_engine.py`: Core Meta Model API (`muse-spark-1.3`) client, prompt isolation, transcript & brand voice injection, Threads hashtag sanitizer, zero-truncation `FIELD_LIMITS` validator/refiner, OS keychain lookup/setup CLI, and formatted human-review runner.
* `reelay/privacy.py`: Cloud AI opt-in gate (`is_cloud_ai_enabled`, `set_cloud_ai_enabled`, `require_cloud_ai_opt_in`) and transparent privacy disclosure (`DISCLOSURE_TEXT`).
* `reelay/REPURPOSE_README.md`: Module documentation, CLI reference, and ticket approval wiring guide.
* `tests/test_repurpose_engine.py`: 17 unit tests covering schema normalization, `FIELD_LIMITS` validation, zero-truncation refinement, Threads hashtag migration, `MemoryTokenStore` keychain fallback, hidden-prompt setup, and transcript prompt-injection escaping.
* `tests/test_privacy.py`: 5 unit tests covering default-off privacy state, blocked calls when disabled, allowed calls after explicit opt-in, environment overrides, and disclosure completeness.
* `SUBMISSION.md`: Hackathon submission write-up and build diff reference.

---

## 4. Verification & Commands

```bash
# 1. One-time keychain setup (hidden no-echo prompt)
./.venv/bin/python -m reelay.repurpose_engine --setup

# 2. Verify key availability without displaying the key
./.venv/bin/python -m reelay.repurpose_engine --check

# 3. Run live Muse Spark proposal & review output on one of Andrew's own videos
./.venv/bin/python -m reelay.repurpose_engine \
  --title "Building Local AI Desktop Tools on macOS" \
  --description "Walking through local-first media management and agent APIs." \
  --transcript "Hey everyone, today we are looking at how Reelay runs locally on Mac while using Meta Muse Spark for six-platform copy generation." \
  --brand-voice "Direct, technical builder tone; clear and grounded."

# 4. Run unit test suites (no API key or network required)
./.venv/bin/pytest tests/test_repurpose_engine.py tests/test_privacy.py -v

# 5. Inspect exact hackathon build diff
git diff pre-hackathon...hackathon-all-nov4
```
