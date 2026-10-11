"""Regression tests for the Phase 1 security fixes (SEC-01 … SEC-05, SEC-13).

Each test proves a specific way around a safeguard is closed. Nothing here touches the network: yt-dlp
and sockets are replaced where a fetch could start, and every URL check is shown to run BEFORE that.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from reelay import rules as R
from reelay.pipeline import PipelineError
from reelay.scoped_pipeline import ApprovalPending, ScopedPipeline
from tests.fakes import FAKE_MP4, approve_and_run, disable_publishing, make_world


@pytest.fixture
def world(tmp_path, monkeypatch):
    disable_publishing(monkeypatch)
    w = make_world(tmp_path, monkeypatch)          # tickets ready for YouTube
    yield w
    w.store.close()


@pytest.fixture
def no_network(monkeypatch):
    """Any attempt to look up a name or open a connection fails the test loudly. (Numbers are still
    parsed: that's local.) Note: libcurl (curl_cffi) does its own networking outside Python's socket
    module; these tests never reach it because everything is refused earlier."""
    def refuse(*a, **k):
        raise AssertionError(f"network access attempted: {a[:1]}")
    real = socket.getaddrinfo

    def numeric_only(host, port, *a, **k):
        if not (k.get("flags", 0) & socket.AI_NUMERICHOST):
            try:
                return real(host, port, *a, **{**k, "flags": k.get("flags", 0) | socket.AI_NUMERICHOST})
            except socket.gaierror:
                refuse((host, port))
        return real(host, port, *a, **k)
    monkeypatch.setattr(socket, "getaddrinfo", numeric_only)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", lambda self, *a, **k: refuse(*a))


class ExplodingYDL:
    """Stands in for yt_dlp.YoutubeDL: constructing it at all means a fetch was about to start."""

    def __init__(self, *a, **k):
        raise AssertionError("yt-dlp was started for a link that should have been refused first")


# ====================================================================== SEC-01: revision restore
def _archive_revision(store, row):
    return next(r for r in store.revisions(row) if r.field == "archived")


def test_restoring_an_unarchive_needs_approval(world):
    row = world.rows[0]
    world.pipeline.archive([row], True)          # a person archives…
    world.pipeline.archive([row], False)         # …and brings it back
    unarchive = _archive_revision(world.store, row)   # newest first: old_value = the archived timestamp
    assert unarchive.old_value is not None and unarchive.new_value is None
    sp = ScopedPipeline(world.pipeline)
    pending = sp.restore_revision(row, unarchive.id)
    assert isinstance(pending, ApprovalPending) and pending.operation == "archive"
    req = world.store.get_approval(pending.approval_id)
    assert req.payload == {"video_ids": [row], "archived": True}
    assert world.store.get_video(row).archived_at is None          # nothing changed yet


def test_restoring_an_archive_needs_approval_then_runs(world):
    row = world.rows[1]
    world.pipeline.archive([row], True)
    archived = _archive_revision(world.store, row)
    assert archived.old_value is None                               # it was on the board before
    sp = ScopedPipeline(world.pipeline)
    pending = sp.restore_revision(row, archived.id)
    assert pending.operation == "archive" and world.store.get_video(row).archived_at is not None
    from reelay import approvals
    approvals.approve(world.store, pending.approval_id, "test person")
    approvals.execute_approved(world.pipeline)
    assert world.store.get_video(row).archived_at is None           # unarchived only after approval


def test_restoring_metadata_still_works_immediately(world):
    row = world.rows[0]
    world.pipeline.update_metadata(row, {"title": "Before"})
    world.pipeline.update_metadata(row, {"title": "After"})
    change = next(r for r in world.store.revisions(row) if r.field == "title")
    sp = ScopedPipeline(world.pipeline)
    with world.store.origin("api"):
        assert sp.restore_revision(row, change.id) is None
    assert world.store.get_post(row).title == "Before"
    assert world.store.revisions(row)[0].origin == "api"             # the restore is itself logged


def test_restoring_a_cover_path_is_checked_like_an_edit(world, tmp_path):
    row = world.rows[0]
    world.pipeline.update_metadata(row, {"thumbnail_path": "/etc/secret.jpg"})     # set in the app
    world.pipeline.update_metadata(row, {"thumbnail_path": str(world.folder / "cover.jpg")})
    change = next(r for r in world.store.revisions(row) if r.field == "thumbnail_path")
    with pytest.raises(PipelineError, match="inside this profile's video folder"):
        ScopedPipeline(world.pipeline).restore_revision(row, change.id)
    assert world.store.get_video(row).ticket.thumbnail_path == str(world.folder / "cover.jpg")


def test_restore_route_answers_202_for_archive_and_200_for_fields(tmp_path, monkeypatch):
    import requests

    from reelay.api import ApiServer, create_app
    from reelay.media import MediaLinks
    disable_publishing(monkeypatch)
    w = make_world(tmp_path, monkeypatch)
    token = "t" * 40
    server = ApiServer(create_app(w.store, w.pipeline, w.accounts, lambda: token, MediaLinks()),
                       host="127.0.0.1", port=0)
    assert server.start()
    h = {"Authorization": f"Bearer {token}"}
    try:
        row = w.rows[0]
        w.pipeline.archive([row], True)
        rev = _archive_revision(w.store, row)
        r = requests.post(f"{server.url}/v1/videos/{row}/revisions/{rev.id}/restore", headers=h, timeout=10)
        assert r.status_code == 202 and r.json()["operation"] == "archive" and r.json()["approval_id"]
        assert w.store.get_video(row).archived_at is not None
        assert approve_and_run(w.store, w.pipeline, r).status == "executed"
        assert w.store.get_video(row).archived_at is None

        w.pipeline.update_metadata(row, {"topic": "one"})
        w.pipeline.update_metadata(row, {"topic": "two"})
        rev = next(x for x in w.store.revisions(row) if x.field == "topic")
        r = requests.post(f"{server.url}/v1/videos/{row}/revisions/{rev.id}/restore", headers=h, timeout=10)
        assert r.status_code == 200 and r.json()["ticket"]["topic"] == "one"
    finally:
        server.stop()
        w.store.close()


# ====================================================================== SEC-02: automations
def _schedule_rule(name="Tuesdays"):
    return R.Rule(name=name, actions=[R.Action("schedule", value="Tuesday 09:00")])


def _fill_rule(template, field="tiktok_text"):
    return R.Rule(name="fill", actions=[R.Action("fill_copy", field=field, value=template)])


def _clear(world, row, field="tiktok_text"):
    world.pipeline.update_metadata(row, {field: ""})


def test_agent_topic_put_into_copy_by_a_rule_needs_approval(world):
    row = world.rows[0]
    _clear(world, row)
    with world.store.origin("api"):
        world.pipeline.update_metadata(row, {"topic": "Buy now at evil.example"})
    rule = R.Rule(name="r", actions=[R.Action("fill_copy", field="tiktok_text", value="About {topic}"),
                                     R.Action("schedule", value="Tuesday 09:00")])
    done = R.apply(world.pipeline, rule, row)
    assert done[0] == "filled TikTok caption" and done[1].startswith("waiting for approval")
    assert world.store.dispatches_for(row) == []
    rev = next(r for r in world.store.revisions(row) if r.field == "tiktok_text")
    assert rev.origin == R.AGENT_INPUT_ORIGIN and rev.new_value == "About Buy now at evil.example"


def test_agent_creator_put_into_copy_by_a_rule_needs_approval(world):
    row = world.rows[0]
    _clear(world, row)
    with world.store.origin("api"):
        world.pipeline.update_metadata(row, {"creator": "@someone"})
    R.apply(world.pipeline, _fill_rule("Made by {creator}"), row)
    assert R.apply(world.pipeline, _schedule_rule(), row)[0].startswith("waiting for approval")
    assert world.store.dispatches_for(row) == []


def test_agent_fields_the_template_doesnt_use_dont_block(world):
    row = world.rows[0]
    _clear(world, row)
    with world.store.origin("api"):
        world.pipeline.update_metadata(row, {"topic": "agent topic", "keywords": "agent words"})
    R.apply(world.pipeline, _fill_rule("{caption}"), row)          # the original caption only
    assert world.store.revisions(row)[0].origin == "automation"
    assert R.apply(world.pipeline, _schedule_rule(), row)[0].startswith("scheduled for")


def test_human_fields_used_by_the_template_dont_block(world):
    row = world.rows[0]
    _clear(world, row)
    world.pipeline.update_metadata(row, {"topic": "Lydia's topic", "creator": "Lydia"})   # in the app
    R.apply(world.pipeline, _fill_rule("{topic} by {creator}"), row)
    assert R.apply(world.pipeline, _schedule_rule(), row)[0].startswith("scheduled for")


def test_fixed_captions_dont_carry_agent_text(world):
    row = world.rows[0]
    _clear(world, row)
    with world.store.origin("api"):
        world.pipeline.update_metadata(row, {"topic": "agent topic"})
    R.apply(world.pipeline, _fill_rule("New video every Tuesday!"), row)
    assert R.apply(world.pipeline, _schedule_rule(), row)[0].startswith("scheduled for")


def test_agent_text_is_remembered_across_separate_rule_runs(world, tmp_path):
    """One rule fills the copy (when added); a different rule schedules it later (when moved). The
    agent's part is stored in the revision log, so it survives between runs, and a fresh connection."""
    from reelay.models import Stage
    from reelay.store import Store
    row = world.rows[0]
    _clear(world, row)
    with world.store.origin("api"):
        world.pipeline.update_metadata(row, {"topic": "agent topic"})
    fill = R.Rule(name="fill", trigger="added",
                  actions=[R.Action("fill_copy", field="tiktok_text", value="#{topic}")])
    later = R.Rule(name="later", trigger="moved", column=Stage.STAGING,
                   actions=[R.Action("schedule", value="Tuesday 09:00")])
    R.save(world.store, world.profile.id, [fill, later])
    seen = R._seen(world.store, world.profile.id)
    seen[fill.id] = [r for r in seen.get(fill.id, []) if r != row]   # as if this video had just arrived
    R._save_seen(world.store, world.profile.id, seen)
    R.run(world.pipeline, world.profile.id, "added")
    other = Store(world.store.path)                                   # e.g. after a restart
    assert "tiktok_text" in R.agent_fields(other, row)
    other.close()
    world.pipeline.move(row, Stage.STAGING)
    seen = R._seen(world.store, world.profile.id)
    seen[later.id] = [r for r in seen.get(later.id, []) if r != row]
    R._save_seen(world.store, world.profile.id, seen)
    entries = R.run(world.pipeline, world.profile.id, "moved")
    assert any(e["video"] == row and e["done"][0].startswith("waiting for approval") for e in entries)
    assert world.store.dispatches_for(row) == []


def test_a_later_human_edit_elsewhere_doesnt_hide_agent_copy(world):
    row = world.rows[0]
    with world.store.origin("api"):
        world.pipeline.update_metadata(row, {"tiktok_text": "agent caption"})
    world.pipeline.update_metadata(row, {"title": "Lydia's title"})   # newest copy edit is the person's
    assert R.apply(world.pipeline, _schedule_rule(), row)[0].startswith("waiting for approval")
    assert world.store.dispatches_for(row) == []
    assert [r.operation for r in world.store.list_approvals(status="pending")] == ["schedule"]


def test_appended_hashtags_keep_the_agents_part(world):
    row = world.rows[0]
    with world.store.origin("api"):
        world.pipeline.update_metadata(row, {"hashtags": "#agent"})
    world.pipeline.update_metadata(row, {"tiktok_text": "Lydia's caption"})
    R.apply(world.pipeline, R.Rule(name="h", actions=[R.Action("add_hashtags", value="#summer")]), row)
    assert world.store.revisions(row)[0].origin == R.AGENT_INPUT_ORIGIN
    assert R.apply(world.pipeline, _schedule_rule(), row)[0].startswith("waiting for approval")
    assert world.store.dispatches_for(row) == []
    assert [r.operation for r in world.store.list_approvals(status="pending")] == ["schedule"]


def test_the_schedule_boundary_refuses_automation_without_approval(world):
    row = world.rows[0]
    with world.store.origin("api"):
        world.pipeline.update_metadata(row, {"description": "agent words"})
    when = datetime.now(timezone.utc) + timedelta(days=3)
    with world.store.origin("automation"), pytest.raises(PipelineError) as exc:
        world.pipeline.schedule(row, scheduled_at=when)
    assert exc.value.code == "needs_approval" and world.store.dispatches_for(row) == []
    # A person scheduling it themselves, or an approved request, is not an automation:
    pending = ScopedPipeline(world.pipeline, requested_by="automation: x").schedule(row, scheduled_at=when)
    from reelay import approvals
    approvals.approve(world.store, pending.approval_id, "test person")
    approvals.execute_approved(world.pipeline)
    assert world.store.dispatches_for(row)


# ====================================================================== SEC-03: links and fetching
GOOD = ["https://www.tiktok.com/@x/video/7123456789012345678", "https://vm.tiktok.com/ZMabc/",
        "https://www.tiktok.com/@x/photo/7123456789012345678", "https://www.instagram.com/reel/Cabc/",
        "https://www.instagram.com/share/reel/abc/", "https://youtu.be/dQw4w9WgXcQ?list=PL1",
        "https://m.youtube.com/shorts/x", "https://www.snapchat.com/spotlight/W7_EDlXWTBiXAEEniNoMPwAAYYWl",
        "https://www.tiktok.com:443/@x", "http://instagram.com/nasa/"]
BAD = ["file:///etc/passwd", "http://127.0.0.1:8471/v1/board", "http://localhost/", "http://169.254.169.254/",
       "http://10.0.0.1/x", "https://[::1]/", "https://evil-tiktok.com/x", "https://tiktok.com.evil.net/x",
       "https://evil.example\\@www.tiktok.com/", "https://user:pw@www.youtube.com/x", "https://www.tiktok.com:22/",
       "ftp://youtube.com/x", "gopher://tiktok.com/", "ytsearch:cats", "javascript:alert(1)", "", None,
       "https://www.tiktok.com/@x\nHost: evil", "https://www.tiktok.com:99999/", "http://[::1"]


@pytest.mark.parametrize("url", GOOD)
def test_supported_links_are_fetchable(url):
    from reelay.urlsafety import fetch_url_problem
    assert fetch_url_problem(url) is None, url


@pytest.mark.parametrize("url", BAD)
def test_prohibited_links_are_refused(url):
    from reelay.urlsafety import fetch_url_problem
    assert fetch_url_problem(url), url


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://127.0.0.1/x", "https://evil.example/x"])
def test_every_entry_point_refuses_before_yt_dlp_starts(url, monkeypatch, no_network, tmp_path):
    import yt_dlp

    from reelay import downloader, fetcher, products, transcripts
    from reelay.models import VideoItem
    from reelay.urlsafety import UnsafeURLError
    monkeypatch.setattr(yt_dlp, "YoutubeDL", ExplodingYDL)
    item = VideoItem(platform="TikTok", video_id="1", url=url)
    with pytest.raises(UnsafeURLError):
        fetcher.list_videos(url, {})
    with pytest.raises(UnsafeURLError):
        fetcher.load_details(item, {})
    with pytest.raises(downloader.DownloadError):
        downloader.download_item(item, tmp_path, "best", {}, lambda i: "x")
    with pytest.raises(transcripts.TranscriptError, match="Can't get a transcript from this link"):
        transcripts.fetch_transcript(url, {}, ExplodingYDL)
    with pytest.raises(UnsafeURLError):
        products.tiktok_products(url, {}, ExplodingYDL)


def test_links_an_extractor_hands_on_are_checked_too(no_network):
    """yt-dlp resolves a url_result through self.extract_info; the guard sits on that call."""
    from reelay.urlsafety import UnsafeURLError
    from reelay.ytdl import guarded_ydl

    class Chaining:
        def __init__(self, opts):
            self.opts = opts

        def extract_info(self, url, download=True, ie_key=None, extra_info=None, process=True,
                         force_generic_extractor=False):
            if url.startswith("https://www.tiktok.com"):
                return self.extract_info(self.next_url, download, ie_key=self.next_key)   # like yt-dlp does
            return {"id": url}

    ydl = guarded_ydl({}, Chaining)
    assert ydl.opts["enable_file_urls"] is False
    ydl.next_url, ydl.next_key = "http://192.168.1.1/admin", "Generic"
    with pytest.raises(UnsafeURLError):
        ydl.extract_info("https://www.tiktok.com/@x/video/1")
    ydl.next_url, ydl.next_key = "localhost", "Generic"            # generic would try it as a web address
    with pytest.raises(UnsafeURLError):
        ydl.extract_info("https://www.tiktok.com/@x/video/1")
    ydl.next_url, ydl.next_key = "dQw4w9WgXcQ", "Youtube"          # a bare id for a platform extractor
    assert ydl.extract_info("https://www.tiktok.com/@x/video/1") == {"id": "dQw4w9WgXcQ"}


def test_the_installed_yt_dlp_resolves_handed_on_links_through_the_guard(no_network):
    """With the real YoutubeDL: a url result pointing at the local network is refused, not fetched."""
    from reelay.urlsafety import UnsafeURLError
    from reelay.ytdl import guarded_ydl
    with guarded_ydl({"quiet": True}) as ydl:
        assert ydl.params["enable_file_urls"] is False
        with pytest.raises(UnsafeURLError):
            ydl.process_ie_result({"_type": "url", "url": "http://127.0.0.1:9/x", "ie_key": "Generic"},
                                  download=False)


def test_the_installed_yt_dlp_honours_the_agent_extractor_allowlist(no_network):
    """Agent fetches only load the platform extractors: a generic link finds no extractor at all."""
    import yt_dlp

    from reelay.urlsafety import restrict_ydl
    with yt_dlp.YoutubeDL(restrict_ydl({"quiet": True})) as ydl:
        names = {ie.IE_NAME.lower() for ie in ydl._ies.values()}
        assert "generic" not in names and "tiktok" in names
        with pytest.raises(yt_dlp.utils.DownloadError, match="No suitable extractor|Unsupported URL"):
            ydl.extract_info("http://127.0.0.1:9/x", download=False)


def test_playlist_entries_with_foreign_links_are_dropped():
    from reelay.fetcher import _flatten
    info = {"entries": [{"id": "1", "url": "https://www.tiktok.com/@x/video/1"},
                        {"id": "2", "url": "http://127.0.0.1/steal"}, {"id": "3", "url": "file:///etc/passwd"}]}
    assert [i.video_id for i in _flatten(info, "TikTok")] == ["1"]


def test_caption_track_links_are_checked(no_network):
    from reelay.transcripts import TranscriptError, fetch_transcript
    opened = []

    class YDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            return {"subtitles": {"en": [{"ext": "vtt", "url": "http://169.254.169.254/latest/meta-data"}]}}

        def urlopen(self, url):
            opened.append(url)
    with pytest.raises(TranscriptError, match="captions link"):
        fetch_transcript("https://www.tiktok.com/@x/video/1", {}, YDL)
    assert opened == []


def test_cover_image_links_never_reach_local_addresses(monkeypatch):
    from reelay.urlsafety import asset_url_problem
    real = socket.getaddrinfo

    def fake_dns(host, port, *a, **k):
        if k.get("flags", 0) & socket.AI_NUMERICHOST:
            return real(host, port, *a, **k)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("151.101.1.1", port))]   # a public CDN
    monkeypatch.setattr(socket, "getaddrinfo", fake_dns)
    assert asset_url_problem("https://p16-sign.tiktokcdn-us.com/obj/cover.jpeg?x=1") is None
    for bad in ("file:///etc/passwd", "http://192.168.0.1/x.jpg", "http://127.0.0.1/x", "http://localhost/x",
                "http://[::1]/x", "http://printer.local/x", "ftp://cdn.example/x", "", None):
        assert asset_url_problem(bad), bad


