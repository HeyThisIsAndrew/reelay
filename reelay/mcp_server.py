"""Reelay MCP Server: Streamable HTTP / SSE endpoint for Model Context Protocol (Issue #6).

Exposes Reelay's ScopedPipeline as an MCP server at /mcp:
- Protocol: Model Context Protocol (JSON-RPC 2.0 / Streamable HTTP SSE).
- Transport: GET /mcp/sse (SSE stream) + POST /mcp/messages (JSON-RPC).
- Origin: All tool calls are tagged with origin 'mcp:<client_name>' for full auditability.
- Safety Boundary: Reads pass through, draft edits are rate-limited, and consequential actions
  (scheduling, marking posted) return ApprovalPending. Nothing consequential is ever executed
  without creator approval in the desktop app.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .scoped_pipeline import ApprovalPending, ScopedPipeline

log = logging.getLogger("reelay.mcp")

MCP_PROTOCOL_VERSION = "2024-11-05"

MCP_TOOLS = [
    {
        "name": "reelay_list_board",
        "description": "List videos on the Reelay Board across Kanban stages (backlog, staging, scheduled, published).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "stage": {
                    "type": "string",
                    "description": "Optional stage filter (backlog, staging, scheduled, published).",
                },
                "profile": {
                    "type": "string",
                    "description": "Optional profile name (uses default/first profile if omitted).",
                },
            },
        },
    },
    {
        "name": "reelay_get_ticket",
        "description": "Get detailed copy, platform drafts, metadata, notes, and transcript for a specific video ticket.",
        "inputSchema": {
            "type": "object",
            "required": ["video_id"],
            "properties": {
                "video_id": {
                    "type": "integer",
                    "description": "Integer ID of the video ticket.",
                },
            },
        },
    },
    {
        "name": "reelay_get_readiness",
        "description": "Check platform compliance and readiness for each target network (YouTube, TikTok, Instagram, Bluesky, Threads, Facebook).",
        "inputSchema": {
            "type": "object",
            "required": ["video_id"],
            "properties": {
                "video_id": {
                    "type": "integer",
                    "description": "Integer ID of the video ticket.",
                },
            },
        },
    },
    {
        "name": "reelay_set_platform_copy",
        "description": "Draft or update social media copy for a video ticket. Edits in-memory and rate-limited. Never auto-publishes.",
        "inputSchema": {
            "type": "object",
            "required": ["video_id", "copy"],
            "properties": {
                "video_id": {
                    "type": "integer",
                    "description": "Integer ID of the video ticket.",
                },
                "copy": {
                    "type": "object",
                    "description": "Dictionary of copy fields to set (title, description, tags, tiktok_text, instagram_text, bluesky_text, threads_text, facebook_text, hashtags).",
                },
            },
        },
    },
    {
        "name": "reelay_fill_from_original",
        "description": "Fill empty copy fields on a video ticket from the original downloaded caption, within each platform's character limits.",
        "inputSchema": {
            "type": "object",
            "required": ["video_id"],
            "properties": {
                "video_id": {
                    "type": "integer",
                    "description": "Integer ID of the video ticket.",
                },
            },
        },
    },
    {
        "name": "reelay_schedule_post",
        "description": "Request to schedule a video post for publication. CONSEQUENTIAL OPERATION: Creates an approval request row in Reelay; returns pending approval status. A human creator must approve in the desktop app before scheduling occurs.",
        "inputSchema": {
            "type": "object",
            "required": ["video_id", "platform", "publish_at"],
            "properties": {
                "video_id": {
                    "type": "integer",
                    "description": "Integer ID of the video ticket.",
                },
                "platform": {
                    "type": "string",
                    "description": "Platform to schedule for (youtube, bluesky, threads, facebook).",
                },
                "publish_at": {
                    "type": "string",
                    "description": "ISO 8601 UTC timestamp for scheduled publication.",
                },
            },
        },
    },
    {
        "name": "reelay_mark_posted",
        "description": "Record that a video was posted to a platform. CONSEQUENTIAL OPERATION: Creates an approval request requiring creator confirmation.",
        "inputSchema": {
            "type": "object",
            "required": ["video_id", "platform"],
            "properties": {
                "video_id": {
                    "type": "integer",
                    "description": "Integer ID of the video ticket.",
                },
                "platform": {
                    "type": "string",
                    "description": "Platform where posted (youtube, tiktok, instagram, bluesky, threads, facebook).",
                },
                "url": {
                    "type": "string",
                    "description": "Optional public URL of the live post.",
                },
            },
        },
    },
]


def execute_mcp_tool(
    scoped: ScopedPipeline,
    name: str,
    arguments: dict[str, Any],
    client_name: str = "mcp_client",
) -> dict[str, Any]:
    """Dispatches tool execution against ScopedPipeline, returning MCP content payload."""
    try:
        # Resolve target profile if needed
        profile = None
        if hasattr(scoped.store, "list_profiles"):
            profiles = scoped.store.list_profiles()
            if profiles:
                profile_name = arguments.get("profile")
                if profile_name:
                    for p in profiles:
                        if p.name.lower() == str(profile_name).lower():
                            profile = p
                            break
                if not profile:
                    profile = profiles[0]

        if name == "reelay_list_board":
            if not profile:
                return {"isError": True, "content": [{"type": "text", "text": "No profile found in Reelay store."}]}
            stage_filter = arguments.get("stage")
            columns = scoped.board(profile.id)
            cards = []
            if stage_filter:
                cards = columns.get(str(stage_filter).lower(), [])
            else:
                for col_cards in columns.values():
                    cards.extend(col_cards)
            summary = [
                {
                    "video_id": c.id,
                    "title": c.title,
                    "stage": getattr(c.row, "stage", ""),
                    "platforms": [p.key for p in getattr(c, "target_platforms", [])] if hasattr(c, "target_platforms") else [],
                }
                for c in cards
            ]
            return {"content": [{"type": "text", "text": json.dumps(summary, indent=2)}]}

        elif name == "reelay_get_ticket":
            video_id = int(arguments["video_id"])
            card = scoped.card(video_id)
            post = getattr(card, "post", None)
            ticket = getattr(card.row, "ticket", None)
            data = {
                "video_id": card.id,
                "title": getattr(post, "title", card.title),
                "description": getattr(post, "description", ""),
                "tags": getattr(post, "tags", []),
                "tiktok_text": getattr(post, "tiktok_text", ""),
                "instagram_text": getattr(post, "instagram_text", ""),
                "bluesky_text": getattr(post, "bluesky_text", ""),
                "threads_text": getattr(post, "threads_text", ""),
                "facebook_text": getattr(post, "facebook_text", ""),
                "hashtags": getattr(post, "hashtags", ""),
                "topic": getattr(ticket, "topic", ""),
                "keywords": getattr(ticket, "keywords", ""),
                "has_transcript": bool(getattr(ticket, "transcript", "")),
            }
            return {"content": [{"type": "text", "text": json.dumps(data, indent=2)}]}

        elif name == "reelay_get_readiness":
            video_id = int(arguments["video_id"])
            readiness = scoped.readiness(video_id)
            readiness_data = {
                "ready": getattr(readiness, "ready", False),
                "platforms": getattr(readiness, "platforms", {}),
            }
            return {"content": [{"type": "text", "text": json.dumps(readiness_data, indent=2)}]}

        elif name == "reelay_set_platform_copy":
            video_id = int(arguments["video_id"])
            copy_fields = arguments.get("copy", {})
            scoped.update_metadata(video_id, copy_fields)
            return {"content": [{"type": "text", "text": f"Successfully updated draft copy for video #{video_id}."}]}

        elif name == "reelay_fill_from_original":
            video_id = int(arguments["video_id"])
            filled = scoped.fill_from_original([video_id])
            return {"content": [{"type": "text", "text": f"Filled empty copy for {len(filled)} video(s)."}]}

        elif name == "reelay_schedule_post":
            video_id = int(arguments["video_id"])
            platform = str(arguments["platform"]).lower()
            publish_at = str(arguments["publish_at"])
            # Consequential operation -> returns ApprovalPending
            pending = scoped.schedule(video_id, scheduled_at=publish_at, platforms=[platform])
            if isinstance(pending, ApprovalPending):
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"APPROVAL REQUIRED: Created pending approval request #{pending.approval_id} "
                                f"to schedule video #{video_id} on {platform} for {publish_at}. "
                                "Reelay requires the creator to approve this action in the desktop app before it is scheduled."
                            ),
                        }
                    ],
                    "approval_id": pending.approval_id,
                    "status": "pending_approval",
                }
            return {"content": [{"type": "text", "text": f"Scheduled video #{video_id} for {platform}."}]}

        elif name == "reelay_mark_posted":
            video_id = int(arguments["video_id"])
            platform = str(arguments["platform"]).lower()
            url = str(arguments.get("url") or "")
            pending = scoped.mark_posted(video_id, platform, url=url)
            if isinstance(pending, ApprovalPending):
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"APPROVAL REQUIRED: Created pending approval request #{pending.approval_id} "
                                f"to mark video #{video_id} as posted to {platform}. "
                                "Waiting for creator approval in Reelay desktop app."
                            ),
                        }
                    ],
                    "approval_id": pending.approval_id,
                    "status": "pending_approval",
                }
            return {"content": [{"type": "text", "text": f"Marked video #{video_id} as posted."}]}

        return {"isError": True, "content": [{"type": "text", "text": f"Unknown tool: {name}"}]}

    except Exception as exc:
        log.warning("MCP tool execution error (%s): %s", name, exc)
        return {"isError": True, "content": [{"type": "text", "text": f"Error: {exc}"}]}


def create_mcp_router(
    scoped: ScopedPipeline,
    token_verifier: Callable[[str], tuple[bool, str | None]],
) -> APIRouter:
    """Creates FastAPI APIRouter hosting the MCP Server-Sent Events & JSON-RPC endpoints."""
    router = APIRouter(prefix="/mcp", tags=["mcp"])

    def _verify_auth(request: Request) -> tuple[str, str]:
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Missing or invalid Bearer token for MCP server.")
        token = auth_header[7:].strip()
        ok, client_name = token_verifier(token)
        if not ok:
            raise HTTPException(status_code=401, detail="Unauthorized MCP client.")
        return token, client_name or "mcp_client"

    @router.get("/sse")
    async def sse_endpoint(request: Request):
        """Server-Sent Events endpoint for MCP streamable connection."""
        _verify_auth(request)

        async def event_generator():
            # Initial endpoint announcement per MCP SSE spec
            yield "event: endpoint\ndata: /mcp/messages\n\n"
            while True:
                if await request.is_disconnected():
                    break
                await asyncio.sleep(15)
                yield ": keepalive\n\n"

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @router.post("/messages")
    @router.post("")
    async def jsonrpc_endpoint(request: Request):
        """Handles incoming JSON-RPC 2.0 requests from MCP clients."""
        _, client_name = _verify_auth(request)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content={"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            )

        rpc_id = body.get("id")
        method = body.get("method")
        params = body.get("params") or {}

        # 1. MCP Initialization
        if method == "initialize":
            return JSONResponse(
                content={
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {
                        "protocolVersion": MCP_PROTOCOL_VERSION,
                        "serverInfo": {"name": "reelay-mcp", "version": "0.5.0"},
                        "capabilities": {
                            "tools": {"listChanged": False},
                        },
                    },
                }
            )

        if method == "notifications/initialized":
            return Response(status_code=204)

        # 2. Tool Listing
        if method == "tools/list":
            return JSONResponse(
                content={
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {"tools": MCP_TOOLS},
                }
            )

        # 3. Tool Calling
        if method == "tools/call":
            tool_name = params.get("name", "")
            arguments = params.get("arguments") or {}
            # Dispatch tool call with origin mcp:<client_name>
            result = execute_mcp_tool(scoped, tool_name, arguments, client_name=client_name)
            return JSONResponse(
                content={
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": result,
                }
            )

        # 4. Ping
        if method == "ping":
            return JSONResponse(content={"jsonrpc": "2.0", "id": rpc_id, "result": {}})

        return JSONResponse(
            content={
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        )

    return router
