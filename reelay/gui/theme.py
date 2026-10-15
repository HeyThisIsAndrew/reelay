"""Themes: Light, Dark (warm near-black, the default), Black (OLED) and Custom (each profile's brand colours).

How it fits together:

- A ``Palette`` is a set of colour tokens (``bg_primary``, ``text_secondary``, ``accent_color`` …).
- ``styles/app.qss`` is a template that only ever refers to tokens; ``ThemeManager.render`` fills them in.
- Custom-drawn widgets (cards, thumbnails, table colours) read ``theme.P``, the active palette, when they paint,
  so switching themes at runtime only needs ``apply_theme()`` followed by a repaint.
- Custom mode builds a whole palette from two colours a person picks (background and accent) with
  ``derive_custom``, keeping text readable whatever they choose.
"""

from __future__ import annotations

import re
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from ..paths import cache_dir

IS_MAC = sys.platform == "darwin"
BASE_PT = 14 if IS_MAC else 11
RADIUS_SM, RADIUS, RADIUS_LG = 6, 8, 10        # small controls · buttons and inputs · panels and cards
RADIUS_CARD = 14                                # integration cards and strips (the spatial card look)
TEMPLATE = Path(__file__).with_name("styles") / "app.qss"


@dataclass(frozen=True)
class Palette:
    name: str
    label: str
    scheme: str            # "dark" | "light": native title bar + checkboxes
    bg_primary: str        # window
    bg_surface: str        # panels, columns, table
    bg_raised: str         # inputs, buttons
    bg_hover: str
    bg_pressed: str
    border: str
    divider: str
    selection: str
    text_primary: str
    text_secondary: str    # supporting text: readable, but doesn't compete with the main text
    text_muted: str        # hints and labels you only read when you look for them
    accent_color: str      # primary buttons, checked controls, progress
    on_accent: str
    accent_hover: str
    accent_pressed: str
    focus: str             # focused input border
    # Status colours, one meaning each. "Ready" has none: it's the normal state, drawn neutral.
    danger: str            # blocked or failed
    success: str           # published, live
    info: str              # scheduled
    warning: str           # waiting for your decision (an approval, an interrupted send)
    on_badge: str          # text on coloured badges

    # Derived tints of the accent (so a profile's brand colour can glow without shouting)
    @property
    def accent_soft(self) -> str:      # hover and selected washes
        return mix(self.bg_surface, self.accent_color, 0.14)

    @property
    def accent_ring(self) -> str:      # hover borders
        return mix(self.bg_surface, self.accent_color, 0.55)

    # Depth: board columns and the Library's grid are recessed wells; cards sit on them a step brighter
    # (dark themes) or white with a soft shadow line (light themes), with a crisper hairline.
    @property
    def bg_well(self) -> str:
        return mix(self.bg_primary, self.bg_raised if self.scheme == "light" else self.bg_surface, 0.5)

    @property
    def card_fill(self) -> str:
        return self.bg_surface if self.scheme == "light" else self.bg_raised

    @property
    def card_edge(self) -> str:
        return mix(self.divider, self.border, 0.5)

    @property
    def card_highlight(self) -> str:        # dark themes: a faint light along a card's top edge
        return mix(self.card_fill, self.text_primary, 0.16)

    @property
    def card_shadow(self) -> str:           # light themes: a soft line under a card
        return mix(self.bg_well, self.text_primary, 0.10)

    # The Help Center's numbered markers: one blue that shows on any screenshot, in every theme
    @property
    def marker(self) -> str:
        return "#2563eb"

    @property
    def marker_ink(self) -> str:            # white numerals on the marker: 5.2:1
        return "#ffffff"

    # Tinted status pills: a soft fill in the status colour, with text in that colour made readable on it
    @property
    def success_soft(self) -> str:
        return mix(self.bg_surface, self.success, 0.18)

    @property
    def success_ink(self) -> str:
        return _readable(self.success, (self.success_soft,), 4.5, self.text_primary)

    @property
    def warning_soft(self) -> str:
        return mix(self.bg_surface, self.warning, 0.18)

    @property
    def warning_ink(self) -> str:
        return _readable(self.warning, (self.warning_soft,), 4.5, self.text_primary)

    @property
    def danger_soft(self) -> str:
        return mix(self.bg_surface, self.danger, 0.18)

    @property
    def danger_ink(self) -> str:
        return _readable(self.danger, (self.danger_soft,), 4.5, self.text_primary)

    # Status colours as text on a card (small type: 4.5:1 against the card and the column behind it)
    @property
    def info_text(self) -> str:             # scheduled
        return _readable(self.info, (self.card_fill, self.bg_surface), 4.5, self.text_primary)

    @property
    def warning_text(self) -> str:          # waiting for your decision
        return _readable(self.warning, (self.card_fill, self.bg_surface), 4.5, self.text_primary)

    def ink_on(self, color: str) -> str:
        """The glyph colour for a small badge filled with ``color`` (a ✓, a clock): the theme's
        badge ink when it reads at 3:1 on it, else whichever of near-white and near-black reads best.
        White on amber was 2.0:1."""
        if contrast(self.on_badge, color) >= 3.0:
            return self.on_badge
        return max(("#ffffff", "#141414"), key=lambda ink: contrast(ink, color))

    @property
    def scrim(self) -> str:            # badges laid over thumbnails (painted with alpha)
        return "#000000" if self.scheme == "dark" else "#141414"


