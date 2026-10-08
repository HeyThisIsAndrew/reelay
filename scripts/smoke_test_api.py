#!/usr/bin/env python3
"""End-to-end smoke test of the Reelay agent API, fully isolated.

Seeds a throwaway data folder (REELAY_HOME) with videos from a library folder
(default: lydia_archive/), starts `python -m reelay.api` on a free port with a
throwaway token, checks every endpoint and the publishing flags over real
HTTP, then stops the server with SIGINT and confirms the port is released.

It never touches your real database, keychain, or any platform.

    .venv/bin/python scripts/smoke_test_api.py [--library lydia_archive]
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "smoke-test-token"
results: list[tuple[bool, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'' if ok else '  -> ' + detail}", flush=True)


def call(base: str, method: str, path: str, body=None, token: str | None = TOKEN):
    req = urllib.request.Request(base + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


def seed(home: Path, library: Path) -> None:
    code = f"""
import sys; sys.path.insert(0, {str(ROOT)!r})
from pathlib import Path
from reelay.library import Library
from reelay.paths import db_path
from reelay.store import Store
store = Store(db_path())
profile = store.create_profile("Lydia Livingood", Path({str(library)!r}))
items = Library(Path({str(library)!r})).load()
for i in items: i.folder = {str(library)!r}
store.save_videos(profile.id, items)
print(len(items))
"""
    env = {**os.environ, "REELAY_HOME": str(home)}
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
    count = int(out.stdout.strip() or 0)
    check(count > 0, f"seeded {count} video(s) from {library}", "no videos with metadata found in that folder")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--library", default=str(ROOT / "lydia_archive"))
    args = parser.parse_args()
    library = Path(args.library).resolve()

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "reelay_home"
        seed(home, library)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        env = {**os.environ, "REELAY_HOME": str(home), "REELAY_PORT": str(port), "REELAY_API_TOKEN": TOKEN,
               "QT_QPA_PLATFORM": "offscreen"}
        for flag in ("REELAY_PUBLISH_BLUESKY", "REELAY_PUBLISH_THREADS", "REELAY_PUBLISH_FACEBOOK",
                     "REELAY_PUBLIC_URL", "REELAY_HOST"):
            env.pop(flag, None)
        server = subprocess.Popen([sys.executable, "-m", "reelay.api"], cwd=ROOT, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        base = f"http://127.0.0.1:{port}"
        try:
            for _ in range(80):
                try:
                    urllib.request.urlopen(base + "/openapi.json", timeout=1)
                    break
                except OSError:
                    time.sleep(0.25)
            status, spec = call(base, "GET", "/openapi.json", token=None)
            check(status == 200 and spec.get("openapi", "").startswith("3.1"), "openapi.json is public, OpenAPI 3.1")

            status, body = call(base, "GET", "/v1/board?profile=Lydia%20Livingood", token=None)
            check(status == 401 and body.get("code") == "unauthorized", "no token -> 401", f"{status} {body}")
            status, body = call(base, "GET", "/v1/board?profile=Lydia%20Livingood", token="wrong")
            check(status == 401, "wrong token -> 401", f"{status}")
            status, body = call(base, "GET", "/v1/board")
            check(status == 400, "missing profile -> 400", f"{status} {body}")

            status, board = call(base, "GET", "/v1/board?profile=lydia%20livingood")
            check(status == 200 and board["total"] >= 1, "board lists the seeded videos", f"{status} {board}")
            vid = board["videos"][0]["id"]
            v = board["videos"][0]
            check(set(v["overrides"]) == {"tiktok_text", "instagram_text", "bluesky_text", "threads_text",
                                          "facebook_text"} and "hashtags" in v, "board has every copy block")

            copy = {"title": "Smoke test title", "tiktok_text": "tiktok copy 🦋", "instagram_text": "ig copy",
                    "bluesky_text": "bluesky copy", "threads_text": "threads copy", "facebook_text": "fb copy",
                    "hashtags": "#smoke #test", "target_platforms": ["youtube", "bluesky", "threads"]}
            status, body = call(base, "PATCH", f"/v1/videos/{vid}", copy)
            check(status == 200 and body["overrides"]["tiktok_text"] == "tiktok copy 🦋"
                  and body["hashtags"] == "#smoke #test", "PATCH saves copy blocks", f"{status} {body}")
            for name, bad in (("Bluesky 301 chars", {"bluesky_text": "x" * 301}),
                              ("Threads emoji count", {"threads_text": "🌊" * 126}),
                              ("Instagram 31 hashtags", {"instagram_text": " ".join(f"#t{n}" for n in range(31))}),
                              ("TikTok 4001 chars", {"tiktok_text": "x" * 4001}),
                              ("unknown field", {"stage": "published"})):
                status, body = call(base, "PATCH", f"/v1/videos/{vid}", {"title": "SHOULD NOT SAVE", **bad})
                check(status == 422, f"PATCH rejects {name} -> 422", f"{status} {body}")
            status, board = call(base, "GET", "/v1/board?profile=Lydia%20Livingood")
            title = next(x["title"] for x in board["videos"] if x["id"] == vid)
            check(title == "Smoke test title", "rejected PATCHes wrote nothing (atomic)", title)
            status, _ = call(base, "PATCH", "/v1/videos/999999", {"title": "x"})
            check(status == 404, "PATCH unknown video -> 404", str(status))

            status, body = call(base, "POST", f"/v1/videos/{vid}/schedule", {"platforms": ["bluesky"]})
            check(status == 422 and body.get("code") == "platform_disabled",
                  "schedule to disabled Bluesky -> 422 platform_disabled", f"{status} {body}")
            status, body = call(base, "POST", f"/v1/videos/{vid}/schedule",
                                {"scheduled_at": "2020-01-01T00:00:00Z"})
            check(status == 422 and body.get("code") == "invalid_time", "past scheduled_at -> 422", f"{status} {body}")
            status, body = call(base, "POST", f"/v1/videos/{vid}/schedule", {})
            check(status == 422 and any("YouTube channel" in p for p in body.get("problems", [])),
                  "schedule without a YouTube channel -> 422 listing the problem", f"{status} {body}")

            status, body = call(base, "GET", "/v1/platforms?profile=Lydia%20Livingood")
            info = {p["platform"]: p for p in body.get("platforms", [])}
            check(status == 200 and info["youtube"]["auto_post"] and not info["bluesky"]["auto_post"]
                  and not info["threads"]["auto_post"] and not info["facebook"]["auto_post"],
                  "platforms: only YouTube auto-posts by default", f"{status} {body}")
            check(TOKEN not in json.dumps(body), "no secrets in /v1/platforms")
        finally:
            server.send_signal(signal.SIGINT)
            try:
                output, _ = server.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                server.kill()
                output, _ = server.communicate()
            check(server.returncode == 0, "server stops cleanly on SIGINT", f"exit {server.returncode}")
            with socket.socket() as s:
                freed = s.connect_ex(("127.0.0.1", port)) != 0
            check(freed, f"port {port} released")
            logs = (home / "logs" / "app.log")
            check(not logs.exists() or TOKEN not in logs.read_text(), "token never written to the log")

    failed = [name for ok, name in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed" + (f"; FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
