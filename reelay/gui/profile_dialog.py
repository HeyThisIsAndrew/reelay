"""New Profile / Edit Profile & Branding: name, tagline, logo and brand colours, with a live preview."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout, QWidget)

from ..models import Profile
from . import branding, theme
from .branding import Brand, BrandError
from .theme import set_prop
from .tips import tip

LOGO_PREVIEW = 88


class ColorField(QWidget):
    """A swatch (opens the colour picker) and a hex field, kept in sync."""

    changed = Signal(str)

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.title = title
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.swatch = QPushButton(objectName="Swatch")
        self.swatch.setCursor(Qt.PointingHandCursor)
        self.swatch.clicked.connect(self._pick)
        tip(self.swatch, f"Pick the {title.lower()} colour")
        self.hex = QLineEdit()
        self.hex.setPlaceholderText("#rrggbb")
        self.hex.setMaxLength(7)
        self.hex.setFixedWidth(112)
        self.hex.editingFinished.connect(lambda: self.set_color(self.hex.text(), emit=True))
        lay.addWidget(self.swatch)
        lay.addWidget(self.hex)
        lay.addStretch()
        self._color = ""

    def color(self) -> str:
        return self._color

    def set_color(self, value: str, emit: bool = False) -> None:
        color = theme.normalize(value) or ""
        if not color and value.strip():
            self.hex.setText(self._color)      # not a colour: put the last good one back
            return
        self._color = color
        self.hex.setText(color)
        pix = QPixmap(30, 22)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0.5, 0.5, 29, 21), 5, 5)
        p.fillPath(path, QColor(color) if color else QColor(theme.P.bg_raised))
        p.end()
        self.swatch.setIcon(QIcon(pix))
        self.swatch.setIconSize(pix.size())
        if emit:
            self.changed.emit(color)

    def _pick(self) -> None:
        chosen = QColorDialog.getColor(QColor(self._color or theme.P.accent_color), self, self.title)
        if chosen.isValid():
            self.set_color(chosen.name(), emit=True)


class BrandPreview(QFrame):
    """A miniature of Reelay in the chosen colours: window, a card, text and a primary button."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(96)
        self.palette_ = theme.P
        self.name = ""

    def set_colors(self, background: str, accent: str, name: str) -> None:
        try:
            self.palette_ = theme.derive_custom(background, accent)
        except theme.ThemeError:
            self.palette_ = theme.P
        self.name = name
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p, pal = QPainter(self), self.palette_
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        window = QPainterPath()
        window.addRoundedRect(r, theme.RADIUS_LG, theme.RADIUS_LG)
        p.fillPath(window, QColor(pal.bg_primary))
        p.setPen(QColor(pal.border))
        p.drawPath(window)
        card = QRectF(r.x() + 14, r.y() + 14, r.width() * 0.62, r.height() - 28)
        cpath = QPainterPath()
        cpath.addRoundedRect(card, theme.RADIUS, theme.RADIUS)
        p.fillPath(cpath, QColor(pal.bg_surface))
        p.setPen(QColor(pal.divider))
        p.drawPath(cpath)
        font = QFont(self.font())
        font.setWeight(QFont.DemiBold)
        p.setFont(font)
        p.setPen(QColor(pal.text_primary))
        p.drawText(card.adjusted(12, 10, -12, 0), Qt.AlignLeft | Qt.AlignTop, self.name or "Your profile")
        font.setWeight(QFont.Normal)
        font.setPointSize(max(font.pointSize() - 2, 8))
        p.setFont(font)
        p.setPen(QColor(pal.text_secondary))
        p.drawText(card.adjusted(12, 32, -12, 0), Qt.AlignLeft | Qt.AlignTop, "Secondary text looks like this")
        btn = QRectF(card.right() + 14, r.center().y() - 15, r.right() - card.right() - 28, 30)
        bpath = QPainterPath()
        bpath.addRoundedRect(btn, theme.RADIUS, theme.RADIUS)
        p.fillPath(bpath, QColor(pal.accent_color))
        font.setWeight(QFont.DemiBold)
        p.setFont(font)
        p.setPen(QColor(pal.on_accent))
        p.drawText(btn, Qt.AlignCenter, "Schedule")
        p.end()