def test_an_agent_edited_link_cant_be_fetched_from_the_app_either(world, monkeypatch, no_network):
    """The agent can't fetch it itself (ScopedPipeline); clicking Get Transcript in the app mustn't either."""
    import yt_dlp
    monkeypatch.setattr(yt_dlp, "YoutubeDL", ExplodingYDL)
    row = world.rows[0]
    with world.store.origin("api"):
        world.pipeline.update_metadata(row, {"source_url": "http://169.254.169.254/latest/meta-data"})
    with pytest.raises(PipelineError, match="Can't get a transcript from this link"):
        world.pipeline.fetch_transcript(row, {})                      # the desktop app's own call


# ====================================================================== SEC-04: files Reelay posts or serves
def _mp4(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(FAKE_MP4)
    return path


def test_valid_library_and_custom_folder_files_are_posted(world, tmp_path):
    from dataclasses import replace

    from reelay import video_tools as vt
    row = world.store.get_video(world.rows[0])
    assert world.pipeline.video_path(row) == world.folder / "video0.mp4"
    edited = _mp4(world.folder / "video0.edited.mp4")
    vt.set_edit(world.store, row.id, edited, ["strip"])
    assert world.pipeline.video_path(row) == edited
    vt.clear_edit(world.store, row.id)
    custom = tmp_path / "older-folder"                               # downloaded before the folder moved
    _mp4(custom / "clip.mp4")
    moved = replace(row, item=replace(row.item, folder=str(custom), filename="clip.mp4"))
    assert world.pipeline.video_path(moved) is None                  # the database alone can't add a folder
    from reelay import trusted_folders
    trusted_folders.trust(custom)                                    # chosen on this computer earlier
    assert world.pipeline.video_path(moved) == custom / "clip.mp4"


def test_edited_paths_outside_the_folders_are_ignored(world, tmp_path):
    from reelay import video_tools as vt
    row = world.store.get_video(world.rows[0])
    outside = _mp4(tmp_path / "elsewhere" / "private.mp4")
    for sneaky in (outside, world.folder / ".." / "elsewhere" / "private.mp4", "/etc/hosts", "/etc/passwd"):
        vt.set_edit(world.store, row.id, sneaky, ["strip"])
        assert world.pipeline.video_path(row) == world.folder / "video0.mp4", sneaky   # the original instead


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlinks not supported")
def test_symlinks_out_of_the_folder_are_refused(world, tmp_path):
    from dataclasses import replace

    from reelay import video_tools as vt
    row = world.store.get_video(world.rows[0])
    secret = _mp4(tmp_path / "elsewhere" / "secret.mp4")
    link = world.folder / "link.mp4"
    try:
        os.symlink(secret, link)
    except OSError:
        pytest.skip("can't create symlinks here")
    vt.set_edit(world.store, row.id, link, ["strip"])
    assert world.pipeline.video_path(row) == world.folder / "video0.mp4"
    linked = replace(row, item=replace(row.item, filename="link.mp4"))
    vt.clear_edit(world.store, row.id)
    assert world.pipeline.video_path(linked) is None                   # recorded file refused, not posted


def test_missing_files_and_files_that_arent_videos(world):
    from dataclasses import replace

    from reelay import video_tools as vt
    row = world.store.get_video(world.rows[0])
    vt.set_edit(world.store, row.id, world.folder / "gone.edited.mp4", ["strip"])
    assert world.pipeline.video_path(row) == world.folder / "video0.mp4"
    vt.clear_edit(world.store, row.id)
    missing = replace(row, item=replace(row.item, filename="not-downloaded.mp4"))
    assert world.pipeline.video_path(missing) == world.folder / "not-downloaded.mp4"   # unchanged: missing
    (world.folder / "notes.mp4").write_text("my passwords")                             # named like a video
    fake = replace(row, item=replace(row.item, filename="notes.mp4"))
    assert world.pipeline.video_path(fake) is None
    system = replace(row, item=replace(row.item, folder="/etc", filename="hosts"))
    assert world.pipeline.video_path(system) is None


def test_media_links_only_for_videos_and_only_the_same_file(tmp_path):
    from reelay.media import MediaLinks
    links = MediaLinks()
    video = _mp4(tmp_path / "v.mp4")
    (tmp_path / "id_rsa").write_text("secret")
    (tmp_path / "secret.mp4").write_text("secret")
    for bad in (tmp_path / "id_rsa", tmp_path / "secret.mp4", tmp_path / "missing.mp4", tmp_path):
        with pytest.raises(ValueError):
            links.issue(bad)
    token = links.issue(video)
    assert links.resolve(token) == video.resolve()
    video.unlink()                                                       # swapped for something else
    if hasattr(os, "symlink"):
        try:
            os.symlink(tmp_path / "id_rsa", video)
        except OSError:
            video.write_bytes(FAKE_MP4)
    else:
        video.write_bytes(FAKE_MP4)
    assert links.resolve(token) is None


def test_media_route_serves_videos_and_refuses_swapped_files(tmp_path, monkeypatch):
    import requests

    from reelay.api import ApiServer, create_app
    from reelay.media import MediaLinks
    disable_publishing(monkeypatch)
    w = make_world(tmp_path, monkeypatch)
    media = MediaLinks()
    server = ApiServer(create_app(w.store, w.pipeline, w.accounts, lambda: "t" * 40, media),
                       host="127.0.0.1", port=0)
    assert server.start()
    try:
        path = w.folder / "video0.mp4"
        token = media.issue(path)
        assert requests.get(f"{server.url}/media/{token}", timeout=10).content == FAKE_MP4
        path.unlink()
        path.write_text("now a different file")
        assert requests.get(f"{server.url}/media/{token}", timeout=10).status_code == 404
    finally:
        server.stop()
        w.store.close()


# ====================================================================== SEC-05: migrations
def test_statements_split_without_breaking_triggers_or_quotes():
    from reelay.store import _MIGRATIONS, sql_statements
    eight = sql_statements(_MIGRATIONS[8])
    triggers = [s for s in eight if s.lstrip().upper().startswith("CREATE TRIGGER")]
    assert len(triggers) == 6 and all(t.rstrip().upper().endswith("END;") for t in triggers)
    assert sql_statements("INSERT INTO t VALUES ('a;b'); SELECT 1;") == ["INSERT INTO t VALUES ('a;b');",
                                                                        "SELECT 1;"]
    with pytest.raises(ValueError):
        sql_statements("CREATE TABLE x (a); CREATE TRIGGER t AFTER INSERT ON x BEGIN SELECT 1;")


def test_a_fresh_database_reaches_the_current_version(tmp_path):
    from reelay.store import SCHEMA_VERSION, Store
    store = Store(tmp_path / "fresh.db")
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert not list(tmp_path.glob("*-backup-*"))                         # nothing to back up
    store.close()


def _database_at(version: int, path: Path, monkeypatch):
    """A database made by an older Reelay (schema `version`), with a profile, a video and its post."""
    import reelay.store as store_module
    current = store_module.SCHEMA_VERSION
    monkeypatch.setattr(store_module, "SCHEMA_VERSION", 1)
    store_module.Store(path).close()
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO profiles (id, name, output_folder, created_at) VALUES (1, 'Old', '/tmp/old', 'x')")
    data = {"platform": "TikTok", "video_id": "1", "url": "https://www.tiktok.com/@x/video/1", "title": "kept"}
    conn.execute("INSERT INTO videos (id, profile_id, platform, video_id, data, updated_at) "
                 "VALUES (1, 1, 'TikTok', '1', ?, 'x')", (json.dumps(data),))
    conn.execute("INSERT INTO posts (video_row, title, updated_at) VALUES (1, 'My title', 'x')")
    conn.commit()
    conn.close()
    if version > 1:
        monkeypatch.setattr(store_module, "SCHEMA_VERSION", version)
        store_module.Store(path).close()
    monkeypatch.setattr(store_module, "SCHEMA_VERSION", current)


@pytest.mark.parametrize("version", range(1, 9))
def test_every_older_database_migrates_and_keeps_its_data(version, tmp_path, monkeypatch):
    from reelay.store import SCHEMA_VERSION, Store
    path = tmp_path / "old.db"
    _database_at(version, path, monkeypatch)
    for leftover in tmp_path.glob("*-backup-*"):
        leftover.unlink()                                                 # from building the old database
    store = Store(path)
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    rows = store.load_videos(1)
    assert [r.item.title for r in rows] == ["kept"] and store.get_post(1).title == "My title"
    assert store.search(1, "kept")                                       # the search index was built
    store.close()
    backups = list(tmp_path.glob("old-backup-*.db"))
    assert len(backups) == 1
    copy = sqlite3.connect(backups[0])
    assert copy.execute("PRAGMA user_version").fetchone()[0] == version
    assert copy.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 1
    copy.close()


def test_backups_never_overwrite_each_other(tmp_path, monkeypatch):
    from reelay.store import Store
    path = tmp_path / "old.db"
    _database_at(8, path, monkeypatch)
    store = Store(path)
    first = store._backup_before_migrating(8)
    second = store._backup_before_migrating(8)
    assert first and second and first != second and first.is_file() and second.is_file()
    store.close()


def test_a_failed_migration_rolls_back_and_the_next_launch_recovers(tmp_path, monkeypatch):
    import reelay.store as store_module
    path = tmp_path / "db.db"
    store_module.Store(path).close()                                     # at the current version
    current = store_module.SCHEMA_VERSION
    good = "CREATE TABLE extra (a);\nCREATE TRIGGER extra_t AFTER INSERT ON extra BEGIN SELECT 1; END;\n"
    monkeypatch.setattr(store_module, "SCHEMA_VERSION", current + 1)
    monkeypatch.setitem(store_module._MIGRATIONS, current + 1, good + "INSERT INTO no_such_table VALUES (1);")
    with pytest.raises(sqlite3.OperationalError):
        store_module.Store(path)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == current             # not advanced
    assert conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'extra%'").fetchall() == []
    conn.execute("BEGIN IMMEDIATE")                                       # nothing left holding the file
    conn.rollback()
    conn.close()
    monkeypatch.setitem(store_module._MIGRATIONS, current + 1, good)      # the fixed version ships
    store = store_module.Store(path)
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == current + 1
    store.close()


def test_a_migration_already_applied_by_another_launch_is_skipped(tmp_path):
    from reelay.store import SCHEMA_VERSION, Store
    store = Store(tmp_path / "db.db")
    store._apply_migration(SCHEMA_VERSION)                                # e.g. another Reelay got there first
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    store.close()


# ====================================================================== SEC-13: thread safety
def test_unmark_posted_deletes_under_the_lock_and_moves_back(world):
    from reelay.models import Stage
    row = world.rows[0]
    posted = world.pipeline.mark_posted(row, "instagram")
    assert world.store.delete_manual_post(999999) is None
    world.pipeline.unmark_posted(posted.id)
    assert world.store.manual_posts(row) == []
    assert world.store.get_video(row).stage != Stage.PUBLISHED


def test_pipeline_never_uses_the_raw_connection():
    import inspect

    import reelay.pipeline as pipeline_module
    assert "store.conn" not in inspect.getsource(pipeline_module)


# ====================================================================== verification follow-ups
@pytest.mark.parametrize("host", ["127.1", "2130706433", "0x7f000001", "127.000.000.001", "0x7f.1", "10.1",
                                  "[::ffff:127.0.0.1]", "[fe80::1]", "0.0.0.0", "[::]", "192.168.1.20",
                                  "169.254.169.254"])
def test_numeric_hosts_are_read_like_the_system_reads_them(host, monkeypatch):
    """127.1, 2130706433 and 0x7f000001 are all 127.0.0.1 to the resolver: they must be refused too."""
    from reelay.urlsafety import asset_url_problem
    real = socket.getaddrinfo

    def numeric_only(h, port, *a, **k):
        assert k.get("flags", 0) & socket.AI_NUMERICHOST, "a name lookup was attempted for a number"
        return real(h, port, *a, **k)
    monkeypatch.setattr(socket, "getaddrinfo", numeric_only)
    assert asset_url_problem(f"http://{host}/cover.jpg"), host


def test_names_are_looked_up_and_must_point_somewhere_public(monkeypatch):
    from reelay.urlsafety import asset_url_problem
    real = socket.getaddrinfo
    table = {"cdn.example": ["151.101.1.1"], "localtest.example": ["127.0.0.1"],
             "split.example": ["151.101.1.1", "10.0.0.5"], "v6local.example": ["::1"]}

    def fake_dns(host, port, *a, **k):
        if k.get("flags", 0) & socket.AI_NUMERICHOST:
            return real(host, port, *a, **k)
        if host not in table:
            raise socket.gaierror("not found")
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
                for ip in table[host]]
    monkeypatch.setattr(socket, "getaddrinfo", fake_dns)
    assert asset_url_problem("https://cdn.example/c.jpg") is None
    for bad in ("https://localtest.example/c.jpg", "https://split.example/c.jpg", "https://v6local.example/c",
                "https://no-such-name.example/c.jpg"):
        assert asset_url_problem(bad), bad


