# Proposal: connecting any AI assistant to Reelay (HTTP + pairing)

Status: draft for review, 2026-10-10. Builds on `docs/security.md` (ScopedPipeline + the approval queue).
Nothing here is implemented yet.

## Goal

One "Connect AI Assistant" flow that works for cloud assistants (Meta Muse, ChatGPT, Google Gemini) and local
ones (Claude Desktop, Antigravity). Every assistant reaches the **same** two front doors:

- **MCP** over Streamable HTTP at `/mcp`, for MCP clients;
- **REST/OpenAPI** at `/v1`, for Custom GPT Actions, Gemini function calling and other OpenAPI clients.

Both require a per-assistant bearer token, and both call `ScopedPipeline`, so the same rate limits and the same
durable `approval_requests` queue apply. No provider can carry out a mutating action without a person's
approval.

```
Cloud AI ──https──▶ Cloudflare edge ──outbound tunnel──▶ cloudflared (on the Mac) ─┐
Local AI ──http://127.0.0.1:8471───────────────────────────────────────────────────┤
                                                                                   ▼
                         Reelay HTTP server (one uvicorn, one FastAPI app)
                           /mcp  (MCP Streamable HTTP)   /v1 (REST)   /oauth, /.well-known (pairing)
                                       │                      │
                                       └──── bearer token → AIClient ────┘
                                                      ▼
                                   ScopedPipeline(profile_ids, scopes, requested_by=client)
                                                      ▼
                         reads │ draft edits (rate limited) │ approval_requests (a person decides)
```

## 1. Unified HTTP transport

- Add the official MCP Python SDK (`mcp`, pinned) and mount its Streamable HTTP app at `/mcp` inside the existing FastAPI app. Same process, port and lifecycle as `/v1` (`ApiServer`), so there's no second server.
- **MCP tools are thin adapters over `ScopedPipeline`,** never over `Pipeline`. One shared function builds each client's `ScopedPipeline`, and both `/v1` and `/mcp` use it. The static boundary test is extended to the MCP module.

| Tool | Class | Notes |
|---|---|---|
| `list_board`, `get_video`, `search_videos`, `get_readiness`, `list_sources`, `get_approval`, `list_approvals` | read | `readOnlyHint: true` |
| `update_ticket`, `fill_from_original`, `fetch_transcript`, `restore_revision` | draft | rate limited |
| `request_schedule`, `request_archive`, `request_bulk_update`, `request_import`, `request_mark_posted`, `request_resolve` | approval | Named `request_*` so models don't think the action happened. They return `{approval_id, status: "pending", expires_at}`. `destructiveHint: true` |

- Agents follow up with `get_approval`; there's no approve tool. Optionally, MCP progress notifications when a request is decided.
- **OpenAPI stays the contract for non-MCP clients:** `api/openapi.yaml`, served at `/openapi.json` with a `servers:` entry set to the tunnel URL, so a Custom GPT can import it directly.

## 2. "Connect AI Assistant" (generic, in the app)

One dialog on the Settings page, with three ways in. Which one to use depends on what the assistant supports, not on which brand it is:

**A. Assistants that support MCP with OAuth** (Claude custom connectors, ChatGPT connectors, and very likely Muse).

1. Reelay makes sure the tunnel is up and shows **Server URL** `https://<tunnel>/mcp` and a **pairing code** (for example `RLY-7KQ4-MZ2P`).
2. The person pastes the URL into the assistant's "add connector" screen.
3. The assistant finds Reelay's OAuth metadata (`/.well-known/oauth-protected-resource` and `/.well-known/oauth-authorization-server`), registers itself (dynamic client registration), and opens Reelay's authorize page in the browser, via the tunnel.
4. That page asks for the pairing code and shows what the assistant will be allowed to do. Entering the code is the handshake. Reelay then issues an authorization code, which the assistant exchanges with PKCE for an access token (1 h) plus a refresh token.
5. The Reelay window updates to "Connected: ChatGPT".

The pairing code proves the person is at this Mac right now; the OAuth flow keeps the long-lived token out of chat transcripts. This is the MCP authorization spec (OAuth 2.1 + PKCE), so compliant clients need nothing custom.

**B. Assistants that only take an API key** (Custom GPT Actions, many Gemini set-ups). These can't do a handshake themselves.

1. Reelay shows the OpenAPI URL and, after the person clicks **Create Key**, a token shown once (`rly_…`).
2. The person pastes it into the assistant's authentication settings (stored by the provider, not in the chat).

The pairing code isn't needed here: creating the key in the app is the proof of presence. The "assistant sends the pairing code to `/v1/pair`" variant is supported for clients that can make one unauthenticated call. It's **not** recommended for chat-based set-up, because both the code and the token would end up in the conversation history.

**C. Local assistants** (Claude Desktop, Antigravity, other MCP clients on this Mac).

1. URL `http://127.0.0.1:8471/mcp`, no tunnel.
2. **Copy Config** produces the client's JSON snippet with a key created for that client.
3. Where a client only speaks stdio, a tiny `reelay-mcp` shim forwards stdio to the local HTTP endpoint (same token, same boundary).

