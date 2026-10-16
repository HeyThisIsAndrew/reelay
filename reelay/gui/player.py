"""The inspector's video player: watch the local file right where you write its captions.

Built on QtMultimedia (FFmpeg backend). The video is drawn in a QGraphicsView (a QGraphicsVideoItem), so the
large play button can sit on top of the picture; on macOS a QVideoWidget is a native surface that overlays
can't reliably cover.

- Shows the cover until you press play; never autoplays. Space or K plays and pauses, J and L skip 10 s,
  ← and → skip 5 s, M mutes (while the player has focus).
- Releases the file the moment you switch videos, so renaming or moving it still works.
- If the format can't be decoded here (YouTube's VP9 / AV1 above 1080p), offers a preview copy (H.264, made
  in the background) or opening it in QuickTime. The original file is never changed.
- Without QtMultimedia (PySide6-Addons missing) it degrades to the cover plus "Open in QuickTime".
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QRectF, QSize, QSizeF, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (QGraphicsItem, QGraphicsPixmapItem, QGraphicsScene, QGraphicsView, QGridLayout,
                               QHBoxLayout, QLabel, QPushButton, QSlider, QStyle, QStyleOptionSlider, QToolButton,
                               QToolTip, QVBoxLayout, QWidget)

from . import icons, theme
from .theme import set_prop
from .tips import tip

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
    MULTIMEDIA = True
except ImportError:          # PySide6-Essentials only
    MULTIMEDIA = False

OPEN_EXTERNALLY = "Open in QuickTime" if sys.platform == "darwin" else "Open in Video Player"
SPEEDS = (1.0, 1.5, 2.0, 0.5)


def clock(ms: int) -> str:
    s = max(0, int(ms // 1000))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


class _PlayBadge(QGraphicsItem):
    """The large accent play button over the picture."""

    SIZE = 58.0

    def __init__(self, on_click):
        super().__init__()
        self.on_click = on_click
        self.setAcceptedMouseButtons(Qt.LeftButton)
        self.setCursor(Qt.PointingHandCursor)
        self.setZValue(10)

    def boundingRect(self) -> QRectF:  # noqa: N802
        return QRectF(0, 0, self.SIZE, self.SIZE)

    def paint(self, p: QPainter, _option, _widget=None) -> None:
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(theme.P.accent_color))
        p.drawEllipse(self.boundingRect())
        pix = icons.pixmap("play", theme.P.on_accent, 26)
        p.drawPixmap(QRectF(self.SIZE / 2 - 11, self.SIZE / 2 - 13, 26, 26).toRect(), pix)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.on_click()
        event.accept()


class _Stage(QGraphicsView):
    """Black stage that keeps the picture (or cover) fitted and the play button centred."""

    clicked = Signal()

    def __init__(self, scene: QGraphicsScene, layout_items, parent=None):
        super().__init__(scene, parent)
        self._layout_items = layout_items
        self.setFrameShape(QGraphicsView.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setBackgroundBrush(QColor(Qt.black))
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.PointingHandCursor)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.scene().setSceneRect(0, 0, self.width(), self.height())
        self._layout_items()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        item = self.itemAt(event.position().toPoint())
        if isinstance(item, _PlayBadge):
            return super().mousePressEvent(event)
        self.setFocus()
        self.clicked.emit()


class Scrubber(QSlider):
    """Accent progress bar: click anywhere to jump there, hover to see the time."""

    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self.setObjectName("Scrubber")
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setRange(0, 0)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(24, super().sizeHint().height())

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(24, super().minimumSizeHint().height())

    def _value_at(self, x: float) -> int:
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, self)
        return QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), int(x - groove.x()),
                                              max(groove.width(), 1))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.maximum() > 0:
            self.setValue(self._value_at(event.position().x()))
            self.sliderMoved.emit(self.value())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self.maximum() > 0:
            QToolTip.showText(event.globalPosition().toPoint(), clock(self._value_at(event.position().x())), self)
        super().mouseMoveEvent(event)


class PlayerWidget(QWidget):
    """Stage + controls. ``set_video(path, cover)`` loads a file (or shows just the cover)."""

    message = Signal(str)                # plain-language progress for the status line

    def __init__(self, compact: bool = False, parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QSizePolicy
        self.setObjectName("Player")
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        self.path: Path | None = None
        self.original: Path | None = None
        self._started = False
        self._speed = 0
        self._popout: PopoutPlayer | None = None
        self._cover: QPixmap | None = None
        self._workers: list = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.scene = QGraphicsScene(self)
        self.poster = QGraphicsPixmapItem()
        self.poster.setTransformationMode(Qt.SmoothTransformation)
        self.scene.addItem(self.poster)
        self.badge = _PlayBadge(self.toggle)
        self.scene.addItem(self.badge)
        self.video_item = None
        if MULTIMEDIA:
            self.video_item = QGraphicsVideoItem()
            self.video_item.setAspectRatioMode(Qt.KeepAspectRatio)
            self.scene.addItem(self.video_item)
        self.stage = _Stage(self.scene, self._layout_items)
        self.stage.clicked.connect(self.toggle)
        lay.addWidget(self.stage, 1)

        # the line under the picture: problems and their fixes
        self.notice = QWidget(objectName="PlayerNotice")
        self.notice.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        nv = QVBoxLayout(self.notice)
        nv.setContentsMargins(14, 10, 14, 10)
        nv.setSpacing(6)
        self.notice_text = QLabel()
        self.notice_text.setWordWrap(True)
        self.notice_text.setProperty("muted", True)
        self.notice_text.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        self.preview_btn = tip(QPushButton("Make a Preview Copy"),
                               "Make a small H.264 copy that plays here (in the background, cached). The original "
                               "file isn't changed.")
        set_prop(self.preview_btn, "primary")
        self.preview_btn.clicked.connect(self.make_preview)
        self.external_btn = tip(QPushButton(OPEN_EXTERNALLY), "Open the video file in your Mac's video player")
        self.external_btn.clicked.connect(self.open_externally)
        nv.addWidget(self.notice_text)
        nv.addWidget(self.preview_btn)
        nv.addWidget(self.external_btn)
        self.notice.setVisible(False)
        lay.addWidget(self.notice)

        # controls
        bar = QWidget(objectName="PlayerBar")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(6, 4, 6, 6)
        bl.setSpacing(1)
        self.play_btn = self._button("play", "Play or pause (Space)")
        self.play_btn.clicked.connect(self.toggle)
        self.scrubber = Scrubber()
        tip(self.scrubber, "Click to jump; hover to see the time")
        self.time = QLabel("0:00 / 0:00", objectName="PlayerTime")
        self.time.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Preferred)
        self.speed_btn = QToolButton(objectName="PlayerButton")
        self.speed_btn.setText("1×")
        self.speed_btn.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Preferred)
        tip(self.speed_btn, "Playback speed")
        self.speed_btn.clicked.connect(self.cycle_speed)
        self.mute_btn = self._button("volume", "Mute or unmute (M)")
        self.mute_btn.setCheckable(True)
        self.mute_btn.toggled.connect(self.set_muted)
        self.loop_btn = self._button("loop", "Loop the video")
        self.loop_btn.setCheckable(True)
        self.loop_btn.toggled.connect(self._set_loop)
        self.popout_btn = self._button("popout", "Watch in its own window")
        self.popout_btn.clicked.connect(self.pop_out)
        self.open_btn = self._button("external", OPEN_EXTERNALLY)
        self.open_btn.clicked.connect(self.open_externally)
        for w in (self.play_btn, self.scrubber, self.time, self.speed_btn, self.mute_btn, self.loop_btn,
                  self.popout_btn, self.open_btn):
            bl.addWidget(w, 1 if w is self.scrubber else 0)
        if compact:
            self.popout_btn.hide()
        lay.addWidget(bar)

        self.player = None
        if MULTIMEDIA:
            self.audio = QAudioOutput(self)
            self.player = QMediaPlayer(self)
            self.player.setAudioOutput(self.audio)
            self.player.setVideoOutput(self.video_item)
            self.player.positionChanged.connect(self._on_position)
            self.player.durationChanged.connect(self._on_duration)
            self.player.playbackStateChanged.connect(self._on_state)
            self.player.errorOccurred.connect(self._on_error)
            self.player.mediaStatusChanged.connect(self._on_status)
            self.video_item.nativeSizeChanged.connect(lambda _s: self._layout_items())
        self.scrubber.sliderMoved.connect(lambda v: self.player and self.player.setPosition(v))
        for keys, slot in (("Space", self.toggle), ("K", self.toggle), ("J", lambda: self.skip(-10_000)),
                           ("L", lambda: self.skip(10_000)), ("Left", lambda: self.skip(-5_000)),
                           ("Right", lambda: self.skip(5_000)), ("M", lambda: self.mute_btn.toggle())):
            QShortcut(QKeySequence(keys), self, context=Qt.WidgetWithChildrenShortcut).activated.connect(slot)
        self._set_controls_enabled(False)

    def _button(self, name: str, text: str) -> QToolButton:
        b = QToolButton(objectName="PlayerButton")
        b.setIcon(icons.icon(name, 16))
        b.setProperty("icon_name", name)
        b.setIconSize(icons.size(16))
        b.setCursor(Qt.PointingHandCursor)
        tip(b, text)
        return b

    def retint(self) -> None:
        for b in self.findChildren(QToolButton, "PlayerButton"):
            name = b.property("icon_name")
            if name:
                b.setIcon(icons.icon(name, 16))
        self.badge.update()

    # ------------------------------------------------------------ loading
    def set_video(self, path: Path | str | None, cover: QPixmap | None = None,
                  preview: Path | str | None = None) -> None:
        """Show ``cover`` and get ready to play ``path`` (or ``preview``, a playable copy of it)."""
        self.clear()
        self.original = Path(path) if path else None
        playable = Path(preview) if preview else self.original
        self._cover = cover
        self._set_poster(cover)
        if playable is None or not playable.is_file():
            self._notice("Download this video to watch it here." if path is None else
                         "The video file isn't on this computer.", preview=False, external=False)
            return
        self.path = playable
        if self.player is None:
            self._notice("The built-in player isn't installed in this copy of Reelay.", preview=False)
            return
        self.player.setSource(QUrl.fromLocalFile(str(playable)))
        self._set_controls_enabled(True)

    def clear(self) -> None:
        """Stop and let go of the file (so it can be renamed or moved)."""
        if self.player is not None:
            self.player.stop()
            self.player.setSource(QUrl())
        self.path = None
        self._started = False
        self.notice.setVisible(False)
        self.poster.setVisible(True)
        self.badge.setVisible(True)
        self.scrubber.setRange(0, 0)
        self.time.setText("0:00 / 0:00")
        self._set_controls_enabled(False)
        if hasattr(self.parentWidget(), "_sync_stage_height"):
            self.parentWidget()._sync_stage_height()

    def _set_poster(self, cover: QPixmap | None) -> None:
        self.poster.setPixmap(cover if cover is not None and not cover.isNull() else QPixmap())
        self._layout_items()

    def _layout_items(self) -> None:
        w, h = max(self.stage.width(), 1), max(self.stage.height(), 1)
        pix = self.poster.pixmap()
        if not pix.isNull():
            scale = min(w / pix.width(), h / pix.height())
            self.poster.setScale(scale)
            self.poster.setPos((w - pix.width() * scale) / 2, (h - pix.height() * scale) / 2)
        if self.video_item is not None:
            self.video_item.setSize(QSizeF(w, h))
            self.video_item.setPos(0, 0)
        self.badge.setPos((w - _PlayBadge.SIZE) / 2, (h - _PlayBadge.SIZE) / 2)

    def _set_controls_enabled(self, on: bool) -> None:
        for w in (self.play_btn, self.scrubber, self.speed_btn, self.mute_btn, self.loop_btn, self.popout_btn):
            w.setEnabled(on)
        self.open_btn.setEnabled(bool(self.original and self.original.is_file()))
        self.badge.setVisible(on and not self._started)

    def _sync_notice_height(self) -> None:
        if self.notice_text.text():
            w = max((self.notice.width() or self.width() or 320) - 28, 120)
            target = max(0, self.notice_text.heightForWidth(w))
            if self.notice_text.minimumHeight() != target:
                self.notice_text.setMinimumHeight(target)
                if self.notice.layout() is not None:
                    self.notice.layout().activate()
                if self.layout() is not None:
                    self.layout().activate()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._sync_notice_height()

    def _notice(self, text: str, preview: bool = False, external: bool = True) -> None:
        self.notice_text.setText(text)
        self.preview_btn.setVisible(preview)
        self.external_btn.setVisible(external and bool(self.original and self.original.is_file()))
        self.notice.setVisible(True)
        self._sync_notice_height()
        if hasattr(self.parentWidget(), "_sync_stage_height"):
            self.parentWidget()._sync_stage_height()

    # ------------------------------------------------------------ controls
    def toggle(self) -> None:
        if self.player is None or self.path is None:
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def pause(self) -> None:
        if self.player is not None and self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()

    def skip(self, ms: int) -> None:
        if self.player is not None and self.path is not None:
            self.player.setPosition(max(0, min(self.player.duration(), self.player.position() + ms)))

    def cycle_speed(self) -> None:
        self._speed = (self._speed + 1) % len(SPEEDS)
        rate = SPEEDS[self._speed]
        self.speed_btn.setText(f"{rate:g}×")
        if self.player is not None:
            self.player.setPlaybackRate(rate)

    def set_muted(self, muted: bool) -> None:
        self.mute_btn.setIcon(icons.icon("mute" if muted else "volume", 16))
        self.mute_btn.setProperty("icon_name", "mute" if muted else "volume")
        if self.player is not None:
            self.audio.setMuted(muted)

    def _set_loop(self, on: bool) -> None:
        if self.player is not None:
            self.player.setLoops(QMediaPlayer.Infinite if on else 1)

    def open_externally(self) -> None:
        target = self.original or self.path
        if target and target.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def pop_out(self) -> None:
        if self.path is None:
            return
        position = self.player.position() if self.player else 0
        self.pause()
        self._popout = PopoutPlayer(self.path, self.poster.pixmap(), position, self.window())
        self._popout.show()

    # ------------------------------------------------------------ player events
    def _on_position(self, ms: int) -> None:
        if not self.scrubber.isSliderDown():
            self.scrubber.setValue(ms)
        self.time.setText(f"{clock(ms)} / {clock(self.player.duration())}")

    def _on_duration(self, ms: int) -> None:
        self.scrubber.setRange(0, max(ms, 0))
        self.time.setText(f"{clock(self.player.position())} / {clock(ms)}")

    def _on_state(self, state) -> None:
        playing = state == QMediaPlayer.PlayingState
        if playing and not self._started:
            self._started = True
            self.poster.setVisible(False)
        self.badge.setVisible(not playing and self.path is not None)
        name = "pause" if playing else "play"
        self.play_btn.setIcon(icons.icon(name, 16))
        self.play_btn.setProperty("icon_name", name)

    def _on_status(self, status) -> None:
        if status == QMediaPlayer.InvalidMedia:
            self._cant_play()

    def _on_error(self, error, _text: str = "") -> None:
        if error != QMediaPlayer.NoError:
            self._cant_play()

    def _cant_play(self) -> None:
        if self.player is not None:
            self.player.setSource(QUrl())
        self.path = None
        self.poster.setVisible(True)
        self._set_controls_enabled(False)
        self._notice("Reelay can't play this file here: its format isn't supported by the built-in player (often "
                     "4K YouTube videos), or the file is damaged. A preview copy usually fixes it.", preview=True)

    def make_preview(self) -> None:
        """Make an H.264 copy that plays here, in the background; switch to it when it's ready."""
        from ..previews import make_preview
        from .workers import CallWorker
        source = self.original
        if source is None:
            return
        self.preview_busy(True)
        self.message.emit("Making a preview copy in the background…")
        worker = CallWorker(make_preview, str(source), parent=self)

        def ready(result) -> None:
            self.preview_busy(False)
            self.message.emit("Preview copy ready.")
            if self.original == source:
                self.set_video(source, self._cover, result)

        def failed(text: str) -> None:
            self.preview_busy(False)
            self.message.emit(text)

        worker.succeeded.connect(ready)
        worker.failed.connect(failed)
        worker.finished.connect(lambda: worker in self._workers and self._workers.remove(worker))
        self._workers.append(worker)
        worker.start()

    def shutdown(self) -> None:
        self.clear()
        if self._popout is not None:
            self._popout.close()
        for worker in list(self._workers):
            worker.wait(5000)

    def preview_busy(self, busy: bool) -> None:
        self.preview_btn.setEnabled(not busy)
        self.preview_btn.setText("Making a Preview Copy…" if busy else "Make a Preview Copy")

    def hideEvent(self, event) -> None:  # noqa: N802
        self.pause()
        super().hideEvent(event)


class PopoutPlayer(QWidget):
    """The same video in its own window (resizable, can go full screen with the green button)."""

    def __init__(self, path: Path, cover: QPixmap, position: int, parent=None):
        super().__init__(parent, Qt.Window)
        self.setWindowTitle(path.name)
        self.resize(960, 600)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.player = PlayerWidget()
        self.player.popout_btn.hide()
        lay.addWidget(self.player)
        self.player.set_video(path, cover)
        if self.player.player is not None:
            self.player.player.setPosition(position)
            self.player.player.play()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.player.clear()
        super().closeEvent(event)