def test_cover_downloads_skip_local_links(monkeypatch, tmp_path):
    from reelay.gui import thumbnails
    opened = []
    monkeypatch.setattr(thumbnails, "fetch_public", lambda *a, **k: opened.append(a) or b"")

    class Signals:
        class done:
            @staticmethod
            def emit(*a):
                pass
    job = thumbnails._FetchCover("k", "http://2130706433/cover.jpg", "", tmp_path / "c.jpg", Signals)
    job.run()
    assert opened == []


@pytest.mark.parametrize("url,generic", [
    ("https://www.instagram.com/share/reel/abc/", True), ("https://www.tiktok.com/@x/photo/7123", True),
    ("https://www.snapchat.com/add/someone", True), ("https://www.snapchat.com/spotlight/abc", False),
    ("https://www.tiktok.com/@x/video/1", False), ("https://www.instagram.com/reel/abc/", False),
    ("https://l.instagram.com/?u=http%3A%2F%2F127.0.0.1%2F", False),
    ("https://www.youtube.com/redirect?q=http://127.0.0.1/", False),
])
def test_the_generic_extractor_only_for_link_formats_that_need_it(url, generic):
    from reelay.urlsafety import app_ydl, restrict_ydl
    opts = app_ydl({"quiet": True}, url)
    assert ("generic" in opts["allowed_extractors"]) is generic and opts["enable_file_urls"] is False
    agent = restrict_ydl({})
    assert app_ydl(agent, url)["allowed_extractors"] == agent["allowed_extractors"]   # never widened


