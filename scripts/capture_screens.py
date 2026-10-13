"""Make the website's app screenshots from the real app, with a demo profile only.

    .venv/bin/python scripts/capture_screens.py [--out DIR] [--themes light,dark] [--only id,id]

Everything happens in a fresh temporary folder (REELAY_HOME) with an in-memory keychain: no real data,
accounts or keys are read or touched, and nothing goes over the network. A demo profile ("Juniper Lane",
"Travel, slowly") gets short generated clips (so covers and the player have real frames), tickets in every
Board column, sample connected accounts, a saved source, an automation rule with an activity entry and a logo.
The window is drawn offscreen at 1440 × 900 with 2x scaling, so each PNG is 2880 × 1800, and saved as
<out>/<slot id>-<theme>@2x.png for the slots in the website's src/data/screens.json.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO.parent / "hey-this-is-andrew" / "sites" / "reelay" / "public" / "screens"
SLOTS = ("library-grid", "download-sources", "command-center", "board", "automations", "agents-settings",
         "accounts", "branding")
WIDTH, HEIGHT, SCALE = 1440, 900, 2

VIDEOS = [  # title, caption, seconds, gradient colours, stage
    ("Golden hour at Big Sur", "The last ten minutes of light on the Big Sur coast. #bigsur #goldenhour #roadtrip",
     22, ("orange", "purple"), "staging"),
    ("Tokyo after midnight", "Tokyo after midnight is a different city. #tokyo #nightwalk", 57,
     ("magenta", "navy"), "staging"),
    ("Desert road trip, day four", "Day four. No signal, no plan, no complaints. #roadtrip #desert", 39,
     ("gold", "sienna"), "scheduled"),
    ("First snow in the Alps", "First snow and I was not ready. #alps #snow", 18, ("white", "steelblue"),
     "scheduled"),
    ("Surf check at dawn", "Surf check at dawn, nobody else out. #surf #morning", 26, ("skyblue", "white"),
     "published"),
    ("Sixty seconds of forest", "Sixty seconds of forest, nothing else. #forest #slowtravel", 60,
     ("seagreen", "khaki"), "published"),
    ("Three knife skills that changed my cooking", "Three knife skills that changed my cooking. #cooking",
     48, ("tan", "black"), "backlog"),
    ("Night market eats", "Night market eats in Taipei. #taipei #streetfood", 45, ("midnightblue", "slateblue"),
     "backlog"),
    ("Ferry to the islands", "Ferry to the islands at 6 am. #greece #islands", 33, ("teal", "lightcyan"), "backlog"),
    ("Train window, Norway", "Train window, Norway. That's it, that's the video. #norway #trains", 41,
     ("darkslategray", "lightsteelblue"), "backlog"),
]


def parse_args():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--themes", default="light,dark")
    ap.add_argument("--only", default=",".join(SLOTS))
    return ap.parse_args()


def make_clips(folder: Path, ffmpeg: str) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    clips = []
    for n, (_t, _c, _s, (a, b), _st) in enumerate(VIDEOS):
        out = folder / f"juniper-{n}.mp4"
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                        "-i", f"gradients=s=540x960:c0={a}:c1={b}:d=3:speed=0.015", "-t", "3",
                        "-pix_fmt", "yuv420p", "-c:v", "libx264", "-y", str(out)], check=True)
        clips.append(out)
    return clips


def main() -> int:
    args = parse_args()
    themes = [t for t in args.themes.split(",") if t]
    only = [s for s in args.only.split(",") if s]
    args.out.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="reelay-screens-"))
    os.environ.update(REELAY_HOME=str(home), REELAY_REDUCE_MOTION="1", QT_QPA_PLATFORM="offscreen",
                      QT_SCALE_FACTOR=str(SCALE))
    sys.path.insert(0, str(REPO))
    from reelay.ytdl import find_ffmpeg
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("ffmpeg is needed to make the demo clips", file=sys.stderr)
        return 1
    clips = make_clips(home / "clips", ffmpeg)
    for theme in themes:
        run_theme(theme, home / theme, clips, args.out, only)
    print(f"Saved to {args.out}")
    return 0


def run_theme(theme: str, home: Path, clips: list[Path], out: Path, only: list[str]) -> None:
    """One isolated data folder per theme (a fresh app each time)."""
    import shutil
    code = f"""
