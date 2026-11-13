"""Stored YouTube API data is refreshed or cleared within 30 days (reelay/youtube_retention.py).
Fakes only: no network, no keychain, no real accounts."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone

import pytest
import requests

import reelay.store as store_module
from reelay.accounts import AccountService
from reelay.models import Account, VideoItem
from reelay.paths import cover_cache_file
from reelay.sources import API_VIDEO_FIELDS, youtube_items
from reelay.store import Store
from reelay.tokens import MemoryTokenStore
from reelay.youtube import ReauthRequired, Tokens, YouTubeError
from reelay.youtube_retention import (CLEARED_CHANNEL_NAME, YouTubeRetention, age, cleared)

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
CHANNEL = "UCmine0000000000000000000"


def iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def resource(vid: str, title: str = "Morning routine", views: int = 477, channel: str = CHANNEL,
             privacy: str = "public", description: str = "How I start the day #calm") -> dict:
    return {"id": vid,
            "snippet": {"title": title, "description": description, "publishedAt": "2026-10-02T15:00:00Z",
                        "channelTitle": "Be Unconventional", "channelId": channel, "tags": ["calm"],
                        "thumbnails": {"high": {"url": f"https://i.ytimg.com/vi/{vid}/hq.jpg"}}},
            "contentDetails": {"duration": "PT47S"},
            "statistics": {"viewCount": str(views), "likeCount": "42", "commentCount": "3"},
            "status": {"privacyStatus": privacy}}


class FakeClient:
    """Stands in for YouTubeClient: answers from dicts, or raises what it's told to."""

    def __init__(self, channel: str = CHANNEL, videos: dict | None = None, error: Exception | None = None):
        self.channel, self.videos, self.error = channel, videos or {}, error
        self.calls: list[tuple] = []

    def my_channel(self) -> dict:
        self.calls.append(("channel",))
        if self.error:
            raise self.error
        return {"id": self.channel, "title": "Be Unconventional (new name)", "handle": "@beunconventional",
                "thumbnail": "https://yt3.ggpht.com/new.jpg", "uploads_playlist": "UU1"}

    def videos_by_id(self, ids):
        self.calls.append(("videos", tuple(ids)))
        if self.error:
            raise self.error
        return {i: self.videos[i] for i in ids if i in self.videos}


class Env:
    def __init__(self, tmp_path, monkeypatch, connected_days_ago: float = 1):
        self.store = Store(tmp_path / "reelay.db")
        self.tokens = MemoryTokenStore()
        self.accounts = AccountService(self.store, self.tokens)
        self.profile = self.store.create_profile("Be Unconventional", tmp_path / "videos")
        self.account = self.store.save_account(Account(
            profile_id=self.profile.id, platform="youtube", external_id=CHANNEL, display_name="Be Unconventional",
            handle="@beunc", avatar_url="https://yt3.ggpht.com/old.jpg", api_fetched_at=iso(connected_days_ago)))
        self.tokens.set(self.account.token_key, Tokens("access", "refresh", 9e12).to_json())
        self.client = FakeClient()
        monkeypatch.setattr(self.accounts, "client_for", lambda account: self.client)
        self.covers = tmp_path / "covers"
        self.covers.mkdir()
        self.library_updates: list[tuple[int, str]] = []
        self.reports = []
        self.retention = YouTubeRetention(self.store, self.accounts, covers_dir=self.covers,
                                          on_changed=self.reports.append,
                                          update_library=lambda pid, item: self.library_updates.append(
                                              (pid, item.key)),
                                          clock=lambda: NOW)

    def list_videos(self, *resources_, days_ago: float) -> dict[str, int]:
        items = youtube_items(list(resources_), fetched_at=iso(days_ago))
        return self.store.save_videos(self.profile.id, items, authorization="Own content")

    def item(self, row_id: int) -> VideoItem:
        return self.store.get_video(row_id).item

    def cover(self, key: str):
        path = cover_cache_file(self.covers, key)
        path.write_bytes(b"cached cover")
        return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    yield e
    e.store.close()


