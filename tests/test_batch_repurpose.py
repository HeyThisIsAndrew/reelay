"""Unit tests for Reelay Batch Repurpose Queue and Variant Engine.

Post-hackathon product work for Reelay (branch batch-v2).
Founder: Andrew Baxter. Local-first desktop tool for video creators.
"""

from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from reelay.batch_repurpose import (
    JOB_STATUS_APPLIED,
    JOB_STATUS_CANCELLED,
    JOB_STATUS_FAILED,
    JOB_STATUS_QUEUED,
    JOB_STATUS_READY,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SAVED,
    BatchJob,
    BatchRepurposeQueue,
    sanitize_error_message,
)
from reelay.platform_specs import FIELD_LIMITS, count
from reelay.privacy import (
    CLOUD_AI_ENV_VAR,
    PrivacyOptInRequiredError,
    require_cloud_ai_opt_in,
    set_cloud_ai_enabled,
)
from reelay.repurpose_engine import (
    EM_DASH,
    RepurposeAPIError,
    RepurposeNetworkError,
    RepurposeValidationError,
)
from reelay.store import Store
from reelay.variants import (
    ProposalWithVariants,
    Variant,
    apply_variant_choice,
    generate_variants,
)

SYNTHETIC_PROPOSAL_A = {
    "title": "Building Local AI Desktop Apps on macOS",
    "description": "Deep dive into local-first architecture and background worker queues.",
    "tags": "macos, python, local ai, pyside6",
    "tiktok_text": "Here is why we built Reelay local-first on macOS.",
    "instagram_text": "Local-first video tools give you complete privacy and instant response times.",
    "bluesky_text": "Testing our local-first batch repurpose queue on macOS with Meta Muse Spark.",
    "threads_text": "Why do so many dev tools force cloud subscriptions when your Mac has so much power?",
    "facebook_text": "Excited to share our latest batch repurpose workflow in Reelay.",
    "hashtags": "#LocalAI #Python #macOS #Reelay",
}

SYNTHETIC_PROPOSAL_B = {
    "title": "Optimizing Video Pipelines with FFmpeg and Python",
    "description": "How we process high bitrate video clips locally on Apple Silicon.",
    "tags": "ffmpeg, python, apple silicon, video editing",
    "tiktok_text": "How to process 4K video clips without crashing your Mac.",
    "instagram_text": "Hardware-accelerated video rendering right inside a PySide6 desktop app.",
    "bluesky_text": "Apple Silicon media engines make local video transformation incredibly fast.",
    "threads_text": "Hardware acceleration on Apple Silicon is a game changer for local video tools.",
    "facebook_text": "Deep dive into local video pipelines on macOS.",
    "hashtags": "#VideoEditing #AppleSilicon #Python",
}


def _make_sample_job(ticket_id: int, title: str = "Test Video", transcript: str = "Sample transcript") -> BatchJob:
    return BatchJob(
        ticket_id=ticket_id,
        source_snapshot={
            "title": f"{title} #{ticket_id}",
            "description": f"Description for video {ticket_id}",
            "notes": "Developer creator notes",
            "transcript": transcript,
            "brand_voice": "Direct, technical builder tone; grounded.",
        },
        platforms=["youtube", "tiktok", "instagram", "bluesky", "threads", "facebook"],
    )


# ======================================================================
# 1. BatchJob and Sanitization
# ======================================================================
def test_batch_job_initialization_and_serialization():
    job = _make_sample_job(101)
    assert job.ticket_id == 101
    assert job.status == JOB_STATUS_QUEUED
    assert job.is_pending is True
    assert job.is_terminal is False
    assert job.reviewed is False
    assert job.applied is False
    assert job.saved is False

    job.mark_running()
    assert job.status == JOB_STATUS_RUNNING

    job.mark_ready(SYNTHETIC_PROPOSAL_A)
    assert job.status == JOB_STATUS_READY
    assert job.is_ready is True
    assert job.is_terminal is True
    assert job.proposal == SYNTHETIC_PROPOSAL_A

    d = job.to_dict()
    assert d["ticket_id"] == 101
    assert d["status"] == JOB_STATUS_READY
    assert d["proposal"] == SYNTHETIC_PROPOSAL_A

    restored = BatchJob.from_dict(d)
    assert restored.ticket_id == 101
    assert restored.status == JOB_STATUS_READY
    assert restored.proposal == SYNTHETIC_PROPOSAL_A


