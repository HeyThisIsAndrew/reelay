"""Tests for the Cloud AI Privacy Opt-In Gate (#12)."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from reelay.privacy import (
    CLOUD_AI_ENV_VAR,
    CLOUD_AI_SETTING_KEY,
    DISCLOSURE_TEXT,
    PrivacyOptInRequiredError,
    is_cloud_ai_enabled,
    require_cloud_ai_opt_in,
    set_cloud_ai_enabled,
)
from reelay.repurpose_engine import generate_repurpose_proposal
from reelay.store import Store

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


def test_default_state_is_disabled(monkeypatch, tmp_path):
    """Cloud AI opt-in defaults to False for any new user or store."""
    monkeypatch.delenv(CLOUD_AI_ENV_VAR, raising=False)

    assert is_cloud_ai_enabled(None) is False
    assert is_cloud_ai_enabled({}) is False

    store = Store(tmp_path / "test_privacy.db")
    try:
        assert is_cloud_ai_enabled(store) is False
    finally:
        store.close()


@patch("reelay.repurpose_engine._call_meta_model")
def test_disabled_call_raises_privacy_opt_in_required_error(mock_call, monkeypatch, tmp_path):
    """When opt-in is disabled, cloud calls are blocked before reaching the network."""
    monkeypatch.delenv(CLOUD_AI_ENV_VAR, raising=False)
    store = Store(tmp_path / "test_privacy_blocked.db")
    try:
        with pytest.raises(PrivacyOptInRequiredError) as exc_info:
            require_cloud_ai_opt_in(store)
        assert "Cloud AI features are disabled" in str(exc_info.value)

        with pytest.raises(PrivacyOptInRequiredError):
            generate_repurpose_proposal(
                title="Test Video",
                description="Test Description",
                api_key="mock-test-key",
                store=store,
            )

        with pytest.raises(PrivacyOptInRequiredError):
            generate_repurpose_proposal(
                title="Test Video",
                description="Test Description",
                api_key="mock-test-key",
                opt_in=False,
            )

        assert mock_call.call_count == 0
    finally:
        store.close()


@patch("reelay.repurpose_engine._call_meta_model")
def test_call_without_store_opt_in_or_env_raises_privacy_error(mock_call, monkeypatch):
    """A direct call with no store, no opt_in, and no env var must raise PrivacyOptInRequiredError."""
    monkeypatch.delenv(CLOUD_AI_ENV_VAR, raising=False)

    with pytest.raises(PrivacyOptInRequiredError) as exc_info:
        generate_repurpose_proposal(
            title="Test Video",
            description="Test Description",
            api_key="mock-test-key",
        )

    assert "Cloud AI features are disabled" in str(exc_info.value)
    assert mock_call.call_count == 0


@patch("reelay.repurpose_engine._call_meta_model")
def test_explicit_opt_in_allows_call_to_proceed(mock_call, monkeypatch, tmp_path):
    """Once the creator explicitly opts in, the cloud call is allowed to proceed."""
    monkeypatch.delenv(CLOUD_AI_ENV_VAR, raising=False)
    mock_call.return_value = json.dumps(SAMPLE_VALID_PAYLOAD)

    store = Store(tmp_path / "test_privacy_allowed.db")
    try:
        set_cloud_ai_enabled(store, True)
        assert is_cloud_ai_enabled(store) is True
        require_cloud_ai_opt_in(store)

        proposal = generate_repurpose_proposal(
            title="Test Video",
            description="Test Description",
            api_key="mock-test-key",
            store=store,
        )
        assert proposal["title"] == SAMPLE_VALID_PAYLOAD["title"]
        assert mock_call.call_count == 1

        # Turning opt-in off immediately blocks subsequent calls
        set_cloud_ai_enabled(store, False)
        assert is_cloud_ai_enabled(store) is False
        with pytest.raises(PrivacyOptInRequiredError):
            generate_repurpose_proposal(
                title="Test Video",
                description="Test Description",
                api_key="mock-test-key",
                store=store,
            )
    finally:
        store.close()


def test_env_var_opt_in_and_opt_out(monkeypatch):
    """Environment variable REELAY_CLOUD_AI_OPT_IN overrides default state."""
    monkeypatch.setenv(CLOUD_AI_ENV_VAR, "1")
    assert is_cloud_ai_enabled() is True
    require_cloud_ai_opt_in()

    monkeypatch.setenv(CLOUD_AI_ENV_VAR, "0")
    assert is_cloud_ai_enabled() is False
    with pytest.raises(PrivacyOptInRequiredError):
        require_cloud_ai_opt_in()


def test_disclosure_text_completeness():
    """Disclosure text clearly states Meta, api.meta.ai/v1, muse-spark-1.3, and non-contributor tier."""
    assert "Meta Model API" in DISCLOSURE_TEXT
    assert "https://api.meta.ai/v1" in DISCLOSURE_TEXT
    assert "muse-spark-1.3" in DISCLOSURE_TEXT
    assert "non-contributor" in DISCLOSURE_TEXT
    assert "brand voice" in DISCLOSURE_TEXT
    assert CLOUD_AI_SETTING_KEY == "privacy.cloud_ai_opt_in"
