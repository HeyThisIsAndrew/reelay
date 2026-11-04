"""Automated screenshot capture workflow for Reelay.

 Launches the real Reelay PySide6 desktop application against an isolated
temporary REELAY_HOME directory and an in-memory token store (never touching
real user databases, settings, or keychain credentials), navigates through
every major route, view, menu, and dialog, overwrites existing screenshots in
place, synchronizes Help Center and marketing website derivatives, validates
every output image, and prints a summary report.

Usage:
    .venv/bin/python scripts/capture_all_routes.py            # full workflow (help + site + routes + website sync)
    .venv/bin/python scripts/capture_all_routes.py --routes-only  # only docs/screenshots/app_routes/
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
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
HELP_DIR = REPO / "reelay" / "gui" / "help_shots"
SITE_REPO = REPO.parent / "hey-this-is-andrew" / "sites" / "reelay"
SITE_SCREENS_DIR = SITE_REPO / "public" / "screens"
SITE_WEB_DIR = SITE_SCREENS_DIR / "web"
SITE_HELP_DIR = SITE_REPO / "public" / "help"
OBSOLETE_SCREENSHOTS = [
    (
        REPO / "docs" / "where-things-are.png",
        "Legacy v0.8.1 composite diagram (created in commit 11db83f) that is not referenced by any code, "
        "README, Help Center guide, or website page; superseded by reelay/gui/help_shots/ and docs/screenshots/app_routes/.",
    ),
]
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


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--routes-only", action="store_true",
                    help="Only capture docs/screenshots/app_routes/ (skip help_shots and website sync)")
    ap.add_argument("--out", type=Path, default=OUT_DIR,
                    help="Output directory for app route screenshots (default: docs/screenshots/app_routes)")
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


def validate_image(path: Path, min_width: int = 100, min_height: int = 100) -> tuple[bool, str]:
    """Verify that `path` exists, is non-empty, and decodes as a valid image."""
    from PySide6.QtGui import QImageReader

    if not path.is_file():
        return False, "file does not exist"
    if path.stat().st_size == 0:
        return False, "file is empty (0 bytes)"
    reader = QImageReader(str(path))
    size = reader.size()
    if not size.isValid() or size.width() < min_width or size.height() < min_height:
        img = reader.read()
        if img.isNull() or img.width() < min_width or img.height() < min_height:
            return False, f"invalid image ({reader.errorString()})"
        size = img.size()
    return True, f"{size.width()}×{size.height()} ({path.stat().st_size // 1024} KB)"


def snapshot_existing(paths: list[Path]) -> set[Path]:
    return {p.resolve() for p in paths if p.exists()}


def main() -> int:
    args = parse_args()
    out_dir: Path = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(REPO))
    from reelay.ytdl import find_ffmpeg

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("Error: ffmpeg not found on PATH", file=sys.stderr)
        return 1

    # Track pre-existing screenshot files before running the workflow so we can
    # accurately distinguish overwritten vs. newly created files.
    pre_existing: set[Path] = set()
    tracked_dirs = [out_dir]
    if not args.routes_only:
        tracked_dirs.extend([HELP_DIR, SITE_SCREENS_DIR, SITE_WEB_DIR, SITE_HELP_DIR])
    for d in tracked_dirs:
        if d.is_dir():
            for ext in ("*.png", "*.webp", "*.jpg", "*.jpeg"):
                pre_existing.update(p.resolve() for p in d.glob(ext))

    captured: list[Path] = []
    failed: list[tuple[str, str]] = []

    # Step 1 & 2: If running the full workflow, capture Help Center shots first
    # (so the in-app Help Center displays up-to-date WebP images when route 29-31
    # are captured) and then capture the 8 website slots (light + dark).
    if not args.routes_only:
        print("=== Step 1/4: Capturing in-app Help Center screenshots (light & dark) ===")
        res = subprocess.run([sys.executable, str(REPO / "scripts" / "capture_screens.py"), "--help-shots"])
        if res.returncode != 0:
            failed.append(("capture_screens.py --help-shots", f"exited with code {res.returncode}"))
        else:
            captured.extend(sorted(HELP_DIR.glob("*.webp")))

        if SITE_REPO.is_dir():
            print("\n=== Step 2/4: Capturing marketing website primary screenshots (light & dark) ===")
            res = subprocess.run([sys.executable, str(REPO / "scripts" / "capture_screens.py")])
            if res.returncode != 0:
                failed.append(("capture_screens.py (website slots)", f"exited with code {res.returncode}"))
            else:
                captured.extend(sorted(SITE_SCREENS_DIR.glob("*@2x.png")))

    # Step 3: Capture all application routes, menus, and dialogs.
    print("\n=== Capturing application route, menu, and dialog screenshots ===")
    home = Path(tempfile.mkdtemp(prefix="reelay-all-routes-"))
    try:
        os.environ.update(
            REELAY_HOME=str(home),
            REELAY_REDUCE_MOTION="1",
            QT_QPA_PLATFORM="offscreen",
            QT_SCALE_FACTOR=str(SCALE),
        )
        clips = make_clips(home / "clips", ffmpeg)
        route_paths, route_failures = run_capture("dark", home / "dark", clips, out_dir, ffmpeg)
        captured.extend(route_paths)
        failed.extend(route_failures)
    finally:
        shutil.rmtree(home, ignore_errors=True)

    # Step 4: Synchronize marketing website assets (Help Center WebPs + responsive web WebPs)
    if not args.routes_only and SITE_REPO.is_dir():
        print("\n=== Step 4/4: Synchronizing marketing website screenshots & derivatives ===")
        node = shutil.which("node")
        if not node:
            failed.append(("website sync", "node not found on PATH"))
        else:
            sync_help = subprocess.run([node, "scripts/sync-help.mjs", str(REPO)], cwd=str(SITE_REPO))
            if sync_help.returncode != 0:
                failed.append(("scripts/sync-help.mjs", f"exited with code {sync_help.returncode}"))
            else:
                captured.extend(sorted(SITE_HELP_DIR.glob("*.webp")))

            manifest_run = subprocess.run([node, "scripts/screens-manifest.mjs"], cwd=str(SITE_REPO))
            if manifest_run.returncode != 0:
                failed.append(("scripts/screens-manifest.mjs", f"exited with code {manifest_run.returncode}"))

            web_run = subprocess.run([node, "scripts/screens-web.mjs"], cwd=str(SITE_REPO))
            if web_run.returncode != 0:
                failed.append(("scripts/screens-web.mjs", f"exited with code {web_run.returncode}"))
            else:
                captured.extend(sorted(SITE_WEB_DIR.glob("*.webp")))

    # Deduplicate captured list while preserving order
    unique_captured: list[Path] = []
    seen: set[Path] = set()
    for p in captured:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            unique_captured.append(rp)

    # Validate every output file
    valid_captured: list[Path] = []
    for p in unique_captured:
        ok, detail = validate_image(p)
        if ok:
            valid_captured.append(p)
        else:
            failed.append((str(p), detail))

    overwritten = [p for p in valid_captured if p in pre_existing]
    created = [p for p in valid_captured if p not in pre_existing]

    print("\n" + "=" * 72)
    print("REELAY SCREENSHOT CAPTURE SUMMARY")
    print("=" * 72)
    print(f"Total valid screenshots captured/synced : {len(valid_captured)}")
    print(f"Existing screenshots overwritten        : {len(overwritten)}")
    print(f"New screenshots created                 : {len(created)}")
    print(f"Screenshots failed                      : {len(failed)}")
    print(f"Obsolete screenshots for manual review  : {len(OBSOLETE_SCREENSHOTS)}")

    if created:
        print("\nNew screenshots created:")
        for p in created:
            print(f"  + {p}")

    if failed:
        print("\nFailures:")
        for name, reason in failed:
            print(f"  ! {name}: {reason}")

    if OBSOLETE_SCREENSHOTS:
        print("\nObsolete screenshots requiring manual review (preserved, not deleted):")
        for obs_path, reason in OBSOLETE_SCREENSHOTS:
            status = "present" if obs_path.exists() else "absent"
            print(f"  ? {obs_path} ({status}): {reason}")

    print("=" * 72)
    return 1 if failed else 0


def run_capture(
    theme: str,
    home: Path,
    clips: list[Path],
    out_dir: Path,
    ffmpeg: str,
) -> tuple[list[Path], list[tuple[str, str]]]:
    from PySide6.QtCore import QBuffer, QIODevice, QPoint, QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPixmap
    from PySide6.QtWidgets import QApplication, QDialog, QFrame, QScrollArea

    app = QApplication.instance() or QApplication(sys.argv)
    from reelay import handoff, library, seo
    from reelay import rules as R
    from reelay.gui import branding
    from reelay.gui.app import create_window
    from reelay.gui.approvals_dialog import ApprovalsDialog
    from reelay.gui.assist_dialogs import GrammarDialog, RewriteDialog, SEODialog
    from reelay.gui.automations_page import RuleDialog
    from reelay.gui.board_tools import ArchivedDialog, ColumnsDialog, ImportDialog
    from reelay.gui.dialogs import RenamePreviewDialog
    from reelay.gui.handoff_dialog import HandoffDialog
    from reelay.gui.profile_dialog import ProfileDialog
    from reelay.gui.setup_dialogs import YouTubeSetupWizard
    from reelay.gui.ticket_widgets import RevisionDialog
    from reelay.gui.video_list_dialog import VideoListDialog
    from reelay.gui.video_widgets import SoundDialog
    from reelay.models import (
        Account,
        Dispatch,
        DispatchStatus,
        ManualPost,
        Post,
        PostStatus,
        Source,
        Stage,
        VideoItem,
    )
    from reelay.paths import db_path
    from reelay.pipeline import utc_iso
    from reelay.scoped_pipeline import request_approval
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

    # Connected accounts (created before posts so YouTube account_id is ready)
    accounts = [
        ("youtube", "UCjuniperlane0000000000", "Juniper Lane", "@juniperlane"),
        ("tiktok", "jl-open-id", "Juniper Lane", "@juniperlane"),
        ("threads", "1784140000000", "juniperlane", "@juniperlane"),
        ("bluesky", "did:plc:juniperlane", "juniperlane.bsky.social", "@juniperlane.bsky.social"),
    ]
    saved = {}
    for platform, ext, name, handle in accounts:
        acc = saved[platform] = store.save_account(Account(
            profile_id=prof.id, platform=platform, external_id=ext,
            display_name=name, handle=handle,
        ))
        secret = (Tokens("demo", "demo", 9e12).to_json() if platform == "youtube" else
                  json.dumps({"access_token": "demo", "user_id": ext, "handle": handle, "app_password": "demo"}))
        tokens.set(acc.token_key, secret)

    # Demo videos (refresh download status so VideoListDialog and Library see all 10 as downloaded)
    items = []
    for n, (title, caption, seconds, _colours, _stage) in enumerate(VIDEOS):
        name = f"TikTok_202610{12 - n:02d}_73{n:02d}.mp4"
        shutil.copy(clips[n], lib / name)
        item = VideoItem(
            platform="TikTok", video_id=f"73{n:02d}9", url=f"https://www.tiktok.com/@juniperlane/video/73{n:02d}9",
            title=title, description=caption, duration=seconds, width=1080, height=1920,
            upload_date=f"202610{12 - n:02d}", uploader="juniperlane", filename=name,
            folder=str(lib), view_count=[18400, 9210, 12100, 4877, 31600, 7300, 22500, 15800, 6100, 11300][n],
        )
        item.refresh_download_status(lib)
        items.append(item)
    ids = store.save_videos(prof.id, items)
    rows = [ids[i.key] for i in items]

    for row, (_t, _caption, _s, _c, stage) in zip(rows, VIDEOS):
        store.set_stage([row], stage)

    # Save initial posts with connected YouTube channel assigned
    store.save_post(Post(
        video_row=rows[0], account_id=saved["youtube"].id,
        title=VIDEOS[0][0], description=VIDEOS[0][1],
        target_platforms=["youtube", "tiktok", "instagram", "threads"],
        tiktok_text=VIDEOS[0][1], instagram_text=VIDEOS[0][1],
    ))
    store.save_post(Post(
        video_row=rows[1], account_id=saved["youtube"].id,
        title=VIDEOS[1][0], description=VIDEOS[1][1],
        target_platforms=["youtube", "tiktok"],
        tiktok_text=VIDEOS[1][1],
    ))
    store.save_post(Post(
        video_row=rows[2], account_id=saved["youtube"].id,
        title=VIDEOS[2][0], description=VIDEOS[2][1],
        target_platforms=["youtube", "tiktok"],
        tiktok_text=VIDEOS[2][1],
    ))

    # Schedule dispatches on rows[2] and rows[3] so Scheduled column shows live schedule times
    # and the sidebar displays the "Up next" card.
    soon = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(days=2, hours=3)
    for n, row in enumerate((rows[2], rows[3])):
        when = utc_iso(soon + timedelta(days=2 * n))
        post = store.get_post(row) or Post(
            video_row=row, title=VIDEOS[3][0], description=VIDEOS[3][1],
            target_platforms=["youtube"],
        )
        post.account_id, post.publish_at = saved["youtube"].id, when
        post.status, post.remote_id = PostStatus.SCHEDULED, f"jl-demo-{n}"
        [sent] = store.schedule_post(
            post,
            [Dispatch(video_row=row, platform="youtube", due_at=when, publish_at=when, account_id=saved["youtube"].id)],
            Stage.SCHEDULED,
        )
        store.finish_dispatch(sent.id, DispatchStatus.SCHEDULED, remote_id=f"jl-demo-{n}")
    store.add_manual_post(ManualPost(
        video_row=rows[2], platform="tiktok", posted_at=utc_iso(soon),
        kind="scheduled", note="Scheduled in TikTok",
    ))

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
    window.board.sync = lambda force=False: None  # demo scheduled posts are offline; never query YouTube
    window.accounts.set_oauth_client(OAuthClient("demo.apps.googleusercontent.com", "demo"))
    window.menuBar().hide()
    window.resize(WIDTH, HEIGHT)
    window.show()

    # Record realistic metadata edits on rows[0] so Change History and Details & Provenance are populated
    window.board.pipeline.update_metadata(rows[0], {
        "description": "The last ten minutes of light on the Big Sur coast. Shot handheld from the turnout at mile 45.",
        "threads_text": "Golden hour at Big Sur.",
        "topic": "California Coast",
        "transcript": "The sun drops behind the ridge at mile forty-five, and for ten minutes the whole coast turns gold.",
    })
    window.board.reload()

    saved_paths: list[Path] = []
    failures: list[tuple[str, str]] = []

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
        if not image.save(str(path)):
            failures.append((f"{name}.png", "failed to save QImage"))
            print(f"  ! Failed to save {name}.png", file=sys.stderr)
            return
        ok, detail = validate_image(path, WIDTH * SCALE, HEIGHT * SCALE)
        if not ok:
            failures.append((f"{name}.png", detail))
            print(f"  ! Invalid image {name}.png: {detail}", file=sys.stderr)
            return
        saved_paths.append(path)
        print(f"  ✓ Saved {name}.png ({detail})")

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

    # Video List / Manifest Dialog (populated with all 10 downloaded videos)
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

    # Import Sheet Dialog (populated with sample CSV preview & mapped columns)
    sheet_dlg = ImportDialog(board.pipeline, window.profile, window)
    sheet_dlg._loaded(
        ["Video ID", "Title", "TikTok caption", "Topic"],
        [
            ["73009", "Golden hour at Big Sur", "The last ten minutes of light on the Big Sur coast. #bigsur #goldenhour", "California Coast"],
            ["73019", "Tokyo after midnight", "Tokyo after midnight is a different city. #tokyo #nightwalk", "City Walks"],
            ["73029", "Desert road trip, day four", "Day four. No signal, no plan, no complaints. #roadtrip #desert", "Southwest"],
        ],
        "october_content_plan.csv",
    )
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
    board.pipeline.archive([rows[9]], archived=False)
    board.reload()
    settle(0.5)

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

    # Assist Menu (QToolButton on CopyBlock)
    assist_btn = panel.blocks["description"].assist_btn
    if assist_btn and assist_btn.menu():
        save_pixmap("13_ticket_assist_menu_open", with_menu(assist_btn.menu(), assist_btn))
    else:
        failures.append(("13_ticket_assist_menu_open.png", "assist_btn or menu not found"))

    # Assist: Spelling Dialog
    grammar_dlg = GrammarDialog(VIDEOS[0][1], store, "TikTok Caption", window, check=lambda t: ([], "Built-in dictionary"))
    save_pixmap("14_assist_spelling_grammar_dialog", with_dialog(grammar_dlg, 560, 460))

    # Assist: Local AI Rewrite Dialog (with offline mock rewriter showing a completed suggestion)
    class _DemoRewriter:
        def status(self) -> tuple[bool, str]:
            return True, "Ollama · llama3.2:3b (local, on this Mac)"

        def rewrite(self, text: str, mode: str, limit: int | None = None) -> str:
            return "Ten minutes of golden light on the Big Sur coast, shot handheld from the mile 45 turnout. #bigsur #goldenhour #roadtrip"

    rewrite_dlg = RewriteDialog(VIDEOS[0][1], store, "Instagram Caption", 2200, len, window, rewriter=_DemoRewriter())
    rewrite_dlg._done("Ten minutes of golden light on the Big Sur coast, shot handheld from the mile 45 turnout. #bigsur #goldenhour #roadtrip")
    save_pixmap("15_assist_rewrite_ai_dialog", with_dialog(rewrite_dlg, 640, 520))

    # Assist: YouTube SEO Dialog (populated with offline SEO score & keyword ideas)
    demo_keywords = [
        "big sur coast",
        "big sur golden hour",
        "pacific coast highway",
        "california road trip",
        "big sur sunset",
        "highway 1 turnout",
    ]
    demo_tags = ["bigsur", "goldenhour", "roadtrip", "california", "pacific coast highway"]
    seo_dlg = SEODialog(VIDEOS[0][0], demo_tags, VIDEOS[0][1], ["big sur coast"], window)
    seo_dlg._show(seo.score(
        VIDEOS[0][0],
        demo_tags,
        "The last ten minutes of light on the Big Sur coast. Shot handheld from the turnout at mile 45.",
        demo_keywords,
    ))
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

    # Change History Dialog (populated with saved user edits)
    revs = store.revisions(rows[0])
    rev_dlg = RevisionDialog(revs, window, on_restore=lambda _rid: None)
    if rev_dlg.tree.topLevelItemCount():
        rev_dlg.tree.setCurrentItem(rev_dlg.tree.topLevelItem(0))
    save_pixmap("19_ticket_change_history_dialog", with_dialog(rev_dlg, 760, 440))

    # Prepare to Publish Handoff (TikTok)
    vrow = store.get_video(rows[0])
    post, _saved = board.pipeline.post_or_default(vrow)
    handoff_dlg = HandoffDialog(handoff.prepare(post, "tiktok", board.pipeline.video_path(vrow), vrow.item.duration), window)
    save_pixmap("20_handoff_prepare_to_publish_tiktok", with_dialog(handoff_dlg, 580, 670))

    # Video Tools expanded + Per-platform Sound Dialog (new UI feature)
    panel.video_section.set_expanded(True)
    settle(0.5)
    scroll_to(panel.video_section, 16)
    demo_audio = lib / "coastal_ambient_loop.m4a"
    subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
        "-c:a", "aac", "-y", str(demo_audio),
    ], check=True)
    sound_dlg = SoundDialog(
        window,
        str(lib),
        title="Different Sound for TikTok",
        note="TikTok posts with this sound; every other platform keeps its own. The original file isn't changed: "
             "Reelay makes a copy for TikTok only. Trending sounds from TikTok's library can't be added by any app: "
             "add those in TikTok when you post it (⋯ → Publish…).",
        ok_text="Use This Sound",
    )
    sound_dlg.preset(demo_audio, 0.25, 1.0)
    save_pixmap("35_ticket_video_sound_dialog", with_dialog(sound_dlg, 520, 440))
    panel.video_section.set_expanded(False)

    board.close_inspector()
    settle(0.5)

    # AI Agent Approvals Dialog (new UI feature)
    approval_req = request_approval(
        store,
        prof.id,
        "schedule",
        {"row_id": rows[0], "publish_at": utc_iso(soon), "platforms": ["youtube", "tiktok"]},
        [rows[0]],
        requested_by="agent",
    )
    board.reload()
    settle(0.4)
    approvals_dlg = ApprovalsDialog(board.pipeline, window.profile, window)
    save_pixmap("36_board_approvals_dialog", with_dialog(approvals_dlg, 720, 460))
    store.transition_approval(approval_req.approval_id, "rejected", decided_by="user")
    board.reload()

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

    # Automations Empty State with 3-step onboarding & Quick-start Recipes
    R.save(store, window.profile.id, [])
    store.set_setting(R.LOG_KEY.format(pid=window.profile.id), "[]")
    auto.refresh()
    settle(0.8)
    save_pixmap("34_automations_empty_recipes", window.grab())
    R.save(store, window.profile.id, [loaded_rule])
    R._log(store, window.profile.id, [{"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "rule": loaded_rule.name,
                                       "video": rows[0], "title": VIDEOS[0][0],
                                       "done": ["filled TikTok caption", "platforms: YouTube, TikTok, Instagram, Threads"]}])
    auto.refresh()

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

    # Scrolled: Appearance (Themes & Tips) at the top of Settings
    theme_btn = next(iter(settings.theme_buttons.values()))
    scroll_to(theme_btn, 100)
    save_pixmap("27_settings_appearance_themes_tips", window.grab())

    # Scrolled: Automatic Posting switches
    posting_box = next(iter(settings.flag_boxes.values()))
    scroll_to(posting_box, 120)
    save_pixmap("28_settings_automatic_posting_switches", window.grab())

    # Scrolled: AI Agents Card (hide Data card below so temporary folder paths are never shown)
    area = settings.findChild(QScrollArea)
    card = settings.copy_token_btn
    while card is not None and not (isinstance(card, QFrame) and card.objectName() == "Card"):
        card = card.parentWidget()
    hidden_widgets = []
    if area is not None and card is not None:
        holder = card.parentWidget().layout()
        index = holder.indexOf(card)
        for i in range(index + 1, holder.count()):
            w = holder.itemAt(i).widget()
            if w is not None and w.isVisible():
                w.hide()
                hidden_widgets.append(w)
        settle(0.3)
        area.verticalScrollBar().setValue(card.mapTo(area.widget(), QPoint(0, 0)).y() - 16)
    settle(0.8)
    save_pixmap("26_settings_ai_agents_api", window.grab())
    for w in hidden_widgets:
        w.show()

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
    print(f"Done capturing {len(saved_paths)} screen views.")
    return saved_paths, failures


if __name__ == "__main__":
    sys.exit(main())

