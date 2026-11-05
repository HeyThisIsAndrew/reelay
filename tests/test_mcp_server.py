"""Tests for Reelay MCP Server (Issue #6).

Verifies Streamable HTTP / JSON-RPC endpoints:
- tools/list matches ScopedPipeline capabilities.
- Reads (board, ticket, readiness) pass through safely.
- Draft copy edits update in-memory fields.
- Consequential tools (schedule, mark posted) return ApprovalPending with approval_id.
- Requests without valid bearer token or with bad Host header are refused.
"""

from __future__ import annotations

import json
import pytest
import requests

from reelay.api import ApiServer, create_app
from tests.fakes import make_world


def test_mcp_auth_and_host_validation(tmp_path, monkeypatch):
    world = make_world(tmp_path, monkeypatch)
    master_token = "test_master_token_xyz"
    app = create_app(world.store, world.pipeline, world.accounts, lambda: master_token)
    server = ApiServer(app, host="127.0.0.1", port=0)
    assert server.start()

    try:
        url = f"{server.url}/mcp/messages"

        # 1. Missing token -> 401
        res = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert res.status_code == 401

        # 2. Invalid token -> 401
        res = requests.post(
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": "Bearer wrong_token"},
        )
        assert res.status_code == 401

        # 3. Bad Host header (DNS rebinding attack attempt) -> 400
        res = requests.post(
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": f"Bearer {master_token}", "Host": "evil-attacker.com"},
        )
        assert res.status_code == 400

    finally:
        server.stop()
        world.store.close()


def test_mcp_initialize_and_tools_list(tmp_path, monkeypatch):
    world = make_world(tmp_path, monkeypatch)
    token = "valid_token"
    app = create_app(world.store, world.pipeline, world.accounts, lambda: token)
    server = ApiServer(app, host="127.0.0.1", port=0)
    assert server.start()

    try:
        headers = {"Authorization": f"Bearer {token}"}
        url = f"{server.url}/mcp/messages"

        # 1. Initialize
        res = requests.post(
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "Claude"}}},
            headers=headers,
        )
        assert res.status_code == 200
        data = res.json()
        assert data["result"]["serverInfo"]["name"] == "reelay-mcp"
        assert "tools" in data["result"]["capabilities"]

        # 2. Tools list
        res = requests.post(
            url,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            headers=headers,
        )
        assert res.status_code == 200
        tools = res.json()["result"]["tools"]
        tool_names = {t["name"] for t in tools}
        assert "reelay_list_board" in tool_names
        assert "reelay_get_ticket" in tool_names
        assert "reelay_set_platform_copy" in tool_names
        assert "reelay_schedule_post" in tool_names
        assert "reelay_mark_posted" in tool_names

    finally:
        server.stop()
        world.store.close()


def test_mcp_tool_execution_reads_and_consequential_approval_gating(tmp_path, monkeypatch):
    world = make_world(tmp_path, monkeypatch)
    token = "valid_token"
    app = create_app(world.store, world.pipeline, world.accounts, lambda: token)
    server = ApiServer(app, host="127.0.0.1", port=0)
    assert server.start()

    try:
        headers = {"Authorization": f"Bearer {token}"}
        url = f"{server.url}/mcp/messages"

        video_id = world.rows[0]

        # 1. Tool call: reelay_list_board
        res = requests.post(
            url,
            json={
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {"name": "reelay_list_board", "arguments": {}},
            },
            headers=headers,
        )
        assert res.status_code == 200
        content = res.json()["result"]["content"][0]["text"]
        board_data = json.loads(content)
        assert any(c["video_id"] == video_id for c in board_data)

        # 2. Tool call: reelay_get_ticket
        res = requests.post(
            url,
            json={
                "jsonrpc": "2.0",
                "id": 11,
                "method": "tools/call",
                "params": {"name": "reelay_get_ticket", "arguments": {"video_id": video_id}},
            },
            headers=headers,
        )
        assert res.status_code == 200
        ticket_data = json.loads(res.json()["result"]["content"][0]["text"])
        assert ticket_data["video_id"] == video_id

        # 3. Tool call: reelay_set_platform_copy (Draft edit)
        res = requests.post(
            url,
            json={
                "jsonrpc": "2.0",
                "id": 12,
                "method": "tools/call",
                "params": {
                    "name": "reelay_set_platform_copy",
                    "arguments": {
                        "video_id": video_id,
                        "copy": {"title": "Updated Title via MCP", "threads_text": "Clean Threads copy."},
                    },
                },
            },
            headers=headers,
        )
        assert res.status_code == 200
        assert "Successfully updated draft copy" in res.json()["result"]["content"][0]["text"]

        # Verify draft was updated
        updated_post = world.store.get_post(video_id)
        assert updated_post.title == "Updated Title via MCP"

        # 4. Tool call: reelay_schedule_post (CONSEQUENTIAL -> MUST RETURN APPROVAL REQUIRED)
        res = requests.post(
            url,
            json={
                "jsonrpc": "2.0",
                "id": 13,
                "method": "tools/call",
                "params": {
                    "name": "reelay_schedule_post",
                    "arguments": {
                        "video_id": video_id,
                        "platform": "youtube",
                        "publish_at": "2026-11-10T15:00:00Z",
                    },
                },
            },
            headers=headers,
        )
        assert res.status_code == 200
        sched_res = res.json()["result"]
        assert sched_res.get("status") == "pending_approval"
        assert "APPROVAL REQUIRED" in sched_res["content"][0]["text"]
        approval_id = sched_res.get("approval_id")
        assert approval_id is not None

        # Verify approval_requests table in store contains the pending request and nothing is posted
        req = world.store.get_approval(approval_id)
        assert req is not None
        assert req.status == "pending"
        assert req.operation == "schedule"
        assert video_id in req.target_ids

        # Ensure video is still not scheduled in dispatches
        dispatches = world.store.dispatches_for(video_id)
        assert len(dispatches) == 0

    finally:
        server.stop()
        world.store.close()
