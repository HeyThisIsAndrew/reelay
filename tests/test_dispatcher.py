"""The background dispatcher, with fake platform publishers (no network)."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from reelay.models import DispatchStatus, Platform, PostStatus, Stage
from reelay.pipeline import utc_iso
from reelay.publishers.base import PermanentError, PublishResult, TransientError
from reelay.scheduler import Dispatcher, backoff_seconds
from tests.fakes import make_world

T0 = datetime.now(timezone.utc).replace(microsecond=0)  # claims made by the pipeline use the real clock


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


class FakePublisher:
    def __init__(self, platform, outcomes=None, delay=0.0):
        self.platform = platform
        self.outcomes = list(outcomes or [])
        self.calls = []
        self.delay = delay
        self.lock = threading.Lock()

    def publish(self, job):
        with self.lock:
            self.calls.append(job)
        if self.delay:
            time.sleep(self.delay)
        outcome = self.outcomes.pop(0) if self.outcomes else "published"
        if isinstance(outcome, Exception):
            raise outcome
        status = DispatchStatus.SCHEDULED if outcome == "scheduled" else DispatchStatus.PUBLISHED
        return PublishResult(status, remote_id=f"{self.platform}-{job.dispatch.video_row}",
                             remote_url=f"https://{self.platform}.example/{job.dispatch.video_row}")


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = make_world(tmp_path, monkeypatch, connect=(Platform.BLUESKY, Platform.THREADS, Platform.FACEBOOK))
    w.store.set_setting("public_url", "https://reelay.example.com")
    yield w
    w.store.close()


def make_dispatcher(world, clock, **publishers):
    pubs = {p: publishers.get(p, FakePublisher(p)) for p in (Platform.BLUESKY, Platform.THREADS, Platform.FACEBOOK)}
    return Dispatcher(world.store, world.pipeline, pubs, interval=0.05, stale_seconds=600, clock=clock), pubs


def schedule(world, row, platforms, when=None):
    when = when or T0 + timedelta(days=1)
    world.pipeline.schedule(row, scheduled_at=when, platforms=platforms, now=T0 - timedelta(minutes=1))
    return when


def statuses(world, row):
    return {d.platform: d.status for d in world.store.dispatches_for(row)}


# ------------------------------------------------------------------ timing (hybrid)
def test_native_platforms_go_now_others_at_publish_time(world):
    clock = Clock()
    dispatcher, pubs = make_dispatcher(world, clock)
    row = world.rows[0]
    when = schedule(world, row, ["youtube", "facebook", "bluesky", "threads"])
    assert statuses(world, row) == {"youtube": "dispatching", "facebook": "pending", "bluesky": "pending",
                                    "threads": "pending"}
    assert len(world.uploads.jobs) == 1  # YouTube went straight to the upload queue

    pubs["facebook"].outcomes = ["scheduled"]
    assert dispatcher.run_once() == 1
    assert statuses(world, row)["facebook"] == "scheduled" and not pubs["bluesky"].calls
    assert pubs["facebook"].calls[0].dispatch.publish_at == utc_iso(when)
    world.uploads.finish(row, True, {"id": "YT1"})  # the YouTube upload completes
    assert statuses(world, row)["youtube"] == "scheduled"

    clock.now = when
    assert dispatcher.run_once() == 2
    assert statuses(world, row)["bluesky"] == "published" and statuses(world, row)["threads"] == "published"
    threads_job = pubs["threads"].calls[0]
    assert threads_job.media_url().startswith("https://reelay.example.com/media/")
    assert threads_job.text.startswith("Caption number 0")
    post = world.store.get_post(row)
    assert post.status == PostStatus.SCHEDULED and world.store.get_video(row).stage == Stage.SCHEDULED
    assert post.remote_url.endswith("YT1")  # the card's main link stays YouTube's


def test_publish_now_without_youtube_completes_the_card(world):
    clock = Clock()
    dispatcher, _ = make_dispatcher(world, clock)
    row = world.rows[1]
    world.pipeline.schedule(row, platforms=["bluesky"], now=T0)
    dispatcher.run_once()
    assert world.store.get_post(row).status == PostStatus.PUBLISHED
    assert world.store.get_video(row).stage == Stage.PUBLISHED


# ------------------------------------------------------------------ claiming
def test_concurrent_workers_never_double_publish(world):
    clock = Clock(T0 + timedelta(days=2))
    slow = FakePublisher(Platform.BLUESKY, delay=0.02)
    for row in world.rows[:3]:
        schedule(world, row, ["bluesky"])
    d1, _ = make_dispatcher(world, clock, bluesky=slow)
    d2 = Dispatcher(world.store, world.pipeline, {Platform.BLUESKY: slow}, interval=0.05, stale_seconds=600,
                    clock=clock)
    threads = [threading.Thread(target=d.run_once) for d in (d1, d2, d1, d2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rows_sent = sorted(job.dispatch.video_row for job in slow.calls)
    assert rows_sent == sorted(world.rows[:3])  # each exactly once
    assert all(statuses(world, r)["bluesky"] == "published" for r in world.rows[:3])


def test_stale_claims_are_requeued_and_logged(world, caplog):
    caplog.set_level(logging.WARNING)
    clock = Clock(T0 + timedelta(days=2))
    row = world.rows[0]
    schedule(world, row, ["bluesky"])
    dispatch = world.store.dispatches_for(row)[0]
    assert world.store.claim_dispatch(dispatch.id, "crashed-run") is not None  # left behind by a crash
    dispatcher, pubs = make_dispatcher(world, clock)
    dispatcher.run_once()
    assert "Requeued stale bluesky dispatch" in caplog.text and "an earlier run" in caplog.text
    assert len(pubs["bluesky"].calls) == 1 and statuses(world, row)["bluesky"] == "published"


def test_own_claim_older_than_timeout_is_requeued(world):
    clock = Clock(T0 + timedelta(days=2))
    row = world.rows[0]
    schedule(world, row, ["bluesky"])
    dispatch = world.store.dispatches_for(row)[0]
    world.store.claim_dispatch(dispatch.id, "this-run")
    world.store.conn.execute("UPDATE dispatches SET claimed_at=? WHERE id=?",
                             (utc_iso(clock.now - timedelta(hours=2)), dispatch.id))
    world.store.conn.commit()
    dispatcher, pubs = make_dispatcher(world, clock)
    dispatcher.run_once()
    assert len(pubs["bluesky"].calls) == 1


def test_interrupted_youtube_upload_resumes_through_the_queue(world):
    row = world.rows[0]
    schedule(world, row, ["youtube"])
    world.uploads.jobs.clear()  # the app quit mid-upload: the queue is gone, the claim remains
    restarted = make_world.__globals__["Pipeline"](world.store, world.accounts, world.uploads, instance_id="next-run")
    dispatcher = Dispatcher(world.store, restarted, {}, interval=1, stale_seconds=600, clock=Clock(T0))
    dispatcher.run_once()
    assert [j[0] for j in world.uploads.jobs] == [row]  # re-queued, not uploaded in parallel
    assert statuses(world, row)["youtube"] == "dispatching"


# ------------------------------------------------------------------ failures
def test_transient_errors_retry_with_backoff(world):
    clock = Clock(T0 + timedelta(days=2))
    flaky = FakePublisher(Platform.BLUESKY, [TransientError("Bluesky: HTTP 503")])
    dispatcher, _ = make_dispatcher(world, clock, bluesky=flaky)
    row = world.rows[0]
    schedule(world, row, ["bluesky"])
    dispatcher.run_once()
    d = world.store.dispatches_for(row)[0]
    assert d.status == "pending" and d.attempts == 1 and "503" in d.error
    assert d.next_attempt_at == utc_iso(clock.now + timedelta(seconds=backoff_seconds(1)))
    dispatcher.run_once()
    assert len(flaky.calls) == 1  # still waiting out the backoff
    clock.advance(seconds=backoff_seconds(1) + 1)
    dispatcher.run_once()
    assert len(flaky.calls) == 2 and statuses(world, row)["bluesky"] == "published"
    assert world.store.dispatches_for(row)[0].error == ""


def test_permanent_failure_on_one_platform_leaves_others_alone(world):
    clock = Clock(T0 + timedelta(days=2))
    broken = FakePublisher(Platform.BLUESKY, [PermanentError("Bluesky: sign-in failed")])
    dispatcher, _ = make_dispatcher(world, clock, bluesky=broken)
    row = world.rows[0]
    schedule(world, row, ["bluesky", "threads"])
    dispatcher.run_once()
    assert statuses(world, row) == {"bluesky": "failed", "threads": "published"}
    post = world.store.get_post(row)
    assert post.status == PostStatus.PUBLISHED and "Bluesky: Bluesky: sign-in failed" in post.error


def test_everything_failing_returns_card_to_staging(world):
    clock = Clock(T0 + timedelta(days=2))
    dispatcher, _ = make_dispatcher(world, clock, bluesky=FakePublisher(Platform.BLUESKY, [PermanentError("no")]))
    row = world.rows[0]
    schedule(world, row, ["bluesky"])
    dispatcher.run_once()
    assert world.store.get_post(row).status == PostStatus.FAILED
    assert world.store.get_video(row).stage == Stage.STAGING
    # and it can be scheduled again, for a new time
    world.pipeline.schedule(row, platforms=["bluesky"], scheduled_at=clock.now + timedelta(hours=1), now=clock.now)
    assert statuses(world, row)["bluesky"] == "pending"


def test_gives_up_after_max_attempts(world):
    clock = Clock(T0 + timedelta(days=2))
    always = FakePublisher(Platform.BLUESKY, [TransientError("Bluesky: HTTP 502")] * 10)
    dispatcher, _ = make_dispatcher(world, clock, bluesky=always)
    dispatcher.max_attempts = 3
    row = world.rows[0]
    schedule(world, row, ["bluesky"])
    for _ in range(5):
        dispatcher.run_once()
        clock.advance(hours=2)
    d = world.store.dispatches_for(row)[0]
    assert d.status == "failed" and "gave up after 3 attempts" in d.error and len(always.calls) == 3


def test_missing_credentials_fail_clearly(world):
    clock = Clock(T0 + timedelta(days=2))
    dispatcher, pubs = make_dispatcher(world, clock)
    row = world.rows[0]
    schedule(world, row, ["threads"])
    account = world.accounts.account_for(world.profile.id, Platform.THREADS)
    world.tokens.delete(account.token_key)  # e.g. removed from the keychain
    dispatcher.run_once()
    d = world.store.dispatches_for(row)[0]
    assert d.status == "failed" and "keychain" in d.error and not pubs["threads"].calls


def test_unexpected_exceptions_are_redacted_and_retried(world):
    clock = Clock(T0 + timedelta(days=2))
    leaky = FakePublisher(Platform.FACEBOOK, [RuntimeError("boom fb-page-token-SECRET-555")])
    dispatcher, _ = make_dispatcher(world, clock, facebook=leaky)
    row = world.rows[0]
    schedule(world, row, ["facebook"])
    dispatcher.run_once()
    d = world.store.dispatches_for(row)[0]
    assert d.status == "pending" and "SECRET" not in d.error


# ------------------------------------------------------------------ lifecycle
def test_background_thread_starts_kicks_and_stops(world):
    dispatcher, pubs = make_dispatcher(world, Clock(T0 + timedelta(days=2)))
    dispatcher.interval = 60
    dispatcher.start()
    assert dispatcher.running
    schedule(world, world.rows[0], ["bluesky"])
    dispatcher.kick()
    deadline = time.time() + 5
    while not pubs["bluesky"].calls and time.time() < deadline:
        time.sleep(0.02)
    dispatcher.stop()
    assert pubs["bluesky"].calls and not dispatcher.running


def test_scheduler_has_no_qt_dependency():
    import ast
    source = Path(__file__).resolve().parent.parent.joinpath("reelay", "scheduler.py").read_text()
    imported = {n.module or "" for n in ast.walk(ast.parse(source)) if isinstance(n, ast.ImportFrom)} | \
        {a.name for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Import) for a in n.names}
    assert not any(name.startswith("PySide6") or name.endswith("uploads") or ".gui" in name for name in imported)


def test_backoff_curve():
    assert [backoff_seconds(n) for n in range(1, 9)] == [60, 120, 240, 480, 960, 1920, 3600, 3600]


def test_upgrade_carries_youtube_schedules_into_dispatches(tmp_path, monkeypatch):
    import reelay.store as store_module
    from reelay.models import Post, VideoItem
    monkeypatch.setattr(store_module, "SCHEMA_VERSION", 1)
    old = store_module.Store(tmp_path / "old.db")
    profile = old.create_profile("Lydia", tmp_path)
    row = old.save_video(profile.id, VideoItem(platform="TikTok", video_id="1", url="u"))
    old.conn.execute(
        "INSERT INTO posts (video_row, title, status, publish_at, remote_id, remote_url, updated_at) "
        "VALUES (?, 'Old', 'scheduled', '2026-12-01T16:00:00Z', 'YTold', 'https://youtube.com/shorts/YTold', 'now')",
        (row,))
    old.conn.commit()
    old.close()
    monkeypatch.setattr(store_module, "SCHEMA_VERSION", 2)
    new = store_module.Store(tmp_path / "old.db")
    [dispatch] = new.dispatches_for(row)
    assert (dispatch.platform, dispatch.status, dispatch.remote_id, dispatch.publish_at) == \
        ("youtube", "scheduled", "YTold", "2026-12-01T16:00:00Z")
    post = new.get_post(row)
    assert post.target_platforms == ["youtube"] and post.bluesky_text is None
    new.close()
