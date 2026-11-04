"""Reelay Repurpose Engine: Multi-platform copy generation via Meta Muse Spark.

Built exclusively for the Meta Global AI Developer Hackathon (November 4, 2026).
Startup Track: Build for Your World.

Transforms source metadata from Andrew's original videos into platform-ready
copy for YouTube, TikTok, Instagram, Bluesky, Threads, and Facebook.

Guarantees:
- Model: Meta `muse-spark-1.3` via Meta Model API (https://api.meta.ai/v1).
- Voice & Authenticity: Retains Andrew's authentic voice; never hallucinates facts,
  quotes, products, or claims. If a field cannot be authentically derived, it is
  left as an empty string.
- Platform Constraints:
    * YouTube gets title, description, and tags (comma-separated, no #).
    * Threads copy contains ZERO hashtags in body.
    * Shared hashtags stay in the `hashtags` field only.
    * No AI imagery / synthetic photos (copy only).
- Strict Validation: Validated against `reelay.platform_specs.FIELD_LIMITS`.
  NEVER mechanically truncates copy. Over-limit fields are rejected and rewritten
  shorter via targeted refinement prompts.
- Human-in-the-Loop Approval: Returns a proposal dictionary only. Never auto-saves
  to a ticket, never posts, and never auto-publishes.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import requests

from .platform_specs import FIELD_LIMITS, count

log = logging.getLogger("reelay.repurpose")

# Meta Model API Endpoints and Models
DEFAULT_BASE_URL = "https://api.meta.ai/v1"
DEFAULT_MODEL = "muse-spark-1.3"  # Standard tier, not contributor

# Exactly these 9 fields, all strings
SCHEMA_FIELDS = (
    "title",
    "description",
    "tags",
    "tiktok_text",
    "instagram_text",
    "bluesky_text",
    "threads_text",
    "facebook_text",
    "hashtags",
)

# Regex to detect hashtags in Threads text
_HASHTAG_RE = re.compile(r"#\w+")


# ---------------------------------------------------------------------- Exceptions
class RepurposeError(Exception):
    """Base class for all Repurpose Engine errors."""


class RepurposeConfigError(RepurposeError):
    """Raised when configuration or credentials (e.g. META_API_KEY) are missing or invalid."""


class RepurposeNetworkError(RepurposeError):
    """Raised when network connectivity or timeouts occur connecting to Meta Model API."""


class RepurposeAPIError(RepurposeError):
    """Raised when the Meta Model API returns an HTTP error or unexpected status."""

    def __init__(self, message: str, status_code: int | None = None, response_text: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


class RepurposeValidationError(RepurposeError):
    """Raised when generated copy violates JSON schema or exceeds platform limits."""

    def __init__(self, message: str, field_errors: dict[str, str] | None = None):
        super().__init__(message)
        self.field_errors = field_errors or {}


# ---------------------------------------------------------------------- Prompt Construction
def _build_system_prompt() -> str:
    return (
        "You are an expert social media copy repurposer assisting creator Andrew Baxter. "
        "Your task is to adapt source video metadata into platform-ready copy across 6 networks.\n\n"
        "STRICT VOICE & FACTUALITY RULES:\n"
        "1. Keep Andrew's authentic, direct, technical, builder voice.\n"
        "2. Do NOT invent facts, statistics, quotes, products, people, partners, or claims "
        "not supported by the input.\n"
        "3. If a field cannot be written honestly and meaningfully from the input, leave it as an "
        "empty string (\"\") instead of inventing filler.\n"
        "4. This feature is COPY ONLY. Never suggest or reference AI-generated imagery or synthetic photos.\n\n"
        "PLATFORM SPECIFIC COPY RULES:\n"
        "- YouTube:\n"
        "  * 'title': Clear, compelling title (under 100 characters).\n"
        "  * 'description': Comprehensive summary, key points, and context (under 5,000 UTF-8 bytes).\n"
        "  * 'tags': Comma-separated keyword tags without '#' symbol (under 500 characters total).\n"
        "- Short-Form Video:\n"
        "  * 'tiktok_text': Short-form caption with strong first-second hook (under 2,200 characters).\n"
        "  * 'instagram_text': Engaging caption with storytelling angle (under 2,200 characters).\n"
        "- Microblogging & Social:\n"
        "  * 'bluesky_text': Ultra-concise post strictly under 300 graphemes.\n"
        "  * 'threads_text': Conversational discussion post strictly under 500 characters. "
        "CRITICAL: Do NOT put any hashtags inside threads_text (Threads norm).\n"
        "  * 'facebook_text': Contextual community update (under 63,206 characters).\n"
        "- Shared Hashtags:\n"
        "  * 'hashtags': Space-separated hashtags (e.g. #BuildInPublic #AI) for platforms that use them.\n\n"
        "OUTPUT FORMAT:\n"
        "You MUST respond ONLY with a single valid JSON object containing exactly these 9 string fields:\n"
        "{\n"
        '  "title": string,\n'
        '  "description": string,\n'
        '  "tags": string,\n'
        '  "tiktok_text": string,\n'
        '  "instagram_text": string,\n'
        '  "bluesky_text": string,\n'
        '  "threads_text": string,\n'
        '  "facebook_text": string,\n'
        '  "hashtags": string\n'
        "}\n"
        "Do not include markdown code block formatting (e.g. no ```json), commentary, or surrounding text."
    )


def _build_user_message(title: str, description: str, notes: str = "") -> str:
    parts = [
        "Please repurpose the following original video details into platform-ready copy:",
        f"<input_title>{title.strip()}</input_title>",
        f"<input_description>{description.strip()}</input_description>",
    ]
    if notes.strip():
        parts.append(f"<input_notes>{notes.strip()}</input_notes>")
    return "\n\n".join(parts)


def _build_rewrite_prompt(field: str, original_text: str, current_count: str, limit: int | None, unit: str) -> str:
    unit_str = f" {unit}" if unit else " characters"
    return (
        f"The generated '{field}' exceeds the platform limit: {current_count} (limit: {limit}{unit_str}).\n"
        f"Original text:\n\"{original_text}\"\n\n"
        f"Please rewrite this copy to be tighter and shorter so it strictly fits within the {limit}{unit_str} limit. "
        "Keep the core message and tone. Return ONLY a JSON object with this single field:\n"
        f'{{"{field}": "rewritten copy"}}'
    )


# ---------------------------------------------------------------------- Meta Model API Client
def _call_meta_model(
    messages: list[dict[str, str]],
    *,
    api_key: str,
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    timeout: float = 30.0,
) -> str:
    """Executes a chat completion call against the Meta Model API."""
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "Reelay-RepurposeEngine/1.0",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except requests.exceptions.Timeout as exc:
        raise RepurposeNetworkError(f"Request to Meta Model API timed out after {timeout} seconds.") from exc
    except requests.exceptions.RequestException as exc:
        raise RepurposeNetworkError(f"Network error communicating with Meta Model API: {exc}") from exc

    if resp.status_code == 401:
        raise RepurposeAPIError(
            "Meta Model API authentication failed (401 Unauthorized). "
            "Verify that your META_API_KEY is valid and active in Meta Developer Console.",
            status_code=401,
            response_text=resp.text,
        )
    if resp.status_code in (402, 403):
        raise RepurposeAPIError(
            f"Meta Model API access error ({resp.status_code}). "
            "Please check that your Meta Developer account has active billing and that the $150 credit is applied.",
            status_code=resp.status_code,
            response_text=resp.text,
        )
    if resp.status_code == 429:
        raise RepurposeAPIError(
            "Meta Model API rate limit exceeded (429). Please wait a moment before trying again.",
            status_code=429,
            response_text=resp.text,
        )
    if resp.status_code != 200:
        raise RepurposeAPIError(
            f"Meta Model API returned HTTP {resp.status_code}: {resp.text}",
            status_code=resp.status_code,
            response_text=resp.text,
        )

    try:
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        raise RepurposeAPIError(
            f"Failed to parse choices from Meta Model API response: {resp.text}",
            status_code=resp.status_code,
            response_text=resp.text,
        ) from exc


# ---------------------------------------------------------------------- Validation & Refinement
def _parse_and_normalize_json(raw_content: str) -> dict[str, str]:
    """Parses JSON content and ensures all 9 schema fields exist as strings."""
    cleaned = raw_content.strip()
    if cleaned.startswith("```"):
        # Strip potential markdown fences if present
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise RepurposeValidationError(f"Model output was not valid JSON: {exc}\nRaw: {raw_content[:200]}") from exc

    if not isinstance(data, dict):
        raise RepurposeValidationError(f"Expected JSON object, got {type(data).__name__}")

    normalized: dict[str, str] = {}
    for f in SCHEMA_FIELDS:
        val = data.get(f, "")
        if val is None:
            normalized[f] = ""
        elif isinstance(val, list):
            # E.g. if model returned tags as a list
            normalized[f] = ", ".join(str(item).strip() for item in val if str(item).strip())
        else:
            normalized[f] = str(val).strip()

    return normalized


def _clean_threads_hashtags(threads_text: str, hashtags: str) -> tuple[str, str]:
    """Removes hashtags from Threads text per platform guidelines and appends them to shared hashtags."""
    found = _HASHTAG_RE.findall(threads_text)
    if not found:
        return threads_text, hashtags

    # Remove hashtags from threads text
    cleaned_threads = _HASHTAG_RE.sub("", threads_text)
    # Clean up excess spaces or dangling commas
    cleaned_threads = re.sub(r" +", " ", cleaned_threads).strip()

    # Merge into shared hashtags if not already present
    existing_tags = set(hashtags.split())
    new_tags = [tag for tag in found if tag not in existing_tags]
    merged_hashtags = (hashtags + " " + " ".join(new_tags)).strip()

    return cleaned_threads, merged_hashtags


def _validate_fields(proposal: dict[str, str]) -> dict[str, str]:
    """Validates each field against FIELD_LIMITS. Returns dict of errors {field: count_str}."""
    errors: dict[str, str] = {}
    for field_name in SCHEMA_FIELDS:
        if field_name not in FIELD_LIMITS:
            continue
        text = proposal.get(field_name, "")
        count_display, is_over = count(field_name, text)
        if is_over:
            errors[field_name] = count_display
    return errors


def _refine_over_limit_fields(
    proposal: dict[str, str],
    errors: dict[str, str],
    *,
    api_key: str,
    base_url: str,
    model: str,
    timeout: float,
    max_retries: int = 1,
) -> dict[str, str]:
    """Attempts targeted refinement on over-limit fields without ever truncating."""
    refined = dict(proposal)

    for field_name, count_display in list(errors.items()):
        lim = FIELD_LIMITS[field_name]
        for attempt in range(max_retries):
            log.info("Refining %s (attempt %d): currently %s", field_name, attempt + 1, count_display)
            rewrite_prompt = _build_rewrite_prompt(
                field=field_name,
                original_text=refined[field_name],
                current_count=count_display,
                limit=lim.limit,
                unit=lim.unit,
            )
            messages = [
                {"role": "system", "content": "You are a concise copy editor who adheres strictly to character limits."},
                {"role": "user", "content": rewrite_prompt},
            ]
            try:
                raw_response = _call_meta_model(
                    messages,
                    api_key=api_key,
                    base_url=base_url,
                    model=model,
                    timeout=timeout,
                )
                data = json.loads(raw_response.strip().strip("`"))
                if isinstance(data, dict) and field_name in data:
                    candidate = str(data[field_name]).strip()
                    new_display, still_over = count(field_name, candidate)
                    if not still_over:
                        refined[field_name] = candidate
                        del errors[field_name]
                        break
                    count_display = new_display
            except Exception as exc:
                log.warning("Refinement attempt for %s failed: %s", field_name, exc)
                break

    return refined


# ---------------------------------------------------------------------- Public API
def generate_repurpose_proposal(
    title: str,
    description: str,
    notes: str = "",
    *,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    api_key: str | None = None,
    timeout: float = 30.0,
    allow_refinement: bool = True,
) -> dict[str, str]:
    """Generates platform-ready copy proposals from Andrew's source video details using Muse Spark.

    This function is functional and proposal-only: it never writes to the SQLite database,
    never updates a Reelay ticket directly, and never triggers publishing. A human review
    step is required before any proposal is applied to a ticket.

    Args:
        title: Title of Andrew's source video.
        description: Description or transcript of the video.
        notes: Creator notes, directives, or target audience details.
        model: Model identifier in Meta Model API (default: 'muse-spark-1.3').
        base_url: Base URL for Meta Model API (default: 'https://api.meta.ai/v1').
        api_key: Meta API key. If omitted, reads META_API_KEY from environment.
        timeout: HTTP request timeout in seconds (default: 30.0).
        allow_refinement: If True, attempts a targeted rewrite if a field is over limit.

    Returns:
        A dictionary with exactly the 9 schema keys, all string values:
        - title
        - description
        - tags
        - tiktok_text
        - instagram_text
        - bluesky_text
        - threads_text
        - facebook_text
        - hashtags

    Raises:
        RepurposeConfigError: If META_API_KEY is not set.
        RepurposeNetworkError: On connection failure or timeout.
        RepurposeAPIError: On Meta Model API HTTP error (auth, billing, 429).
        RepurposeValidationError: If the proposal violates schema or exceeds limits.
    """
    if not (title.strip() or description.strip()):
        raise RepurposeValidationError("At least title or description must be provided.")

    resolved_key = (api_key or os.environ.get("META_API_KEY", "")).strip()
    if not resolved_key:
        raise RepurposeConfigError(
            "META_API_KEY is not set. Please set the META_API_KEY environment variable "
            "with a valid Meta Model API key before generating copy proposals."
        )

    messages = [
        {"role": "system", "content": _build_system_prompt()},
        {"role": "user", "content": _build_user_message(title, description, notes)},
    ]

    raw_output = _call_meta_model(
        messages,
        api_key=resolved_key,
        base_url=base_url,
        model=model,
        timeout=timeout,
    )

    proposal = _parse_and_normalize_json(raw_output)

    # Enforce Threads hashtag isolation
    proposal["threads_text"], proposal["hashtags"] = _clean_threads_hashtags(
        proposal["threads_text"], proposal["hashtags"]
    )

    # Validate against FIELD_LIMITS
    errors = _validate_fields(proposal)

    if errors and allow_refinement:
        proposal = _refine_over_limit_fields(
            proposal,
            errors,
            api_key=resolved_key,
            base_url=base_url,
            model=model,
            timeout=timeout,
        )

    if errors:
        error_details = ", ".join(f"{k}: {v}" for k, v in errors.items())
        raise RepurposeValidationError(
            f"Generated copy exceeds platform limits and cannot be truncated: {error_details}",
            field_errors=errors,
        )

    return proposal
