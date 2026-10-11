"""Reelay sign-in server ("broker"): the one place that holds Reelay's app
secrets for TikTok and Meta (Instagram, Threads, Facebook).

Why it exists: those platforms require the app secret to turn a sign-in code
into an access token, and a secret shipped inside a desktop app can be
extracted. The broker does that one step and hands the tokens back; it stores
nothing longer than the few minutes a sign-in takes.

Endpoints (all JSON):
  POST /v1/oauth/{provider}/token     {code, code_verifier, redirect_uri}  -> tokens   (TikTok: loopback flow)
  GET  /v1/oauth/{provider}/callback  ?code&state  (Meta redirects here). The state must start with the app's
                                      loopback port ("<port>.<random>"): the code is exchanged and the browser is
                                      sent straight back to http://127.0.0.1:<port>/callback?state=…&key=<one-time
                                      key>. Any other state is refused BEFORE the code is exchanged, so no tokens
                                      are ever made that only the state could claim. Held for 10 minutes.
  POST /v1/oauth/session/claim        {state, key} -> tokens once (both must match), then forgotten

  (There is no "poll by state" route: the state travels through the browser and the platform, so it must
  never be enough on its own to collect tokens.)
  POST /v1/oauth/{provider}/refresh   {refresh_token | access_token} -> tokens
  POST /v1/oauth/tiktok/revoke        {access_token} -> revoked (needs the client secret, so it's done here)

Run:  REELAY_BROKER_PUBLIC_URL=https://auth.example.com \\
      TIKTOK_CLIENT_KEY=… TIKTOK_CLIENT_SECRET=… \\
      INSTAGRAM_APP_ID=… INSTAGRAM_APP_SECRET=… THREADS_APP_ID=… THREADS_APP_SECRET=… \\
      FACEBOOK_APP_ID=… FACEBOOK_APP_SECRET=… \\
      uvicorn server.auth_broker:app --host 0.0.0.0 --port 8080

Endpoints and parameters follow each platform's docs as of October 2026;
check them again before going live. Secrets and tokens are never logged.
"""

from __future__ import annotations

import os
import re
import secrets
import threading
import time
from urllib.parse import urlencode

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

SESSION_TTL = 600
LOOPBACK_STATE = re.compile(r"^(\d{4,5})\.[A-Za-z0-9_-]{20,}$")    # "<port>.<random>" from the desktop app
META_GRAPH = "v23.0"

_PAGE = """<!doctype html><meta charset="utf-8"><title>Reelay</title>
<body style="background:#1f1e1d;color:#ecebe7;font-family:-apple-system,Segoe UI,sans-serif;display:flex;
align-items:center;justify-content:center;height:90vh;text-align:center"><div><h2>{title}</h2>
<p style="color:#a9a8a2">{body}</p></div></body>"""


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def default_config() -> dict:
    return {
        "public_url": _env("REELAY_BROKER_PUBLIC_URL").rstrip("/"),
        "tiktok": {"id": _env("TIKTOK_CLIENT_KEY"), "secret": _env("TIKTOK_CLIENT_SECRET")},
        "instagram": {"id": _env("INSTAGRAM_APP_ID"), "secret": _env("INSTAGRAM_APP_SECRET")},
        "threads": {"id": _env("THREADS_APP_ID"), "secret": _env("THREADS_APP_SECRET")},
        "facebook": {"id": _env("FACEBOOK_APP_ID"), "secret": _env("FACEBOOK_APP_SECRET")},
    }