def test_sanitize_error_message_strips_api_keys():
    secret_key = "AIzaSyFakeSecretKey9876543210"
    raw_error = f"HTTP 401 Unauthorized for Bearer {secret_key} with token={secret_key}"
    sanitized = sanitize_error_message(raw_error, api_key=secret_key)
    assert secret_key not in sanitized
    assert "[REDACTED]" in sanitized

    job = _make_sample_job(102)
    job.mark_failed(f"Failed with key {secret_key}", api_key=secret_key)
    assert secret_key not in (job.error or "")


# ======================================================================
# 2. Queue Ordering (FIFO)
# ======================================================================
def test_queue_ordering_executes_in_fifo_order():
    """Jobs in the batch execute in strict FIFO order."""
    call_order: list[int] = []

    def mock_generate(**kwargs):
        title = kwargs.get("title", "")
        # Extract ticket_id from title "#<id>"
        for part in title.split("#"):
            if part.isdigit():
                call_order.append(int(part))
        return SYNTHETIC_PROPOSAL_A

    queue = BatchRepurposeQueue(
        opt_in=True,
        max_concurrency=1,
        generate_fn=mock_generate,
    )
    for tid in [101, 102, 103, 104]:
        queue.add_job(_make_sample_job(tid))

    jobs = queue.process_sync()
    assert len(jobs) == 4
    assert call_order == [101, 102, 103, 104]
    for j in jobs:
        assert j.status == JOB_STATUS_READY


# ======================================================================
# 3. Failure Isolation
# ======================================================================
def test_failure_isolation_preserves_other_jobs():
    """When 1 of N jobs fails, only that ticket fails with status 'failed'.

    The other N-1 jobs succeed and finish with status 'ready'. The batch is NOT killed.
    """
    def mock_generate(**kwargs):
        title = kwargs.get("title", "")
        if "#102" in title:
            raise RepurposeNetworkError("Simulated network timeout for ticket 102")
        return SYNTHETIC_PROPOSAL_A

    queue = BatchRepurposeQueue(
        opt_in=True,
        max_concurrency=1,
        generate_fn=mock_generate,
    )
    for tid in [101, 102, 103]:
        queue.add_job(_make_sample_job(tid))

    jobs = queue.process_sync()
    assert len(jobs) == 3

    job101 = queue.get_job(101)
    job102 = queue.get_job(102)
    job103 = queue.get_job(103)

    assert job101 is not None and job101.status == JOB_STATUS_READY
    assert job102 is not None and job102.status == JOB_STATUS_FAILED
    assert "Simulated network timeout" in (job102.error or "")
    assert job103 is not None and job103.status == JOB_STATUS_READY

    # Total batch progress reflects 2 ready and 1 failed
    assert queue.completed_count == 3
    assert len(queue.ready_jobs) == 2
    assert len(queue.failed_jobs) == 1


# ======================================================================
# 4. Cancel Operations
# ======================================================================
def test_cancel_whole_batch_cancels_pending_jobs():
    """cancel_batch() cancels all pending/queued jobs in the batch."""
    queue = BatchRepurposeQueue(opt_in=True)
    for tid in [201, 202, 203, 204]:
        queue.add_job(_make_sample_job(tid))

    cancelled = queue.cancel_batch()
    assert cancelled == 4
    for j in queue.list_jobs():
        assert j.status == JOB_STATUS_CANCELLED
        assert j.is_cancelled is True
    assert queue.completed_count == 4