@pytest.mark.parametrize("url", ["https://l.instagram.com/?u=http%3A%2F%2F127.0.0.1%2F",
                                 "https://lm.instagram.com/?u=x", "https://www.youtube.com/redirect?q=http://127.0.0.1/",
                                 "https://m.youtube.com/attribution_link?u=/x", "https://www.tiktok.com/link/v2?target=x"])
def test_platform_redirector_links_are_refused_before_yt_dlp(url, monkeypatch, no_network):
    """The platforms' "leave this site" redirectors bounce to any address (YouTube's is even claimed by
    yt-dlp's youtube:tab extractor), so they're refused before yt-dlp starts."""
    import yt_dlp

    from reelay import fetcher
    from reelay.urlsafety import UnsafeURLError, fetch_url_problem, media_url_problem
    assert fetch_url_problem(url) and media_url_problem(url)
    monkeypatch.setattr(yt_dlp, "YoutubeDL", ExplodingYDL)
    with pytest.raises(UnsafeURLError, match="redirect link"):
        fetcher.list_videos(url, {})


def test_an_unclaimed_platform_link_finds_no_extractor_without_the_network(no_network):
    """With the installed yt-dlp and Reelay's extractor list: a platform-host link no platform extractor
    claims (and that doesn't need the generic one) is refused without any request."""
    import yt_dlp

    from reelay.urlsafety import app_ydl
    from reelay.ytdl import guarded_ydl
    url = "https://help.instagram.com/some/page"
    with guarded_ydl(app_ydl({"quiet": True}, url)) as ydl:
        assert "generic" not in {ie.IE_NAME.lower() for ie in ydl._ies.values()}
        with pytest.raises(yt_dlp.utils.DownloadError, match="No suitable extractor|Unsupported URL"):
            ydl.extract_info(url, download=False)


