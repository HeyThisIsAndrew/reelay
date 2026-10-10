"""The theming engine: token templates, the Custom palette and the no-literal-colours rule."""

import random
import re
from pathlib import Path

import pytest

from reelay.gui import theme

GUI = Path(theme.__file__).parent


def test_every_mode_fills_every_token():
    template = theme.TEMPLATE.read_text(encoding="utf-8")
    for mode in theme.MODES:
        css = theme.render(template, theme.tokens_for(theme.manager.palette_for(mode), icons=False))
        assert not re.search(r"\{[a-z][a-z0-9_]*\}", css), mode
        assert theme.manager.palette_for(mode).accent_color in css


def test_unknown_tokens_are_an_error():
    with pytest.raises(theme.ThemeError, match="no_such_token"):
        theme.render("QWidget { color: {no_such_token}; }", {})
    assert theme.render("QWidget { color: {a}; }", {"a": "red"}) == "QWidget { color: red; }"


def test_no_literal_colours_outside_the_theme():
    """Colours live in theme.py only, so every theme (and every profile's Custom colours) applies everywhere.
    Platform logos are content, not theme."""
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", re.sub(r"/\*.*?\*/", "", theme.TEMPLATE.read_text(), flags=re.S))
    offenders = []
    for path in GUI.glob("*.py"):
        if path.name in ("theme.py", "logos.py"):
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"""["']#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?["']""", line):
                offenders.append(f"{path.name}:{n}")
    assert offenders == []


def test_built_in_text_is_readable():
    for p in theme.THEMES.values():
        for bg in (p.bg_primary, p.bg_surface, p.bg_raised):
            assert theme.contrast(p.text_primary, bg) >= 7, p.name
            assert theme.contrast(p.text_secondary, bg) >= 4.5, p.name
            assert theme.contrast(p.text_muted, bg) >= 3, p.name


@pytest.mark.parametrize("background,accent", [
    ("#ffffff", "#e4572e"), ("#000000", "#ffffff"), ("#0b1d3a", "#f5c518"), ("#808080", "#808080"),
    ("#f6e7d3", "#5b3a29"), ("#ff00ff", "#00ff00"), ("#123", "#abc"),
] + [("#%06x" % random.Random(i).randrange(0x1000000), "#%06x" % random.Random(i + 99).randrange(0x1000000))
     for i in range(25)])
def test_custom_palettes_keep_text_readable(background, accent):
    p = theme.derive_custom(background, accent)
    assert p.name == "custom" and p.bg_primary == theme.normalize(background)
    assert p.scheme == ("dark" if theme.is_dark(p.bg_primary) else "light")
    best = max(theme.contrast(p.bg_primary, "#ffffff"), theme.contrast(p.bg_primary, "#000000"))
    for bg in (p.bg_primary, p.bg_surface, p.bg_raised):
        assert theme.contrast(p.text_primary, bg) >= min(7, best - 0.5)
        assert theme.contrast(p.text_secondary, bg) >= min(4.5, best - 0.5)
        assert theme.contrast(p.text_muted, bg) >= min(3, best - 0.5)
    assert theme.contrast(p.on_accent, p.accent_color) >= 3.5           # button labels stay legible


def test_custom_colours_are_validated_and_warned_about():
    with pytest.raises(theme.ThemeError):
        theme.derive_custom("not a colour", "#ffffff")
    assert theme.normalize("ABC") == "#aabbcc" and theme.normalize("#12345g") is None
    assert theme.accent_warning("#101010", "#121212")                   # invisible buttons
    assert theme.accent_warning("#101010", "#f2aa4c") == ""
    assert theme.manager.palette_for("custom", ("junk", "junk")).bg_primary == theme.DEFAULT_CUSTOM[0]