THEMES: dict[str, Palette] = {
    "dark": Palette(
        name="dark", label="Dark", scheme="dark",
        bg_primary="#1f1e1d", bg_surface="#262624", bg_raised="#302f2d", bg_hover="#3a3936", bg_pressed="#44433f",
        border="#393835", divider="#2d2c2a", selection="#3f3e3a",
        text_primary="#ecebe7", text_secondary="#a9a8a2", text_muted="#85847e",
        accent_color="#ecebe7", on_accent="#1a1918", accent_hover="#ffffff", accent_pressed="#d6d5d0",
        focus="#85847e", danger="#de8a77", success="#9db894", info="#3b82f6", warning="#f2a93b",
        on_badge="#1f1e1d"),
    "black": Palette(
        name="black", label="Black", scheme="dark",
        bg_primary="#000000", bg_surface="#0a0a0a", bg_raised="#161616", bg_hover="#1f1f1f", bg_pressed="#2a2a2a",
        border="#262626", divider="#141414", selection="#242424",
        text_primary="#f2f2f2", text_secondary="#a3a3a3", text_muted="#7a7a7a",
        accent_color="#f2f2f2", on_accent="#000000", accent_hover="#ffffff", accent_pressed="#d4d4d4",
        focus="#7a7a7a", danger="#e5806b", success="#94b98b", info="#3b82f6", warning="#f2a93b",
        on_badge="#000000"),
    "light": Palette(
        name="light", label="Light", scheme="light",
        bg_primary="#f4f3ef", bg_surface="#ffffff", bg_raised="#f0efeb", bg_hover="#e7e6e1", bg_pressed="#dcdbd5",
        border="#dddcd6", divider="#eceae5", selection="#e4e3dd",
        text_primary="#1f1e1d", text_secondary="#5f5e59", text_muted="#86857f",
        accent_color="#1f1e1d", on_accent="#ffffff", accent_hover="#000000", accent_pressed="#3a3936",
        focus="#86857f", danger="#b5503b", success="#4f7a45", info="#3b82f6", warning="#f2a93b",
        on_badge="#ffffff"),
}
MODES = ("light", "dark", "black", "custom")
MODE_LABELS = {"light": "Light", "dark": "Dark", "black": "Black", "custom": "Custom"}
DEFAULT_THEME = "dark"
# Colours a new profile's Custom theme starts from: Dark's background with a calm blue accent.
DEFAULT_CUSTOM = ("#1f1e1d", "#6f9ef0")

P: Palette = THEMES[DEFAULT_THEME]


class ThemeError(ValueError):
    pass


# ---------------------------------------------------------------- colour maths (plain Python, no Qt)
def normalize(color: str) -> str | None:
    """'#ABC', 'abc', '#aabbcc' → '#aabbcc'; anything else → None."""
    text = (color or "").strip().lstrip("#").lower()
    if re.fullmatch(r"[0-9a-f]{3}", text):
        text = "".join(c * 2 for c in text)
    return f"#{text}" if re.fullmatch(r"[0-9a-f]{6}", text) else None


def _rgb(color: str) -> tuple[int, int, int]:
    c = normalize(color)
    if c is None:
        raise ThemeError(f"Not a colour: {color!r}")
    return int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)


def _hex(rgb) -> str:
    return "#" + "".join(f"{max(0, min(255, round(v))):02x}" for v in rgb)


def mix(a: str, b: str, amount: float) -> str:
    """``amount`` of the way from colour a to colour b."""
    ra, rb = _rgb(a), _rgb(b)
    return _hex(x + (y - x) * amount for x, y in zip(ra, rb))


