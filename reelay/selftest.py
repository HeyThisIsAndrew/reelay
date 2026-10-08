"""`Reelay --self-test`: verify a packaged build has everything it needs.

Runs without a window and without network access. Exit code 0 = all good.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path


def run() -> int:
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, fn) -> None:
        try:
            detail = fn() or ""
            checks.append((name, True, str(detail)))
        except Exception as exc:  # noqa: BLE001
            checks.append((name, False, f"{type(exc).__name__}: {exc}"))

    def ytdlp():
        import yt_dlp
        from yt_dlp.extractor import gen_extractor_classes
        names = {ie.IE_NAME for ie in gen_extractor_classes()}
        assert {"TikTok", "tiktok:user", "youtube"} <= names, "TikTok/YouTube extractors missing"
        return yt_dlp.version.__version__

    def impersonation():
        import yt_dlp
        with yt_dlp.YoutubeDL({"quiet": True}) as ydl:
            targets = list(ydl._get_available_impersonate_targets())
        assert targets, "no browser impersonation targets (curl_cffi missing)"
        return f"{len(targets)} targets"

    def ffmpeg():
        from .ytdl import find_ffmpeg
        path = find_ffmpeg()
        assert path and Path(path).is_file(), "ffmpeg not found"
        return path

    def keychain():
        import keyring
        backend = keyring.get_keyring()
        assert "fail" not in type(backend).__module__, f"no usable keychain backend ({backend})"
        return type(backend).__name__

    def database():
        from .store import Store
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "t.db")
            store.create_profile("check", tmp)
            store.close()
        return "ok"

    def qt():
        from PySide6.QtGui import QImageReader
        formats = {bytes(f).decode() for f in QImageReader.supportedImageFormats()}
        assert {"jpeg", "png", "webp"} <= formats, f"image plugins missing: {formats}"
        return "jpeg/png/webp"

    def agent_api():
        import urllib.request

        from .accounts import AccountService
        from .api import ApiServer, create_app
        from .pipeline import Pipeline
        from .store import Store
        from .tokens import MemoryTokenStore
        from .uploads import UploadManager
        store = Store(":memory:")
        accounts = AccountService(store, MemoryTokenStore())
        server = ApiServer(create_app(store, Pipeline(store, accounts, UploadManager()), accounts, lambda: "t"),
                           host="127.0.0.1", port=0)
        try:
            assert server.start(), server.error
            with urllib.request.urlopen(server.url + "/openapi.json", timeout=5) as resp:
                spec = resp.read().decode()
            assert '"openapi":"3.1' in spec.replace(" ", ""), "unexpected OpenAPI document"
        finally:
            server.stop()
            store.close()
        return "serves OpenAPI 3.1"

    def help_page():
        from .gui.help_page import HELP_FILE
        text = HELP_FILE.read_text(encoding="utf-8")
        assert text.startswith("# Reelay Help"), "help.md is missing or damaged"
        return f"{len(text.splitlines())} lines, offline"

    for name, fn in (("yt-dlp", ytdlp), ("TikTok impersonation", impersonation), ("ffmpeg", ffmpeg),
                     ("Keychain", keychain), ("Database", database), ("Qt image plugins", qt),
                     ("Agent API server", agent_api), ("Help page", help_page)):
        check(name, fn)

    ok = all(passed for _, passed, _ in checks)
    lines = [f"{'PASS' if passed else 'FAIL'}  {name:<22} {detail}" for name, passed, detail in checks]
    lines.append(f"Self-test {'passed' if ok else 'FAILED'}")
    report = "\n".join(lines)
    if sys.stdout is not None:  # windowed Windows builds have no console
        print(report, flush=True)
    try:
        from .paths import log_dir
        (log_dir() / "self-test.txt").write_text(report + "\n", encoding="utf-8")
    except OSError:
        pass
    return 0 if ok else 1
