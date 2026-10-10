"""Durable sends: nothing vanishes and nothing is posted twice when Reelay is
closed, crashes, loses power, or the computer sleeps. Every platform is fake."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from reelay.models import PostStatus, Stage
from reelay.pipeline import INTERRUPTED_MESSAGE, MISSED_MESSAGE, PAUSED_MESSAGE, UNSURE_MESSAGE, PipelineError, utc_iso
from reelay.scheduler import Dispatcher
from reelay.uploads import UploadFailure
from tests.fakes import approve_and_run, connect_platform, enable_publishing, make_world

NOW = datetime.now(timezone.utc).replace(microsecond=0)


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


class Pub:
    def __init__(self, platform):
        self.platform, self.calls = platform, []

    def publish(self, job):
        from reelay.publishers.base import PublishResult
        self.calls.append(job)
        return PublishResult(status="published", remote_id="R1", remote_url="https://example.com/R1")


@pytest.fixture
def world(tmp_path, monkeypatch):
    enable_publishing(monkeypatch)
    w = make_world(tmp_path, monkeypatch, connect=("bluesky", "threads", "facebook"))
    yield w
    w.store.close()


def schedule(w, row, platforms, when):
    """Schedule for `when`; a time in the past is written straight to the database (as if it
    was scheduled days ago and Reelay was closed since)."""
    future = max(when, datetime.now(timezone.utc) + timedelta(days=1))
    w.pipeline.schedule(row, scheduled_at=future, platforms=platforms, now=NOW - timedelta(minutes=1))
    if when < future:
        w.store.conn.execute("UPDATE dispatches SET due_at=?, publish_at=CASE WHEN platform IN ('youtube', "
                             "'facebook') THEN publish_at ELSE ? END WHERE video_row=? AND status='pending'",
                             (utc_iso(when), utc_iso(when), row))
        w.store.conn.commit()


def dispatcher(w, clock=None, instance="next-run"):
    from reelay.pipeline import Pipeline
    p = Pipeline(w.store, w.accounts, w.uploads, instance_id=instance)
    pubs = {name: Pub(name) for name in ("bluesky", "threads", "facebook")}
    return Dispatcher(w.store, p, pubs, interval=1, stale_seconds=600, clock=clock or Clock()), p, pubs


def status(w, row, platform):
    return next(d for d in w.store.dispatches_for(row) if d.platform == platform)


# ------------------------------------------------------------------ crash mid-send
def test_crash_mid_send_holds_unsafe_platforms_and_resumes_safe_ones(world):
    row = world.rows[0]
    schedule(world, row, ["facebook", "bluesky"], NOW + timedelta(days=1))
    for d in world.store.dispatches_for(row):
        world.store.claim_dispatch(d.id, "crashed-run")
    d, pipeline, pubs = dispatcher(world)
    held = d.reconcile()
    assert [x.platform for x in held] == ["facebook"] and held[0].error == INTERRUPTED_MESSAGE
    d.run_once()
    resumed = status(world, row, "bluesky")
    assert resumed.status == "pending" and resumed.claimed_by is None   # resumable: requeued for its time
    assert status(world, row, "facebook").status == "interrupted" and not pubs["facebook"].calls
    card = pipeline.card(row)
    assert card.coverage["facebook"].state == "needs_work" and "Interrupted" in card.coverage["facebook"].reason
    assert world.store.get_post(row).status == PostStatus.SCHEDULED       # held, not failed, not "uploading"


def test_resolving_an_interrupted_send(world):
    row, other = world.rows[0], world.rows[1]
    schedule(world, row, ["facebook", "bluesky"], NOW - timedelta(hours=1))
    schedule(world, other, ["bluesky"], NOW + timedelta(days=1))
    fb, bsky = (status(world, row, p) for p in ("facebook", "bluesky"))
    cancel_me = status(world, other, "bluesky")
    for x in (fb, bsky, cancel_me):
        world.store.interrupt_dispatch(x.id, INTERRUPTED_MESSAGE)
    world.store.set_checkpoint(bsky.id, '{"rkey": "old"}')
    d, pipeline, pubs = dispatcher(world)
    with pytest.raises(PipelineError, match="Facebook link"):
        pipeline.resolve_interrupted(fb.id, "posted", "https://bsky.app/x")
    pipeline.resolve_interrupted(fb.id, "posted", "https://www.facebook.com/reel/123")
    done = status(world, row, "facebook")
    assert done.status in ("published", "scheduled") and done.remote_url.endswith("/reel/123")
    assert done.remote_id == "123"
    pipeline.resolve_interrupted(cancel_me.id, "cancel")
    assert status(world, other, "bluesky").status == "failed" and "Cancelled" in status(world, other, "bluesky").error
    pipeline.resolve_interrupted(bsky.id, "retry")
    again = status(world, row, "bluesky")
    assert again.status == "pending" and again.publish_at is None and again.checkpoint == ""   # time passed: now
    d.clock = Clock(datetime.now(timezone.utc) + timedelta(seconds=1))
    d.run_once()
    assert status(world, row, "bluesky").status == "published"
    with pytest.raises(PipelineError, match="isn't waiting"):
        pipeline.resolve_interrupted(bsky.id, "retry")


# ------------------------------------------------------------------ missed while closed / asleep
def test_missed_sends_wait_but_on_time_ones_go(world):
    late, on_time = world.rows[0], world.rows[1]
    schedule(world, late, ["bluesky"], NOW - timedelta(hours=3))              # would now go out 3 hours late
    schedule(world, on_time, ["youtube"], NOW + timedelta(days=1))            # YouTube holds it: still on time
    world.uploads.jobs.clear()                                                # never handed over: still waiting
    world.store.conn.execute("UPDATE dispatches SET status='pending', claimed_by=NULL, claimed_at=NULL, due_at=? "
                             "WHERE video_row=?", (utc_iso(NOW - timedelta(hours=3)), on_time))
    world.store.conn.commit()
    d, pipeline, pubs = dispatcher(world)
    held = d.reconcile()
    assert [(x.video_row, x.error) for x in held] == [(late, MISSED_MESSAGE)]
    d.run_once()
    assert not pubs["bluesky"].calls and status(world, on_time, "youtube").status == "dispatching"
    assert pipeline.card(late).row.stage == Stage.SCHEDULED


def test_wake_from_sleep_checks_for_missed_sends(world, monkeypatch):
    row = world.rows[0]
    clock = Clock(NOW - timedelta(hours=5))
    d, _pipeline, pubs = dispatcher(world, clock)
    schedule(world, row, ["bluesky"], NOW - timedelta(hours=2))
    calls = []
    real = d.run_once

    def once():
        calls.append(clock.now)
        result = real()
        if len(calls) == 1:
            clock.now = NOW                       # the laptop slept for 5 hours between passes
        else:
            d._stop.set()
        return result
    monkeypatch.setattr(d, "run_once", once)
    d.interval = 0.01
    t = threading.Thread(target=d._loop)
    t.start()
    t.join(5)
    assert status(world, row, "bluesky").status == "interrupted" and not pubs["bluesky"].calls


# ------------------------------------------------------------------ YouTube uploads
def test_upload_failures_retry_only_when_nothing_could_be_posted(world):
    row = world.rows[0]
    schedule(world, row, ["youtube"], NOW + timedelta(days=1))
    yt = status(world, row, "youtube")
    world.store.claim_dispatch(yt.id, "this-run")
    p = world.pipeline
    msg = p._on_upload_finished(row, False, UploadFailure("Network problem during upload: reset", transient=True))
    again = status(world, row, "youtube")
    assert again.status == "pending" and again.attempts == 1 and "Retrying" in again.error and "tries again" in msg
    world.store.claim_dispatch(yt.id, "this-run")
    p._on_upload_finished(row, False, UploadFailure("Upload kept failing (timeout)", transient=True, uncertain=True))
    assert status(world, row, "youtube").status == "interrupted"
    assert status(world, row, "youtube").error == UNSURE_MESSAGE
    p.resolve_interrupted(yt.id, "retry")
    world.store.claim_dispatch(yt.id, "this-run")
    p._on_upload_finished(row, False, "YouTube said: quotaExceeded")
    assert status(world, row, "youtube").status == "failed"


def test_closing_mid_upload_pauses_then_continues(world):
    row = world.rows[0]
    world.pipeline.schedule(row, platforms=["youtube"], now=NOW)          # publish as soon as it's uploaded
    yt = status(world, row, "youtube")
    world.store.claim_dispatch(yt.id, "this-run")
    world.pipeline.pause_upload(row)
    paused = status(world, row, "youtube")
    assert paused.status == "pending" and paused.checkpoint == "paused" and paused.error == PAUSED_MESSAGE
    later = Clock(NOW + timedelta(days=1))                                 # reopened the next day
    world.uploads.jobs.clear()
    d, _pipeline, _pubs = dispatcher(world, later)
    assert d.reconcile() == []                                             # a paused upload isn't "missed"
    d.run_once()
    assert [j[0] for j in world.uploads.jobs] == [row] and status(world, row, "youtube").checkpoint == ""
    world.pipeline.pause_upload(row, uncertain=True)
    assert status(world, row, "youtube").status == "interrupted"


class FakeWorker:
    def __init__(self, row_id, result=None, last_piece=False, finishes_while_stopping=False):
        self.row_id, self.result, self.last_piece_sent = row_id, result, last_piece
        self.finishes = finishes_while_stopping
        self.cancelled = False
        self.done = type("S", (), {"disconnect": lambda s: None})()
        self.failed = self.done

    def cancel(self):
        self.cancelled = True

    def wait(self, _ms):
        if self.finishes:
            self.result = {"id": "V9"}


@pytest.mark.gui
def test_upload_manager_shutdown_records_before_stopping(qtbot):
    from reelay.uploads import UploadJob, UploadManager
    events = []

    def setup(worker, queued=()):
        m = UploadManager()
        m.pause_handler = lambda row, unsure: events.append(("pause", row, unsure, worker.cancelled))
        m.completion_handler = lambda row, ok, payload: events.append(("done", row, ok, payload))
        m._worker = worker
        m._queue.extend(UploadJob(r, None, Path("x.mp4"), {}) for r in queued)
        return m

    setup(FakeWorker(1), queued=[2]).shutdown()
    assert events == [("pause", 2, False, False), ("pause", 1, False, False)]   # written before cancel()
    events.clear()
    setup(FakeWorker(3, last_piece=True, finishes_while_stopping=True)).shutdown()
    assert events == [("done", 3, True, {"id": "V9"})]                         # landed at the last moment
    events.clear()
    w = FakeWorker(4, last_piece=True)
    setup(w).shutdown()
    assert events == [("pause", 4, True, False)] and w.cancelled                # may be on YouTube: ask


# ------------------------------------------------------------------ UI and API
@pytest.mark.gui
def test_recovery_dialog_and_card(world, qtbot):
    from reelay.gui.board_page import CardDelegate
    from reelay.gui.recovery_dialog import RecoveryDialog
    a, b = world.rows[0], world.rows[1]
    schedule(world, a, ["facebook"], NOW + timedelta(days=1))
    schedule(world, b, ["bluesky"], NOW + timedelta(days=1))
    held = []
    for row in (a, b):
        x = world.store.dispatches_for(row)[0]
        held.append(world.store.interrupt_dispatch(x.id, INTERRUPTED_MESSAGE))
    text, _ = CardDelegate._status(world.pipeline.card(a))
    assert text == "⚠ Interrupted on Facebook: open to decide"
    dialog = RecoveryDialog(world.pipeline, held, ask_link=lambda p: "https://www.facebook.com/reel/77")
    qtbot.addWidget(dialog)
    dialog._posted(held[0].id, "facebook")
    assert status(world, a, "facebook").status == "scheduled"     # Facebook holds it until its time
    assert len(dialog.row_widgets) == 1
    dialog._resolve(held[1].id, "cancel")
    assert dialog.result() == RecoveryDialog.Accepted and dialog.resolved[-1] == (held[1].id, "cancel")


def test_resolve_through_the_api(tmp_path, monkeypatch):
    import requests

    from reelay.api import ApiServer, create_app
    from reelay.media import MediaLinks
    enable_publishing(monkeypatch)
    w = make_world(tmp_path, monkeypatch)
    connect_platform(w.accounts, w.profile.id, "bluesky")
    row = w.rows[0]
    schedule(w, row, ["bluesky"], NOW + timedelta(days=1))
    x = w.store.dispatches_for(row)[0]
    server = ApiServer(create_app(w.store, w.pipeline, w.accounts, lambda: "t" * 40, MediaLinks()),
                       host="127.0.0.1", port=0)
    assert server.start()
    try:
        h = {"Authorization": "Bearer " + "t" * 40}
        url = f"{server.url}/v1/videos/{row}/publications/bluesky/resolve"
        assert requests.post(url, json={"action": "retry"}, headers=h, timeout=10).status_code == 409
        w.store.interrupt_dispatch(x.id, INTERRUPTED_MESSAGE)
        r = requests.post(url, json={"action": "posted", "url": "https://bsky.app/profile/x/post/3k"},
                          headers=h, timeout=10)
        assert w.store.dispatches_for(row)[0].status == "interrupted"                # waits for a person
        assert approve_and_run(w.store, w.pipeline, r).status == "executed"
        pub = w.store.dispatches_for(row)[0]
        assert pub.status == "published" and pub.remote_url.endswith("/3k")
    finally:
        server.stop()
        w.store.close()