def luminance(color: str) -> float:
    def channel(v: int) -> float:
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(v) for v in _rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio, 1 (same) to 21 (black on white)."""
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def is_dark(color: str) -> bool:
    return contrast(color, "#ffffff") >= contrast(color, "#000000")


def _readable(color: str, against: tuple[str, ...], target: float, toward: str) -> str:
    """Move ``color`` toward ``toward`` until it reaches ``target`` contrast with every colour in ``against``."""
    for step in range(21):
        candidate = mix(color, toward, step / 20)
        if all(contrast(candidate, bg) >= target for bg in against):
            return candidate
    return toward


def derive_custom(background: str, accent: str) -> Palette:
    """A full palette from two colours. Text always stays readable; the accent is used as picked
    (Settings warns when it's hard to see against the background, see ``accent_warning``)."""
    bg, accent = normalize(background), normalize(accent)
    if bg is None or accent is None:
        raise ThemeError("Pick a background and an accent colour.")
    dark = is_dark(bg)
    base = THEMES["dark" if dark else "light"]
    # Text: off-white or off-black with a hint of the background's hue.
    text = _readable(mix("#f4f3ef" if dark else "#1c1b1a", bg, 0.06), (bg,), 12.0,
                     "#ffffff" if dark else "#000000")
    # Panels step from the background toward the text colour (dark) or brighten (light). On a mid-tone
    # background that would eat into text contrast, so the steps shrink until text stays readable.
    wanted = min(7.0, contrast(text, bg) - 0.5)
    for k in (1.0, 0.75, 0.5, 0.3, 0.15, 0.0):
        if dark:   # like the built-in Dark theme
            surface, raised, hover, pressed = (mix(bg, text, a * k) for a in (0.035, 0.08, 0.13, 0.18))
        else:      # panels a touch brighter than the window, controls a touch darker
            surface = mix(bg, "#ffffff", 0.7 * k)
            raised, hover, pressed = (mix(bg, text, a * k) for a in (0.025, 0.06, 0.1))
        if min(contrast(text, c) for c in (surface, raised)) >= wanted:
            break
    if dark:
        border, divider, selection = mix(bg, text, 0.13), mix(bg, text, 0.065), mix(bg, text, 0.15)
    else:
        border, divider, selection = mix(bg, text, 0.1), mix(bg, text, 0.05), mix(bg, text, 0.08)
    panels = (bg, surface, raised)
    text_secondary = _readable(mix(text, bg, 0.38), panels, 4.5, text)
    text_muted = _readable(mix(text, bg, 0.55), panels, 3.0, text)
    on_accent = "#ffffff" if contrast(accent, "#ffffff") >= contrast(accent, "#111111") else "#111111"
    lighter, darker = mix(accent, "#ffffff", 0.14), mix(accent, "#000000", 0.14)
    return replace(base, name="custom", label="Custom", bg_primary=bg, bg_surface=surface, bg_raised=raised,
                   bg_hover=hover, bg_pressed=pressed, border=border, divider=divider, selection=selection,
                   text_primary=text, text_secondary=text_secondary, text_muted=text_muted,
                   accent_color=accent, on_accent=on_accent,
                   accent_hover=lighter if dark else darker, accent_pressed=darker if dark else lighter,
                   focus=accent, on_badge=bg if dark else "#ffffff")


def accent_warning(background: str, accent: str) -> str:
    """Plain-language warning when the accent would be hard to see, else ''."""
    bg, acc = normalize(background), normalize(accent)
    if bg is None or acc is None:
        return ""
    if contrast(bg, acc) < 2.0:
        return "The accent is very close to the background, so buttons will be hard to see."
    if contrast(bg, acc) < 3.0:
        return "The accent is a little faint against this background."
    return ""


# ---------------------------------------------------------------- template
_TOKEN = re.compile(r"\{([a-z][a-z0-9_]*)\}")


def render(template: str, tokens: dict[str, object]) -> str:
    """Fill in ``{token}`` placeholders. An unknown token is an error, never a silently broken stylesheet."""
    missing = sorted({m for m in _TOKEN.findall(template) if m not in tokens})
    if missing:
        raise ThemeError("The stylesheet uses unknown tokens: " + ", ".join(missing))
    return _TOKEN.sub(lambda m: str(tokens[m.group(1)]), template)


def _icon(kind: str, color: str) -> str:
    """Write a tinted dropdown chevron or checkmark and return its path for the stylesheet."""
    path = cache_dir() / f"{kind}-{color.lstrip('#')}.svg"
    if not path.is_file():
        shape = ('width="10" height="6" viewBox="0 0 10 6"><path d="M1 1l4 4 4-4" stroke-width="1.6"'
                 if kind == "chevron" else
                 'width="12" height="12" viewBox="0 0 12 12"><path d="M2.5 6.2l2.4 2.4 4.6-5" stroke-width="1.8"')
        path.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" {shape} fill="none" stroke="{color}" '
                        'stroke-linecap="round" stroke-linejoin="round"/></svg>', encoding="utf-8")
    return Path(path).as_posix()