# ------------------------------------------------------------------ provenance
def test_data_api_listings_are_stamped_with_time_channel_and_fields():
    item = youtube_items([resource("v1")], fetched_at="2026-11-01T00:00:00Z")[0]
    assert item.api_fetched_at == "2026-11-01T00:00:00Z" and item.api_channel_id == CHANNEL
    assert set(item.api_fields) == set(API_VIDEO_FIELDS)
    record = item.to_record()
    assert VideoItem.from_record(record).api_fields == item.api_fields      # survives the database


def test_a_field_filled_from_yt_dlp_stops_counting_as_api_data():
    item = youtube_items([resource("v1")], fetched_at=iso(3))[0]
    item.update_from_info({"title": "Morning routine (yt-dlp)", "width": 1080, "height": 1920})
    assert "title" not in item.api_fields and "view_count" in item.api_fields
    plain = VideoItem(platform="YouTube", video_id="v1", url="u", title="t", description="d",
                      upload_date="20261002", view_count=1, like_count=1, comment_count=1, tags=["x"],
                      uploader="u", thumbnail_url="https://x/t.jpg", duration=1.0)
    item.merge_metadata(plain)      # everything non-empty now comes from elsewhere
    assert item.api_fields == ["availability"] and item.api_fetched_at == iso(3)


def test_merging_keeps_the_oldest_stamp_of_the_api_data_still_held():
    old = youtube_items([resource("v1")], fetched_at=iso(20))[0]
    fresh = youtube_items([resource("v1", description="")], fetched_at=iso(1))[0]
    old.merge_metadata(fresh)
    assert old.api_fetched_at == iso(20)            # the old description is still there, from 20 days ago
    whole = youtube_items([resource("v1")], fetched_at=iso(1))[0]
    old.merge_metadata(whole)
    assert old.api_fetched_at == iso(1)


def test_age_treats_missing_or_broken_stamps_as_too_old():
    assert age(iso(2), NOW) == timedelta(days=2)
    assert age("", NOW) > timedelta(days=10_000) and age("yesterday", NOW) > timedelta(days=10_000)


# ------------------------------------------------------------------ videos
def test_recent_data_is_left_alone_without_any_api_call(env):
    env.list_videos(resource("v1"), days_ago=10)
    report = env.retention.run_once()
    assert not report.changed and env.client.calls == [] and env.reports == []


def test_due_video_is_refreshed_through_the_connected_channel_and_creator_edits_survive(env):
    rows = env.list_videos(resource("v1"), resource("v2"), days_ago=28)
    v1, v2 = rows["YouTube_v1"], rows["YouTube_v2"]
    env.store.set_notes(v1, "Film the follow-up")
    env.store.update_ticket(v2, {"creator": "Andrew"})          # the creator's own edit
    item = env.item(v1)
    item.custom_name = "morning-routine"
    env.store.save_video(env.profile.id, item)
    cover = env.cover("YouTube_v1")
    env.client.videos = {"v1": resource("v1", title="Morning routine, updated", views=9001),
                         "v2": resource("v2", title="Second one")}

    report = env.retention.run_once()

    assert report.refreshed_videos == 2 and report.cleared_videos == 0
    assert env.client.calls == [("videos", ("v1", "v2"))]          # one call for both
    fresh = env.item(v1)
    assert (fresh.title, fresh.view_count, fresh.api_fetched_at) == ("Morning routine, updated", 9001, iso(0))
    assert fresh.custom_name == "morning-routine"
    row1, row2 = env.store.get_video(v1), env.store.get_video(v2)
    assert row1.notes == "Film the follow-up" and row1.ticket.creator == "Be Unconventional"
    assert row2.ticket.creator == "Andrew"
    assert not cover.exists()                                     # the old thumbnail is dropped
    assert env.reports and set(env.reports[0].keys) == {"YouTube_v1", "YouTube_v2"}


