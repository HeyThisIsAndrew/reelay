"""The authorization boundary for agents (ScopedPipeline + approvals): reads pass,
draft edits are rate limited, and anything that posts, archives or changes many
tickets waits for a person. Nothing leaves this machine."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from reelay import approvals
from reelay.models import ApprovalStatus, Stage, VideoItem
from reelay.pipeline import PipelineError
from reelay.scoped_pipeline import (MAX_IDS_PER_CALL, MAX_PENDING_PER_PROFILE, RateLimiter, ScopedPipeline,
                                    request_approval, snapshot)
from tests.fakes import disable_publishing, make_world

MUTATORS = ("schedule", "_schedule_write", "start_upload", "archive", "bulk_update", "apply_sheet",
            "set_platform_state", "confirm_handoff", "mark_posted", "resolve_interrupted", "enqueue_youtube")


@pytest.fixture
def world(tmp_path, monkeypatch):
    disable_publishing(monkeypatch)
    w = make_world(tmp_path, monkeypatch)          # tickets ready for YouTube
    yield w
    w.store.close()


class Spy:
    """Wraps the real pipeline and records every mutating call that gets through."""

    def __init__(self, real):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "calls", [])

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if name in MUTATORS:
            def record(*a, **k):
                self.calls.append(name)
                return attr(*a, **k)
            return record
        return attr


def scoped(world, **kw):
    spy = Spy(world.pipeline)
    return ScopedPipeline(spy, **kw), spy


def future(days=2):
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0)


# ------------------------------------------------------------------ schema
def test_v8_database_gets_the_approvals_table(tmp_path, monkeypatch):
    import reelay.store as store_module
    current = store_module.SCHEMA_VERSION
    monkeypatch.setattr(store_module, "SCHEMA_VERSION", 8)
    store_module.Store(tmp_path / "v8.db").close()
    monkeypatch.setattr(store_module, "SCHEMA_VERSION", current)
    store = store_module.Store(tmp_path / "v8.db")
    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(approval_requests)")}
    assert {"id", "profile_id", "operation", "payload", "target_hash", "status", "created_at", "expires_at",
            "target_ids", "requested_by", "decided_at", "decided_by", "result"} <= cols
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("INSERT INTO approval_requests (profile_id, operation, payload, status, created_at, "
                           "expires_at) VALUES (1, 'x', '{}', 'done', 'a', 'b')")
    store.close()


def test_state_machine_refuses_illegal_moves(world):
    p = request_approval(world.store, world.profile.id, "archive", {"video_ids": [world.rows[0]], "archived": True},
                         [world.rows[0]])
    assert world.store.transition_approval(p.approval_id, ApprovalStatus.EXECUTED) is None   # not from pending
    assert world.store.transition_approval(p.approval_id, ApprovalStatus.APPROVED, "me").status == "approved"
    assert world.store.transition_approval(p.approval_id, ApprovalStatus.REJECTED) is None   # already decided
    assert world.store.transition_approval(p.approval_id, ApprovalStatus.EXECUTED).status == "executed"
    assert world.store.transition_approval(p.approval_id, ApprovalStatus.FAILED) is None     # final


# ------------------------------------------------------------------ the boundary
def test_mutations_never_reach_the_pipeline(world):
    sp, spy = scoped(world)
    a, b = world.rows[0], world.rows[1]
    results = [
        sp.schedule(a, scheduled_at=future(), platforms=["youtube"]),
        sp.start_upload(world.store.get_post(b)),
        sp.archive([a, b]),
        sp.archive([a], archived=False),
        sp.bulk_update([a, b], {"topic": "Eyewear"}),
        sp.set_platform_state(a, "tiktok", "posted", url="https://www.tiktok.com/@x/video/1"),
        sp.mark_posted(a, "instagram", url="https://www.instagram.com/reel/x"),
    ]
    assert spy.calls == []                                            # not one mutating call got through
    assert all(r.status == "pending" and r.approval_id for r in results)
    assert world.store.dispatches_for(a) == [] and world.store.get_video(a).archived_at is None
    assert world.store.get_video(a).ticket.topic != "Eyewear" and world.store.manual_posts(a) == []
    assert world.uploads.jobs == []
    assert {r.operation for r in world.store.list_approvals()} == {"schedule", "archive", "bulk_update",
                                                                   "set_platform_state", "mark_posted"}


def test_force_cannot_skip_approval_and_preflight_fails_fast(world):
    sp, spy = scoped(world)
    pending = sp.schedule(world.rows[0], scheduled_at=future(), force=True)
    assert pending.status == "pending" and world.store.get_approval(pending.approval_id).payload["force"] is True
    with pytest.raises(PipelineError) as err:
        sp.schedule(world.rows[0], platforms=["bluesky"])               # not enabled: refused now, no request
    assert err.value.code == "platform_disabled" and len(world.store.list_approvals()) == 1
    with pytest.raises(PipelineError, match="in the past"):
        sp.schedule(world.rows[0], scheduled_at=datetime.now(timezone.utc) - timedelta(hours=1))
    with pytest.raises(PipelineError, match="too soon"):
        sp.schedule(world.rows[0], scheduled_at=datetime.now(timezone.utc) + timedelta(minutes=12))
    assert spy.calls == []


@pytest.mark.parametrize("bad", [True, False, "1", 1.0, 0, -3, 2 ** 63, None, [1]])
def test_video_ids_are_strictly_validated(world, bad):
    sp, spy = scoped(world)
    with pytest.raises(PipelineError) as err:
        sp.update_metadata(bad, {"topic": "x"})
    assert err.value.code == "invalid_id"
    with pytest.raises(PipelineError):
        sp.archive([world.rows[0], bad])
    assert spy.calls == [] and world.store.list_approvals() == []


def test_profile_scope_and_mixing(world, tmp_path):
    other = world.store.create_profile("Other", tmp_path / "other")
    foreign = world.store.save_video(other.id, VideoItem(platform="TikTok", video_id="x1", url="https://www.tiktok.com/@o/video/1"))
    sp, _ = scoped(world, profile_ids={world.profile.id})
    with pytest.raises(PipelineError) as err:
        sp.card(foreign)
    assert err.value.code == "not_found"                              # doesn't even reveal it exists
    with pytest.raises(PipelineError, match="same profile"):
        ScopedPipeline(world.pipeline).archive([world.rows[0], foreign])
    with pytest.raises(PipelineError, match="At most"):
        sp.bulk_update(list(range(1, MAX_IDS_PER_CALL + 2)), {"topic": "x"})


def test_duplicates_are_reused_and_pending_is_capped(world, monkeypatch):
    sp, _ = scoped(world)
    first = sp.archive([world.rows[0]])
    again = sp.archive([world.rows[0]])
    assert again.reused and again.approval_id == first.approval_id and len(world.store.list_approvals()) == 1
    monkeypatch.setattr("reelay.scoped_pipeline.MAX_PENDING_PER_PROFILE", 1)
    with pytest.raises(PipelineError) as err:
        sp.archive([world.rows[1]])
    assert err.value.code == "too_many_pending"
    assert MAX_PENDING_PER_PROFILE == 100


def test_schedule_requests_expire_before_the_post_time(world):
    sp, _ = scoped(world)
    when = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=2)
    req = world.store.get_approval(sp.schedule(world.rows[0], scheduled_at=when).approval_id)
    expires = datetime.fromisoformat(req.expires_at.replace("Z", "+00:00"))
    assert expires == when - timedelta(minutes=10)


def test_draft_edits_pass_but_are_rate_limited(world):
    clock = [0.0]
    sp, spy = scoped(world, limiter=RateLimiter(capacity=3, per_second=1, clock=lambda: clock[0]))
    for n in range(3):
        sp.update_metadata(world.rows[0], {"topic": f"t{n}"})
    assert world.store.get_video(world.rows[0]).ticket.topic == "t2"
    with pytest.raises(PipelineError) as err:
        sp.update_metadata(world.rows[0], {"topic": "t3"})
    assert err.value.code == "rate_limited"
    clock[0] += 2
    sp.update_metadata(world.rows[0], {"topic": "t3"})
    assert spy.calls == []


def test_store_view_is_read_only(world):
    sp, _ = scoped(world)
    assert sp.store.get_video(world.rows[0]).id == world.rows[0]
    for name in ("save_post", "set_stage", "update_ticket", "conn", "create_approval", "transition_approval",
                 "delete_video", "set_setting", "tx"):
        with pytest.raises(AttributeError):
            getattr(sp.store, name)
    with pytest.raises(AttributeError):
        sp.store.conn = None


def test_transcripts_only_from_platform_links(world, monkeypatch):
    sp, _ = scoped(world)
    seen = {}
    monkeypatch.setattr(type(world.pipeline), "fetch_transcript",
                        lambda self, row, opts, replace_existing=False, fetch=None: seen.setdefault("opts", opts))
    sp.fetch_transcript(world.rows[0], {"quiet": True})
    assert seen["opts"]["allowed_extractors"] and seen["opts"]["enable_file_urls"] is False
    sp.update_metadata(world.rows[1], {"source_url": "http://192.168.1.1/admin"})   # an agent edits the link…
    with pytest.raises(PipelineError, match="Only TikTok, Instagram and YouTube"):
        sp.fetch_transcript(world.rows[1], {})                                         # …and can't use it


def test_cover_paths_stay_in_the_library(world, tmp_path):
    sp, _ = scoped(world)
    inside = world.folder / "cover.jpg"
    sp.update_metadata(world.rows[0], {"thumbnail_path": str(inside)})
    for bad in ("/etc/passwd", str(world.folder / ".." / "secret.jpg"), str(world.folder / "notes.txt")):
        with pytest.raises(PipelineError):
            sp.update_metadata(world.rows[0], {"thumbnail_path": bad})


# ------------------------------------------------------------------ deciding and carrying out
def test_approve_then_the_app_carries_it_out(world):
    sp, _ = scoped(world)
    pending = sp.schedule(world.rows[0], scheduled_at=future())
    req = approvals.approve(world.store, pending.approval_id, "andrew (test)")
    assert req.status == "approved" and req.decided_by == "andrew (test)"
    assert world.store.dispatches_for(world.rows[0]) == []           # deciding doesn't execute
    [done] = approvals.execute_approved(world.pipeline)
    assert done.status == "executed" and done.result["dispatches"][0]["platform"] == "youtube"
    assert world.store.get_video(world.rows[0]).stage == Stage.SCHEDULED
    assert approvals.execute_approved(world.pipeline) == []          # once only
    with pytest.raises(approvals.ApprovalError, match="executed"):
        approvals.approve(world.store, pending.approval_id, "again")


def test_changed_ticket_makes_the_request_stale(world):
    sp, _ = scoped(world)
    pending = sp.schedule(world.rows[0], scheduled_at=future())
    world.pipeline.update_metadata(world.rows[0], {"title": "A different title"})   # edited while waiting
    req = approvals.approve(world.store, pending.approval_id, "me")
    assert req.status == "stale" and world.store.dispatches_for(world.rows[0]) == []
    # changed between approval and execution: also stale, nothing done
    pending = sp.archive([world.rows[1]])
    approvals.approve(world.store, pending.approval_id, "me")
    (world.folder / "video1.mp4").write_bytes(b"replaced file")
    [done] = approvals.execute_approved(world.pipeline)
    assert done.status == "stale" and world.store.get_video(world.rows[1]).archived_at is None


def test_reject_expire_and_failures(world):
    sp, _ = scoped(world)
    p1 = sp.archive([world.rows[0]])
    assert approvals.reject(world.store, p1.approval_id, "me", "not now").status == "rejected"
    p2 = sp.bulk_update([world.rows[1]], {"topic": "x"})
    world.store.conn.execute("UPDATE approval_requests SET expires_at='2000-01-01T00:00:00Z' WHERE id=?",
                             (p2.approval_id,))
    world.store.conn.commit()
    with pytest.raises(approvals.ApprovalError, match="expired"):
        approvals.approve(world.store, p2.approval_id, "me")
    p3 = sp.bulk_update([world.rows[2]], {"keywords": "k" * 5000})    # too long: fails when carried out
    approvals.approve(world.store, p3.approval_id, "me")
    [done] = approvals.execute_approved(world.pipeline)
    assert done.status == "failed" and done.result["code"]


def test_interrupted_execution_is_never_repeated(world):
    sp, _ = scoped(world)
    pending = sp.archive([world.rows[0]])
    approvals.approve(world.store, pending.approval_id, "me")
    assert world.store.mark_approval_started(pending.approval_id)    # …and then Reelay quit
    [done] = approvals.execute_approved(world.pipeline)
    assert done.status == "failed" and "stopped" in done.result["error"]
    assert world.store.get_video(world.rows[0]).archived_at is None


def test_approved_work_runs_from_the_dispatcher(world):
    from reelay.scheduler import Dispatcher
    sp, _ = scoped(world)
    pending = sp.archive([world.rows[0]])
    approvals.approve(world.store, pending.approval_id, "me")
    Dispatcher(world.store, world.pipeline, {}, interval=1).run_once()
    assert world.store.get_approval(pending.approval_id).status == "executed"
    assert world.store.get_video(world.rows[0]).archived_at is not None
    assert world.store.revisions(world.rows[0])[0].origin == "approval"


def test_snapshot_covers_what_matters(world):
    before = snapshot(world.store, world.rows[0])
    world.pipeline.set_notes(world.rows[0], "just a note")              # notes don't matter
    assert snapshot(world.store, world.rows[0]) == before
    world.pipeline.update_metadata(world.rows[0], {"tiktok_text": "new caption"})
    assert snapshot(world.store, world.rows[0]) != before


# ------------------------------------------------------------------ automations guard
def test_rules_cannot_schedule_agent_written_copy(world, monkeypatch):
    from reelay import rules as R
    rule = R.Rule(name="Tuesdays", actions=[R.Action("schedule", value="Tuesday 09:00")])
    row = world.rows[0]
    with world.store.origin("api"):
        world.pipeline.update_metadata(row, {"description": "Written by an agent"})
    done = R.apply(world.pipeline, rule, row)
    assert done[0].startswith("waiting for approval (request #") and world.store.dispatches_for(row) == []
    req = world.store.list_approvals()[0]
    assert req.operation == "schedule" and req.requested_by == "automation: Tuesdays"
    world.pipeline.update_metadata(row, {"description": "Checked by Lydia"})   # a person edits after
    assert R.apply(world.pipeline, rule, row)[0].startswith("scheduled for")


def test_rule_edits_dont_hide_an_agent_edit(world):
    from reelay import rules as R
    row = world.rows[1]
    with world.store.origin("api"):
        world.pipeline.update_metadata(row, {"tiktok_text": "agent words"})
    rule = R.Rule(name="r", actions=[R.Action("add_hashtags", value="#x"),
                                     R.Action("schedule", value="Tuesday 09:00")])
    done = R.apply(world.pipeline, rule, row)
    assert done[0] == "added #x" and done[1].startswith("waiting for approval")


# ------------------------------------------------------------------ terminal
def test_terminal_commands(world, capsys):
    sp, _ = scoped(world)
    pending = sp.archive([world.rows[0]])
    assert approvals.main(["list"], store=world.store) == 0
    assert f"#{pending.approval_id}  Archive / unarchive  [pending]" in capsys.readouterr().out
    assert approvals.main(["approve", str(pending.approval_id)], store=world.store) == 0
    assert "Approved" in capsys.readouterr().out
    assert approvals.main(["approve", str(pending.approval_id)], store=world.store) == 1
    other = sp.archive([world.rows[1]])
    assert approvals.main(["reject", str(other.approval_id), "--reason", "no"], store=world.store) == 0
    assert approvals.main(["list"], store=world.store) == 0
    assert "Nothing waiting" in capsys.readouterr().out.splitlines()[-1]


# ------------------------------------------------------------------ the REST API can't get around it
def test_no_route_reaches_a_mutating_pipeline_method(world, monkeypatch):
    """Every route that changes state, over real HTTP, with a spy in place of the pipeline."""
    import requests

    from reelay.api import ApiServer, create_app
    from reelay.media import MediaLinks
    from tests.fakes import connect_platform
    connect_platform(world.accounts, world.profile.id, "bluesky")
    spy = Spy(world.pipeline)
    server = ApiServer(create_app(world.store, spy, world.accounts, lambda: "t" * 40, MediaLinks()),
                       host="127.0.0.1", port=0)
    assert server.start()
    h = {"Authorization": "Bearer " + "t" * 40}
    a, b = world.rows[0], world.rows[1]
    from reelay.models import Source
    src = world.store.add_source(Source(profile_id=world.profile.id, name="s", platform="tiktok",
                                        url="https://www.tiktok.com/@x"))
    tables = ("posts", "dispatches", "manual_posts", "sources")

    def counts():
        return {t: world.store.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables} | {
            "archived": world.store.conn.execute("SELECT COUNT(*) FROM videos WHERE archived_at IS NOT NULL")
            .fetchone()[0]}
    before = counts()
    sheet = "Video ID,Topic\n7000,Eyewear\n"
    calls = [
        ("post", f"/v1/videos/{a}/schedule", {"scheduled_at": future().isoformat()}),
        ("post", f"/v1/videos/{a}/schedule?force=true", {}),
        ("post", f"/v1/videos/{a}/archive", None), ("post", f"/v1/videos/{a}/unarchive", None),
        ("post", "/v1/videos/bulk-update", {"video_ids": [a, b], "changes": {"topic": "x"}}),
        ("post", f"/v1/videos/{a}/platforms/tiktok", {"state": "posted"}),
        ("post", f"/v1/videos/{a}/manual-posts", {"platform": "instagram"}),
        ("post", f"/v1/import?profile={world.profile.id}&dry_run=false", {"csv": sheet}),
        ("delete", f"/v1/sources/{src.id}", None),
    ]
    try:
        for method, path, body in calls:
            r = getattr(requests, method)(server.url + path, json=body, headers=h, timeout=10)
            assert r.status_code == 202, (path, r.status_code, r.text)
        assert spy.calls == [] and counts() == before and world.uploads.jobs == []
        assert len(world.store.list_approvals(status="pending")) == len(calls)
    finally:
        server.stop()


def test_api_module_only_uses_the_boundary():
    """Static check: inside create_app, `pipeline.*` is a ScopedPipeline method and `store.*` a read."""
    import ast
    import inspect

    import reelay.api as api_module
    from reelay.scoped_pipeline import ReadOnlyStore
    tree = ast.parse(inspect.getsource(api_module))
    create = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "create_app")
    allowed_pipeline = {n for n in dir(ScopedPipeline) if not n.startswith("_")}
    bad = []
    nodes = [n for stmt in create.body for n in ast.walk(stmt)]   # the body (not the signature's type hints)
    for node in nodes:
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "pipeline" and node.attr not in allowed_pipeline:
                bad.append(f"pipeline.{node.attr}")
            if node.value.id == "store" and node.attr not in ReadOnlyStore.READS:
                bad.append(f"store.{node.attr}")
            if node.value.id == "accounts":
                bad.append(f"accounts.{node.attr}")
        if isinstance(node, ast.Name) and node.id in ("Pipeline", "SourceService", "AccountService", "Store") \
                and isinstance(getattr(node, "ctx", None), ast.Load):
            bad.append(node.id)
    # the one allowed use of the raw objects: wrapping them, and binding two read-only account helpers
    allowed = {"accounts.has_credentials", "accounts.oauth_client"}
    assert [b for b in bad if b not in allowed] == []


# ------------------------------------------------------------------ input handling (downloader, links, paths)
def test_download_outside_the_folder_is_refused(tmp_path, monkeypatch):
    import reelay.downloader as dl
    from reelay.models import VideoItem as Item
    outside = tmp_path / "elsewhere" / "evil.mp4"
    outside.parent.mkdir()
    outside.write_bytes(b"x")
    inside_file = tmp_path / "lib" / "ok.mp4"

    class FakeYDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            return {"id": "1", "title": "t"}

        def process_ie_result(self, info, download=True):
            return {"requested_downloads": [{"filepath": str(FakeYDL.target)}]}
    monkeypatch.setattr(dl.yt_dlp, "YoutubeDL", FakeYDL)
    folder = tmp_path / "lib"
    folder.mkdir()
    item = Item(platform="TikTok", video_id="1", url="https://www.tiktok.com/@x/video/1")
    FakeYDL.target = outside
    with pytest.raises(dl.DownloadError, match="outside the chosen folder"):
        dl.download_item(item, folder, "best", {}, lambda i: "name")
    inside_file.write_bytes(b"v")
    FakeYDL.target = inside_file
    assert dl.download_item(item, folder, "best", {}, lambda i: "name").filename == "ok.mp4"


def test_link_and_path_rules():
    from reelay.urlsafety import image_path_problem, inside, media_url_problem
    for ok in ("https://www.tiktok.com/@x/video/1", "https://vm.tiktok.com/Z/", "http://instagram.com/reel/a",
               "https://youtu.be/dQw4w9WgXcQ", "https://m.youtube.com/shorts/x"):
        assert media_url_problem(ok) is None, ok
    for bad in ("file:///etc/passwd", "http://127.0.0.1:8471/v1/board", "https://evil-tiktok.com/x",
                "https://tiktok.com.evil.net/x", "javascript:alert(1)", "ftp://youtube.com/x",
                "https://user:pw@www.youtube.com/x", "", "https://[::1]/"):
        assert media_url_problem(bad), bad
    assert inside("/a/b/c.mp4", "/a/b") and not inside("/a/b/../c.mp4", "/a/b") and not inside("/a/bc/x", "/a/b")
    assert image_path_problem("", "/lib") is None and image_path_problem("/lib/x.gif", "/lib")
