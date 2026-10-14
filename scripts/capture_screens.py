"""Make the website's app screenshots from the real app, with a demo profile only.

    .venv/bin/python scripts/capture_screens.py [--out DIR] [--themes light,dark] [--only id,id]
    .venv/bin/python scripts/capture_screens.py --help-shots      # the in-app Help Center's pictures

Everything happens in a fresh temporary folder (REELAY_HOME) with an in-memory keychain: no real data,
accounts or keys are read or touched, and nothing goes over the network. A demo profile ("Juniper Lane",
"Travel, slowly") gets short generated clips (so covers and the player have real frames), tickets in every
Board column, sample connected accounts, a saved source, an automation rule with an activity entry and a logo.
The window is drawn offscreen at 1440 × 900 with 2x scaling, so each PNG is 2880 × 1800, and saved as
<out>/<slot id>-<theme>@2x.png for the slots in the website's src/data/screens.json.

--help-shots saves the Help Center's screenshots instead (reelay/gui/help_shots/<id>-<theme>.webp) and
marks.json: where each control the guides point at sits in each picture, measured from the real widgets
(as fractions of the window), so the numbered markers land on the right control after any re-capture.
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
HELP_OUT = REPO / "reelay" / "gui" / "help_shots"
HELP_WIDTH = 1760              # saved width of a Help picture (sharp at 2x in the Help Center's frame)

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
    ap.add_argument("--help-shots", action="store_true", help="make the in-app Help Center's pictures")
    args = ap.parse_args()
    if args.help_shots and args.out == DEFAULT_OUT:
        args.out = HELP_OUT
    return args


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
        run_theme(theme, home / theme, clips, args.out, only, "help" if args.help_shots else "site")
    print(f"Saved to {args.out}")
    return 0


def run_theme(theme: str, home: Path, clips: list[Path], out: Path, only: list[str], mode: str = "site") -> None:
    """One isolated data folder per theme (a fresh app each time)."""
    import shutil
    code = f"""