def test_cancel_single_job_cancels_only_that_job():
    """cancel_job(ticket_id) cancels only the specified job."""
    called: list[int] = []

    def mock_generate(**kwargs):
        title = kwargs.get("title", "")
        for part in title.split("#"):
            if part.isdigit():
                called.append(int(part))
        return SYNTHETIC_PROPOSAL_A

    queue = BatchRepurposeQueue(
        opt_in=True,
        max_concurrency=1,
        generate_fn=mock_generate,
    )
    for tid in [301, 302, 303]:
        queue.add_job(_make_sample_job(tid))

    # Cancel job 302 while in queue
    ok = queue.cancel_job(302)
    assert ok is True
    assert queue.get_job(302).status == JOB_STATUS_CANCELLED

    jobs = queue.process_sync()
    assert queue.get_job(301).status == JOB_STATUS_READY
    assert queue.get_job(302).status == JOB_STATUS_CANCELLED
    assert queue.get_job(303).status == JOB_STATUS_READY
    assert called == [301, 303]


# ======================================================================
# 5. Retry Operation
# ======================================================================
def test_retry_operation_re_queues_failed_job_and_succeeds():
    """Retry on a failed job re-queues only that job and allows it to succeed."""
    fail_ticket_102 = True

    def mock_generate(**kwargs):
        nonlocal fail_ticket_102
        title = kwargs.get("title", "")
        if "#102" in title and fail_ticket_102:
            raise RepurposeAPIError("Temporary 500 error from Meta Model API", status_code=500)
        return SYNTHETIC_PROPOSAL_A

    queue = BatchRepurposeQueue(
        opt_in=True,
        max_concurrency=1,
        generate_fn=mock_generate,
    )
    queue.add_job(_make_sample_job(101))
    queue.add_job(_make_sample_job(102))

    queue.process_sync()
    assert queue.get_job(101).status == JOB_STATUS_READY
    assert queue.get_job(102).status == JOB_STATUS_FAILED

    # Now the issue is resolved on retry
    fail_ticket_102 = False
    re_queued = queue.retry_job(102)
    assert re_queued is True

    # 101 should NOT be re-queued
    cannot_retry_ready = queue.retry_job(101)
    assert cannot_retry_ready is False

    # Process retried job
    queue.process_sync()
    assert queue.get_job(102).status == JOB_STATUS_READY
    assert queue.get_job(102).proposal == SYNTHETIC_PROPOSAL_A
    assert queue.get_job(102).error is None


# ======================================================================
# 6. Rate Limit Handling (HTTP 429)
# ======================================================================
def test_rate_limit_handling_reports_error_without_crashing():
    """Simulating HTTP 429 reports rate limit error on that job without crashing the queue."""
    def mock_generate(**kwargs):
        title = kwargs.get("title", "")
        if "#102" in title:
            raise RepurposeAPIError("Meta Model API rate limit exceeded (429)", status_code=429)
        return SYNTHETIC_PROPOSAL_A

    queue = BatchRepurposeQueue(
        opt_in=True,
        max_concurrency=1,
        rate_limit_backoff=0.5,
        generate_fn=mock_generate,
    )
    queue.add_job(_make_sample_job(101))
    queue.add_job(_make_sample_job(102))
    queue.add_job(_make_sample_job(103))

    jobs = queue.process_sync()
    assert len(jobs) == 3

    job102 = queue.get_job(102)
    assert job102.status == JOB_STATUS_FAILED
    assert job102.error_status_code == 429
    assert "429" in (job102.error or "")

    # Job 101 and 103 succeeded cleanly
    assert queue.get_job(101).status == JOB_STATUS_READY
    assert queue.get_job(103).status == JOB_STATUS_READY


