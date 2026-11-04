# Reelay Repurpose Engine (`reelay.repurpose_engine`)

**Hackathon Feature:** Meta Global AI Developer Hackathon — Startup Track (Build for Your World)  
**Date Built:** November 4, 2026  
**Author:** Andrew Baxter  
**Model:** Meta `muse-spark-1.3` (Meta Model API `https://api.meta.ai/v1`, Standard non-contributor tier)

---

## 1. What It Does

The Repurpose Engine transforms an original video from a Reelay ticket into platform-ready social copy across six networks:
* **YouTube:** Generates title, description, and comma-separated tags (without `#`).
* **TikTok:** Generates short-form caption with a first-second hook.
* **Instagram Reels:** Generates conversational caption.
* **Bluesky:** Generates concise microblog copy strictly under 300 graphemes.
* **Threads:** Generates conversational copy with no hashtags in the body (Reelay's Threads editorial policy: any `#tags` are moved automatically to the shared `hashtags` field, both after the first draft and after any shorter rewrite).
* **Facebook:** Generates contextual community post.
* **Shared Hashtags:** Concentrates platform hashtags in the shared `hashtags` field.

### Key Safeguards
* **Creator Voice:** Maintains Andrew's authentic, direct, technical voice using the active brand voice profile.
* **Source Grounding:** System prompt rules instruct the model to rely only on the provided source input and not to invent claims, quotes, partners, or facts.
* **Prompt-Isolation Safeguard:** Transcripts are wrapped in `<input_transcript>` tags with case-insensitive closing-tag escaping (`</input_` &rarr; `< /input_`) so spoken text is framed as data rather than instructions. This reduces risk but does not make hallucination or prompt injection impossible, so every proposal goes through human review before it is saved.
* **No-Truncation Policy:** Every field is validated against Reelay's configured platform-limit registry, `reelay.platform_specs.FIELD_LIMITS`. Copy is **never mechanically truncated**: an over-limit field gets one targeted shorter rewrite from Muse Spark, and if it is still over the limit the engine raises `RepurposeValidationError` instead of returning it.
* **Safe Error Handling:** Fails gracefully with `RepurposeConfigError("META_API_KEY is not set...")` if credentials are unset, never crashing Reelay.

### Privacy, Network Use & Dependencies
* Reelay is a local desktop workflow with **optional, explicit cloud generation**. Ticket text is sent to Meta only after the creator opts in to Cloud AI (off by default) and clicks Generate Proposal. Reelay's pre-existing optional YouTube SEO keyword ideas (`reelay/seo.py`) also use the network, only when "Get SEO Suggestions" is clicked.
* Requests go to `muse-spark-1.3` on `https://api.meta.ai/v1`, the Standard non-contributor tier. Per Meta's documentation for this tier, Standard-mode requests are not used to train Meta's models.
* HTTP uses `requests`, which is not listed directly in `pyproject.toml` or `requirements.txt`; it is installed indirectly through `yt-dlp[default]`.

---

## 2. What It Does NOT Do

* **No Auto-Save of Ticket Copy:** Does NOT write generated copy to the SQLite `posts` or `videos` tables autonomously (only the user's explicit `privacy.cloud_ai_opt_in` checkbox preference is saved when toggled in the dialog; ticket copy is only saved when the creator clicks **Save Changes** on the ticket).
* **No Auto-Publishing:** Does NOT schedule, upload, or post to any platform.
* **No Third-Party Scraping:** Does NOT use downloaded third-party TikTok or Instagram videos.
* **No AI Imagery:** Does NOT generate images, thumbnails, or synthetic photos. Copy only.
* **No Cut Features:** Does NOT implement Thumbnail Studio or MCP pairing (explicitly cut from submission).

---

## 3. How to Run It

### Key Management & Configuration

Reelay reuses its existing `tokens.py` architecture (`KeyringTokenStore`, service `Reelay`).
The Repurpose Engine resolves the Meta Model API key in this strict order:
1. `META_API_KEY` environment variable.
2. OS Keychain via `KeyringTokenStore` (service: `Reelay`, key: `META_API_KEY`).
3. If neither exists, raises `RepurposeConfigError`.

#### Saving the Key to OS Keychain (One-Time Setup)
Run the explicit setup command, which uses a secure, no-echo terminal prompt (the key is never recorded in shell history, logs, or chat):
```bash
python -m reelay.repurpose_engine --setup
```

You can verify that the key is configured without ever displaying it:
```bash
python -m reelay.repurpose_engine --check
```

To clear the key from the keychain:
```bash
python -m reelay.repurpose_engine --clear
```

Alternatively, you can provide the key via environment variable:
```bash
export META_API_KEY="your-key-here"
```

### Python API Usage
```python
from reelay.repurpose_engine import generate_repurpose_proposal

proposal = generate_repurpose_proposal(
    title="Building an Agent-First Desktop Video Pipeline",
    description="Walking through local-first media management and agent APIs.",
    notes="Focus on indie hackers, developers, and desktop software.",
    transcript="Hey everyone, in this video we are looking at local-first media tools on macOS.",
    brand_voice="Direct, technical builder voice; zero fluff.",
    opt_in=True,
)

# Output dictionary with all 9 string fields:
print("YouTube Title:", proposal["title"])
print("Bluesky Post:", proposal["bluesky_text"])
print("Threads Post:", proposal["threads_text"])
print("Shared Hashtags:", proposal["hashtags"])
```

### Live CLI Proposal & Review Runner
Run a live proposal pass from the terminal (displays the privacy disclosure, calls `muse-spark-1.3`, validates all 9 fields against `FIELD_LIMITS`, and outputs the formatted review proposal without auto-saving):
```bash
python -m reelay.repurpose_engine \
  --title "Building Local AI Desktop Tools on macOS" \
  --description "Walking through local-first media management and agent APIs." \
  --transcript "Hey everyone, today we are looking at how Reelay runs locally on Mac." \
  --brand-voice "Direct, technical builder tone."
```

### Dry-Run Test (No API Key Required)
Run the automated test suite (39 tests) verifying schema, limit validation, privacy opt-in gate, GUI dialog, and error handling:
```bash
pytest tests/test_repurpose_engine.py tests/test_privacy.py tests/test_repurpose_gui.py -v
```

---

## 4. How the Approval Step Wires into a Ticket

Reelay adheres strictly to creator agency: **AI proposes, human decides**.

```
[ Reelay Video Ticket (PostPanel) ]
          │
          ▼ (Click "Repurpose with Muse Spark")
[ RepurposeDialog + CallWorker (muse-spark-1.3) ]
          │
          ▼ (Proposal Dict: 9 fields in memory with PASS badges)
[ Creator Review & Approval Gate ] ◄── (Human edits, approves, or cancels)
          │
          ▼ (Click "Apply to Ticket" -> fills PostPanel blocks in memory)
[ Creator Clicks "Save Changes" to Commit to SQLite ]
```

### Desktop GUI Integration (`reelay/gui/post_panel.py`, `reelay/gui/repurpose_dialog.py`, & `reelay/__main__.py`):
Launch the desktop app directly:
```bash
./.venv/bin/python -m reelay
```
1. The creator selects a video card on the Board and clicks **"Repurpose with Muse Spark"** in `PostPanel`.
2. `RepurposeDialog` opens, displays the Cloud AI transparency disclosure (`DISCLOSURE_TEXT`), an explicit opt-in checkbox, and a read-only **Source Snapshot** showing the exact title, description, notes, transcript, and brand voice guidance being sent.
3. Clicking **"Generate Proposal"** requires the consent checkbox to be checked (an unchecked box blocks generation even if `REELAY_CLOUD_AI_OPT_IN=1` is set in the environment) and runs `generate_repurpose_proposal` on a background `CallWorker` thread without blocking cancel/close.
4. All 9 platform fields are displayed in editable boxes with live `FIELD_LIMITS` counters and `PASS` badges.
5. Clicking **"Apply to Ticket"** verifies the target ticket is unchanged and unlocked, then populates the in-memory `CopyBlock` widgets on `PostPanel` without writing ticket copy to SQLite.
6. The creator reviews the ticket and clicks **"Save Changes"** to persist the ticket copy.
