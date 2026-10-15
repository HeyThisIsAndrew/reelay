"""The one registry of publishing destinations (reelay/platform_specs.py)."""

from __future__ import annotations

from dataclasses import fields

from reelay import platform_specs as S
from reelay import platform_text as T
from reelay.flags import FLAG_PLATFORMS
from reelay.models import MANUAL_PLATFORMS, Platform, Post


def test_every_platform_has_an_entry_and_every_copy_field_is_on_post():
    assert set(S.SPECS) == set(Platform.LABELS)
    post_fields = {f.name for f in fields(Post)}
    for spec in S.SPECS.values():
        assert spec.label == Platform.LABELS[spec.key]                 # labels come from models, not a copy
        assert set(spec.copy_fields) <= post_fields, spec.key
    assert set(S.COPY_FIELDS) <= post_fields and set(S.COPY_FIELDS) == set(S.FIELD_LIMITS)
    assert [s.key for s in S.TICKET] == list(MANUAL_PLATFORMS)


def test_limits_are_the_platform_text_ones():
    assert S.FIELD_LIMITS["tiktok_text"].limit == T.TIKTOK_MAX_CHARS == 4000
    assert S.FIELD_LIMITS["instagram_text"].limit == T.INSTAGRAM_MAX_CHARS
    assert S.FIELD_LIMITS["bluesky_text"].limit == T.BLUESKY_MAX_GRAPHEMES
    assert S.FIELD_LIMITS["threads_text"].limit == T.THREADS_MAX_CHARS
    assert S.FIELD_LIMITS["facebook_text"].limit == T.FACEBOOK_MAX_CHARS
    assert S.count("bluesky_text", "👩‍👩‍👧" * 3) == ("3/300", False)          # graphemes, as Bluesky counts
    assert S.count("threads_text", "hi 👍") == ("7/500", False)              # emoji count as bytes on Threads
    assert S.count("description", "é" * 2) == ("4/5,000 bytes", False)
    assert S.count("tags", "a, #bb") == ("5/500", False)
    assert S.count("tiktok_text", "x" * 4001)[1]


def test_how_each_platform_posts_follows_flags():
    assert S.spec(Platform.YOUTUBE).posting == S.ALWAYS
    assert {k for k, s in S.SPECS.items() if s.posting == S.WHEN_ON} == set(FLAG_PLATFORMS)
    assert {k for k, s in S.SPECS.items() if s.posting == S.BY_HAND} == set(Platform.HAND_ONLY)
    assert {k for k, s in S.SPECS.items() if s.posting == S.PLANNED} == set(Platform.PLANNED)


def test_lookups():
    assert S.by_label("TikTok").key == Platform.TIKTOK and S.by_label("Vimeo") is None
    assert S.platform_of_field("threads_text") == Platform.THREADS and S.platform_of_field("hashtags") is None
    assert S.spec(Platform.YOUTUBE).caption_field == "description"
    assert S.spec(Platform.INSTAGRAM).caption_field == "instagram_text"