def tokens_for(p: Palette, icons: bool = True) -> dict[str, object]:
    tokens: dict[str, object] = asdict(p)
    tokens.update(bg_well=p.bg_well, card_fill=p.card_fill, card_edge=p.card_edge, marker=p.marker,
                  marker_ink=p.marker_ink)
    tokens.update(accent_soft=p.accent_soft, accent_ring=p.accent_ring, success_soft=p.success_soft,
                  success_ink=p.success_ink, warning_soft=p.warning_soft, warning_ink=p.warning_ink,
                  danger_soft=p.danger_soft, danger_ink=p.danger_ink, warning_text=p.warning_text,
                  info_text=p.info_text)
    tokens.update(font_xs=BASE_PT - 2, font_sm=BASE_PT - 1, font_pt=BASE_PT, font_lg=BASE_PT + 1,
                  font_h2=BASE_PT + 2, font_h1=BASE_PT + 4, font_title=BASE_PT + 8, font_display=BASE_PT + 22,
                  radius_sm=RADIUS_SM, radius=RADIUS, radius_lg=RADIUS_LG, radius_card=RADIUS_CARD)
    tokens["chevron"] = _icon("chevron", p.text_secondary) if icons else ""
    tokens["check"] = _icon("check", p.on_accent) if icons else ""
    return tokens


def stylesheet(p: Palette) -> str:
    return render(TEMPLATE.read_text(encoding="utf-8"), tokens_for(p))


# ---------------------------------------------------------------- applying
class ThemeManager:
    """Knows the four modes and applies one to the app."""

    def palette_for(self, mode: str, custom: tuple[str, str] | None = None) -> Palette:
        if mode == "custom":
            background, accent = custom or DEFAULT_CUSTOM
            try:
                return derive_custom(background, accent)
            except ThemeError:
                return derive_custom(*DEFAULT_CUSTOM)
        return THEMES.get(mode, THEMES[DEFAULT_THEME])

    def apply(self, app: QApplication, mode: str = DEFAULT_THEME,
              custom: tuple[str, str] | None = None) -> Palette:
        global P
        P = self.palette_for(mode, custom)
        app.setStyle("Fusion")
        try:  # Qt 6.8+: matching native title bar on macOS and Windows 11
            app.styleHints().setColorScheme(Qt.ColorScheme.Dark if P.scheme == "dark" else Qt.ColorScheme.Light)
        except AttributeError:
            pass

        pal = QPalette()
        roles = {
            QPalette.Window: P.bg_primary, QPalette.WindowText: P.text_primary, QPalette.Base: P.bg_surface,
            QPalette.AlternateBase: P.bg_raised, QPalette.Text: P.text_primary, QPalette.Button: P.bg_raised,
            QPalette.ButtonText: P.text_primary, QPalette.Highlight: P.accent_color,
            QPalette.HighlightedText: P.on_accent, QPalette.ToolTipBase: P.bg_raised,
            QPalette.ToolTipText: P.text_primary, QPalette.PlaceholderText: P.text_muted,
            QPalette.Link: P.text_primary, QPalette.Mid: P.border, QPalette.Dark: P.bg_primary,
            QPalette.Light: P.bg_hover, QPalette.BrightText: P.text_primary,
        }
        for role, color in roles.items():
            pal.setColor(role, QColor(color))
        for role in (QPalette.Text, QPalette.WindowText, QPalette.ButtonText):
            pal.setColor(QPalette.Disabled, role, QColor(P.text_muted))
        app.setPalette(pal)

        font = app.font()
        font.setPointSize(BASE_PT)
        app.setFont(font)
        app.setStyleSheet(stylesheet(P))
        return P


manager = ThemeManager()


def apply_theme(app: QApplication, name: str = DEFAULT_THEME, custom: tuple[str, str] | None = None) -> Palette:
    return manager.apply(app, name, custom)


_PROPER = {"youtube": "YouTube", "tiktok": "TikTok", "instagram": "Instagram", "bluesky": "Bluesky",
           "threads": "Threads", "facebook": "Facebook", "snapchat": "Snapchat", "reelay": "Reelay", "ai": "AI",
           "api": "API", "seo": "SEO", "id": "ID"}


def heading(text: str) -> str:
    """A section heading in sentence case ("QUICK-START RECIPES" → "Quick-start recipes",
    "YOUTUBE TITLE" → "YouTube title"); text that isn't all capitals is left exactly as written."""
    if not text or text != text.upper() or not any(c.isalpha() for c in text):
        return text
    words = text.lower().split(" ")
    out = []
    for n, w in enumerate(words):
        fixed = _PROPER.get(w)
        out.append(fixed if fixed else (w[:1].upper() + w[1:] if n == 0 else w))
    return " ".join(out)


def set_prop(widget, name: str, value=True) -> None:
    """Set a dynamic property used by the stylesheet and re-polish."""
    widget.setProperty(name, value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
