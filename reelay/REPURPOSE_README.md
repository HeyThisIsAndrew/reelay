# Reelay Repurpose Engine (`reelay.repurpose_engine`)

**Hackathon Feature:** Meta Global AI Developer Hackathon — Startup Track (Build for Your World)  
**Date Built:** November 4, 2026  
**Author:** Andrew Baxter  
**Model:** Meta `muse-spark-1.3` (Meta Model API `https://api.meta.ai/v1`)

---

## 1. What It Does

The Repurpose Engine transforms an original video from a Reelay ticket into platform-ready social copy across six networks:
* **YouTube:** Generates title, description, and comma-separated tags (without `#`).
* **TikTok:** Generates short-form caption with a first-second hook.
* **Instagram Reels:** Generates conversational caption.
* **Bluesky:** Generates concise microblog copy strictly under 300 graphemes.
* **Threads:** Generates engaging copy adhering to Threads culture with **zero hashtags** in the text.
* **Facebook:** Generates contextual community post.
* **Shared Hashtags:** Concentrates platform hashtags in the shared `hashtags` field.

### Key Guarantees
* **Creator Voice:** Maintains Andrew's authentic, direct, technical voice.
* **Factuality First:** Never hallucinates claims, quotes, partners, or facts not present in the source input.
* **Zero-Truncation Policy:** Strictly validated against `reelay.platform_specs.FIELD_LIMITS`. Copy is **never chopped off or mechanically truncated**. Over-limit fields trigger targeted refinement or raise a clear validation error.
* **Safe Error Handling:** Fails gracefully with `RepurposeConfigError("META_API_KEY is not set...")` if credentials are unset, never crashing Reelay.

---

## 2. What It Does NOT Do

* **No Auto-Save:** Does NOT write to the SQLite database or modify tickets autonomously.
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
)

# Output dictionary with all 9 string fields:
print("YouTube Title:", proposal["title"])
print("Bluesky Post:", proposal["bluesky_text"])
print("Threads Post:", proposal["threads_text"])
print("Shared Hashtags:", proposal["hashtags"])
```

### Dry-Run Test (No API Key Required)
Run the automated test suite verifying schema, limit validation, and error handling:
```bash
pytest tests/test_repurpose_engine.py -v
```

---

## 4. How the Approval Step Wires into a Ticket

Reelay adheres strictly to creator agency: **AI proposes, human decides**.

```
[ Reelay Video Ticket ]
          │
          ▼ (Input: title, description, notes)
[ Repurpose Engine (muse-spark-1.3) ]
          │
          ▼ (Proposal Dict: 9 fields in memory)
[ Creator Review & Approval Gate ] ◄── (Human approves, edits, or rejects)
          │
          ▼ (Explicit Human Approval)
[ Apply to Ticket via Scoped Pipeline ]
```

### Approval Staging Mechanics:
1. The user initiates a repurpose action from the Reelay ticket panel or agent API.
2. The engine returns an in-memory dictionary proposal with character/byte count validation indicators.
3. The UI / CLI presents the proposal to Andrew.
4. Andrew inspects each field.
5. Upon clicking **"Approve & Apply"**, the fields are committed into `reelay.scoped_pipeline` as a batch update.

---

## 5. Wiring Instructions for Claude (1:00 PM PT) or Andrew

To preserve stability and avoid breaking Reelay's existing 324-test suite before the deadline, core files remain unmodified. Below are the precise, minimal integration steps for wiring the engine into the GUI and API:

### Files to Touch (Minimal Wiring):
1. **`reelay/gui/ticket_widgets.py`**:
   * Add a **"Repurpose with Muse Spark"** button to `DetailPanel` or `TicketEditWidget`.
   * On click: Read current `ticket.title`, `ticket.description`, and `ticket.notes`.
   * Launch `generate_repurpose_proposal` on a background thread (`QRunnable` or `threading.Thread`) to keep UI fluid.
   * Open a review preview modal displaying before/after diffs with character count badges.
   * On "Accept Proposal", call `self._commit(...)` for each approved field.

2. **`reelay/api.py`**:
   * Add route: `POST /v1/videos/{id}/repurpose`
   * Request body: `{"notes": "...", "dry_run": true}`
   * Returns: The proposal dict with platform count validations.
   * When `dry_run=false`, requires an approval ticket or creator session token before committing to `scoped_pipeline`.