def test_video_gone_from_youtube_is_cleared_but_the_creators_own_work_stays(env):
    row = env.list_videos(resource("v1"), days_ago=28)["YouTube_v1"]
    env.store.set_notes(row, "keep me")
    item = env.item(row)
    item.custom_name, item.filename, item.folder = "keeper", "v1.mp4", str(env.profile.output_folder)
    env.store.save_video(env.profile.id, item)
    env.store.update_ticket(row, {"topic": "routines"})
    cover = env.cover("YouTube_v1")
    env.client.videos = {}                                          # deleted on YouTube

    report = env.retention.run_once()

    assert report.cleared_videos == 1
    gone = env.item(row)
    for name in API_VIDEO_FIELDS:
        assert getattr(gone, name) in (None, "", []), name
    assert (gone.api_fields, gone.api_fetched_at, gone.api_channel_id) == ([], "", "")
    assert (gone.custom_name, gone.filename, gone.video_id) == ("keeper", "v1.mp4", "v1")
    stored = env.store.get_video(row)
    assert stored.notes == "keep me" and stored.ticket.topic == "routines"
    assert stored.ticket.creator is None and stored.ticket.original_publish_date is None
    assert stored.ticket.source_url == "https://www.youtube.com/watch?v=v1"     # built from the id, kept
    assert not cover.exists()
    assert env.library_updates == [(env.profile.id, "YouTube_v1")]   # the sidecar no longer has the old data
    assert env.store.search(env.profile.id, "morning") == []            # out of the search index too
    assert env.retention.run_once().changed is False                     # nothing left to do


def test_disconnecting_the_channel_clears_its_videos_right_away(env):
    row = env.list_videos(resource("v1"), days_ago=2)["YouTube_v1"]
    env.accounts.disconnect(env.account)
    report = env.retention.run_once()
    assert report.cleared_videos == 1 and env.item(row).title == ""
    assert env.client.calls == []


def test_revoked_sign_in_clears_the_videos_and_the_channel_details(env):
    env.store.set_account_api_data(env.account.id, "Be Unconventional", "@beunc", "https://x/a.jpg", iso(28))
    row = env.list_videos(resource("v1"), days_ago=28)["YouTube_v1"]
    env.client.error = ReauthRequired("revoked")

    report = env.retention.run_once()

    assert report.cleared_videos == 1 and report.cleared_channels == 1
    account = env.store.get_account(env.account.id)
    assert (account.display_name, account.handle, account.avatar_url, account.api_fetched_at) == \
        (CLEARED_CHANNEL_NAME, "", "", None)
    assert account.external_id == CHANNEL                            # still knows which channel to reconnect
    assert env.item(row).title == ""
    assert env.retention.run_once().cleared_channels == 0            # cleared once, not every day


def test_missing_sign_in_counts_as_not_connected(env, monkeypatch):
    row = env.list_videos(resource("v1"), days_ago=28)["YouTube_v1"]

    def no_tokens(account):
        raise YouTubeError("Reconnect Be Unconventional: its sign-in is missing from the keychain.")
    monkeypatch.setattr(env.accounts, "client_for", no_tokens)
    assert env.retention.run_once().cleared_videos == 1 and env.item(row).title == ""


@pytest.mark.parametrize("error", [requests.ConnectionError("offline"),
                                   YouTubeError("quota", 403, "quotaExceeded")])
def test_a_passing_failure_waits_a_day_but_never_past_30_days(env, error):
    soon = env.list_videos(resource("v1"), days_ago=28)["YouTube_v1"]
    late = env.list_videos(resource("v2"), days_ago=29.5)["YouTube_v2"]
    env.client.error = error

    report = env.retention.run_once()

    assert report.waiting == 1 and report.cleared_videos == 1
    assert env.item(soon).title == "Morning routine"                 # tried again tomorrow
    assert env.item(late).title == ""                                # tomorrow would be over 30 days


def test_only_the_api_fields_are_cleared_not_ones_from_yt_dlp(env):
    row = env.list_videos(resource("v1"), days_ago=28)["YouTube_v1"]
    item = env.item(row)
    item.update_from_info({"title": "Title yt-dlp read from the page", "duration": 47})
    env.store.save_video(env.profile.id, item)
    env.client.videos = {}
    env.retention.run_once()
    after = env.item(row)
    assert after.title == "Title yt-dlp read from the page" and after.duration == 47
    assert after.view_count is None and after.description == ""