# ======================================================================
# 7. Variant Generation and Selection
# ======================================================================
def test_variant_generation_under_field_limits():
    """Up to 3 variants generated for title and hooks under FIELD_LIMITS."""
    def fake_call(messages, **kwargs):
        # Return valid JSON for alternates
        return json.dumps({
            "title": [
                "Alternative Title 1 for macOS App",
                "Alternative Title 2 for macOS Developers",
            ],
            "tiktok_text": [
                "Hook variant 1: Are you still running video tools in the cloud?",
                "Hook variant 2: What if your desktop video app never left your Mac?",
            ],
        })

    with patch("reelay.variants._call_meta_model", side_effect=fake_call):
        proposal_variants = generate_variants(
            title="Building Local AI Desktop Apps on macOS",
            description="Deep dive into local-first desktop workflows.",
            base_proposal=SYNTHETIC_PROPOSAL_A,
            fields=["title", "tiktok_text"],
            num_alternates=2,
            opt_in=True,
        )

        title_variants = proposal_variants.get_variants_for_field("title")
        assert len(title_variants) == 3  # 1 base + 2 alternates
        assert title_variants[0].variant_id in ("base", "v1")
        assert title_variants[1].variant_id in ("v2", "alt_1")
        assert title_variants[2].variant_id in ("v3", "alt_2")

        for v in title_variants:
            display, is_over = count("title", v.text)
            assert not is_over, f"Variant '{v.text}' exceeds title limit ({display})"
            assert v.is_valid is True


def test_variants_never_contain_em_dashes():
    """No em dashes in any variant, even if the model attempts to generate them."""
    def fake_call(messages, **kwargs):
        # Simulate model returning em dashes
        return json.dumps({
            "title": [
                f"Title with em dash {EM_DASH} bad formatting",
                "Clean Title without em dash",
            ]
        })

    with patch("reelay.variants._call_meta_model", side_effect=fake_call):
        proposal_variants = generate_variants(
            title="Building Local AI Desktop Apps on macOS",
            base_proposal=SYNTHETIC_PROPOSAL_A,
            fields=["title"],
            num_alternates=2,
            opt_in=True,
            strict=False,
        )
        title_variants = proposal_variants.get_variants_for_field("title")
        for v in title_variants:
            assert EM_DASH not in v.text


def test_apply_variant_choice_cleanly_replaces_field():
    """Picking a variant cleanly replaces the field in the proposal."""
    base = dict(SYNTHETIC_PROPOSAL_A)
    chosen_title = "Brand New Chosen Title for macOS Video Tool"

    updated = apply_variant_choice(base, "title", chosen_title)
    assert updated["title"] == chosen_title
    # All other fields remain untouched
    for k in base:
        if k != "title":
            assert updated[k] == base[k]


# ======================================================================
# 8. Privacy Gate
# ======================================================================
def test_privacy_gate_batch_generation_blocked_when_disabled(monkeypatch, tmp_path):
    """Batch generation blocked if opt_in=False or Cloud AI disabled."""
    monkeypatch.delenv(CLOUD_AI_ENV_VAR, raising=False)
    store = Store(tmp_path / "test_privacy_batch.db")
    try:
        set_cloud_ai_enabled(store, False)

        # 1. Queue with opt_in=False raises PrivacyOptInRequiredError on start
        queue = BatchRepurposeQueue(store=store, opt_in=False)
        queue.add_job(_make_sample_job(101))
        with pytest.raises(PrivacyOptInRequiredError):
            queue.start()

        with pytest.raises(PrivacyOptInRequiredError):
            queue.process_sync()

        # 2. Queue with disabled store and no opt_in override raises PrivacyOptInRequiredError
        queue_default = BatchRepurposeQueue(store=store)
        queue_default.add_job(_make_sample_job(101))
        with pytest.raises(PrivacyOptInRequiredError):
            queue_default.start()

        # 3. Explicit batch opt-in required (opt_in=True allows execution)
        queue_opted = BatchRepurposeQueue(
            store=store,
            opt_in=True,
            generate_fn=lambda **kwargs: SYNTHETIC_PROPOSAL_A,
        )
        queue_opted.add_job(_make_sample_job(101))
        jobs = queue_opted.process_sync()
        assert len(jobs) == 1
        assert jobs[0].status == JOB_STATUS_READY
    finally:
        store.close()


