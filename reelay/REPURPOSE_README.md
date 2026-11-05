# Reelay Repurpose Engine (`reelay.repurpose_engine`)

**Initial Build (Hackathon):** Meta Global AI Developer Hackathon — Startup Track (Build for Your World)  
**Date Built:** November 4, 2026  
**Post-Hackathon Product Work:** Shippable Version 2 (Batch Repurposing Pipeline, Branch `batch-v2`)  
**Author:** Andrew Baxter  
**Model:** Meta Muse Spark (`muse-spark-1.3`) via the Meta Model API (`https://api.meta.ai/v1`, Standard non-contributor tier)  

---

## 1. Overview & Capabilities

The Reelay Repurpose Engine transforms source videos into publication-ready, platform-tailored copy across six major networks:
* **YouTube:** Generates title, description, and comma-separated tags (without `#`).
* **TikTok:** Generates short-form caption with a high-retention first-second hook.
* **Instagram Reels:** Generates conversational short-form caption.
* **Bluesky:** Generates concise microblog copy strictly under 300 graphemes.
* **Threads:** Generates conversational copy with zero hashtags in the body (any `#tags` are moved automatically to the shared `hashtags` field, both on initial draft and after shorter rewrites).
* **Facebook:** Generates contextual community post.
* **Shared Hashtags:** Concentrates platform hashtags into a single shared field.

### Evolution from Hackathon to Post-Hackathon v2

| Feature | Hackathon Submission (v1) | Shippable Product Release (v2) |
| :--- | :--- | :--- |
| **Workflow Scope** | Single ticket at a time via `PostPanel` | Multi-ticket batch repurposing across entire archives |
| **Selection** | Single active card | Multi-select on Kanban board (⌘-click / Shift-click / Column) |
| **Queue Engine** | Synchronous single-call worker | Asynchronous background queue (`BatchRepurposeQueue`) |
| **Variant Generation** | Single rigid proposal (9 fields) | Up to 3 creative variants for titles & hooks (`reelay.variants`) |
| **Failure Handling** | Immediate modal error | Failure isolation: 1 failed ticket never kills the batch |
| **Review & Commit** | Single "Apply to Ticket" button | Two-Stage Commit: "Apply Reviewed Only" &rarr; "Save Changes" |

---

## 2. Batch Architecture (`batch-v2`)

The Batch Repurposing subsystem is organized into modular, decoupled components designed for local-first reliability and creator agency:

```
reelay/
├── repurpose_engine.py      # Core prompt construction, Meta Model API client, 
│                            # validation against FIELD_LIMITS, zero-truncation rewrites
├── batch_repurpose.py       # BatchJob dataclass, BatchRepurposeQueue manager,
│                            # rate-limited execution, cancellation, and retry
├── variants.py              # Variant and ProposalWithVariants engine,
│                            # 3-variant title/hook generation under FIELD_LIMITS
├── platform_specs.py        # Central registry of character, byte, and grapheme limits
├── privacy.py               # Cloud AI consent gate and technical disclosure
└── gui/
    ├── repurpose_dialog.py  # Single-ticket creator review modal
    ├── batch_dialog.py      # BatchReviewDialog with multi-ticket queue,
    │                        # platform logos, PASS/OVER LIMIT pills, variant picker
    ├── board_page.py        # Board multi-select and BulkPanel "Batch Repurpose" launch
    └── post_panel.py        # Ticket inspector with in-memory CopyBlock population
```

### Module Roles & Responsibilities

#### `reelay/batch_repurpose.py` (`BatchRepurposeQueue`)
* **`BatchJob`:** Dataclass encapsulating `ticket_id`, `source_snapshot` (`title`, `description`, `notes`, `transcript`, `brand_voice`), `platforms`, `status` (`queued`, `running`, `ready`, `failed`, `applied`, `saved`, `cancelled`), `proposal`, `variants`, `error`, `reviewed`, `applied`, and `saved`.
* **Worker Queue:** Processes jobs asynchronously with a configurable concurrency cap to respect Meta Model API rate limits without freezing the PySide6 UI.
* **Failure Isolation:** Catches exceptions per ticket. If one video fails (e.g., transient network glitch or 429 rate limit), only that job transitions to `failed`; all other jobs in the batch continue processing.
* **Sanitized Error Reporting:** Redacts any sensitive data, ensuring raw API keys or Authorization headers can never be logged, printed, or displayed.
* **Cancel & Retry:** Provides `cancel_batch()` and `cancel_job(ticket_id)` for granular control, alongside `retry_job(ticket_id)` to re-queue only failed or cancelled tickets.
* **Privacy Verification:** Re-verifies explicit Cloud AI opt-in before processing any batch.

