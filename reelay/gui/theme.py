"""Three monotone themes: Light, Dark (warm near-black, the default) and Black (OLED).

Widgets read colors from `theme.P` (the active palette), so switching themes
at runtime only needs `apply_theme()` followed by a repaint.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from ..paths import cache_dir

IS_MAC = sys.platform == "darwin"
BASE_PT = 13 if IS_MAC else 10


@dataclass(frozen=True)
class Palette:
    name: str
    label: str
    scheme: str            # "dark" | "light": native title bar + checkboxes
    bg: str                # window
    surface: str           # panels, table
    raised: str            # inputs, buttons
    hover: str
    pressed: str
    border: str
    divider: str
    selection: str
    text: str
    text2: str
    text3: str
    primary_bg: str        # primary buttons invert the theme
    primary_fg: str
    primary_hover: str
    primary_pressed: str
    danger: str
    success: str


THEMES: dict[str, Palette] = {
    "dark": Palette(
        name="dark", label="Dark", scheme="dark",
        bg="#1f1e1d", surface="#262624", raised="#302f2d", hover="#3a3936", pressed="#44433f",
        border="#393835", divider="#2d2c2a", selection="#3f3e3a",
        text="#ecebe7", text2="#a9a8a2", text3="#76756f",
        primary_bg="#ecebe7", primary_fg="#1a1918", primary_hover="#ffffff", primary_pressed="#d6d5d0",
        danger="#de8a77", success="#9db894"),
    "black": Palette(
        name="black", label="Black", scheme="dark",
        bg="#000000", surface="#0a0a0a", raised="#161616", hover="#1f1f1f", pressed="#2a2a2a",
        border="#262626", divider="#141414", selection="#242424",
        text="#f2f2f2", text2="#a3a3a3", text3="#6b6b6b",
        primary_bg="#f2f2f2", primary_fg="#000000", primary_hover="#ffffff", primary_pressed="#d4d4d4",
        danger="#e5806b", success="#94b98b"),
    "light": Palette(
        name="light", label="Light", scheme="light",
        bg="#f4f3ef", surface="#ffffff", raised="#f0efeb", hover="#e7e6e1", pressed="#dcdbd5",
        border="#dddcd6", divider="#eceae5", selection="#e4e3dd",
        text="#1f1e1d", text2="#5f5e59", text3="#94938d",
        primary_bg="#1f1e1d", primary_fg="#ffffff", primary_hover="#000000", primary_pressed="#3a3936",
        danger="#b5503b", success="#4f7a45"),
}
DEFAULT_THEME = "dark"

P: Palette = THEMES[DEFAULT_THEME]


def _chevron(color: str) -> str:
    """Write a tinted dropdown chevron and return its path for the stylesheet."""
    path = cache_dir() / f"chevron-{color.lstrip('#')}.svg"
    if not path.is_file():
        path.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="6" viewBox="0 0 10 6">'
            f'<path d="M1 1l4 4 4-4" fill="none" stroke="{color}" stroke-width="1.6" '
            'stroke-linecap="round" stroke-linejoin="round"/></svg>', encoding="utf-8")
    return Path(path).as_posix()


def _check(color: str) -> str:
    path = cache_dir() / f"check-{color.lstrip('#')}.svg"
    if not path.is_file():
        path.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 12 12">'
            f'<path d="M2.5 6.2l2.4 2.4 4.6-5" fill="none" stroke="{color}" stroke-width="1.8" '
            'stroke-linecap="round" stroke-linejoin="round"/></svg>', encoding="utf-8")
    return Path(path).as_posix()


def stylesheet(p: Palette) -> str:
    chevron = _chevron(p.text2)
    check = _check(p.primary_fg)
    return f"""