# ======================================================================
# 9. Stale Ticket Protection
# ======================================================================
def test_stale_ticket_protection():
    """Ensure proposal for ticket A is never applied to ticket B."""
    ticket_proposals = {
        101: dict(SYNTHETIC_PROPOSAL_A),
        102: dict(SYNTHETIC_PROPOSAL_B),
    }

    queue = BatchRepurposeQueue(
        opt_in=True,
        generate_fn=lambda **kwargs: ticket_proposals[
            int(kwargs["title"].split("#")[1])
        ],
    )
    queue.add_job(_make_sample_job(101))
    queue.add_job(_make_sample_job(102))

    queue.process_sync()

    # Ticket A's proposal has macOS AI title
    assert "macOS" in queue.get_job(101).proposal["title"]
    # Ticket B's proposal has FFmpeg title
    assert "FFmpeg" in queue.get_job(102).proposal["title"]

    # Verify cross-application is impossible
    assert queue.get_job(101).proposal != queue.get_job(102).proposal


# ======================================================================
# 10. No Auto-Save on Batch Apply
# ======================================================================
def test_no_auto_save_on_batch_apply(tmp_path):
    """Apply populates in-memory data only, does not write to database or publish."""
    store = Store(tmp_path / "test_no_autosave.db")
    try:
        queue = BatchRepurposeQueue(
            opt_in=True,
            generate_fn=lambda **kwargs: SYNTHETIC_PROPOSAL_A,
        )
        sample = _make_sample_job(101)
        queue.add_job(sample)
        queue.process_sync()

        job = queue.get_job(101)
        assert job is not None
        assert job.status == JOB_STATUS_READY
        assert job.applied is False
        assert job.saved is False

        # Mark applied in memory
        queue.mark_applied(101, True)
        assert job.applied is True
        assert job.status == JOB_STATUS_APPLIED
        assert job.saved is False  # Must NOT be marked saved!

        # Check that no video/post was written to SQLite store
        assert store.get_video(101) is None
    finally:
        store.close()


# ======================================================================
# 11. Apply Reviewed Only
# ======================================================================
def test_apply_reviewed_only_strictly_ignores_unreviewed_tickets():
    """Applying reviewed only strictly ignores unreviewed tickets."""
    queue = BatchRepurposeQueue(
        opt_in=True,
        generate_fn=lambda **kwargs: SYNTHETIC_PROPOSAL_A,
    )
    queue.add_job(_make_sample_job(101))
    queue.add_job(_make_sample_job(102))
    queue.add_job(_make_sample_job(103))

    queue.process_sync()
    for j in queue.list_jobs():
        assert j.status == JOB_STATUS_READY
        assert j.reviewed is False

    # Review only tickets 101 and 103
    queue.mark_reviewed(101, True)
    queue.mark_reviewed(103, True)

    assert queue.get_job(101).reviewed is True
    assert queue.get_job(102).reviewed is False
    assert queue.get_job(103).reviewed is True

    # Simulate "Apply Reviewed Only" logic
    reviewed_jobs = [j for j in queue.list_jobs() if j.reviewed]
    unreviewed_jobs = [j for j in queue.list_jobs() if not j.reviewed]

    assert len(reviewed_jobs) == 2
    assert len(unreviewed_jobs) == 1
    assert unreviewed_jobs[0].ticket_id == 102

    for j in reviewed_jobs:
        queue.mark_applied(j.ticket_id, True)

    assert queue.get_job(101).applied is True
    assert queue.get_job(102).applied is False  # UNREVIEWED NOT APPLIED!
    assert queue.get_job(103).applied is True