def test_an_agent_write_is_tagged_even_if_the_caller_forgets(world):
    row = world.rows[0]
    ScopedPipeline(world.pipeline).update_metadata(row, {"description": "agent text, no origin set"})
    assert world.store.revisions(row)[0].origin == "api"
    assert R.apply(world.pipeline, _schedule_rule(), row)[0].startswith("waiting for approval")
    assert world.store.dispatches_for(row) == []
    with world.store.origin("mcp:assistant"):          # a more specific agent origin is kept
        ScopedPipeline(world.pipeline, origin="mcp").update_metadata(row, {"topic": "x"})
    assert world.store.revisions(row)[0].origin == "mcp:assistant"


def test_every_api_write_runs_as_the_agent():
    """Static: inside create_app, every call that can write goes through `with store.origin("api")`."""
    import ast
    import inspect

    import reelay.api as api_module
    writes = {"save_draft", "update_metadata", "restore_revision", "fill_from_original", "fetch_transcript",
              "add_source", "update_source", "schedule", "start_upload", "archive", "bulk_update", "apply_sheet",
              "set_platform_state", "mark_posted", "resolve_interrupted", "delete_source"}
    tree = ast.parse(inspect.getsource(api_module))
    create = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "create_app")
    parents = {c: n for n in ast.walk(create) for c in ast.iter_child_nodes(n)}
    found, bare = 0, []
    for node in ast.walk(create):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and \
                getattr(node.func.value, "id", None) == "pipeline" and node.func.attr in writes:
            found += 1
            p, ok = node, False
            while p in parents and not ok:
                p = parents[p]
                ok = isinstance(p, ast.With) and any(
                    isinstance(i.context_expr, ast.Call) and getattr(i.context_expr.func, "attr", "") == "origin"
                    and ast.unparse(i.context_expr.args[0]) == "'api'" for i in p.items)
            if not ok:
                bare.append(f"line {node.lineno}: pipeline.{node.func.attr}")
    assert found >= 15 and bare == []


