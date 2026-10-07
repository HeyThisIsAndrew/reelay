from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from reelay.models import Account, Post, PostStatus, Privacy, Stage, VideoItem
from reelay.publishing import (build_resource, default_post, humanize_name, reconcile, to_utc_iso,
                               validate_post)
from reelay.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def vid(n, **kw):
    return VideoItem(platform="TikTok", video_id=str(n), url=f"https://t/{n}", upload_date="20261002",
                     description=kw.pop("description", f"caption {n} #growth #Healing"), **kw)


# ------------------------------------------------------------------ store
def test_schema_and_default_profile(store, tmp_path):
    from reelay.store import SCHEMA_VERSION
    assert store.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    p = store.active_profile(tmp_path / "lib")
    assert p.name == "My Channel" and store.active_profile(tmp_path).id == p.id  # stable


def test_profiles_crud_and_switch(store, tmp_path):
    a = store.create_profile("Lydia Livingood", tmp_path / "a")
    b = store.create_profile("Andrew", tmp_path / "b")
    store.set_active_profile(b.id)
    assert store.active_profile(tmp_path).name == "Andrew"
    a.template = "{date}_{custom}"
    store.update_profile(a)
    assert store.get_profile(a.id).template == "{date}_{custom}"
    assert [p.name for p in store.list_profiles()] == ["Andrew", "Lydia Livingood"]
    store.delete_profile(b.id)
    assert store.active_profile(tmp_path).id == a.id


def test_video_upsert_preserves_stage_and_updates_metadata(store, tmp_path):
    p = store.create_profile("P", tmp_path)
    ids = store.save_videos(p.id, [vid(1), vid(2)])
    store.set_stage([ids["TikTok_1"]], Stage.STAGING)
    changed = vid(1, custom_name="new name")
    store.save_video(p.id, changed)
    rows = {r.item.video_id: r for r in store.load_videos(p.id)}
    assert rows["1"].stage == Stage.STAGING and rows["1"].item.custom_name == "new name"
    assert rows["2"].stage == Stage.BACKLOG
    assert [r.item.video_id for r in store.load_videos(p.id, [Stage.STAGING])] == ["1"]


def test_same_video_in_two_profiles_is_independent(store, tmp_path):
    a, b = store.create_profile("A", tmp_path), store.create_profile("B", tmp_path)
    store.save_videos(a.id, [vid(1)])
    store.save_videos(b.id, [vid(1)])
    assert len(store.load_videos(a.id)) == len(store.load_videos(b.id)) == 1


def test_stage_positions_append_and_reorder(store, tmp_path):
    p = store.create_profile("P", tmp_path)
    ids = list(store.save_videos(p.id, [vid(1), vid(2), vid(3)]).values())
    store.set_stage(ids, Stage.STAGING)
    assert [r.id for r in store.load_videos(p.id, [Stage.STAGING])] == ids
    store.reorder(list(reversed(ids)))
    assert [r.id for r in store.load_videos(p.id, [Stage.STAGING])] == list(reversed(ids))
    with pytest.raises(ValueError):
        store.set_stage(ids, "nonsense")


def test_posts_roundtrip_and_interrupted_uploads(store, tmp_path):
    p = store.create_profile("P", tmp_path)
    row = store.save_video(p.id, vid(1))
    post = store.save_post(Post(video_row=row, title="Hi", tags=["a", "b"], publish_at="2026-10-20T16:00:00Z",
                                status=PostStatus.UPLOADING))
    again = store.save_post(Post(video_row=row, title="Hi 2", status=PostStatus.UPLOADING))
    assert again.id == post.id  # one post per video
    loaded = store.get_post(row)
    assert loaded.title == "Hi 2" and loaded.tags == []
    assert store.reset_interrupted_uploads() == 1
    assert store.get_post(row).status == PostStatus.DRAFT
    assert store.posts_with_status(p.id, [PostStatus.DRAFT])[0].video_row == row


