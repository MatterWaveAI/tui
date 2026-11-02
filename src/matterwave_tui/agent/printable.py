"""Text from the workspace, the model, a command or an MCP server, made safe
to put on the terminal.

Rich and Textual pass ESC through, so a file's contents, a command's output
or the model's words could otherwise carry escape sequences to the terminal:
clear the screen or move the cursor to draw a fake approval dialog, set the
clipboard (OSC 52), or plant a link. Bidirectional overrides can make a
command read differently from what runs. `shown` spells each such character
out instead (`\\x1b`, `\\u202e`), so what the user reads is what is there.
"""

from __future__ import annotations

import re

# C0 controls other than tab and newline, DEL, C1 controls, and the bidi
# embeddings, overrides and isolates: never part of honest prose.
_CONTROLS = "\x00-\x08\x0b-\x1f\x7f-\x9f‪-‮⁦-⁩"
# Also spelled out where every character counts (a command, a path, a diff
# under approval): zero-width characters, the direction marks, the line and
# paragraph separators and the BOM, which hide text or reorder it.
_INVISIBLE = "؜᠎​-‏  ⁠-⁥﻿"

_PROSE = re.compile(f"\r\n|[{_CONTROLS}]")
_STRICT = re.compile(f"\r\n|[{_CONTROLS}{_INVISIBLE}]")


def _spell(match: re.Match[str]) -> str:
    text = match.group(0)
    if text == "\r\n":
        return "\n"
    if text == "\r":
        return "\\r"
    code = ord(text)
    return f"\\x{code:02x}" if code <= 0xFF else f"\\u{code:04x}"


def shown(text: str, *, strict: bool = False) -> str:
    """`text` with its control characters and bidi overrides spelled out,
    and CRLF as a plain line break; tabs and line breaks stay. `strict` also
    spells out the invisible characters, for what is being approved."""
    return (_STRICT if strict else _PROSE).sub(_spell, text)