def test_a_rule_chain_through_a_ticket_field_needs_approval(world):
    row = world.rows[0]
    _clear(world, row)
    with world.store.origin("api"):
        world.pipeline.update_metadata(row, {"creator": "@agent"})
    R.apply(world.pipeline, R.Rule(name="a", actions=[R.Action("set_ticket", field="topic", value="by {creator}")]),
            row)
    assert world.store.latest_field_origins(row)["topic"] == R.AGENT_INPUT_ORIGIN
    R.apply(world.pipeline, _fill_rule("{topic}"), row)
    assert R.apply(world.pipeline, _schedule_rule(), row)[0].startswith("waiting for approval")
    assert world.store.dispatches_for(row) == []


def test_publishing_refuses_a_file_outside_the_folders(world, tmp_path):
    from reelay import video_tools as vt
    from reelay.models import Dispatch, Platform
    from reelay.publishers.base import PermanentError
    from tests.fakes import connect_platform
    connect_platform(world.accounts, world.profile.id, Platform.BLUESKY)
    row = world.rows[0]
    outside = _mp4(tmp_path / "elsewhere" / "private.mp4")
    vt.set_edit(world.store, row, outside, ["strip"])
    (world.folder / "video0.mp4").write_text("not a video any more")
    with pytest.raises(PermanentError, match="missing"):
        world.pipeline.build_job(Dispatch(video_row=row, platform=Platform.BLUESKY, due_at="2026-01-01T00:00:00Z"))


