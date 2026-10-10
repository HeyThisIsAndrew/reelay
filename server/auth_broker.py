"""Reelay sign-in server ("broker"): the one place that holds Reelay's app
secrets for TikTok and Meta (Instagram, Threads, Facebook).

Why it exists: those platforms require the app secret to turn a sign-in code
into an access token, and a secret shipped inside a desktop app can be
extracted. The broker does that one step and hands the tokens back; it stores
nothing longer than the few minutes a sign-in takes.

Endpoints (all JSON):
  POST /v1/oauth/{provider}/token     {code, code_verifier, redirect_uri}  -> tokens   (TikTok: loopback flow)
  GET  /v1/oauth/{provider}/callback  ?code&state  (Meta redirects here; result held for 10 minutes)
  GET  /v1/oauth/session/{state}      -> tokens once, then forgotten (202 while waiting)
  POST /v1/oauth/{provider}/refresh   {refresh_token | access_token} -> tokens

Run:  REELAY_BROKER_PUBLIC_URL=https://auth.example.com \\
      TIKTOK_CLIENT_KEY=… TIKTOK_CLIENT_SECRET=… \\
      INSTAGRAM_APP_ID=… INSTAGRAM_APP_SECRET=… THREADS_APP_ID=… THREADS_APP_SECRET=… \\
      FACEBOOK_APP_ID=… FACEBOOK_APP_SECRET=… \\
      uvicorn server.auth_broker:app --host 0.0.0.0 --port 8080

Endpoints and parameters follow each platform's docs as of October 2026;
check them again before going live. Secrets and tokens are never logged.
"""

from __future__ import annotations

import html
import os
import threading
import time

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

SESSION_TTL = 600
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
    sessions: dict[str, tuple[float, dict]] = {}
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
        try:
            if error or not code:
                result = {"error": error_description or error or "No authorization code was returned."}
            else:
                redirect = f"{config['public_url']}/v1/oauth/{provider}/callback"
                result = exchange(provider, code, redirect)
        except HTTPException as exc:
            result = {"error": exc.detail}
        with lock:
            sessions[state] = (clock() + SESSION_TTL, result)
        ok = "error" not in result
        return HTMLResponse(_PAGE.format(title="You're connected" if ok else "Sign-in didn't finish",
                                         body="You can close this tab and go back to Reelay." if ok
                                         else html.escape(str(result["error"]))))

    @app.get("/v1/oauth/session/{state}")
    def session_result(state: str):
        now = clock()
        with lock:
            for key in [k for k, (expires, _r) in sessions.items() if expires < now]:
                sessions.pop(key, None)
            entry = sessions.pop(state, None)     # handed out once, then forgotten
        if entry is None:
            return JSONResponse({"status": "waiting"}, status_code=202)
        return JSONResponse(entry[1])

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
