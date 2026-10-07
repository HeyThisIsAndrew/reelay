from pathlib import Path

from reelay.library import Library
from reelay.manifest import read_custom_names, write_manifest
from reelay.models import Status, VideoItem
from reelay.naming import assign_filenames, render_stem, sanitize_stem, slugify


def make(vid="123", date="20261002", custom="", desc="the loneliness in the middle #growth", **kw):
    return VideoItem(platform="TikTok", video_id=vid, url=f"https://tiktok.com/v/{vid}",
                     upload_date=date, custom_name=custom, description=desc,
                     title=kw.pop("title", desc[:20] + "..."), **kw)


# ---------------------------------------------------------------- naming
def test_default_template_matches_cli_convention():
    assert render_stem("{platform}_{date}_{id}", make()) == "TikTok_20261002_123"


def test_slugify_strips_hashtags_emoji_and_apostrophes():
    assert slugify("Who you've been ✨🦋 #growth #trust") == "who-youve-been"


def test_custom_falls_back_to_id_and_illegal_chars_removed():
    assert render_stem("{date}_{custom}", make()) == "20261002_123"
    assert render_stem("{custom}", make(custom='Letting go: part 1/2?')) == "Letting go part 12"


def test_unknown_tokens_are_left_alone_and_missing_date_is_zeros():
    assert render_stem("{date}_{oops}", make(date="")) == "00000000_{oops}"


def test_empty_result_falls_back_to_default():
    assert render_stem("{custom}", make(vid="")) .startswith("TikTok_20261002")


def test_windows_reserved_names_are_escaped():
    assert sanitize_stem("CON") == "_CON"


def test_duplicate_names_get_suffixes_case_insensitively():
    a, b, c = make("1", custom="Hook"), make("2", custom="hook"), make("3", custom="Hook")
    names = assign_filenames([a, b, c], "{custom}")
    assert names == {"TikTok_1": "Hook.mp4", "TikTok_2": "hook_2.mp4", "TikTok_3": "Hook_3.mp4"}


def test_hook_uses_caption_when_title_is_truncated_or_generic():
    assert make().hook == "the loneliness in the middle #growth"
    assert make(title="Video by lydia", desc="Real hook\nmore").hook == "Real hook"
    assert make(title="A real title", desc="caption").hook == "A real title"


# ---------------------------------------------------------------- manifest
def test_manifest_escapes_pipes_and_round_trips_custom_names(tmp_path: Path):
    item = make(custom="name | with pipe", filename="TikTok_20261002_123.mp4",
                status=Status.DOWNLOADED, desc="line one | pipe\nline two")
    path = tmp_path / "manifest.md"
    write_manifest(path, [item], ["https://tiktok.com/@x"])
    text = path.read_text()
    assert r"line one \| pipe line two" in text
    assert read_custom_names(path) == {"TikTok_20261002_123.mp4": "name | with pipe"}


# ---------------------------------------------------------------- library / renames
def _downloaded(lib: Library, vid: str, filename: str, custom: str) -> VideoItem:
    (lib.folder / filename).write_bytes(vid.encode())
    item = make(vid, custom=custom, filename=filename, status=Status.DOWNLOADED)
    lib.save(item)
    return item


def test_batch_rename_handles_swaps_and_keeps_contents(tmp_path: Path):
    lib = Library(tmp_path)
    a = _downloaded(lib, "1", "first.mp4", custom="second")
    b = _downloaded(lib, "2", "second.mp4", custom="first")
    steps = lib.plan_renames([a, b], "{custom}")
    assert {(s.old, s.new) for s in steps} == {("first.mp4", "second.mp4"), ("second.mp4", "first.mp4")}
    assert lib.apply_renames(steps) == []
    assert (tmp_path / "second.mp4").read_bytes() == b"1"
    assert (tmp_path / "first.mp4").read_bytes() == b"2"
    reloaded = {i.video_id: i.filename for i in lib.load()}
    assert reloaded == {"1": "second.mp4", "2": "first.mp4"}


def test_rename_refuses_to_overwrite_unrelated_file(tmp_path: Path):
    lib = Library(tmp_path)
    a = _downloaded(lib, "1", "a.mp4", custom="taken")
    (tmp_path / "taken.mp4").write_bytes(b"someone else's file")
    errors = lib.apply_renames(lib.plan_renames([a], "{custom}"))
    assert errors and (tmp_path / "a.mp4").exists()
    assert (tmp_path / "taken.mp4").read_bytes() == b"someone else's file"


def test_case_only_rename(tmp_path: Path):
    lib = Library(tmp_path)
    a = _downloaded(lib, "1", "hook.mp4", custom="Hook")
    assert lib.apply_renames(lib.plan_renames([a], "{custom}")) == []
    assert [p.name for p in tmp_path.glob("*.mp4")] == ["Hook.mp4"]


def test_load_skips_records_whose_video_was_deleted(tmp_path: Path):
    lib = Library(tmp_path)
    _downloaded(lib, "1", "keep.mp4", custom="")
    _downloaded(lib, "2", "gone.mp4", custom="")
    (tmp_path / "gone.mp4").unlink()
    assert [i.video_id for i in lib.load()] == ["1"]


def test_custom_names_persist_for_undownloaded_items(tmp_path: Path):
    lib = Library(tmp_path)
    lib.save_custom_names([make("9", custom="later")])
    assert lib.load_custom_names() == {"TikTok_9": "later"}
    lib.save_custom_names([make("9", custom="")])
    assert lib.load_custom_names() == {}