def test_listing_again_meanwhile_wins_over_a_stale_refresh(env):
    row = env.list_videos(resource("v1"), days_ago=28)["YouTube_v1"]
    original = env.client.videos_by_id

    def relisted_during_the_call(ids):
        env.list_videos(resource("v1", title="Brand new listing"), days_ago=0)
        return original(ids)
    env.client.videos = {}
    env.client.videos_by_id = relisted_during_the_call
    report = env.retention.run_once()
    assert report.cleared_videos == 0 and env.item(row).title == "Brand new listing"


def test_videos_of_another_channel_are_not_refreshed_with_this_channels_sign_in(env):
    row = env.list_videos(resource("v1"), days_ago=28)["YouTube_v1"]
    env.client.videos = {"v1": resource("v1", channel="UCsomeoneelse000000000000")}
    assert env.retention.run_once().cleared_videos == 1 and env.item(row).title == ""


def test_a_read_only_database_is_left_to_the_copy_that_can_edit_it(env, tmp_path):
    env.list_videos(resource("v1"), days_ago=29.9)
    env.client.error = ReauthRequired("revoked")
    viewer = Store(tmp_path / "reelay.db", read_only=True)
    try:
        retention = YouTubeRetention(viewer, env.accounts, clock=lambda: NOW)
        assert retention.run_once().changed is False
    finally:
        viewer.close()


# ------------------------------------------------------------------ channels
def test_channel_details_are_refreshed_when_due(env):
    env.store.set_account_api_data(env.account.id, "Be Unconventional", "@beunc", "https://x/a.jpg", iso(27))
    report = env.retention.run_once()
    account = env.store.get_account(env.account.id)
    assert report.refreshed_channels == 1
    assert (account.display_name, account.handle, account.api_fetched_at) == \
        ("Be Unconventional (new name)", "@beunconventional", iso(0))


def test_channel_details_are_not_fetched_before_they_are_due(env):
    env.retention.run_once()
    assert env.client.calls == []


def test_a_different_channel_behind_the_sign_in_clears_the_details(env):
    env.store.set_account_api_data(env.account.id, "Be Unconventional", "@beunc", "https://x/a.jpg", iso(28))
    env.client.channel = "UCother00000000000000000"
    assert env.retention.run_once().cleared_channels == 1


def test_connecting_stamps_the_channel_details(env):
    account = env.accounts.finish_connect(env.profile.id, {"id": "UCnew000000000000000000", "title": "New"},
                                          Tokens("a", "r", 9e12))
    assert age(env.store.get_account(account.id).api_fetched_at, datetime.now(timezone.utc)) < timedelta(
        minutes=5)