* {{ outline: none; }}
QMainWindow, QDialog {{ background: {p.bg}; }}
QWidget {{ color: {p.text}; }}
QWidget#Page, QWidget#TopBar, QWidget#BottomBar {{ background: {p.bg}; }}
QWidget#Sidebar {{ background: {p.surface}; border-right: 1px solid {p.divider}; }}
QWidget#ListArea, QFrame#Card {{ background: {p.surface}; border: 1px solid {p.divider}; border-radius: 10px; }}
QWidget#CardBody {{ background: transparent; }}
QPushButton[segment] {{ border-radius: 0; padding: 4px 7px; min-height: 18px; font-size: {BASE_PT - 1}pt;
                        background: {p.raised}; color: {p.text2}; border: 1px solid {p.border}; margin-left: -1px; }}
QPushButton[segment="first"] {{ border-top-left-radius: 7px; border-bottom-left-radius: 7px; margin-left: 0; }}
QPushButton[segment="last"] {{ border-top-right-radius: 7px; border-bottom-right-radius: 7px; }}
QPushButton[segment]:checked {{ background: {p.text}; color: {p.bg}; border-color: {p.text}; font-weight: 600; }}
QPushButton[segment]:disabled {{ color: {p.text3}; }}
QPushButton[segment]:checked:disabled {{ background: {p.text3}; color: {p.bg}; }}
QFrame#SelectionStrip {{ background: {p.raised}; border: 1px solid {p.border}; border-radius: 8px; }}
QFrame#Card QFrame#Card {{ background: {p.raised}; }}
QWidget#DetailPanel, QScrollArea#DetailScroll, QWidget#DetailBody {{ background: {p.surface}; }}
QFrame#DetailFrame {{ background: {p.surface}; border: 1px solid {p.divider}; border-radius: 10px; }}
QFrame#BoardColumn {{ background: {p.surface}; border: 1px solid {p.divider}; border-radius: 10px; }}

QLabel {{ background: transparent; }}
QLabel[muted="true"] {{ color: {p.text2}; }}
QLabel[faint="true"] {{ color: {p.text3}; }}
QLabel[danger="true"] {{ color: {p.danger}; }}
QLabel[success="true"] {{ color: {p.success}; }}
QLabel[badge="ready"] {{ color: {p.bg}; background: {p.success}; border-radius: 9px; padding: 2px 10px;
                         font-weight: 700; letter-spacing: 0.5px; }}
QLabel[badge="not_ready"] {{ color: {p.bg}; background: {p.danger}; border-radius: 9px; padding: 2px 10px;
                             font-weight: 700; letter-spacing: 0.5px; }}
QLabel[section="true"] {{ color: {p.text3}; font-size: {BASE_PT - 2}pt; font-weight: 600;
                          letter-spacing: 0.5px; padding-top: 6px; }}
QLabel#PageTitle {{ font-size: {BASE_PT + 7}pt; font-weight: 700; }}
QLabel#DetailTitle {{ font-size: {BASE_PT + 2}pt; font-weight: 600; }}
QLabel#EmptyTitle {{ font-size: {BASE_PT + 4}pt; font-weight: 600; color: {p.text}; }}
QLabel#FilenamePreview {{ background: {p.bg}; border: 1px solid {p.divider}; border-radius: 6px;
                          padding: 6px 8px; color: {p.text2}; font-family: Menlo, Consolas, monospace;
                          font-size: {BASE_PT - 1}pt; }}

QLineEdit, QComboBox, QSpinBox, QDateTimeEdit, QPlainTextEdit {{
    background: {p.raised}; border: 1px solid {p.border}; border-radius: 7px;
    padding: 6px 10px; selection-background-color: {p.pressed}; selection-color: {p.text}; min-height: 18px;
}}
QPlainTextEdit {{ padding: 6px 8px; }}
QLineEdit:focus, QComboBox:focus, QDateTimeEdit:focus, QPlainTextEdit:focus {{ border-color: {p.text3}; }}
QLineEdit:disabled, QComboBox:disabled, QDateTimeEdit:disabled, QPlainTextEdit:disabled {{
    color: {p.text3}; background: {p.surface}; }}
