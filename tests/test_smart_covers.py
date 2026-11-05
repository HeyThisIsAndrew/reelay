"""Tests for Smart Covers: frame selection, subject cutout, platform safe areas, and compositing (Issues #10 & #11)."""

from __future__ import annotations

from pathlib import Path
import pytest
from PySide6.QtGui import QColor, QImage

from reelay.covers import (
    CANVAS_SIZES,
    SAFE_MARGINS,
    composite_smart_cover,
    extract_frame,
    generate_smart_cover_variants,
    segment_subject,
    suggest_frames,
)


def _make_dummy_frame(path: Path, w: int = 1280, h: int = 720) -> Path:
    img = QImage(w, h, QImage.Format_RGB32)
    img.fill(QColor("#2B6CB0"))
    assert img.save(str(path), "PNG")
    return path


def test_suggest_frames_distribution():
    # 60s video -> 5 candidate timestamps distributed across duration
    candidates = suggest_frames("dummy.mp4", duration_s=60.0, count=5)
    assert len(candidates) == 5
    assert all(0.0 <= t <= 60.0 for t in candidates)
    # Check increasing order
    assert candidates == sorted(candidates)
    # Avoid extreme ends (0s and 60s)
    assert candidates[0] > 0.0
    assert candidates[-1] < 60.0


def test_extract_frame_fallback(qtbot, tmp_path):
    frame_path = tmp_path / "extracted_frame.png"
    result = extract_frame(tmp_path / "nonexistent.mp4", timestamp_s=5.0, out_path=frame_path)
    assert result.is_file()
    img = QImage(str(result))
    assert not img.isNull()
    assert img.width() == 1280
    assert img.height() == 720


def test_segment_subject_creates_alpha_transparency(qtbot, tmp_path):
    base_frame = _make_dummy_frame(tmp_path / "base.png")
    cutout_path = tmp_path / "cutout.png"

    res = segment_subject(base_frame, out_path=cutout_path)
    assert res.is_file()

    img = QImage(str(res))
    assert not img.isNull()
    assert img.hasAlphaChannel()
    # Check that outer corner pixel has alpha = 0 (transparent background)
    corner_color = img.pixelColor(0, 0)
    assert corner_color.alpha() == 0


def test_composite_smart_cover_16_9_and_9_16(qtbot, tmp_path):
    base_frame = _make_dummy_frame(tmp_path / "frame.png")
    cutout = segment_subject(base_frame, out_path=tmp_path / "cutout.png")

    # Test 16:9 (YouTube)
    out_16_9 = composite_smart_cover(
        cutout_path=cutout,
        aspect_ratio="16:9",
        style="blur",
        frame_path=base_frame,
        title="10 Simple Habits That Changed My Life",
        brand_color="#E53E3E",
        out_path=tmp_path / "cover_16_9.jpg",
    )
    assert out_16_9.is_file()
    img_16_9 = QImage(str(out_16_9))
    assert img_16_9.width() == CANVAS_SIZES["16:9"][0]
    assert img_16_9.height() == CANVAS_SIZES["16:9"][1]

    # Test 9:16 (Shorts/Reels/TikTok)
    out_9_16 = composite_smart_cover(
        cutout_path=cutout,
        aspect_ratio="9:16",
        style="gradient",
        frame_path=base_frame,
        title="Stop doing this right now!",
        brand_color="#3182CE",
        out_path=tmp_path / "cover_9_16.jpg",
    )
    assert out_9_16.is_file()
    img_9_16 = QImage(str(out_9_16))
    assert img_9_16.width() == CANVAS_SIZES["9:16"][0]
    assert img_9_16.height() == CANVAS_SIZES["9:16"][1]


def test_generate_smart_cover_variants(qtbot, tmp_path):
    dummy_video = tmp_path / "clip.mp4"
    dummy_video.write_bytes(b"dummy_video_bytes")

    variants = generate_smart_cover_variants(
        video_path=dummy_video,
        timestamp_s=2.5,
        title="Mastering Creator Workflows",
        output_dir=tmp_path / "variants",
        brand_color="#805AD5",
    )

    assert "16:9" in variants
    assert "9:16" in variants
    assert len(variants["16:9"]) == 4
    assert len(variants["9:16"]) == 4

    for var in variants["16:9"] + variants["9:16"]:
        assert Path(var["path"]).is_file()
        assert var["style"] in ("blur", "gradient", "contrast", "studio")