# ------------------------------------------------------------------ older databases
def test_upgrading_marks_data_listed_before_this_version(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    monkeypatch.setattr(store_module, "SCHEMA_VERSION", 11)
    old = Store(path)
    profile = old.create_profile("Hey, This Is Andrew", tmp_path / "videos")
    for platform, external_id in (("youtube", CHANNEL), ("bluesky", "did:plc:x")):    # as 1.0 wrote them
        old.conn.execute("INSERT INTO accounts (profile_id, platform, external_id, display_name, connected_at) "
                         "VALUES (?, ?, ?, 'Hey, This Is Andrew', '2026-10-01T09:00:00Z')",
                         (profile.id, platform, external_id))
    listed = youtube_items([resource("v1")])[0]
    record = {k: v for k, v in listed.to_record().items() if not k.startswith("api_")}   # as 1.0 wrote it
    tiktok = VideoItem(platform="TikTok", video_id="7001", url="https://www.tiktok.com/@x/video/7001", title="t")
    old.save_videos(profile.id, [tiktok])
    old.conn.execute("INSERT INTO videos (profile_id, platform, video_id, data, updated_at, imported_at) "
                     "VALUES (?, 'YouTube', 'v1', ?, '2026-10-20T00:00:00Z', '2026-10-05T00:00:00Z')",
                     (profile.id, json.dumps(record)))
    old.conn.commit()
    old.close()
    monkeypatch.setattr(store_module, "SCHEMA_VERSION", 12)

    new = Store(path)
    try:
        assert new.conn.execute("PRAGMA user_version").fetchone()[0] == 12
        youtube, bluesky = sorted(new.list_accounts(profile.id), key=lambda a: a.platform != "youtube")
        assert youtube.api_fetched_at == "2026-10-01T09:00:00Z" and bluesky.api_fetched_at is None
        rows = {r.item.platform: r.item for r in new.load_videos(profile.id)}
        assert rows["YouTube"].api_fetched_at == "2026-10-05T00:00:00Z"
        assert set(rows["YouTube"].api_fields) == set(API_VIDEO_FIELDS)
        assert rows["TikTok"].api_fields == [] and rows["TikTok"].api_fetched_at == ""
        assert [r.item.video_id for r in new.videos_with_youtube_api_data()] == ["v1"]
    finally:
        new.close()
    assert list(tmp_path.glob("old-backup-v11-*.db"))             # a copy was kept before upgrading


def test_an_older_record_without_a_channel_is_refreshed_through_the_profiles_channel(env):
    row = env.list_videos(resource("v1"), days_ago=40)["YouTube_v1"]
    with env.store.tx() as c:
        c.execute("UPDATE videos SET data = json_set(data, '$.api_channel_id', '') WHERE id = ?", (row,))
    env.client.videos = {"v1": resource("v1", title="Refreshed")}
    assert env.retention.run_once().refreshed_videos == 1
    assert env.item(row).title == "Refreshed" and env.item(row).api_channel_id == CHANNEL


# ------------------------------------------------------------------ running it
def test_runs_at_start_and_again_when_kicked(env):
    passes = []
    done = threading.Event()
    real = env.retention.run_once

    def counting():
        report = real()
        passes.append(report)
        done.set()
        return report
    env.retention.run_once = counting
    env.retention.start()
    try:
        assert done.wait(5)
        done.clear()
        env.retention.kick()
        assert done.wait(5)
    finally:
        env.retention.stop()
    assert len(passes) >= 2


def test_clearing_helper_resets_only_api_fields():
    item = youtube_items([resource("v1")], fetched_at=iso(1))[0]
    item.custom_name, item.width = "mine", 1080
    out = cleared(item)
    assert (out.title, out.tags, out.view_count, out.custom_name, out.width) == ("", [], None, "mine", 1080)
    assert item.title == "Morning routine"                          # the original isn't changed


def test_schema_has_the_new_column(env):
    cols = {r[1] for r in env.store.conn.execute("PRAGMA table_info(accounts)")}
    assert "api_fetched_at" in cols


# ------------------------------------------------------------------ the window
def test_the_window_drops_old_covers_and_the_older_listing_when_data_ages(qtbot, tmp_path, monkeypatch):
    from PySide6.QtGui import QPixmap
    from PySide6.QtWidgets import QApplication

    from reelay.gui.app import create_window
    from reelay.paths import db_path
    from reelay.youtube_retention import RetentionReport
    store = Store(db_path())
    profile = store.create_profile("Be Unconventional", tmp_path / "lib")
    store.set_active_profile(profile.id)
    window = create_window(QApplication.instance(), store, MemoryTokenStore())
    qtbot.addWidget(window)
    try:
        retention = window.services.youtube_retention
        assert retention.on_changed is not None
        listed = youtube_items([resource("v1")], fetched_at=iso(28))
        store.save_videos(profile.id, listed)
        window.download.fetched = list(listed)
        window.thumbs._covers["YouTube_v1"] = QPixmap(4, 4)
        retention.on_changed(RetentionReport(cleared_videos=1, keys=["YouTube_v1"]))   # as from its thread
        qtbot.waitUntil(lambda: window.download.fetched == [], timeout=2000)
        assert "YouTube_v1" not in window.thumbs._covers
    finally:
        window.close()
        store.close()