import sys; sys.path.insert(0, {str(REPO)!r})
from scripts.capture_screens import capture
capture({theme!r}, {str(home)!r}, {[str(c) for c in clips]!r}, {str(out)!r}, {only!r})
"""
    env = {**os.environ, "REELAY_HOME": str(home)}
    home.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(home / "videos", ignore_errors=True)
    subprocess.run([sys.executable, "-c", code], check=True, env=env)


# ---------------------------------------------------------------------- inside the per-theme process
def capture(theme: str, home: str, clips: list[str], out: str, only: list[str]) -> None:
    import shutil

    from PySide6.QtCore import QBuffer, QIODevice, QPoint, QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPixmap
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    from reelay import rules as R
    from reelay.gui import branding
    from reelay.gui.app import create_window
    from reelay.models import Account, Post, Source, Stage, VideoItem
    from reelay.paths import db_path
    from reelay.store import Store
    from reelay.tokens import MemoryTokenStore
    from reelay.youtube import OAuthClient, Tokens

    out_dir = Path(out)
    lib = Path(home) / "videos" / "Juniper Lane"
    lib.mkdir(parents=True, exist_ok=True)
    store = Store(db_path())
    tokens = MemoryTokenStore()
    prof = store.create_profile("Juniper Lane", lib)
    store.set_active_profile(prof.id)
    store.set_setting("theme", theme)
    store.set_setting("ui.library.page_size", "24")

    # ---- demo videos, tickets in every column
    items = []
    for n, (title, caption, seconds, _colours, _stage) in enumerate(VIDEOS):
        name = f"TikTok_202610{12 - n:02d}_73{n:02d}.mp4"
        shutil.copy(clips[n], lib / name)
        items.append(VideoItem(platform="TikTok", video_id=f"73{n:02d}9", url=f"https://www.tiktok.com/@juniperlane/video/73{n:02d}9",
                               title=title, description=caption, duration=seconds, width=1080, height=1920,
                               upload_date=f"202610{12 - n:02d}", uploader="juniperlane", filename=name,
                               folder=str(lib), view_count=[18400, 9210, 12100, 4877, 31600, 7300, 22500, 15800,
                                                            6100, 11300][n]))
    ids = store.save_videos(prof.id, items)
    rows = [ids[i.key] for i in items]
    for row, (_t, caption, _s, _c, stage) in zip(rows, VIDEOS):
        store.set_stage([row], stage)
    copy = {"title": VIDEOS[0][0], "description": "The last ten minutes of light on the Big Sur coast. Shot handheld "
            "from the turnout at mile 45.", "tiktok_text": VIDEOS[0][1], "instagram_text": VIDEOS[0][1],
            "threads_text": "Golden hour at Big Sur."}
    store.save_post(Post(video_row=rows[0], target_platforms=["youtube", "tiktok", "instagram", "threads"], **copy))
    store.save_post(Post(video_row=rows[2], title=VIDEOS[2][0], description=VIDEOS[2][1],
                         target_platforms=["youtube", "tiktok"], tiktok_text=VIDEOS[2][1]))

    # ---- accounts (an in-memory keychain; nothing real)
    accounts = [("youtube", "UCjuniperlane0000000000", "Juniper Lane", "@juniperlane"),
                ("tiktok", "jl-open-id", "Juniper Lane", "@juniperlane"),
                ("threads", "1784140000000", "juniperlane", "@juniperlane"),
                ("bluesky", "did:plc:juniperlane", "juniperlane.bsky.social", "@juniperlane.bsky.social")]
    for platform, ext, name, handle in accounts:
        acc = store.save_account(Account(profile_id=prof.id, platform=platform, external_id=ext,
                                         display_name=name, handle=handle))
        secret = (Tokens("demo", "demo", 9e12).to_json() if platform == "youtube" else
                  json.dumps({"access_token": "demo", "user_id": ext, "handle": handle, "app_password": "demo"}))
        tokens.set(acc.token_key, secret)
    store.add_source(Source(profile_id=prof.id, name="@juniperlane on TikTok", platform="tiktok",
                            url="https://www.tiktok.com/@juniperlane", mine=True))
    store.add_source(Source(profile_id=prof.id, name="@juniperlane on Instagram", platform="instagram",
                            url="https://www.instagram.com/juniperlane/", mine=True))

    # ---- branding: a tagline and a logo
    logo = QImage(256, 256, QImage.Format_ARGB32)
    logo.fill(Qt.transparent)
    p = QPainter(logo)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(branding_colour("accent", theme)))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(QRectF(0, 0, 256, 256), 64, 64)
    f = QFont()
    f.setPixelSize(120)
    f.setBold(True)
    p.setFont(f)
    p.setPen(QColor(branding_colour("ink", theme)))
    p.drawText(QRectF(0, 0, 256, 256), Qt.AlignCenter, "JL")
    p.end()
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    logo.save(buf, "PNG")
    branding.save(store, prof.id, branding.Brand(tagline="Travel, slowly", logo=bytes(buf.data())))

    # ---- an automation rule and what it did
    rule = R.Rule(name="Cross-post every new Short", actions=[
        R.Action("fill_copy", field="tiktok_text", value="{caption}"),
        R.Action("set_platforms", value="YouTube, TikTok, Instagram, Threads")])
    R.save(store, prof.id, [rule])
    R._log(store, prof.id, [{"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "rule": rule.name,
                             "video": rows[0], "title": VIDEOS[0][0],
                             "done": ["filled TikTok caption", "platforms: YouTube, TikTok, Instagram, Threads"]}])

    window = create_window(app, store, tokens)
    window.accounts.set_oauth_client(OAuthClient("demo.apps.googleusercontent.com", "demo"))
    window.menuBar().hide()            # on a Mac the menu bar is at the top of the screen, not in the window
    window.resize(WIDTH, HEIGHT)
    window.show()

    def settle(seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            app.processEvents()
            time.sleep(0.02)

    def save(slot: str, pix: QPixmap) -> None:
        if slot not in only:
            return
        path = out_dir / f"{slot}-{theme}@2x.png"
        image = pix.toImage()
        if image.width() != WIDTH * SCALE or image.height() != HEIGHT * SCALE:
            image = image.scaled(WIDTH * SCALE, HEIGHT * SCALE, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        image.save(str(path))
        print(f"  {path.name}  {image.width()}×{image.height()}")

    def with_dialog(dialog) -> QPixmap:
        """The window dimmed, with `dialog` drawn centred on it (as macOS shows a sheet)."""
        dialog.show()
        settle(1.2)
        base = window.grab()
        sheet = dialog.grab()
        dialog.hide()
        canvas = QPixmap(base)
        p = QPainter(canvas)
        p.fillRect(QRectF(0, 0, WIDTH, HEIGHT), QColor(0, 0, 0, 90))
        x = (WIDTH - dialog.width()) // 2
        y = max(30, (HEIGHT - dialog.height()) // 2)
        p.drawPixmap(QPoint(x, y), sheet)
        p.end()
        return canvas

    print(f"{theme}:")
    # Library
    window.go("download")
    settle(5)
    save("library-grid", window.grab())
    page = window.download
    page.url.setText("https://www.tiktok.com/@juniperlane")
    page.open_sources()
    dialog = page.sources_dialog
    dialog.resize(640, 760)
    save("download-sources", with_dialog(dialog))
    # Board
    window.go("board")
    settle(2)
    board = window.board
    save("board", window.grab())
    col = board.columns[Stage.STAGING]
    idx = next(col.model.index(r) for r, c in enumerate(col.model.cards) if c.id == rows[0])
    board._on_select(col, idx)
    settle(4)
    save("command-center", window.grab())
    board.close_inspector()
    # Automations
    window.go("automations")
    settle(1.5)
    save("automations", window.grab())
    # Settings → AI agents (the key itself is never on screen; it's only ever copied)
    window.go("settings")
    settle(1)
    settings = window.settings_page
    from PySide6.QtWidgets import QFrame, QScrollArea
    area = settings.findChild(QScrollArea)
    card = settings.copy_token_btn
    while card is not None and not (isinstance(card, QFrame) and card.objectName() == "Card"):
        card = card.parentWidget()
    if area is not None and card is not None:          # the AI agents card at the top (no file paths in view)
        holder = card.parentWidget().layout()
        index = holder.indexOf(card)
        for i in range(index + 1, holder.count()):     # the sections below show this run's temporary folders
            w = holder.itemAt(i).widget()
            if w is not None:
                w.hide()
        settle(0.3)
        area.verticalScrollBar().setValue(card.mapTo(area.widget(), QPoint(0, 0)).y() - 16)
    settle(0.8)
    save("agents-settings", window.grab())
    # Accounts
    window.go("accounts")
    window.accounts_page.refresh()
    settle(1.5)
    save("accounts", window.grab())
    # Branding: Edit Profile & Branding
    from reelay.gui.profile_dialog import ProfileDialog
    window.go("board")
    settle(0.5)
    dlg = ProfileDialog(store, window.profile, theme == "custom", window)
    save("branding", with_dialog(dlg))
    window.close()
    store.close()


def branding_colour(which: str, theme: str) -> str:
    """The demo logo's colours (a warm amber mark with dark initials), from the theme's palette."""
    from reelay.gui import theme as T
    p = T.THEMES.get(theme, T.THEMES["dark"])
    return p.warning if which == "accent" else p.bg_primary if theme != "light" else p.text_primary


if __name__ == "__main__":
    sys.exit(main())
