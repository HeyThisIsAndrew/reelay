#!/usr/bin/env python3
"""
reelay.cli — command-line version of Reelay.  Run: python -m reelay.cli URL

Bulk-downloads a creator's video archive (TikTok, Instagram, YouTube) with
standardized filenames, per-video metadata, and a manifest.md review table.
Shares its engine (the `reelay` package) with the desktop app.

Re-running is safe: videos already on disk are skipped and custom names are kept.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from yt_dlp.utils import DownloadError, ExtractorError

from reelay.downloader import QUALITY_720, QUALITY_BEST, download_item
from reelay.fetcher import list_videos
from reelay.library import Library
from reelay.naming import DEFAULT_TEMPLATE, render_stem
from reelay.ytdl import base_options, find_ffmpeg, friendly_error

log = logging.getLogger("reelay")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Bulk-download a creator's TikTok / Instagram / YouTube videos "
                    "with standardized filenames and a Markdown review manifest.",
    )
    p.add_argument("urls", nargs="*", help="Profile, playlist, or single-video URL(s).")
    p.add_argument("-a", "--batch-file", type=Path,
                   help="Text file with one URL per line (# comments allowed).")
    p.add_argument("-o", "--output-dir", type=Path, default=Path("downloads"),
                   help="Where videos, metadata/ and manifest.md go (default: ./downloads).")
    p.add_argument("--quality", choices=[QUALITY_BEST, QUALITY_720], default=QUALITY_BEST,
                   help="best = highest available (default); 720p = smaller files.")
    p.add_argument("--template", default=DEFAULT_TEMPLATE,
                   help="Filename template (default: %(default)s). Tokens: {platform} {date} "
                        "{ymd} {id} {custom} {title} {uploader} {n}")
    p.add_argument("--cookies-from-browser", metavar="BROWSER",
                   help="Load login cookies from a browser (chrome, safari, firefox…). "
                        "Usually required for Instagram.")
    p.add_argument("--cookies", type=Path, metavar="FILE", help="Netscape-format cookies.txt file.")
    p.add_argument("--limit", type=int, metavar="N", help="Only the N newest videos per URL.")
    p.add_argument("--sleep", type=float, default=2.0, metavar="SECONDS",
                   help="Pause between videos to avoid rate limits (default: 2).")
    p.add_argument("--manifest-only", action="store_true",
                   help="Skip downloading; just rebuild manifest.md from existing metadata.")
    p.add_argument("-v", "--verbose", action="store_true", help="Show yt-dlp debug output.")
    args = p.parse_args(argv)

    if args.batch_file:
        if not args.batch_file.is_file():
            p.error(f"batch file not found: {args.batch_file}")
        for line in args.batch_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                args.urls.append(line)
    if not args.urls and not args.manifest_only:
        p.error("provide at least one URL (or --batch-file / --manifest-only)")
    return args


def run(args: argparse.Namespace) -> int:
    library = Library(args.output_dir.resolve())
    library.ensure()
    existing = {i.key: i for i in library.load()}
    custom_names = library.load_custom_names()
    failures: list[tuple[str, str]] = []
    downloaded = skipped = 0
    interrupted = False

    if not args.manifest_only:
        if not find_ffmpeg():
            log.warning("ffmpeg not found: merging video/audio and MP4 remuxing will fail.")
        opts = base_options(args.cookies_from_browser, args.cookies)

        queue = []
        for source in args.urls:
            log.info("Scanning %s ...", source)
            try:
                _, found = list_videos(source, opts, args.limit)
            except (DownloadError, ExtractorError) as exc:
                reason = friendly_error(exc, source)
                log.error("  Could not read %s: %s", source, reason)
                failures.append((source, reason))
                continue
            log.info("  Found %d video(s)", len(found))
            queue.extend(found)

        total = len(queue)
        try:
            for n, item in enumerate(queue, 1):
                prefix = f"[{n}/{total}]"
                if item.key in existing:
                    log.info("%s Already downloaded, skipping %s", prefix, item.url)
                    skipped += 1
                    continue
                item.custom_name = custom_names.get(item.key, "")
                log.info("%s Downloading %s", prefix, item.url)
                try:
                    done = download_item(item, library.folder, args.quality, opts,
                                         name_for=lambda i, n=n: render_stem(args.template, i, n))
                except (DownloadError, ExtractorError) as exc:
                    reason = friendly_error(exc, item.url)
                    log.error("%s FAILED: %s", prefix, reason)
                    failures.append((item.url, reason))
                except Exception as exc:  # noqa: BLE001 — one video must never kill the batch
                    log.exception("%s Unexpected error", prefix)
                    failures.append((item.url, f"Unexpected error: {exc}"))
                else:
                    library.save(done)
                    existing[done.key] = done
                    downloaded += 1
                    log.info("%s Saved %s", prefix, done.filename)
                if args.sleep and n < total:
                    time.sleep(args.sleep)
        except KeyboardInterrupt:
            interrupted = True
            log.warning("\nInterrupted. Writing manifest for what's downloaded so far.")

    library.write_manifest(library.load(), args.urls, failures)
    log.info("\nDone. Downloaded: %d | Skipped (already had): %d | Failed: %d",
             downloaded, skipped, len(failures))
    log.info("Manifest: %s", library.manifest_path)
    if interrupted:
        return 130
    return 2 if failures else 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