class ProfileDialog(QDialog):
    """``values()`` → (name, Brand, use_custom_theme) after it's accepted."""

    def __init__(self, store, profile: Profile | None = None, custom_theme_on: bool = False, parent=None):
        super().__init__(parent)
        self.store, self.profile = store, profile
        self.setWindowTitle("Edit Profile & Branding" if profile else "New Profile")
        self.setMinimumWidth(520)
        self.resize(600, 640)
        self.brand = branding.load(store, profile.id) if profile else Brand()
        self._theme_touched = False
        self._colors_chosen = self.brand.has_colors     # saved colours are never replaced by a new logo's

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(14)
        intro = QLabel("A profile is one creator or brand: its own videos, board, accounts and look. The logo and "
                       "colours show which profile you're working in." if not profile else
                       "How this profile looks in Reelay. The logo is its icon in the sidebar and menus.")
        intro.setWordWrap(True)
        intro.setProperty("muted", True)
        lay.addWidget(intro)

        # Logo
        top = QHBoxLayout()
        top.setSpacing(16)
        self.logo_label = QLabel(objectName="LogoPreview")
        self.logo_label.setFixedSize(LOGO_PREVIEW + 12, LOGO_PREVIEW + 12)
        self.logo_label.setAlignment(Qt.AlignCenter)
        top.addWidget(self.logo_label)
        lc = QVBoxLayout()
        lc.addStretch()
        lc.addWidget(QLabel("Logo", objectName="DetailTitle"))
        hint = QLabel("A square PNG with a transparent background looks best.")
        hint.setProperty("faint", True)
        hint.setWordWrap(True)
        lc.addWidget(hint)
        lb = QHBoxLayout()
        self.choose_logo = tip(QPushButton("Choose Logo…"), "Pick an image file for this profile's logo")
        self.choose_logo.clicked.connect(self._choose_logo)
        self.remove_logo = tip(QPushButton("Remove"), "Go back to the profile's initials")
        set_prop(self.remove_logo, "flat")
        self.remove_logo.clicked.connect(self._remove_logo)
        lb.addWidget(self.choose_logo)
        lb.addWidget(self.remove_logo)
        lb.addStretch()
        lc.addLayout(lb)
        lc.addStretch()
        top.addLayout(lc, 1)
        lay.addLayout(top)

        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        self.name = QLineEdit(profile.name if profile else "")
        self.name.setPlaceholderText("e.g. Be Unconventional")
        self.name.textChanged.connect(self._changed)
        self.tagline = QLineEdit(self.brand.tagline)
        self.tagline.setPlaceholderText("e.g. capture, create, caffeinate")
        self.tagline.setMaxLength(120)
        form.addRow("Name", self.name)
        form.addRow("Tagline", self.tagline)
        self.background = ColorField("Background")
        self.accent = ColorField("Accent")
        bg, acc = (self.brand.background, self.brand.accent) if self.brand.has_colors else theme.DEFAULT_CUSTOM
        self.background.set_color(bg)
        self.accent.set_color(acc)
        self.background.changed.connect(self._colors_changed)
        self.accent.changed.connect(self._colors_changed)
        self.match_btn = tip(QPushButton("Match Logo"), "Pick a background and accent from the logo's colours")
        self.match_btn.clicked.connect(self._match_logo)
        self.background.layout().insertWidget(2, self.match_btn)
        form.addRow("Background", self.background)
        form.addRow("Accent", self.accent)
        lay.addLayout(form)

        self.preview = BrandPreview()
        lay.addWidget(self.preview)
        self.warning = QLabel()
        self.warning.setProperty("danger", True)
        self.warning.setWordWrap(True)
        lay.addWidget(self.warning)
        self.use_custom = QCheckBox("Show Reelay in these colours (the Custom theme)")
        tip(self.use_custom, "Custom shows each profile in its own brand colours. Light, Dark and Black look the "
                             "same for every profile.")
        self.use_custom.setChecked(custom_theme_on)
        self.use_custom.toggled.connect(lambda _on: setattr(self, "_theme_touched", True))
        lay.addWidget(self.use_custom)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok = buttons.button(QDialogButtonBox.Ok)
        self.ok.setText("Save" if profile else "Create Profile")
        set_prop(self.ok, "primary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self._changed()

    # ------------------------------------------------------------ logo
    def _choose_logo(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose a Logo", "",
                                              "Images (*.png *.jpg *.jpeg *.webp *.svg *.gif *.bmp)")
        if not path:
            return
        try:
            self.brand.logo = branding.logo_from_file(path)
        except BrandError as exc:
            QMessageBox.warning(self, "Logo", str(exc))
            return
        if not self._colors_chosen:
            self._match_logo()                 # no colours picked yet: start from the logo's
        self._changed()

    def _remove_logo(self) -> None:
        self.brand.logo = b""
        self._changed()

    def _match_logo(self) -> None:
        suggested = branding.suggest_colors(self.brand.logo)
        if suggested:
            self.background.set_color(suggested[0])
            self.accent.set_color(suggested[1])
            self._colors_changed()

    # ------------------------------------------------------------ live state
    def _colors_changed(self, *_args) -> None:
        self._colors_chosen = True
        if not self._theme_touched and not self.use_custom.isChecked():
            self.use_custom.blockSignals(True)
            self.use_custom.setChecked(True)   # picking brand colours usually means wanting to see them
            self.use_custom.blockSignals(False)
        self._changed()

    def _changed(self, *_args) -> None:
        self.brand.background, self.brand.accent = self.background.color(), self.accent.color()
        name = self.name.text().strip()
        stand_in = Profile(name=name or "?", output_folder="", color=self.profile.color if self.profile
                           else (self.brand.accent or theme.P.text_muted))
        self.logo_label.setPixmap(branding.avatar_pixmap(stand_in, self.brand, LOGO_PREVIEW))
        self.remove_logo.setVisible(bool(self.brand.logo))
        self.match_btn.setEnabled(bool(self.brand.logo))
        self.preview.set_colors(self.brand.background, self.brand.accent, name)
        self.warning.setText(theme.accent_warning(self.brand.background, self.brand.accent))
        self.warning.setVisible(bool(self.warning.text()))
        self.ok.setEnabled(bool(name))

    def values(self) -> tuple[str, Brand, bool]:
        self.brand.tagline = self.tagline.text().strip()
        return self.name.text().strip(), self.brand, self.use_custom.isChecked()