def test_threads_media_links_are_made_only_for_the_checked_file(world, tmp_path):
    from reelay.media import MediaLinks
    from reelay.models import Dispatch, Platform
    from tests.fakes import connect_platform
    connect_platform(world.accounts, world.profile.id, Platform.THREADS)
    world.store.set_setting("public_url", "https://example.trycloudflare.com")
    world.pipeline.media = MediaLinks()
    row = world.rows[0]
    job = world.pipeline.build_job(Dispatch(video_row=row, platform=Platform.THREADS, due_at="2026-01-01T00:00:00Z"))
    link = job.media_url()
    token = link.rsplit("/", 1)[1]
    assert link.startswith("https://example.trycloudflare.com/media/")
    assert world.pipeline.media.resolve(token) == (world.folder / "video0.mp4").resolve()


def test_a_failure_inside_the_search_migration_rolls_back_completely(tmp_path, monkeypatch):
    import reelay.store as store_module
    path = tmp_path / "v7.db"
    current = store_module.SCHEMA_VERSION
    monkeypatch.setattr(store_module, "SCHEMA_VERSION", 7)
    store_module.Store(path).close()
    monkeypatch.setattr(store_module, "SCHEMA_VERSION", 8)
    real8 = store_module._MIGRATIONS[8]
    monkeypatch.setitem(store_module._MIGRATIONS, 8, real8 + "\nINSERT INTO no_such_table VALUES (1);")
    with pytest.raises(sqlite3.OperationalError):
        store_module.Store(path)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 7
    assert conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'video_search%'").fetchall() == []
    conn.execute("INSERT INTO profiles (name, output_folder, created_at) VALUES ('still works', '/x', 'x')")
    conn.commit()
    conn.close()
    monkeypatch.setitem(store_module._MIGRATIONS, 8, real8)
    monkeypatch.setattr(store_module, "SCHEMA_VERSION", current)
    store = store_module.Store(path)
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == current
    assert store.search(store.list_profiles()[0].id, "anything") == []          # the index exists and works
    store.close()


def test_the_backup_includes_changes_still_in_the_wal(tmp_path, monkeypatch):
    import reelay.store as store_module
    path = tmp_path / "w.db"
    current = store_module.SCHEMA_VERSION
    monkeypatch.setattr(store_module, "SCHEMA_VERSION", current - 1)
    store_module.Store(path).close()
    monkeypatch.setattr(store_module, "SCHEMA_VERSION", current)
    writer = sqlite3.connect(path)
    assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("INSERT INTO profiles (name, output_folder, created_at) VALUES ('only in the wal', '/x', 'x')")
    writer.commit()
    assert (tmp_path / "w.db-wal").stat().st_size > 0
    store_module.Store(path).close()
    backup = sqlite3.connect(next(tmp_path.glob("w-backup-*.db")))
    assert backup.execute("SELECT name FROM profiles").fetchall() == [("only in the wal",)]
    assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    backup.close()
    writer.close()


# ====================================================================== folders the database alone can't choose
def test_folders_named_only_by_the_database_are_refused_with_a_clear_reason(world, tmp_path):
    from dataclasses import replace

    from reelay import video_tools as vt
    row = world.store.get_video(world.rows[0])
    private = _mp4(tmp_path / "Private" / "family.mp4")
    evil = replace(row, item=replace(row.item, folder=str(private.parent), filename="family.mp4"))
    assert world.pipeline.video_path(evil) is None
    assert "wasn't chosen on this computer" in world.pipeline.file_problem(evil)
    profile = world.store.get_profile(row.profile_id)
    profile.output_folder = "/"                                       # an edited (shared or restored) database
    world.store.update_profile(profile)
    vt.set_edit(world.store, row.id, private, ["strip"])
    assert world.pipeline.video_path(world.store.get_video(row.id)) == world.folder / "video0.mp4"  # still the
    # original: "/" isn't trusted, and the original sits in the folder this computer chose
    assert any("wasn't chosen on this computer" in p or "missing" in p
               for p in [world.pipeline.file_problem(evil)])


def test_a_database_is_trusted_on_first_open_only(tmp_path):
    from reelay import trusted_folders
    from reelay.store import Store
    db = tmp_path / "shared.db"
    store = Store(db)
    store.create_profile("Lydia", tmp_path / "lydia-videos")
    store.close()
    trusted_folders._save({"folders": [], "databases": []})          # as on a computer that never opened it
    store = Store(db)
    assert trusted_folders.first_open(store) is True                  # e.g. Open Shared Database, or updating
    assert trusted_folders.is_trusted(tmp_path / "lydia-videos" / "sub")
    profile = store.list_profiles()[0]
    profile.output_folder = str(tmp_path / "somewhere-else")          # later edits by anyone with the file
    store.update_profile(profile)
    assert trusted_folders.first_open(store) is False
    assert not trusted_folders.is_trusted(tmp_path / "somewhere-else")
    store.close()