#### `reelay/variants.py` (`VariantEngine`)
* **`Variant` & `ProposalWithVariants`:** Data structures holding alternate creative options alongside the primary proposal.
* **Targeted Angles:** Generates up to 3 variants (1 base proposal + up to 2 alternates) for high-impact fields: YouTube Title (e.g. SEO-optimized, question/hook, direct story) and short-form hooks (`tiktok_text` / `instagram_text`).
* **Strict Platform Compliance:** Every variant is checked against `reelay.platform_specs.FIELD_LIMITS`.
* **Zero Mechanical Truncation:** Copy is never truncated mid-word or with trailing ellipses; over-limit text is rewritten by Muse Spark.
* **The No-Invention Rule:** Variants are grounded strictly in the provided `<input_transcript>`, title, and creator notes. The model is forbidden from inventing fake claims, partners, or facts.
* **Style Rules:** Zero em dashes (`EM_DASH_RULE`), and Threads body hashtags are moved to shared tags.

#### `reelay/gui/batch_dialog.py` (`BatchReviewDialog`)
* **Creator-Friendly Header:** Displays `"Repurpose these videos (N videos selected)"` with subtext `"One batch, six platforms each, you approve before saving."`
* **Privacy Gate:** Prominent banner with `"ⓘ How it works"` disclosure overlay and mandatory per-batch opt-in checkbox (`"Allow Muse Spark to draft copy for this batch"`).
* **Always-Visible Platform Logos:** Uses Reelay's existing `platform_pixmap` chips for YouTube, TikTok, Instagram, Bluesky, Threads, and Facebook across every row.
* **Compliance Badges:** Real-time **`PASS`** (green) and **`OVER LIMIT`** (red) pills for each ticket.
* **Review Tracking:** Visual badges distinguish `Unreviewed` (neutral) from `Reviewed` (purple).
* **Persistence Status:** Clearly marks proposals as `In Memory Only` until committed.
* **Two-Stage Commit:** Features the **"Apply Reviewed Only (X of N)"** action button, which strictly filters to tickets where `reviewed == True`.

---

## 3. Key Safeguards & Creator Safety Rules

1. **Local-First by Default:** Reelay operates locally on your machine. All video files, SQLite records, brand voices, and revisions reside on your disk. Network calls only occur when you explicitly trigger cloud features.
2. **Privacy Gate Default OFF:** Cloud AI opt-in (`privacy.cloud_ai_opt_in`) is disabled by default. Every batch session requires fresh, explicit creator consent. Generation is strictly blocked with `PrivacyOptInRequiredError` if unchecked.
3. **No Training on Creator Content:** Requests are routed to `muse-spark-1.3` on `https://api.meta.ai/v1` under the Standard non-contributor tier. Per Meta's technical documentation for this tier, request data is not used to train Meta's foundation models.
4. **Prompt Isolation Safeguard:** Transcripts are enclosed in `<input_transcript>` tags with case-insensitive closing-tag escaping (`</input_` &rarr; `< /input_`), ensuring spoken dialogue is processed as inert data rather than executable instructions.
5. **No Mechanical Truncation:** Copy is never arbitrarily cut off. Over-limit fields trigger a targeted shorter rewrite from Muse Spark; if still over-limit, a validation error is surfaced rather than mutilated copy.
6. **No Invention Rule:** Grounded strictly in source text. If a field cannot be written honestly without hallucinating external facts, the engine returns empty text rather than fabricating claims.
7. **Human-in-the-Loop Unconditional:** AI proposes, creator decides. Generated copy is held in memory until the creator explicitly clicks "Apply Reviewed Only", and persisted to SQLite only when the creator clicks "Save Changes" on the ticket.

---

## 4. What It Does NOT Do

* **No Autonomous Database Writes:** Does NOT write generated copy to SQLite `posts` or `videos` tables automatically.
* **No Auto-Publishing:** Does NOT publish, schedule, or upload to YouTube, TikTok, Instagram, Bluesky, Threads, or Facebook.
* **No Application of Unreviewed Proposals:** "Apply Reviewed Only" strictly excludes any ticket that the creator has not opened and reviewed.
* **No Stale Overwrites:** Strict ticket ID matching prevents a proposal meant for Video A from being applied to Video B.
* **No Third-Party Scraping:** Does NOT use downloaded third-party TikTok or Instagram videos.
* **No AI Imagery:** Does NOT generate synthetic photos, thumbnails, or deepfakes. Copywriting only.

---

## 5. Configuration & Key Management

Reelay uses its existing OS Keychain architecture (`reelay/tokens.py`, service `Reelay`). The Repurpose Engine resolves credentials in strict order:
1. `META_API_KEY` environment variable.
2. OS Keychain via `KeyringTokenStore` (service: `Reelay`, key: `META_API_KEY`).
3. If neither exists, raises `RepurposeConfigError`.

### Secure Keychain Setup (No Shell History Exposure)
Run the setup command with an interactive, no-echo prompt:
```bash
python -m reelay.repurpose_engine --setup
```

Verify that your key is properly configured without displaying it:
```bash
python -m reelay.repurpose_engine --check
```

