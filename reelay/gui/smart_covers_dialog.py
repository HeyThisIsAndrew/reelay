"""Smart Covers Dialog: Pick frame, cut out subject, and composite branded 16:9 & 9:16 covers (Issues #10 & #11)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..covers import (
    CANVAS_SIZES,
    composite_smart_cover,
    extract_frame,
    generate_smart_cover_variants,
    segment_subject,
    suggest_frames,
)
from .theme import set_prop
from .tips import tip
from .widgets import label


class SmartCoversDialog(QDialog):
    """Local-first Smart Covers creator: extract frames, isolate subjects, composite safe-area covers."""

    def __init__(self, video_path: str | Path, ticket_title: str = "", brand_color: str = "#3182CE", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Smart Covers")
        self.resize(780, 620)
        self.video_path = Path(video_path)
        self.ticket_title = ticket_title
        self.brand_color = brand_color
        self.selected_cover_path: str | None = None

        self.current_frame_path: Path | None = None
        self.current_cutout_path: Path | None = None
        self.variants: dict[str, list[dict[str, Any]]] = {"16:9": [], "9:16": []}

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 14)
        lay.setSpacing(12)

        # Header
        lay.addWidget(label("Smart Covers (Local AI on this Mac)", name="PageTitle"))
        lay.addWidget(
            label(
                "Pick a frame from your video, isolate the subject, and composite 16:9 (YouTube) "
                "or 9:16 (Shorts/Reels/TikTok) covers. All processing runs entirely on this computer; "
                "your video never leaves your Mac.",
                "muted",
            )
        )

        # Step 1: Frame selection row
        frame_box = QFrame()
        frame_box.setStyleSheet("background-color: #1a1a20; border: 1px solid #2e2e3a; border-radius: 8px; padding: 10px;")
        fbl = QVBoxLayout(frame_box)
        fbl.setContentsMargins(10, 8, 10, 8)
        fbl.setSpacing(8)

        fbl.addWidget(label("1. CHOOSE A FRAME", name="SectionTitle"))

        ctl_row = QHBoxLayout()
        ctl_row.setSpacing(10)
        ctl_row.addWidget(label("Time (s):"))
        self.time_spin = QDoubleSpinBox()
        self.time_spin.setRange(0.0, 3600.0)
        self.time_spin.setSingleStep(0.5)
        self.time_spin.setValue(1.0)
        ctl_row.addWidget(self.time_spin)

        self.extract_btn = tip(QPushButton("Use This Frame"), "Extract exact frame at the specified timestamp")
        set_prop(self.extract_btn, "primary")
        self.extract_btn.clicked.connect(self._extract_current_frame)
        ctl_row.addWidget(self.extract_btn)

        self.suggest_btn = tip(QPushButton("Suggest Candidates"), "Find candidate frames across the video")
        self.suggest_btn.clicked.connect(self._suggest_candidates)
        ctl_row.addWidget(self.suggest_btn)
        ctl_row.addStretch()
        fbl.addLayout(ctl_row)

        # Candidate chips row
        self.candidate_layout = QHBoxLayout()
        self.candidate_layout.setSpacing(6)
        fbl.addLayout(self.candidate_layout)

        lay.addWidget(frame_box)

        # Step 2: Previews and Make Covers
        mid_layout = QHBoxLayout()
        mid_layout.setSpacing(14)

        # Left: Frame & Cutout Preview
        prev_card = QFrame()
        prev_card.setFixedWidth(280)
        prev_card.setStyleSheet("background-color: #16161c; border: 1px solid #292934; border-radius: 6px; padding: 8px;")
        pcl = QVBoxLayout(prev_card)
        pcl.setSpacing(8)
        pcl.addWidget(label("Subject Cutout Preview", "muted"))

        self.preview_label = QLabel("No frame chosen")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setFixedSize(260, 160)
        self.preview_label.setStyleSheet("background-color: #0d0d11; border: 1px dashed #3a3a48; border-radius: 4px; color: #777788;")
        pcl.addWidget(self.preview_label)

        self.cutout_btn = tip(QPushButton("Find Subject (Cutout)"), "Isolate subject on a transparent background")
        self.cutout_btn.setEnabled(False)
        self.cutout_btn.clicked.connect(self._run_cutout)
        pcl.addWidget(self.cutout_btn)

        self.make_covers_btn = tip(QPushButton("Make Branded Covers"), "Generate 16:9 and 9:16 covers inside safe areas")
        set_prop(self.make_covers_btn, "primary")
        self.make_covers_btn.setEnabled(False)
        self.make_covers_btn.clicked.connect(self._make_covers)
        pcl.addWidget(self.make_covers_btn)
        pcl.addStretch()
        mid_layout.addWidget(prev_card)

        # Right: Generated Covers Gallery
        self.tabs = QTabWidget()
        self.tab_16_9 = QWidget()
        self.tab_9_16 = QWidget()

        self.grid_16_9 = QGridLayout(self.tab_16_9)
        self.grid_16_9.setSpacing(8)
        self.grid_9_16 = QGridLayout(self.tab_9_16)
        self.grid_9_16.setSpacing(8)

        self.tabs.addTab(self.tab_16_9, "16:9 YouTube (1280×720)")
        self.tabs.addTab(self.tab_9_16, "9:16 Shorts / TikTok (1080×1920)")
        mid_layout.addWidget(self.tabs, 1)

        lay.addLayout(mid_layout, 1)

        # Footer
        foot = QHBoxLayout()
        self.status_lbl = label("", "muted")
        foot.addWidget(self.status_lbl)
        foot.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        foot.addWidget(cancel_btn)

        self.apply_btn = tip(QPushButton("Use as Cover"), "Set this cover for the ticket (undoable)")
        set_prop(self.apply_btn, "primary")
        self.apply_btn.setEnabled(False)
        self.apply_btn.clicked.connect(self._apply_chosen_cover)
        foot.addWidget(self.apply_btn)

        lay.addLayout(foot)

        # Initial extraction
        self._extract_current_frame()
        self._suggest_candidates()

    def _extract_current_frame(self) -> None:
        ts = self.time_spin.value()
        cache_dir = self.video_path.parent / ".covers_cache"
        self.current_frame_path = extract_frame(self.video_path, ts, cache_dir / f"frame_{int(ts*1000)}.png")
        self._show_pixmap(self.current_frame_path)
        self.cutout_btn.setEnabled(True)
        self.make_covers_btn.setEnabled(True)
        self.status_lbl.setText(f"Extracted frame at {ts:.2f}s")

    def _suggest_candidates(self) -> None:
        # Clear existing chips
        while self.candidate_layout.count():
            item = self.candidate_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        candidates = suggest_frames(self.video_path, count=5)
        for ts in candidates:
            mins, secs = int(ts // 60), ts % 60
            chip = QPushButton(f"{mins}:{secs:04.1f}")
            chip.setStyleSheet("padding: 3px 8px; font-size: 11px;")
            chip.clicked.connect(lambda _, t=ts: self._jump_to_candidate(t))
            self.candidate_layout.addWidget(chip)
        self.candidate_layout.addStretch()

    def _jump_to_candidate(self, ts: float) -> None:
        self.time_spin.setValue(ts)
        self._extract_current_frame()

    def _run_cutout(self) -> None:
        if not self.current_frame_path:
            return
        cache_dir = self.video_path.parent / ".covers_cache"
        self.current_cutout_path = segment_subject(self.current_frame_path, cache_dir / "cutout.png")
        self._show_pixmap(self.current_cutout_path)
        self.status_lbl.setText("Subject isolated with alpha transparency.")

    def _show_pixmap(self, path: Path | str) -> None:
        pix = QPixmap(str(path))
        if not pix.isNull():
            scaled = pix.scaled(self.preview_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.preview_label.setPixmap(scaled)

    def _make_covers(self) -> None:
        if not self.current_frame_path:
            return
        if not self.current_cutout_path:
            self._run_cutout()

        cache_dir = self.video_path.parent / ".covers_cache"
        self.variants = generate_smart_cover_variants(
            video_path=self.video_path,
            timestamp_s=self.time_spin.value(),
            title=self.ticket_title,
            output_dir=cache_dir,
            brand_color=self.brand_color,
        )

        self._populate_gallery()
        self.status_lbl.setText("Generated 4 covers for 16:9 and 4 covers for 9:16 within platform safe areas.")

    def _populate_gallery(self) -> None:
        # Clear existing gallery widgets
        for grid in (self.grid_16_9, self.grid_9_16):
            while grid.count():
                item = grid.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()

        # 16:9
        for idx, var in enumerate(self.variants.get("16:9", [])):
            card = self._create_variant_card(var, thumb_w=180, thumb_h=100)
            row, col = idx // 2, idx % 2
            self.grid_16_9.addWidget(card, row, col)

        # 9:16
        for idx, var in enumerate(self.variants.get("9:16", [])):
            card = self._create_variant_card(var, thumb_w=100, thumb_h=160)
            row, col = idx // 2, idx % 2
            self.grid_9_16.addWidget(card, row, col)

    def _create_variant_card(self, var: dict[str, Any], thumb_w: int, thumb_h: int) -> QWidget:
        card = QFrame()
        card.setStyleSheet("QFrame { background-color: #1a1a22; border: 1px solid #333342; border-radius: 6px; padding: 6px; } QFrame:hover { border: 1px solid #4a90e2; }")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(6, 6, 6, 6)
        cl.setSpacing(4)

        img_lbl = QLabel()
        img_lbl.setAlignment(Qt.AlignCenter)
        img_lbl.setFixedSize(thumb_w, thumb_h)
        pix = QPixmap(var["path"])
        if not pix.isNull():
            img_lbl.setPixmap(pix.scaled(thumb_w, thumb_h, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        cl.addWidget(img_lbl)

        name_lbl = QLabel(var["name"])
        name_lbl.setAlignment(Qt.AlignCenter)
        name_lbl.setStyleSheet("font-size: 11px; font-weight: bold; color: #d0d0e0;")
        cl.addWidget(name_lbl)

        btn = QPushButton("Select")
        btn.setStyleSheet("padding: 2px 8px; font-size: 11px;")
        btn.clicked.connect(lambda _, p=var["path"]: self._select_variant(p, card))
        cl.addWidget(btn)

        return card

    def _select_variant(self, path: str, card: QWidget) -> None:
        self.selected_cover_path = path
        self.apply_btn.setEnabled(True)
        self.status_lbl.setText(f"Selected: {Path(path).name}")

    def _apply_chosen_cover(self) -> None:
        if self.selected_cover_path:
            self.accept()