import sys; sys.path.insert(0, {str(REPO)!r})
from scripts.capture_screens import capture
capture({theme!r}, {str(home)!r}, {[str(c) for c in clips]!r}, {str(out)!r}, {only!r}, {mode!r})
"""
    env = {**os.environ, "REELAY_HOME": str(home)}
    home.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(home / "videos", ignore_errors=True)
    subprocess.run([sys.executable, "-c", code], check=True, env=env)


# ---------------------------------------------------------------------- inside the per-theme process
def capture(theme: str, home: str, clips: list[str], out: str, only: list[str], mode: str = "site") -> None:
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
    if mode == "help":          # a second creator, so the profile menu has someone to switch to
        store.create_profile("Mesa Roasters", Path(home) / "videos" / "Mesa Roasters")
    if mode == "help":          # a listed channel not downloaded yet: the Library shows its platform tabs
        store.save_videos(prof.id, [
            VideoItem(platform="YouTube", video_id=f"jlyt{n}", url=f"https://www.youtube.com/watch?v=jlyt{n}",
                      title=title, description=title, duration=seconds, width=1920, height=1080,
                      upload_date=f"202609{20 - n:02d}", uploader="Juniper Lane", view_count=views)
            for n, (title, seconds, views) in enumerate([("Two weeks in Lisbon, the long version", 1260, 48200),
                                                         ("Packing for a month with one bag", 742, 30100),
                                                         ("Night trains of Europe, ranked", 1515, 61900)])])
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
    if mode == "help":
        capture_help(app, theme, store, window, rows, Path(out))
        window.close()
        store.close()
        return

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


def capture_help(app, theme: str, store, window, rows: list[int], out: Path) -> None:
    """The Help Center's pictures, and where each control it points at sits in them (marks.json)."""
    from PySide6.QtCore import QPoint, QRect, QRectF, Qt
    from PySide6.QtGui import QColor, QPainter, QPixmap
    from PySide6.QtWidgets import QPushButton, QScrollArea

    from reelay.models import Stage
    out.mkdir(parents=True, exist_ok=True)
    suffix = "dark" if theme != "light" else "light"
    marks_path = out / "marks.json"
    manifest = json.loads(marks_path.read_text()) if marks_path.exists() else {}

    def settle(seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            app.processEvents()
            time.sleep(0.02)

    def frac(rect: QRect) -> list[float]:
        return [round(rect.x() / WIDTH, 4), round(rect.y() / HEIGHT, 4),
                round(rect.width() / WIDTH, 4), round(rect.height() / HEIGHT, 4)]

    def at(widget, offset: QPoint = QPoint(0, 0), host=None) -> list[float]:
        """Where `widget` sits in the picture: in the window, or in a sheet drawn at `offset`. A checkbox or a
        tab bar is measured by what's drawn (its text, its tabs), not by the row it stretches across."""
        from PySide6.QtWidgets import QCheckBox, QTabBar
        area = QRect(QPoint(0, 0), widget.size())
        if isinstance(widget, QCheckBox):
            area.setWidth(min(area.width(), widget.sizeHint().width()))
        elif isinstance(widget, QTabBar) and widget.count():
            area.setWidth(widget.tabRect(widget.count() - 1).right() + 1)
        if host is None:               # only the part on screen (a long section in a scrolled panel is cut off)
            area = area.intersected(widget.visibleRegion().boundingRect())
            if area.isEmpty():
                return None
        rect = QRect(widget.mapTo(host or window, area.topLeft()) + offset, area.size())
        return frac(rect.intersected(QRect(0, 0, WIDTH, HEIGHT)))

    def save(shot: str, pix: QPixmap, marks: dict[str, list[float]]) -> None:
        image = pix.toImage().scaledToWidth(HELP_WIDTH, Qt.SmoothTransformation)
        path = out / f"{shot}-{suffix}.webp"
        image.save(str(path), "WEBP", 86)
        manifest[shot] = {"marks": {k: v for k, v in marks.items() if v}}
        print(f"  {path.name}  {image.width()}×{image.height()}  {path.stat().st_size // 1024} KB")

    def sheet(dialog, width: int | None = None, height: int | None = None) -> tuple[QPixmap, QPoint]:
        """The window dimmed, with `dialog` drawn on it as macOS shows a sheet; and where the sheet sits."""
        if width or height:
            dialog.resize(width or dialog.width(), height or dialog.height())
        dialog.show()
        settle(1.0)
        base, drawn = window.grab(), dialog.grab()
        dialog.hide()
        canvas = QPixmap(base)
        p = QPainter(canvas)
        p.fillRect(QRectF(0, 0, WIDTH, HEIGHT), QColor(0, 0, 0, 90))
        offset = QPoint((WIDTH - dialog.width()) // 2, max(30, (HEIGHT - dialog.height()) // 2))
        p.drawPixmap(offset, drawn)
        p.end()
        return canvas, offset

    def with_menu(menu, anchor) -> tuple[QPixmap, QPoint]:
        """The window with `menu` open under `anchor` (a menu is its own window, so it's drawn on)."""
        menu.ensurePolished()
        menu.resize(menu.sizeHint())
        drawn = menu.grab()
        pos = anchor.mapTo(window, QPoint(0, anchor.height() + 4))
        pos.setX(min(pos.x(), WIDTH - menu.width() - 8))
        canvas = QPixmap(window.grab())
        p = QPainter(canvas)
        p.drawPixmap(pos, drawn)
        p.end()
        return canvas, pos

    def menu_marks(menu, pos: QPoint, names: dict[str, str]) -> dict[str, list[float]]:
        marks = {"menu": frac(QRect(pos, menu.size()))}
        for action in menu.actions():
            for key, text in names.items():
                if action.text().replace("&&", "&") == text:
                    marks[key] = frac(menu.actionGeometry(action).translated(pos))
        return marks

    def scroll_to(widget, margin: int = 12) -> None:
        area = widget.parentWidget()
        while area is not None and not isinstance(area, QScrollArea):
            area = area.parentWidget()
        if area is not None:
            area.verticalScrollBar().setValue(widget.mapTo(area.widget(), QPoint(0, 0)).y() - margin)
            settle(0.5)

    print(f"help {theme}:")
    # ---- Library: platform tabs, two videos ticked
    window.go("download")
    settle(4)
    page = window.download
    page._refresh_tags()
    settle(0.5)
    pending = [i.key for i in page.model.items() if not i.is_downloaded][:2]
    page.model.set_checked(pending, True)
    settle(1)
    save("library", window.grab(), {
        "sources": at(page.source_btn), "link": at(page.url), "list": at(page.fetch_btn),
        "tabs": at(page.platform_tabs), "select": at(page.select_all_btn), "download": at(page.download_btn),
        "filter": at(page.filter_btn), "search": at(page.search), "view": at(page.view_toggle),
        "tiles": at(page.tile_controls), "gear": at(page.settings_btn), "pages": at(page.page_size),
        "videolist": at(page.manifest_btn)})
    page.model.set_checked(pending, False)
    # ---- Download Sources
    page.url.setText("https://www.tiktok.com/@juniperlane")
    page.open_sources()
    pix, offset = sheet(page.sources_dialog, 640, 700)
    save("sources", pix, {"dialog": frac(QRect(offset, page.sources_dialog.size()))})
    # ---- Board, its ⋯ menu, and the profile menu
    window.go("board")
    settle(2)
    board = window.board
    save("board", window.grab(), {
        "search": at(board.search), "filter": at(board.filter_btn), "refresh": at(board.sync_btn),
        "more": at(board.more_btn), "backlog": at(board.columns[Stage.BACKLOG]),
        "staging": at(board.columns[Stage.STAGING]), "published": at(board.columns[Stage.PUBLISHED])})
    more = board.more_btn.menu()
    pix, pos = with_menu(more, board.more_btn)
    save("board-more", pix, {"more": at(board.more_btn), "search": at(board.search),
                             "published": at(board.columns[Stage.PUBLISHED]), **menu_marks(more, pos, {
        "import": "Import Sheet…", "columns": "Edit Columns…", "archived": "Archived Videos…"})})
    menu = window.profile_menu
    window._fill_profile_menu()
    pix, pos = with_menu(menu, window.profile_btn)
    save("profile", pix, {"profile": at(window.profile_btn), **menu_marks(menu, pos, {
        "new": "New Profile…", "brand": "Edit Profile & Branding…"})})
    # ---- a ticket: readiness, captions, posting
    col = board.columns[Stage.STAGING]
    idx = next(col.model.index(r) for r, c in enumerate(col.model.cards) if c.id == rows[0])
    board._on_select(col, idx)
    settle(4)
    panel = board.panel
    card_rect = col.view.visualRect(idx)
    card = frac(QRect(col.view.viewport().mapTo(window, card_rect.topLeft()), card_rect.size()))
    save("ticket", window.grab(), {
        "card": card, "ready": at(panel.readiness_header), "platforms": at(panel.platforms_section),
        "save": at(panel.save_btn), "post": at(panel.upload_btn), "close": at(board.inspector.close_btn)})
    scroll_to(panel.fill_btn)
    section = panel.copy_sections["youtube"]
    copy = next((b for b in section.findChildren(QPushButton) if b.text() == "Copy"), section)
    save("ticket-copy", window.grab(), {"card": card, "fill": at(panel.fill_btn), "caption": at(section),
                                        "copy": at(copy)})
    scroll_to(panel.platforms_section)
    row = panel.platform_rows["tiktok"]
    save("ticket-platforms", window.grab(), {
        "platforms": at(panel.platforms_section), "youtube": at(panel.platform_rows["youtube"]),
        "tiktok": at(row), "publish": at(row.handoff_btn) if row.handoff_btn.isVisible() else at(row),
        "post": at(panel.upload_btn)})
    panel.details.toggle.setChecked(True)                 # open: the transcript, cover and topic fields show
    settle(0.5)
    scroll_to(panel.details, 16)
    save("ticket-details", window.grab(), {"details": at(panel.details.toggle), "video": at(panel.video_section),
                                           "history": at(panel.history_section)})
    panel.details.toggle.setChecked(False)
    settle(0.3)
    scroll_to(panel.when, 190)                            # who can see it and when, above the buttons
    save("ticket-post", window.grab(), {"auto": at(panel.auto_section), "when": at(panel.when),
                                        "post": at(panel.upload_btn), "summary": at(panel.dock_summary)})
    # ---- Prepare to Publish (TikTok)
    from reelay import handoff
    from reelay.gui.handoff_dialog import HandoffDialog
    vrow = store.get_video(rows[0])
    post, _saved = board.pipeline.post_or_default(vrow)
    dialog = HandoffDialog(handoff.prepare(post, "tiktok", board.pipeline.video_path(vrow), vrow.item.duration),
                           window)
    pix, offset = sheet(dialog)
    save("handoff", pix, {"dialog": frac(QRect(offset, dialog.size()))})
    board.close_inspector()
    settle(0.5)
    # ---- Archived videos
    board.pipeline.archive([rows[9]])
    board.reload()
    settle(1)
    from reelay.gui.board_tools import ArchivedDialog
    dialog = ArchivedDialog(board.pipeline, window.profile, window)
    pix, offset = sheet(dialog)
    save("archived", pix, {"dialog": frac(QRect(offset, dialog.size())),
                           "restore": at(dialog.restore_btn, offset, dialog)})
    # ---- Accounts
    window.go("accounts")
    window.accounts_page.refresh()
    settle(1.5)
    acc = window.accounts_page
    cards = acc.platform_cards
    save("accounts", window.grab(), {
        "count": at(acc.count), "youtube": at(acc.youtube_card), "instagram": at(cards["instagram"]),
        "facebook": at(cards["facebook"]), "bluesky": at(cards["bluesky"]), "snapchat": at(cards["snapchat"]),
        "advanced": at(acc.advanced_btn) if acc.advanced_btn.isVisible() else at(acc.youtube_card)})
    # ---- Automations, and a rule
    window.go("automations")
    settle(1.5)
    auto = window.automations_page
    first_rule = auto.rules_box.itemAt(0).widget() if auto.rules_box.count() else auto
    save("automations", window.grab(), {"new": at(auto.new_btn), "recipes": at(auto.recipes_btn),
                                        "rule": at(first_rule), "activity": at(auto.log)})
    from reelay import rules as R
    from reelay.gui.automations_page import RuleDialog
    rule = R.load(store, window.profile.id)[0]
    dialog = RuleDialog(rule, board.column_config, window, board.pipeline, window.profile.id)
    pix, offset = sheet(dialog, 760, 760)
    save("rule", pix, {"dialog": frac(QRect(offset, dialog.size()))})
    # ---- Settings: appearance and tips; automatic posting
    window.go("settings")
    settle(1)
    settings = window.settings_page
    buttons = list(settings.theme_buttons.values())
    scroll_to(buttons[0], 120)
    first, last = buttons[0].mapTo(window, QPoint(0, 0)), buttons[-1].mapTo(window, QPoint(0, 0))
    themes = QRect(first, QPoint(last.x() + buttons[-1].width(), first.y() + buttons[0].height()))
    save("settings", window.grab(), {"theme": frac(themes), "tips": at(settings.tips_box)})
    box = next(iter(settings.flag_boxes.values()))
    scroll_to(box, 140)
    posting = box.parentWidget()
    save("settings-posting", window.grab(), {"posting": at(posting),
                                             **{p: at(b) for p, b in settings.flag_boxes.items()}})
    marks_path.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")


def branding_colour(which: str, theme: str) -> str:
    """The demo logo's colours (a warm amber mark with dark initials), from the theme's palette."""
    from reelay.gui import theme as T
    p = T.THEMES.get(theme, T.THEMES["dark"])
    return p.warning if which == "accent" else p.bg_primary if theme != "light" else p.text_primary


if __name__ == "__main__":
    sys.exit(main())