def test_accounts_unique_and_cascade(store, tmp_path):
    a, b = store.create_profile("A", tmp_path), store.create_profile("B", tmp_path)
    acc = store.save_account(Account(profile_id=a.id, platform="youtube", external_id="UC1", display_name="Ch"))
    same = store.save_account(Account(profile_id=a.id, platform="youtube", external_id="UC1", display_name="Renamed"))
    assert same.id == acc.id and store.get_account(acc.id).display_name == "Renamed"
    other = store.save_account(Account(profile_id=b.id, platform="youtube", external_id="UC1", display_name="Ch"))
    assert store.account_in_use_elsewhere(other)
    row = store.save_video(a.id, vid(1))
    store.save_post(Post(video_row=row, account_id=acc.id))
    store.delete_account(acc.id)
    assert store.get_post(row).account_id is None  # SET NULL, post survives
    store.delete_profile(a.id)
    assert store.load_videos(a.id) == [] and store.get_post(row) is None
    assert not store.account_in_use_elsewhere(other)


def test_settings(store):
    assert store.get_setting("theme", "dark") == "dark"
    store.set_setting("theme", "black")
    store.set_setting("theme", "light")
    assert store.get_setting("theme") == "light"


# ------------------------------------------------------------------ publishing
def test_default_post_uses_custom_name_hashtags_and_strips_brackets():
    item = vid(1, custom_name="letting-go-of-who-you-were", description="a <b> caption #Growth #growth")
    post = default_post(7, item, account_id=3)
    assert post.title == "Letting go of who you were"
    assert post.tags == ["growth"] and "<" not in post.description and post.account_id == 3


def test_default_post_title_falls_back_to_hook():
    assert default_post(1, vid(1, description="first sentence here please. second")).title == \
        "First sentence here please."
    assert humanize_name("my_cool--name") == "My cool name"


NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)


def _post(**kw):
    base = dict(video_row=1, account_id=1, title="Title", description="d", privacy=Privacy.PUBLIC)
    base.update(kw)
    return Post(**base)


def test_validate_post(tmp_path):
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    assert validate_post(_post(), f, NOW) == []
    assert validate_post(_post(publish_at="2026-10-08T00:00:00Z"), f, NOW) == []
    errs = validate_post(_post(account_id=None, title="", publish_at="2026-10-07T12:05:00Z"), None, NOW)
    assert len(errs) == 4
    assert any("100" in e for e in validate_post(_post(title="x" * 101), f, NOW))
    assert any("< or >" in e for e in validate_post(_post(description="a<b"), f, NOW))


def test_build_resource_scheduling_forces_private():
    sched = build_resource(_post(privacy=Privacy.PUBLIC, publish_at="2026-10-20T16:00:00Z", tags=["a"]))
    assert sched["status"] == {"selfDeclaredMadeForKids": False, "privacyStatus": "private",
                               "publishAt": "2026-10-20T16:00:00Z"}
    now = build_resource(_post(privacy=Privacy.UNLISTED), add_shorts_tag=True)
    assert now["status"]["privacyStatus"] == "unlisted" and now["snippet"]["description"].endswith("#Shorts")
    once = build_resource(_post(description="hi #shorts"), add_shorts_tag=True)
    assert once["snippet"]["description"].lower().count("#shorts") == 1


def test_reconcile():
    p = _post(status=PostStatus.SCHEDULED, publish_at="2026-10-07T10:00:00Z")
    assert reconcile(p, {"privacy": "public"}, NOW) == (PostStatus.PUBLISHED, "")
    status, note = reconcile(p, {"privacy": "private", "publish_at": None}, NOW)
    assert status == PostStatus.SCHEDULED and "audit" in note
    assert reconcile(p, None, NOW)[0] == PostStatus.FAILED
    future = _post(status=PostStatus.SCHEDULED, publish_at="2026-10-09T10:00:00Z")
    assert reconcile(future, {"privacy": "private", "publish_at": future.publish_at}, NOW) == (PostStatus.SCHEDULED, "")


def test_to_utc_iso():
    aware = datetime(2026, 10, 20, 9, 0, tzinfo=timezone(timedelta(hours=-7)))
    assert to_utc_iso(aware) == "2026-10-20T16:00:00Z"