QLineEdit#UrlField {{ font-size: {BASE_PT + 1}pt; padding: 9px 12px; border-radius: 9px; }}
QComboBox::drop-down, QDateTimeEdit::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow, QDateTimeEdit::down-arrow {{ image: url("{chevron}"); width: 10px; height: 6px; margin-right: 10px; }}
QComboBox QAbstractItemView {{ background: {p.raised}; border: 1px solid {p.border}; border-radius: 6px;
                               selection-background-color: {p.hover}; padding: 4px; outline: none; }}
QDateTimeEdit::up-button, QDateTimeEdit::down-button {{ width: 0; border: none; }}
QCalendarWidget QWidget {{ background: {p.raised}; color: {p.text}; }}
QCalendarWidget QToolButton {{ background: transparent; border: none; padding: 4px 8px; }}
QCalendarWidget QAbstractItemView:enabled {{ selection-background-color: {p.primary_bg}; selection-color: {p.primary_fg}; }}

QPushButton, QToolButton {{
    background: {p.raised}; border: 1px solid {p.border}; border-radius: 7px;
    padding: 6px 14px; color: {p.text}; font-size: {BASE_PT}pt;
}}
QPushButton:hover, QToolButton:hover {{ background: {p.hover}; }}
QPushButton:pressed, QToolButton:pressed {{ background: {p.pressed}; }}
QPushButton:disabled, QToolButton:disabled {{ color: {p.text3}; background: {p.surface}; border-color: {p.divider}; }}
QPushButton:checked {{ background: {p.primary_bg}; color: {p.primary_fg}; border-color: {p.primary_bg}; }}
QToolButton::menu-indicator {{ image: none; width: 0; }}
QPushButton[primary="true"] {{ background: {p.primary_bg}; color: {p.primary_fg}; border: 1px solid {p.primary_bg};
                               font-weight: 600; padding: 7px 18px; }}
QPushButton[primary="true"]:hover {{ background: {p.primary_hover}; }}
QPushButton[primary="true"]:pressed {{ background: {p.primary_pressed}; }}
QPushButton[primary="true"]:disabled {{ background: {p.raised}; color: {p.text3}; border-color: {p.border}; }}
QPushButton[flat="true"] {{ background: transparent; border: none; color: {p.text2}; padding: 4px 6px; text-align: left; }}
QPushButton[flat="true"]:hover {{ color: {p.text}; }}
QPushButton[danger="true"] {{ color: {p.danger}; }}
QPushButton[nav="true"] {{ background: transparent; border: none; border-radius: 7px; text-align: left;
                           padding: 8px 12px; color: {p.text2}; font-weight: 500; }}
QPushButton[nav="true"]:hover {{ background: {p.hover}; color: {p.text}; }}
QPushButton[nav="true"]:checked {{ background: {p.selection}; color: {p.text}; font-weight: 600; }}
QToolButton#ProfileButton {{ background: transparent; border: 1px solid transparent; border-radius: 8px;
                              padding: 6px 8px; text-align: left; font-weight: 600; }}
QToolButton#ProfileButton:hover {{ background: {p.hover}; }}

QTableView, QListView, QTreeWidget {{
    background: {p.surface}; border: none; border-radius: 10px; gridline-color: transparent;
    selection-background-color: {p.selection}; selection-color: {p.text};
}}
QTableView::item {{ border-bottom: 1px solid {p.divider}; padding: 0 6px; }}
QTableView::item:selected {{ background: {p.selection}; }}
QTreeWidget::item {{ padding: 6px 2px; border-bottom: 1px solid {p.divider}; }}
QListView#BoardList {{ background: transparent; border: none; }}
QHeaderView {{ background: {p.surface}; border: none; border-top-left-radius: 10px; border-top-right-radius: 10px; }}
QHeaderView::section {{
    background: {p.surface}; color: {p.text3}; border: none; border-bottom: 1px solid {p.border};
    padding: 8px 6px; font-size: {BASE_PT - 2}pt; font-weight: 600;
}}
QHeaderView::section:hover {{ color: {p.text2}; }}
QTableCornerButton::section {{ background: {p.surface}; border: none; }}

QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle {{ background: {p.hover}; border-radius: 3px; min-height: 30px; min-width: 30px; }}
QScrollBar::handle:hover {{ background: {p.pressed}; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ background: none; border: none; height: 0; width: 0; }}

QProgressBar {{ background: {p.raised}; border: none; border-radius: 2px; max-height: 4px; min-height: 4px; }}
QProgressBar::chunk {{ background: {p.text}; border-radius: 2px; }}
QSplitter::handle {{ background: transparent; }}
QSplitter::handle:horizontal {{ width: 10px; }}

QMenu {{ background: {p.raised}; border: 1px solid {p.border}; border-radius: 8px; padding: 5px; }}
QMenu::item {{ padding: 6px 22px 6px 12px; border-radius: 5px; }}
QMenu::item:selected {{ background: {p.hover}; }}
QMenu::item:disabled {{ color: {p.text3}; }}
QMenu::separator {{ height: 1px; background: {p.border}; margin: 5px 8px; }}
QToolTip {{ background: {p.raised}; color: {p.text}; border: 1px solid {p.border}; border-radius: 6px; padding: 6px 8px; }}
QMessageBox QLabel {{ color: {p.text}; }}
QTextBrowser {{ background: transparent; border: none; }}
QCheckBox, QRadioButton {{ spacing: 8px; }}
QCheckBox::indicator, QTableView::indicator {{
    width: 14px; height: 14px; border-radius: 4px; border: 1px solid {p.text3}; background: {p.raised}; }}
QCheckBox::indicator:hover, QTableView::indicator:hover {{ border-color: {p.text2}; }}
QCheckBox::indicator:checked, QTableView::indicator:checked {{
    background: {p.primary_bg}; border-color: {p.primary_bg}; image: url("{check}"); }}
QCheckBox::indicator:disabled {{ background: {p.surface}; border-color: {p.border}; }}
"""


def apply_theme(app: QApplication, name: str = DEFAULT_THEME) -> Palette:
    global P
    P = THEMES.get(name, THEMES[DEFAULT_THEME])
    app.setStyle("Fusion")
    try:  # Qt 6.8+: matching native title bar on macOS and Windows 11
        app.styleHints().setColorScheme(Qt.ColorScheme.Dark if P.scheme == "dark" else Qt.ColorScheme.Light)
    except AttributeError:
        pass

    pal = QPalette()
    roles = {
        QPalette.Window: P.bg, QPalette.WindowText: P.text, QPalette.Base: P.surface,
        QPalette.AlternateBase: P.raised, QPalette.Text: P.text, QPalette.Button: P.raised,
        QPalette.ButtonText: P.text, QPalette.Highlight: P.primary_bg, QPalette.HighlightedText: P.primary_fg,
        QPalette.ToolTipBase: P.raised, QPalette.ToolTipText: P.text, QPalette.PlaceholderText: P.text3,
        QPalette.Link: P.text, QPalette.Mid: P.border, QPalette.Dark: P.bg, QPalette.Light: P.hover,
        QPalette.BrightText: P.text,
    }
    for role, color in roles.items():
        pal.setColor(role, QColor(color))
    for role in (QPalette.Text, QPalette.WindowText, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, QColor(P.text3))
    app.setPalette(pal)

    font = app.font()
    font.setPointSize(BASE_PT)
    app.setFont(font)
    app.setStyleSheet(stylesheet(P))
    return P


def set_prop(widget, name: str, value=True) -> None:
    """Set a dynamic property used by the stylesheet and re-polish."""
    widget.setProperty(name, value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
