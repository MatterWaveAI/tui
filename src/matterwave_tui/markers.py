"""The marks the screen draws beside rows and lines, each with its word.

A symbol alone is a picture a screen reader names ("pushpin") or skips,
so a mark that stands alone carries its word too: `badge("pinned")` is
"📌 pinned". `--plain` draws the words alone, for a reader, a braille
display or a terminal without the glyphs; `lead` and `icon` cover a mark
that sits before words of its own.
"""

from __future__ import annotations

SYMBOLS = {
    "pinned": "📌",
    "archived": "🗄",
    "folder": "📁",
    "unread": "✉",
    "warning": "⚠",
    "critical": "⛔",
    "notice": "ℹ",
    "good": "👍",
    "bad": "👎",
    "tool": "⚙",
    "result": "↳",
    "error": "✗",
    "current": "●",
}

_plain = False


def configure(plain: bool) -> None:
    """Words alone (`--plain`), or symbols with their words."""
    global _plain
    _plain = plain


def is_plain() -> bool:
    return _plain


def badge(name: str) -> str:
    """A mark that says something on its own: "📌 pinned", or "pinned"."""
    return name if _plain else f"{SYMBOLS[name]} {name}"


def lead(name: str) -> str:
    """A mark before what it marks: "📁 " before a folder's name, or
    "folder: " in words."""
    return f"{name}: " if _plain else f"{SYMBOLS[name]} "


def icon(name: str) -> str:
    """Decoration before words that already say it: "✉ " before "3
    unread", nothing in words."""
    return "" if _plain else f"{SYMBOLS[name]} "
