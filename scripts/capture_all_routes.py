"""Capture screenshots of every single route, view, menu, and dialog in Reelay.

Outputs to docs/screenshots/app_routes/ with descriptive names.
Uses offscreen Qt rendering so no desktop windows flicker or steal focus.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "docs" / "screenshots" / "app_routes"
WIDTH, HEIGHT, SCALE = 1440, 900, 2

VIDEOS = [
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


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="reelay-all-routes-"))
    os.environ.update(
        REELAY_HOME=str(home),
        REELAY_REDUCE_MOTION="1",
        QT_QPA_PLATFORM="offscreen",
        QT_SCALE_FACTOR=str(SCALE)
    )
    sys.path.insert(0, str(REPO))
    from reelay.ytdl import find_ffmpeg
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("ffmpeg not found", file=sys.stderr)
        return 1
    clips = make_clips(home / "clips", ffmpeg)

    # Run in dark and light themes (or default dark for comprehensive capture)
    run_capture("dark", home / "dark", clips, OUT_DIR)
    print(f"\nAll screenshots successfully saved to: {OUT_DIR}")
    return 0


def run_capture(theme: str, home: Path, clips: list[Path], out_dir: Path):
    from PySide6.QtCore import QBuffer, QIODevice, QPoint, QRect, QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPixmap
    from PySide6.QtWidgets import QApplication, QDialog, QFrame, QPushButton, QScrollArea

    app = QApplication.instance() or QApplication(sys.argv)
    from reelay import assist, handoff, library
    from reelay import rules as R
    from reelay.gui import branding
    from reelay.gui.app import create_window
    from reelay.gui.assist_dialogs import GrammarDialog, RewriteDialog, SEODialog
    from reelay.gui.automations_page import RuleDialog
    from reelay.gui.board_tools import ArchivedDialog, ColumnsDialog, ImportDialog
    from reelay.gui.dialogs import RenamePreviewDialog
    from reelay.gui.handoff_dialog import HandoffDialog
    from reelay.gui.profile_dialog import ProfileDialog
    from reelay.gui.setup_dialogs import YouTubeSetupWizard
    from reelay.gui.ticket_widgets import RevisionDialog
    from reelay.gui.video_list_dialog import VideoListDialog
    from reelay.models import Account, Post, Source, Stage, VideoItem
    from reelay.paths import db_path
    from reelay.store import Store
    from reelay.tokens import MemoryTokenStore
    from reelay.youtube import OAuthClient, Tokens

    lib = Path(home) / "videos" / "Juniper Lane"
    lib.mkdir(parents=True, exist_ok=True)
    store = Store(db_path())
    tokens = MemoryTokenStore()
    prof = store.create_profile("Juniper Lane", lib)
    store.set_active_profile(prof.id)
    store.set_setting("theme", theme)
    store.set_setting("ui.library.page_size", "24")

    # Second creator profile
    store.create_profile("Mesa Roasters", Path(home) / "videos" / "Mesa Roasters")

    # Demo videos
    items = []
    for n, (title, caption, seconds, _colours, _stage) in enumerate(VIDEOS):
        name = f"TikTok_202610{12 - n:02d}_73{n:02d}.mp4"
        shutil.copy(clips[n], lib / name)
        items.append(VideoItem(
            platform="TikTok", video_id=f"73{n:02d}9", url=f"https://www.tiktok.com/@juniperlane/video/73{n:02d}9",
            title=title, description=caption, duration=seconds, width=1080, height=1920,
            upload_date=f"202610{12 - n:02d}", uploader="juniperlane", filename=name,
            folder=str(lib), view_count=[18400, 9210, 12100, 4877, 31600, 7300, 22500, 15800, 6100, 11300][n]
        ))
    ids = store.save_videos(prof.id, items)
    rows = [ids[i.key] for i in items]

    for row, (_t, caption, _s, _c, stage) in zip(rows, VIDEOS):
        store.set_stage([row], stage)
    copy = {
        "title": VIDEOS[0][0],
        "description": "The last ten minutes of light on the Big Sur coast. Shot handheld from the turnout at mile 45.",
        "tiktok_text": VIDEOS[0][1], "instagram_text": VIDEOS[0][1],
        "threads_text": "Golden hour at Big Sur."
    }
    store.save_post(Post(video_row=rows[0], target_platforms=["youtube", "tiktok", "instagram", "threads"], **copy))
    store.save_post(Post(video_row=rows[2], title=VIDEOS[2][0], description=VIDEOS[2][1],
                         target_platforms=["youtube", "tiktok"], tiktok_text=VIDEOS[2][1]))

    # Connected accounts
    accounts = [
        ("youtube", "UCjuniperlane0000000000", "Juniper Lane", "@juniperlane"),
        ("tiktok", "jl-open-id", "Juniper Lane", "@juniperlane"),
        ("threads", "1784140000000", "juniperlane", "@juniperlane"),
        ("bluesky", "did:plc:juniperlane", "juniperlane.bsky.social", "@juniperlane.bsky.social")
    ]
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

    # Branding logo & tagline
    logo = QImage(256, 256, QImage.Format_ARGB32)
    logo.fill(Qt.transparent)
    p = QPainter(logo)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#E8883B"))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(QRectF(0, 0, 256, 256), 64, 64)
    f = QFont()
    f.setPixelSize(120)
    f.setBold(True)
    p.setFont(f)
    p.setPen(QColor("#FFFFFF"))
    p.drawText(QRectF(0, 0, 256, 256), Qt.AlignCenter, "JL")
    p.end()
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    logo.save(buf, "PNG")
    branding.save(store, prof.id, branding.Brand(tagline="Travel, slowly", logo=bytes(buf.data())))

    # Automation rule & activity
    rule = R.Rule(name="Cross-post every new Short", actions=[
        R.Action("fill_copy", field="tiktok_text", value="{caption}"),
        R.Action("set_platforms", value="YouTube, TikTok, Instagram, Threads")])
    R.save(store, prof.id, [rule])
    R._log(store, prof.id, [{"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "rule": rule.name,
                             "video": rows[0], "title": VIDEOS[0][0],
                             "done": ["filled TikTok caption", "platforms: YouTube, TikTok, Instagram, Threads"]}])

    window = create_window(app, store, tokens)
    window.accounts.set_oauth_client(OAuthClient("demo.apps.googleusercontent.com", "demo"))
    window.menuBar().hide()
    window.resize(WIDTH, HEIGHT)
    window.show()

    def settle(seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            app.processEvents()
            time.sleep(0.02)

    def save_pixmap(name: str, pix: QPixmap) -> None:
        path = out_dir / f"{name}.png"
        image = pix.toImage()
        if image.width() != WIDTH * SCALE or image.height() != HEIGHT * SCALE:
            image = image.scaled(WIDTH * SCALE, HEIGHT * SCALE, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        image.save(str(path))
        print(f"  ✓ Saved {name}.png ({image.width()}×{image.height()})")

    def with_dialog(dialog: QDialog, width: int | None = None, height: int | None = None) -> QPixmap:
        if width or height:
            dialog.resize(width or dialog.width(), height or dialog.height())
        dialog.show()
        settle(1.0)
        base = window.grab()
        sheet = dialog.grab()
        dialog.hide()
        canvas = QPixmap(base)
        p = QPainter(canvas)
        p.fillRect(QRectF(0, 0, WIDTH, HEIGHT), QColor(0, 0, 0, 110))
        x = (WIDTH - dialog.width()) // 2
        y = max(30, (HEIGHT - dialog.height()) // 2)
        p.drawPixmap(QPoint(x, y), sheet)
        p.end()
        return canvas

    def with_menu(menu, anchor) -> QPixmap:
        menu.ensurePolished()
        menu.resize(menu.sizeHint())
        drawn = menu.grab()
        pos = anchor.mapTo(window, QPoint(0, anchor.height() + 4))
        pos.setX(min(pos.x(), WIDTH - menu.width() - 8))
        canvas = QPixmap(window.grab())
        p = QPainter(canvas)
        p.drawPixmap(pos, drawn)
        p.end()
        return canvas

    def scroll_to(widget, margin: int = 12) -> None:
        area = widget.parentWidget()
        while area is not None and not isinstance(area, QScrollArea):
            area = area.parentWidget()
        if area is not None:
            area.verticalScrollBar().setValue(widget.mapTo(area.widget(), QPoint(0, 0)).y() - margin)
            settle(0.4)

    print("Capturing route screens...")

    # =========================================================================
    # 1. MEDIA LIBRARY
    # =========================================================================
    window.go("download")
    settle(3.0)
    save_pixmap("01_library_grid_view", window.grab())

    # Library List view
    page = window.download
    page.view_toggle.set_mode("list", emit=True)
    settle(1.5)
    save_pixmap("02_library_list_view", window.grab())
    page.view_toggle.set_mode("grid", emit=True)

    # Download Sources Dialog
    page.url.setText("https://www.tiktok.com/@juniperlane")
    page.open_sources()
    save_pixmap("03_library_sources_dialog", with_dialog(page.sources_dialog, 640, 720))

    # Video List / Manifest Dialog
    vlist_dlg = VideoListDialog(items, lib, window.thumbs, lib / "manifest.md", parent=window)
    save_pixmap("04_library_video_list_manifest", with_dialog(vlist_dlg, 920, 620))

    # Rename Preview Dialog
    rename_steps = [library.RenameStep(i, i.filename, f"20261012_{i.video_id}_juniper.mp4") for i in items[:4]]
    rename_dlg = RenamePreviewDialog(rename_steps, window)
    save_pixmap("05_library_rename_files_dialog", with_dialog(rename_dlg, 760, 460))

    # =========================================================================
    # 2. THE BOARD (KANBAN)
    # =========================================================================
    window.go("board")
    settle(2.0)
    board = window.board
    save_pixmap("06_board_kanban_four_columns", window.grab())

    # Board ⋯ menu
    more = board.more_btn.menu()
    save_pixmap("07_board_more_menu_open", with_menu(more, board.more_btn))

    # Import Sheet Dialog
    sheet_dlg = ImportDialog(board.pipeline, window.profile, window)
    save_pixmap("08_board_import_sheet_dialog", with_dialog(sheet_dlg, 860, 660))

    # Edit Columns Dialog
    counts = {s: 2 for s in Stage.ALL}
    col_dlg = ColumnsDialog(board.column_config, counts, window)
    save_pixmap("09_board_edit_columns_dialog", with_dialog(col_dlg, 540, 520))

    # Archived Videos Dialog
    board.pipeline.archive([rows[9]])
    board.reload()
    arch_dlg = ArchivedDialog(board.pipeline, window.profile, window)
    save_pixmap("10_board_archived_videos_dialog", with_dialog(arch_dlg, 640, 480))

    # =========================================================================
    # 3. COMMAND CENTER / TICKET INSPECTOR
    # =========================================================================
    col = board.columns[Stage.STAGING]
    idx = next(col.model.index(r) for r, c in enumerate(col.model.cards) if c.id == rows[0])
    board._on_select(col, idx)
    settle(3.0)
    panel = board.panel
    save_pixmap("11_ticket_overview_command_center", window.grab())

    # Scrolled to Platform Copy
    scroll_to(panel.fill_btn)
    save_pixmap("12_ticket_platform_copy_sections", window.grab())

    # Assist Menu
    section = panel.copy_sections["youtube"]
    assist_btn = next((b for b in section.findChildren(QPushButton) if "Assist" in b.text()), None)
    if assist_btn and assist_btn.menu():
        save_pixmap("13_ticket_assist_menu_open", with_menu(assist_btn.menu(), assist_btn))

    # Assist: Spelling Dialog
    grammar_dlg = GrammarDialog(VIDEOS[0][1], store, "TikTok Caption", window, check=lambda t: ([], "Built-in dictionary"))
    save_pixmap("14_assist_spelling_grammar_dialog", with_dialog(grammar_dlg, 560, 460))

    # Assist: Local AI Rewrite Dialog
    rewrite_dlg = RewriteDialog(VIDEOS[0][1], store, "Instagram Caption", 2200, len, window)
    save_pixmap("15_assist_rewrite_ai_dialog", with_dialog(rewrite_dlg, 640, 520))

    # Assist: YouTube SEO Dialog
    seo_dlg = SEODialog(VIDEOS[0][0], ["roadtrip", "bigsur"], VIDEOS[0][1], ["big sur coast"], window)
    save_pixmap("16_assist_youtube_seo_dialog", with_dialog(seo_dlg, 680, 540))

    # Scrolled to Platforms & Posting Dock
    scroll_to(panel.when, 160)
    save_pixmap("17_ticket_posting_dock_schedule", window.grab())

    # Details & Provenance open
    panel.details.toggle.setChecked(True)
    settle(0.5)
    scroll_to(panel.details, 16)
    save_pixmap("18_ticket_details_provenance_transcript", window.grab())
    panel.details.toggle.setChecked(False)

    # Change History Dialog
    rev_dlg = RevisionDialog(store.revisions(rows[0]), window)
    save_pixmap("19_ticket_change_history_dialog", with_dialog(rev_dlg, 660, 480))

    # Prepare to Publish Handoff (TikTok)
    vrow = store.get_video(rows[0])
    post, _saved = board.pipeline.post_or_default(vrow)
    handoff_dlg = HandoffDialog(handoff.prepare(post, "tiktok", board.pipeline.video_path(vrow), vrow.item.duration), window)
    save_pixmap("20_handoff_prepare_to_publish_tiktok", with_dialog(handoff_dlg, 580, 620))

    board.close_inspector()
    settle(0.5)

    # =========================================================================
    # 4. AUTOMATIONS PAGE
    # =========================================================================
    window.go("automations")
    settle(1.5)
    auto = window.automations_page
    save_pixmap("21_automations_page_overview", window.grab())

    # Recipes dropdown
    if auto.recipes_btn.menu():
        save_pixmap("22_automations_recipes_menu", with_menu(auto.recipes_btn.menu(), auto.recipes_btn))

    # Rule Editor Dialog
    loaded_rule = R.load(store, window.profile.id)[0]
    rule_dlg = RuleDialog(loaded_rule, board.column_config, window, board.pipeline, window.profile.id)
    save_pixmap("23_automations_rule_editor_dialog", with_dialog(rule_dlg, 760, 760))

    # =========================================================================
    # 5. ACCOUNTS PAGE
    # =========================================================================
    window.go("accounts")
    window.accounts_page.refresh()
    settle(1.5)
    save_pixmap("24_accounts_page_overview", window.grab())

    # Advanced YouTube Setup Dialog
    yt_setup = YouTubeSetupWizard(window)
    save_pixmap("25_accounts_youtube_setup_wizard", with_dialog(yt_setup, 700, 560))

    # =========================================================================
    # 6. SETTINGS PAGE
    # =========================================================================
    window.go("settings")
    settle(1.0)
    settings = window.settings_page

    # Top: AI Agents Card
    save_pixmap("26_settings_ai_agents_api", window.grab())

    # Scrolled: Themes & Tips
    theme_btn = next(iter(settings.theme_buttons.values()))
    scroll_to(theme_btn, 100)
    save_pixmap("27_settings_appearance_themes_tips", window.grab())

    # Scrolled: Automatic Posting
    posting_box = next(iter(settings.flag_boxes.values()))
    scroll_to(posting_box, 120)
    save_pixmap("28_settings_automatic_posting_switches", window.grab())

    # =========================================================================
    # 7. HELP CENTER
    # =========================================================================
    window.go("help")
    settle(1.5)
    save_pixmap("29_help_center_home_bento", window.grab())

    # Tour step 1
    window.help_page.start_tour(0)
    settle(1.0)
    save_pixmap("30_help_center_tour_step_1", window.grab())

    # Guide view
    window.help_page.open_guide("profiles")
    settle(1.0)
    save_pixmap("31_help_center_guide_view", window.grab())
    window.help_page.show_home()

    # =========================================================================
    # 8. SIDEBAR & PROFILE SWITCHER
    # =========================================================================
    window.go("board")
    settle(0.5)
    window._fill_profile_menu()
    save_pixmap("32_profile_switcher_dropdown", with_menu(window.profile_menu, window.profile_btn))

    # Profile & Branding Dialog
    prof_dlg = ProfileDialog(store, window.profile, False, window)
    save_pixmap("33_profile_branding_dialog", with_dialog(prof_dlg, 600, 640))

    window.close()
    store.close()
    print("Done capturing all 33 screen views.")


if __name__ == "__main__":
    sys.exit(main())