def test_the_trust_list_is_private_and_fails_closed(tmp_path):
    import stat as st

    from reelay import paths, trusted_folders
    trusted_folders.trust(tmp_path / "videos")
    f = paths.data_dir() / trusted_folders.FILE_NAME
    if os.name == "posix":
        assert st.S_IMODE(os.stat(f).st_mode) & 0o077 == 0
    f.write_text("{not json")
    assert not trusted_folders.is_trusted(tmp_path / "videos")         # unreadable: nothing is trusted


def test_new_profiles_trust_their_own_folder(tmp_path):
    from reelay import trusted_folders
    from reelay.store import Store
    store = Store(tmp_path / "s.db")
    store.create_profile("New", tmp_path / "new-videos")
    assert trusted_folders.is_trusted(tmp_path / "new-videos")
    store.close()


# ====================================================================== pinned downloads (covers, caption files)
@pytest.fixture
def local_http():
    """A tiny HTTP server on 127.0.0.1 whose routes the test sets."""
    import http.server
    import threading
    routes = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):                                               # noqa: N802
            status, headers, body = routes.get(self.path, (404, {}, b""))
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            routes.setdefault("_hosts", []).append(self.headers.get("Host"))

        def log_message(self, *a):
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], routes
    server.shutdown()


def _resolver(monkeypatch, answers):
    """Stub DNS: each name returns its answers in turn (to simulate a name that changes)."""
    calls = []
    real = socket.getaddrinfo

    def fake(host, port, *a, **k):
        if k.get("flags", 0) & socket.AI_NUMERICHOST:
            return real(host, port, *a, **k)
        calls.append(host)
        seq = answers.get(host)
        if not seq:
            raise socket.gaierror("not found")
        ip = seq.pop(0) if len(seq) > 1 else seq[0]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return calls


def test_a_name_is_looked_up_once_and_the_checked_address_is_used(local_http, monkeypatch):
    from reelay.safefetch import fetch_public
    port, routes = local_http
    routes["/cover.jpg"] = (200, {}, b"JPEGDATA")
    # The first answer is the (test-)allowed address; a rebinding second answer would be a private one.
    calls = _resolver(monkeypatch, {"cdn.test": ["127.0.0.1", "10.0.0.5"]})
    data = fetch_public(f"http://cdn.test:{port}/cover.jpg", allowed=lambda ip: ip == "127.0.0.1")
    assert data == b"JPEGDATA" and calls == ["cdn.test"]                 # one lookup, no second chance
    assert routes["_hosts"] == [f"cdn.test:{port}"]                      # the name still goes in Host


def test_names_pointing_anywhere_private_are_never_connected(monkeypatch, no_network):
    from reelay.safefetch import FetchRefused, fetch_public
    _resolver(monkeypatch, {"rebind.test": ["10.0.0.5"], "mixed.test": ["151.101.1.1"]})
    with pytest.raises(FetchRefused, match="local network"):
        fetch_public("https://rebind.test/c.jpg")


def test_redirects_are_checked_hop_by_hop(local_http, monkeypatch):
    from reelay.safefetch import FetchRefused, fetch_public
    port, routes = local_http
    allowed = {"127.0.0.1"}
    _resolver(monkeypatch, {"cdn.test": ["127.0.0.1"], "inside.test": ["10.0.0.5"]})
    routes["/ok"] = (302, {"Location": f"http://cdn.test:{port}/final"}, b"")
    routes["/final"] = (200, {}, b"DONE")
    assert fetch_public(f"http://cdn.test:{port}/ok", allowed=lambda ip: ip in allowed) == b"DONE"
    for target in ("http://169.254.169.254/latest/meta-data", "http://inside.test/x", "file:///etc/passwd",
                   "http://2130706433/x"):
        routes["/bounce"] = (302, {"Location": target}, b"")
        with pytest.raises(FetchRefused):
            fetch_public(f"http://cdn.test:{port}/bounce", allowed=lambda ip: ip in allowed)
    routes["/loop"] = (302, {"Location": f"http://cdn.test:{port}/loop"}, b"")
    with pytest.raises(FetchRefused, match="Too many redirects"):
        fetch_public(f"http://cdn.test:{port}/loop", allowed=lambda ip: ip in allowed)
    routes["/big"] = (200, {}, b"x" * 2048)
    with pytest.raises(FetchRefused, match="larger"):
        fetch_public(f"http://cdn.test:{port}/big", allowed=lambda ip: ip in allowed, max_bytes=1024)


def test_https_is_verified_for_the_name_not_the_address(monkeypatch):
    """The connection goes to the checked address, but TLS checks the certificate for the host name."""
    from reelay import safefetch
    seen = {}

    class Ctx:
        def wrap_socket(self, sock, server_hostname=None):
            seen["sni"] = server_hostname
            raise OSError("stop here")
    monkeypatch.setattr(safefetch, "_connect", lambda found, timeout: seen.setdefault("addr", found[4]))
    _resolver(monkeypatch, {"cdn.test": ["151.101.1.1"]})
    with pytest.raises(safefetch.FetchRefused):
        safefetch.fetch_public("https://cdn.test/c.jpg", context=Ctx())
    assert seen == {"addr": ("151.101.1.1", 443), "sni": "cdn.test"}


def test_caption_files_on_cdns_use_the_pinned_download(monkeypatch):
    from reelay import safefetch, transcripts
    got = []
    monkeypatch.setattr(safefetch, "fetch_public", lambda url, headers=None: got.append(url) or b"WEBVTT\n\nhi")

    class YDL:
        def __init__(self, opts):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            return {"subtitles": {"en": [{"ext": "vtt", "url": "https://v16.tiktokcdn-us.com/sub.vtt"}]}}

        def urlopen(self, url):
            raise AssertionError("a CDN caption file must not go through the unpinned opener")
    text, _ = transcripts.fetch_transcript("https://www.tiktok.com/@x/video/1", {}, YDL)
    assert text == "hi" and got == ["https://v16.tiktokcdn-us.com/sub.vtt"]