### Pairing codes

- **Format:** 8 random base32 characters (~40 bits), shown as `RLY-XXXX-XXXX`.
- **One-time use, short-lived:** expires after 10 minutes and can't be reused.
- **Brute-force protection:** limited to 5 attempts, after which the code is void; the endpoint is also rate-limited per IP at the edge.
- **Storage:** only a hash is stored.
- **One code in the dialog at a time;** cancelling voids it.

### Tokens and clients (new table, schema v10)

`ai_clients`: `id, name` (e.g. "ChatGPT", editable), `provider_hint, kind (oauth|key|local), token_hash` (SHA-256, never the token), `refresh_hash, scopes, profile_ids, created_at, last_used_at, last_ip, revoked_at`.

- **Scopes:** `read`, `draft`, `request` (create approval requests). A read-only assistant is one tick box away.
- **Profiles:** each client is limited to chosen profiles (`ScopedPipeline(profile_ids=…)`).
- **Attribution:** every request is attributed. `requested_by = "ChatGPT (client #3)"` shows in the approval list and the revision log (`origin = "api:3"` / `"mcp:3"`; the Automations guard already treats `api*` and `mcp*` as agent edits).
- **Revoking:** **Disconnect** in the dialog revokes a client at once. The existing single agent token keeps working as the "Local (legacy)" client until replaced.

## 3. Unified security

- One authentication dependency for `/v1` and `/mcp`. It resolves the bearer token to an `AIClient`, checks it isn't revoked or expired, and builds that client's `ScopedPipeline`. Without a valid token, only `/.well-known/*`, `/oauth/*` and pairing are reachable.
- **No "local means trusted" shortcut.** Through the tunnel every request arrives from `127.0.0.1` (cloudflared forwards locally), so the source address must never grant anything. It doesn't today, and a test will keep it that way.
- **Only these paths go through the tunnel:** `/mcp`, `/v1`, `/oauth`, `/.well-known`, `/openapi.json`, `/media/{token}` (Threads). Everything else returns 404 on tunnel traffic (detected by the `Cf-Connecting-IP` / `Cf-Ray` headers).
- **Approvals become a first-class screen in the app:** an Approvals page with badges, notifications, and approve or reject showing a diff of what changes. The terminal command stays for testing. A consumer product can't rely on a terminal.
- **Kill switch:** "Pause all AI access" in the dialog and the menu bar, which refuses every token at once.
- **Audit log:** per client, the last N requests (method, tool or route, status). Never bodies or secrets.

## The tunnel: the decision that matters most

Today Reelay doesn't run a tunnel. It stores a public URL (`REELAY_PUBLIC_URL`) for one the user set up for Threads. To "make sure the tunnel is active", Reelay has to manage `cloudflared` itself (bundle or download it, start and stop it with the app, read its URL). There are three options:

| Option | Set-up for the user | URL | Cost and ops | Fit |
|---|---|---|---|---|
| **Quick tunnel** (`cloudflared tunnel --url …`, `*.trycloudflare.com`) | none | **changes every time Reelay starts**, which breaks every connected cloud assistant | free, no account | Demos only |
| **The user's own named tunnel** (their Cloudflare account and domain) | sign-in to Cloudflare, pick a hostname | stable | free tier | Power users (like "use my own developer keys") |
| **Reelay-operated tunnels** (Reelay's Cloudflare account; per-user hostname such as `lydia-7k2.connect.reelay.app`, tunnel credentials issued at sign-up) | none | stable | Reelay runs a small provisioning service and pays per usage; needs abuse handling | **The consumer default** |

**Recommendation:** Reelay-operated as the default, own named tunnel as "Advanced", and quick tunnel only as a fallback that warns that the address will change. The provisioning service can live next to the OAuth broker (`server/`).

## Phasing

1. **Server side:**
   - the `ai_clients` table and per-client tokens with scopes;
   - one auth dependency shared by `/v1` and `/mcp`, and a `ScopedPipeline` per client;
   - the MCP tools;
   - tests (boundary, scopes, revocation, tunnel-path filtering).
2. **App:**
   - the Connect AI Assistant dialog (paths B and C, local and API key);
   - the Approvals page with notifications;
   - the kill switch.
3. **OAuth:** the authorization server for path A (metadata, dynamic registration, authorize page with the pairing code, token and refresh, PKCE).
4. **Tunnel:** cloudflared management (quick tunnel plus own named tunnel), path filtering and health checks.
5. **Reelay-operated tunnels** (provisioning service). Separate project; needs a domain and a Cloudflare account.

## Open questions

- **Muse:** confirm Meta Muse's connector protocol (MCP over HTTP with OAuth is assumed, as with ChatGPT and Claude) and whether it needs dynamic client registration.
- **Gemini:** which surface? The Gemini CLI and Antigravity speak MCP locally; Gemini in the consumer app may need OpenAPI or extensions.
- **Default scopes for a new assistant:** `read + draft + request`, or read-only until the person widens it?
- **Tunnel option:** which one ships first (see the decision above)?
