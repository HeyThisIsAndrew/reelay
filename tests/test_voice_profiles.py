"""Voice profiles drive the Repurpose Engine (Phase 3): tone, banned phrases, emoji use, hashtag style, call to
action and a YouTube footer. No model or network is contacted."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from reelay import assist
from reelay.models import BrandProfile
from reelay.repurpose_engine import (RepurposeValidationError, _build_system_prompt, _validate_fields,
                                     add_footer, apply_voice_rules, generate_repurpose_proposal)
from reelay.store import Store

FOOTER = "━━━━━━━━━━━━\nCheck out my other socials:\n@BeUnconventionalHQ"
VOICE = BrandProfile(profile_id=1, name="Hey, This Is Andrew", tone="first person, honest, specific",
                     avoid="game-changer, dive in, smash that like button", hashtags="#buildinpublic",
                     emoji="none", hashtag_style="3 to 5 specific tags, #Shorts first on Shorts",
                     cta="end with one real question for the comments", footer=FOOTER)
GOOD = {
    "title": "Why I Bought the Sony 16-35mm GM Twice",
    "description": "My first 16-35 GM broke, so I bought the GM II the same week. Constant f/2.8 in a lens this "
                   "small is why it never leaves my a7CR.",
    "tags": "sony 16-35mm gm ii, sony a7cr, run and gun",
    "tiktok_text": "My 16-35 GM broke. I bought it again the same week. Here's why.",
    "instagram_text": "Bought the same lens twice. Constant f/2.8 in something this compact is hard to give up.",
    "bluesky_text": "My 16-35 GM broke, so I bought the GM II. Constant f/2.8 that small is hard to quit.",
    "threads_text": "Bought the same lens twice. Would you have switched to a different focal range?",
    "facebook_text": "My 16-35 GM broke and I replaced it with the GM II the same week. Which lens would you rebuy?",
    "hashtags": "#Shorts #sonyalpha #buildinpublic",
}


def test_voice_fields_round_trip_through_the_store_and_reach_the_prompt(tmp_path):
    store = Store(tmp_path / "v.db")
    profile = store.create_profile("Hey This Is Andrew", tmp_path / "lib")
    saved = store.save_brand_profile(BrandProfile(**{**VOICE.__dict__, "profile_id": profile.id, "id": None,
                                                     "active": True}))
    again = store.get_brand_profile(saved.id)
    assert (again.emoji, again.hashtag_style, again.cta, again.footer) == (
        "none", VOICE.hashtag_style, VOICE.cta, FOOTER)
    prompt = assist.system_prompt(again)
    assert "Emoji: No emoji at all." in prompt
    assert "Hashtag style: 3 to 5 specific tags" in prompt
    assert "Call to action: end with one real question" in prompt
    assert FOOTER not in prompt                       # the footer is added afterwards, never written by the model
    # an unknown emoji level is not stored
    again.emoji = "lots"
    assert store.get_brand_profile(store.save_brand_profile(again).id).emoji == ""


def test_the_system_prompt_makes_the_transcript_the_source_of_truth():
    prompt = _build_system_prompt(assist.system_prompt(VOICE))
    assert "SOURCE OF TRUTH" in prompt and "follow the transcript" in prompt
    assert "PASTE-READY" in prompt and "—" not in prompt.replace("(—)", "")


def test_banned_phrases_and_placeholders_are_problems():
    bad = dict(GOOD, tiktok_text="This lens is a game-changer.", facebook_text="Get it here: [link]")
    errors = _validate_fields(bad, VOICE)
    assert errors["tiktok_text"] == "breaks the voice profile: uses 'game-changer'"
    assert errors["facebook_text"].startswith("breaks the voice profile: has a placeholder")
    assert _validate_fields(GOOD, VOICE) == {}


def test_emoji_rules_never_change_the_words():
    noisy = dict(GOOD, title="Why I Bought It Twice 👀", tiktok_text="Bought it twice 📸🔥 really")
    assert apply_voice_rules(noisy, VOICE)["tiktok_text"] == "Bought it twice really"
    sparing = BrandProfile(profile_id=1, name="x", emoji="sparing")
    kept = apply_voice_rules(noisy, sparing)
    assert kept["title"] == "Why I Bought It Twice" and kept["tiktok_text"] == noisy["tiktok_text"]
    assert apply_voice_rules(noisy, None) == noisy


def test_footer_is_added_once_and_counts_toward_the_youtube_limit():
    out = add_footer(GOOD, FOOTER)
    assert out["description"].endswith(FOOTER) and out["description"].count(FOOTER) == 1
    assert add_footer(out, FOOTER)["description"] == out["description"]
    long_body = dict(GOOD, description="x" * 4990)
    assert "description" in _validate_fields(long_body, VOICE, FOOTER)       # 4,990 + footer > 5,000 bytes
    assert "description" not in _validate_fields(long_body, VOICE, "")


@patch("reelay.repurpose_engine._call_meta_model")
def test_generate_rewrites_a_banned_phrase_and_adds_the_footer(mock_call):
    first = dict(GOOD, tiktok_text="This lens is a game-changer 🔥")
    mock_call.side_effect = [json.dumps(first), json.dumps({"tiktok_text": "This lens never leaves my camera."})]
    out = generate_repurpose_proposal("t", "d", transcript="the 16-35 GM is my favorite lens", api_key="k",
                                      opt_in=True, voice=VOICE)
    assert out["tiktok_text"] == "This lens never leaves my camera."
    assert out["description"].endswith(FOOTER)
    system = mock_call.call_args_list[0].args[0][0]["content"]
    assert "Never use these words or phrases" in system and "game-changer" in system
    rewrite = mock_call.call_args_list[1].args[0][1]["content"]
    assert "breaks the voice profile: uses 'game-changer'" in rewrite


@patch("reelay.repurpose_engine._call_meta_model")
def test_a_draft_that_keeps_breaking_the_voice_is_refused_not_saved(mock_call):
    first = dict(GOOD, threads_text="Let's dive in. Smash that like button.")
    mock_call.side_effect = [json.dumps(first), json.dumps({"threads_text": "Dive in and smash that like button."})]
    with pytest.raises(RepurposeValidationError) as exc:
        generate_repurpose_proposal("t", "d", api_key="k", opt_in=True, voice=VOICE)
    assert "breaks the voice profile" in str(exc.value)
    assert "threads_text" in exc.value.field_errors


# ------------------------------------------------------------------ batch: transcript first, then the voice
def test_batch_fetches_missing_transcripts_and_drafts_in_the_profiles_voice(tmp_path, monkeypatch):
    from reelay import transcripts
    from reelay.batch_repurpose import BatchJob, BatchRepurposeQueue, JOB_STATUS_FAILED, JOB_STATUS_READY
    from reelay.models import VideoItem

    store = Store(tmp_path / "b.db")
    profile = store.create_profile("Hey This Is Andrew", tmp_path / "lib")
    store.save_brand_profile(BrandProfile(**{**VOICE.__dict__, "profile_id": profile.id, "id": None,
                                             "active": True}))
    ids = store.save_videos(profile.id, [
        VideoItem(platform="YouTube", video_id=f"v{n}", url=f"https://www.youtube.com/shorts/v{n}", title=f"T{n}")
        for n in range(3)])
    rows = [ids[f"YouTube_v{n}"] for n in range(3)]
    store.update_ticket(rows[2], {"transcript": "already here"})

    fetched = []

    def fake_fetch(url, opts):
        fetched.append(url)
        if url.endswith("v1"):
            raise transcripts.TranscriptError("no captions")
        return "the 16-35 GM is my favorite lens", {}
    monkeypatch.setattr(transcripts, "fetch_transcript", fake_fetch)

    seen = {}

    def generate(**kwargs):
        seen[kwargs["title"]] = (kwargs["transcript"], kwargs.get("voice"))
        if kwargs["title"] == "T1":
            raise RepurposeValidationError("Generated copy still breaks the voice profile after a rewrite")
        return GOOD

    queue = BatchRepurposeQueue(opt_in=True, store=store, generate_fn=generate, fetch_transcripts=True)
    for n, row in enumerate(rows):
        queue.add_job(BatchJob(ticket_id=row, source_snapshot={"title": f"T{n}", "description": "",
                                                                  "notes": "", "brand_voice": "",
                                                                  "transcript": store.get_video(row).ticket.transcript
                                                                  or ""}))
    jobs = {j.ticket_id: j for j in queue.process_sync()}

    assert fetched == ["https://www.youtube.com/shorts/v0", "https://www.youtube.com/shorts/v1"]   # not v2
    assert seen["T0"][0] == "the 16-35 GM is my favorite lens" and seen["T0"][1].name == VOICE.name
    assert store.get_video(rows[0]).ticket.transcript == "the 16-35 GM is my favorite lens"
    assert seen["T2"][0] == "already here"
    # per-ticket status: one failure never stops the others
    assert [jobs[r].status for r in rows] == [JOB_STATUS_READY, JOB_STATUS_FAILED, JOB_STATUS_READY]
    assert "breaks the voice profile" in jobs[rows[1]].error


# ------------------------------------------------------------------ voice files
def test_the_owners_voice_file_imports_and_round_trips(tmp_path):
    from pathlib import Path

    from reelay.voices import VoiceFileError, export_voice, import_voice
    store = Store(tmp_path / "f.db")
    profile = store.create_profile("Hey This Is Andrew", tmp_path / "lib")
    shipped = Path(__file__).resolve().parent.parent / "voices" / "hey-this-is-andrew.json"
    brand = import_voice(store, profile.id, shipped, use=True)
    assert brand.active and brand.emoji == "sparing" and "game-changer" in brand.avoid
    assert brand.footer.startswith("━━━━") and "—" not in brand.footer + brand.guidance + brand.tone
    # importing again updates the same voice instead of adding a second one
    again = import_voice(store, profile.id, shipped)
    assert again.id == brand.id and again.active and len(store.list_brand_profiles(profile.id)) == 1
    out = tmp_path / "out.json"
    export_voice(again, out)
    assert import_voice(store, profile.id, out).id == brand.id
    bad = tmp_path / "bad.json"
    bad.write_text('{"name": "x"}')
    with pytest.raises(VoiceFileError):
        import_voice(store, profile.id, bad)


def test_drafts_get_enough_time():
    """A real nine-field draft took 57 s; the old 60 s limit failed about half the time."""
    import inspect

    from reelay.batch_repurpose import BatchRepurposeQueue
    from reelay.repurpose_engine import DEFAULT_TIMEOUT
    assert DEFAULT_TIMEOUT >= 150
    assert inspect.signature(generate_repurpose_proposal).parameters["timeout"].default == DEFAULT_TIMEOUT
    assert inspect.signature(BatchRepurposeQueue.__init__).parameters["timeout"].default == DEFAULT_TIMEOUT