def create_broker(config: dict | None = None, session=None, clock=time.time) -> FastAPI:
    config = config or default_config()
    http = session or requests
    claims: dict[str, tuple[float, str, dict]] = {}       # bounce flow: state → (expires, one-time key, result)
    lock = threading.Lock()
    app = FastAPI(title="Reelay sign-in server", docs_url=None, redoc_url=None, openapi_url=None)

    def creds(provider: str) -> dict:
        c = config.get(provider) or {}
        if not c.get("id") or not c.get("secret"):
            raise HTTPException(404, f"{provider} sign-in isn't set up on this server.")
        return c

    def parse(resp) -> dict:
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code != 200 or not isinstance(data, dict):
            msg = data.get("error_description") or data.get("error") if isinstance(data, dict) else None
            if isinstance(msg, dict):
                msg = msg.get("message")
            raise HTTPException(502, f"The platform refused the sign-in: {msg or resp.status_code}")
        return data

    # ---------------------------------------------------------------- exchanges
    def exchange(provider: str, code: str, redirect_uri: str, verifier: str = "") -> dict:
        c = creds(provider)
        if provider == "tiktok":
            return parse(http.post("https://open.tiktokapis.com/v2/oauth/token/", data={
                "client_key": c["id"], "client_secret": c["secret"], "code": code, "grant_type": "authorization_code",
                "redirect_uri": redirect_uri, "code_verifier": verifier}, timeout=30))
        if provider == "instagram":
            short = parse(http.post("https://api.instagram.com/oauth/access_token", data={
                "client_id": c["id"], "client_secret": c["secret"], "code": code.removesuffix("#_"),
                "grant_type": "authorization_code", "redirect_uri": redirect_uri}, timeout=30))
            long = parse(http.get("https://graph.instagram.com/access_token", params={
                "grant_type": "ig_exchange_token", "client_secret": c["secret"],
                "access_token": short["access_token"]}, timeout=30))
            return {**long, "user_id": short.get("user_id")}
        if provider == "threads":
            short = parse(http.post("https://graph.threads.com/oauth/access_token", data={
                "client_id": c["id"], "client_secret": c["secret"], "code": code,
                "grant_type": "authorization_code", "redirect_uri": redirect_uri}, timeout=30))
            long = parse(http.get("https://graph.threads.com/access_token", params={
                "grant_type": "th_exchange_token", "client_secret": c["secret"],
                "access_token": short["access_token"]}, timeout=30))
            return {**long, "user_id": short.get("user_id")}
        if provider == "facebook":
            short = parse(http.get(f"https://graph.facebook.com/{META_GRAPH}/oauth/access_token", params={
                "client_id": c["id"], "client_secret": c["secret"], "code": code,
                "redirect_uri": redirect_uri}, timeout=30))
            return parse(http.get(f"https://graph.facebook.com/{META_GRAPH}/oauth/access_token", params={
                "grant_type": "fb_exchange_token", "client_id": c["id"], "client_secret": c["secret"],
                "fb_exchange_token": short["access_token"]}, timeout=30))
        raise HTTPException(404, f"Unknown provider {provider}.")

    @app.post("/v1/oauth/{provider}/token")
    async def token(provider: str, request: Request):
        body = await request.json()
        if not body.get("code") or not body.get("redirect_uri"):
            raise HTTPException(422, "code and redirect_uri are required.")
        return JSONResponse(exchange(provider, body["code"], body["redirect_uri"], body.get("code_verifier", "")))

    @app.get("/v1/oauth/{provider}/callback", response_class=HTMLResponse)
    def callback(provider: str, state: str = "", code: str = "", error: str = "", error_description: str = ""):
        if not state:
            raise HTTPException(400, "Missing state.")
        bounce = LOOPBACK_STATE.match(state)
        if not bounce or not 1024 <= int(bounce.group(1)) <= 65535:
            # Not a sign-in started by a current Reelay: don't turn the code into tokens at all.
            return HTMLResponse(_PAGE.format(title="Sign-in didn't finish",
                                             body="This sign-in link isn't from a current version of Reelay. "
                                                  "Update Reelay and start the sign-in again from Accounts."),
                                status_code=400)
        try:
            if error or not code:
                result = {"error": error_description or error or "No authorization code was returned."}
            else:
                redirect = f"{config['public_url']}/v1/oauth/{provider}/callback"
                result = exchange(provider, code, redirect)
        except HTTPException as exc:
            result = {"error": exc.detail}
        # Back to the app on this person's computer, with a one-time key to claim the result.
        key = secrets.token_urlsafe(32)
        with lock:
            if "error" not in result:
                claims[state] = (clock() + SESSION_TTL, key, result)
        query = {"state": state, "key": key} if "error" not in result else {"state": state, "error": "denied",
                                                                             "error_description": result["error"]}
        return RedirectResponse(f"http://127.0.0.1:{bounce.group(1)}/callback?{urlencode(query)}", status_code=302)

    @app.post("/v1/oauth/session/claim")
    async def claim(request: Request):
        body = await request.json()
        state, key = str(body.get("state", "")), str(body.get("key", ""))
        now = clock()
        with lock:
            for k in [k for k, (expires, _key, _r) in claims.items() if expires < now]:
                claims.pop(k, None)
            entry = claims.get(state)
            if entry is None or not key or not secrets.compare_digest(entry[1], key):
                raise HTTPException(404, "This sign-in has expired. Start it again in Reelay.")
            claims.pop(state, None)                       # handed out once, then forgotten
        return JSONResponse(entry[2])

    @app.post("/v1/oauth/tiktok/revoke")
    async def tiktok_revoke(request: Request):
        body = await request.json()
        c = creds("tiktok")
        resp = http.post("https://open.tiktokapis.com/v2/oauth/revoke/", data={
            "client_key": c["id"], "client_secret": c["secret"], "token": body.get("access_token", "")}, timeout=30)
        if resp.status_code != 200:
            raise HTTPException(502, "TikTok didn't confirm the revoke.")
        return JSONResponse({"revoked": True})

    @app.post("/v1/oauth/{provider}/refresh")
    async def refresh(provider: str, request: Request):
        body = await request.json()
        c = creds(provider)
        if provider == "tiktok":
            return JSONResponse(parse(http.post("https://open.tiktokapis.com/v2/oauth/token/", data={
                "client_key": c["id"], "client_secret": c["secret"], "grant_type": "refresh_token",
                "refresh_token": body.get("refresh_token", "")}, timeout=30)))
        if provider == "instagram":
            return JSONResponse(parse(http.get("https://graph.instagram.com/refresh_access_token", params={
                "grant_type": "ig_refresh_token", "access_token": body.get("access_token", "")}, timeout=30)))
        if provider == "threads":
            return JSONResponse(parse(http.get("https://graph.threads.com/refresh_access_token", params={
                "grant_type": "th_refresh_token", "access_token": body.get("access_token", "")}, timeout=30)))
        raise HTTPException(400, f"{provider} tokens are long-lived; sign in again when they expire.")

    return app


app = create_broker()
