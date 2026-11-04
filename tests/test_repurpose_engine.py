"""Unit and dry-run tests for reelay.repurpose_engine.

Requires NO API key and makes NO real network requests.
Verifies schema compliance, FIELD_LIMITS enforcement, zero-truncation guarantee,
Threads hashtag isolation, and error handling.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from reelay.platform_specs import FIELD_LIMITS, count
from reelay.repurpose_engine import (
    DEFAULT_MODEL,
    SCHEMA_FIELDS,
    RepurposeAPIError,
    RepurposeConfigError,
    RepurposeValidationError,
    _clean_threads_hashtags,
    _parse_and_normalize_json,
    _validate_fields,
    generate_repurpose_proposal,
)


# Sample valid mock payload
SAMPLE_VALID_PAYLOAD = {
    "title": "Building Local AI Pipelines on macOS",
    "description": "A deep dive into local-first desktop architectures and loopback APIs.",
    "tags": "python, macos, local ai, developer tools",
    "tiktok_text": "Here is why we built a local AI pipeline instead of another cloud SaaS.",
    "instagram_text": "Building tools that run directly on your hardware gives you total privacy and zero latency.",
    "bluesky_text": "Local-first video automation running entirely on macOS. Here is how it works under the hood.",
    "threads_text": "Why are so many dev tools cloud-only when local machines have so much power?",
    "facebook_text": "Excited to share our latest architecture update for Reelay: local agent orchestration.",
    "hashtags": "#BuildInPublic #LocalAI #Python",
}


def test_missing_api_key_raises_config_error(monkeypatch):
    """When META_API_KEY is unset, generation fails gracefully with RepurposeConfigError."""
    monkeypatch.delenv("META_API_KEY", raising=False)

    with pytest.raises(RepurposeConfigError) as exc_info:
        generate_repurpose_proposal(
            title="Test Title",
            description="Test Description",
            api_key=None,
        )

    assert "META_API_KEY is not set" in str(exc_info.value)


def test_empty_input_raises_validation_error():
    """Empty title and description raise RepurposeValidationError immediately."""
    with pytest.raises(RepurposeValidationError) as exc_info:
        generate_repurpose_proposal(
            title="   ",
            description="",
            api_key="mock-key",
        )
    assert "At least title or description must be provided" in str(exc_info.value)


def test_parse_and_normalize_json():
    """Verify that all 9 schema fields are present and coerced to strings."""
    raw = json.dumps(SAMPLE_VALID_PAYLOAD)
    proposal = _parse_and_normalize_json(raw)

    assert set(proposal.keys()) == set(SCHEMA_FIELDS)
    for field in SCHEMA_FIELDS:
        assert isinstance(proposal[field], str)
        assert len(proposal[field]) > 0


def test_threads_hashtags_cleaned_and_migrated():
    """Threads text must have hashtags removed; removed tags are migrated to shared hashtags."""
    threads_raw = "Testing local agents on Mac #buildinpublic #pythonrocks and more text."
    shared_raw = "#tech"

    cleaned_threads, merged_shared = _clean_threads_hashtags(threads_raw, shared_raw)

    assert "#" not in cleaned_threads
    assert "Testing local agents on Mac and more text." == cleaned_threads
    assert "#tech" in merged_shared
    assert "#buildinpublic" in merged_shared
    assert "#pythonrocks" in merged_shared


def test_validation_within_limits():
    """Field limit verification passes when copy is within limits."""
    errors = _validate_fields(SAMPLE_VALID_PAYLOAD)
    assert errors == {}


def test_zero_truncation_over_limit_detection():
    """Over-limit copy must be flagged in errors and NEVER mechanically cut off."""
    oversized = dict(SAMPLE_VALID_PAYLOAD)
    # Bluesky limit is 300 graphemes; make it 350
    oversized["bluesky_text"] = "X" * 350

    errors = _validate_fields(oversized)
    assert "bluesky_text" in errors

    display, is_over = count("bluesky_text", oversized["bluesky_text"])
    assert is_over is True
    # Verify copy is intact (not truncated)
    assert len(oversized["bluesky_text"]) == 350


@patch("reelay.repurpose_engine._call_meta_model")
def test_successful_mocked_generation(mock_call):
    """Verifies complete generation flow with mocked Meta Model API response."""
    mock_call.return_value = json.dumps(SAMPLE_VALID_PAYLOAD)

    proposal = generate_repurpose_proposal(
        title="Sample Title",
        description="Sample Description",
        notes="Dev audience",
        api_key="test-key-mock",
    )

    assert proposal["title"] == SAMPLE_VALID_PAYLOAD["title"]
    assert proposal["bluesky_text"] == SAMPLE_VALID_PAYLOAD["bluesky_text"]
    assert proposal["threads_text"] == SAMPLE_VALID_PAYLOAD["threads_text"]
    assert mock_call.call_count == 1


@patch("reelay.repurpose_engine._call_meta_model")
def test_over_limit_triggers_refinement_or_error(mock_call):
    """If model produces over-limit copy and refinement still exceeds, raises RepurposeValidationError."""
    over_limit_payload = dict(SAMPLE_VALID_PAYLOAD)
    over_limit_payload["title"] = "T" * 150  # Limit is 100

    # Mock both initial call and rewrite call returning over-limit text
    mock_call.side_effect = [
        json.dumps(over_limit_payload),
        json.dumps({"title": "T" * 120}),  # Still over 100
    ]

    with pytest.raises(RepurposeValidationError) as exc_info:
        generate_repurpose_proposal(
            title="Sample Title",
            description="Sample Description",
            api_key="test-key-mock",
            allow_refinement=True,
        )

    assert "exceeds platform limits and cannot be truncated" in str(exc_info.value)
    assert "title" in exc_info.value.field_errors