Clear the key from the keychain:
```bash
python -m reelay.repurpose_engine --clear
```

Alternatively, export the key in your terminal session:
```bash
export META_API_KEY="your-key-here"
```

---

## 6. Python API Usage

### Single-Ticket Proposal Generation
```python
from reelay.repurpose_engine import generate_repurpose_proposal

proposal = generate_repurpose_proposal(
    title="Building Local AI Desktop Tools on macOS",
    description="Exploring local-first media pipelines and offline agent architectures.",
    notes="Target audience: solo developers, indie hackers, desktop engineers.",
    transcript="Hey everyone, today we're walking through how Reelay runs locally on macOS.",
    brand_voice="Direct, technical builder tone; zero buzzwords.",
    opt_in=True,
)

print("YouTube Title:", proposal["title"])
print("Bluesky Post:", proposal["bluesky_text"])
print("Threads Post:", proposal["threads_text"])
print("Shared Hashtags:", proposal["hashtags"])
```

### Batch Repurposing Queue API
```python
from reelay.batch_repurpose import BatchJob, BatchRepurposeQueue

jobs = [
    BatchJob(
        ticket_id=101,
        source_snapshot={
            "title": "Local SQLite Architecture",
            "description": "Deep dive into local database patterns.",
            "notes": "Focus on reliability.",
            "transcript": "In this video we look at SQLite concurrency.",
            "brand_voice": "Technical builder.",
        },
        platforms=["youtube", "tiktok", "bluesky", "threads"],
    ),
    BatchJob(
        ticket_id=102,
        source_snapshot={
            "title": "Async Worker Pools in PySide6",
            "description": "Keeping desktop GUIs responsive under load.",
            "notes": "PySide6 tips.",
            "transcript": "Never block the Qt main thread with network calls.",
            "brand_voice": "Direct and practical.",
        },
        platforms=["youtube", "instagram", "facebook"],
    ),
]

# Initialize queue with concurrency limit
queue = BatchRepurposeQueue(jobs, max_workers=2)

# Start background generation (requires explicit opt_in=True)
queue.start(opt_in=True)
queue.wait()

for job in queue.jobs:
    print(f"Ticket {job.ticket_id}: Status={job.status}, Error={job.error}")
    if job.proposal:
        print("  YouTube Title:", job.proposal["title"])
```

---

## 7. Testing & Verification

Run the complete test suite (single-ticket engine, privacy opt-in gate, variant generation, batch queue, and GUI lifecycle):

```bash
./.venv/bin/pytest \
  tests/test_repurpose_engine.py \
  tests/test_privacy.py \
  tests/test_repurpose_gui.py \
  tests/test_batch_repurpose.py \
  tests/test_batch_gui.py \
  -v
```

All unit tests run completely offline with mocked API responses and synthetic tokens—no real API keys or network requests required.

---

## 8. Desktop GUI Approval Integration

Reelay adheres strictly to creator agency: **AI proposes, human decides**.

### Batch Approval Lifecycle

```
[ Kanban Board Multi-Select (⌘-Click / Shift-Click) ]
                         │
                         ▼ (Click "Batch Repurpose with Muse Spark" on BulkPanel)
[ BatchReviewDialog Modal Opens ]
                         │
                         ▼ (Creator checks: "Allow Muse Spark to draft copy for this batch")
[ BatchRepurposeQueue Runs in Background (muse-spark-1.3) ]
                         │
                         ▼ (Proposals arrive in memory with PASS / OVER LIMIT pills)
[ Creator Review Gate ] ◄── (Creator opens tickets, inspects copy, toggles variants)
                         │
                         ▼ (Click "Apply Reviewed Only" -> fills in-memory CopyBlocks)
[ PostPanel Ticket Inspector ]
                         │
                         ▼ (Creator reviews ticket and clicks "Save Changes")
[ Persisted to SQLite (posts, videos, revisions) ]
```

### Two-Stage Commit Safeguards in GUI
1. **Multi-Selection Trigger:** Selecting cards on the Board displays `BulkPanel` with ticket counts and the **Batch Repurpose** launch button.
2. **Explicit Per-Batch Opt-In:** Generation is locked until the creator toggles the opt-in checkbox in `BatchReviewDialog`.
3. **Always-Visible Platforms & Badges:** Platform logos (YouTube, TikTok, Instagram, Bluesky, Threads, Facebook) and PASS / OVER LIMIT badges keep compliance front and center.
4. **Selective Review:** Tickets are marked `Reviewed` only when the creator inspects their proposal.
5. **Apply Reviewed Only:** Clicking the apply button copies generated text only into the in-memory widgets of reviewed tickets. Unreviewed tickets remain untouched.
6. **Ticket-by-Ticket Persistence:** The creator clicks **"Save Changes"** in `PostPanel` to commit each ticket to SQLite. No auto-publishing or background commits ever occur.
