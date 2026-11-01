"""Terminal image rendering for attachment parts: the kitty graphics
protocol, iTerm2's inline images (OSC 1337), and sixel behind Pillow.

Detection is env-only, the way the app reads its other configuration
(`MATTERWAVE_BASE_URL`, `MATTERWAVE_STATE_DIR`): a DA1 query would have to
fight a running Textual app for the terminal's answer. The terminals say
who they are instead. Kitty (`KITTY_WINDOW_ID`, `KITTY_PID`, a `TERM` that
names it), Ghostty and WezTerm speak the kitty protocol; iTerm2
(`TERM_PROGRAM=iTerm.app`, `LC_TERMINAL=iTerm2`) its own; foot, mlterm,
Konsole, contour and Windows Terminal (`WT_SESSION`) draw sixel.
`MATTERWAVE_IMAGES` is the toggle:

* unset or `auto` -- whichever the terminal's env names, otherwise none;
* `off` -- never render, the text placeholder stays;
* `kitty` / `iterm2` / `sixel` -- force one.

The sequences here are pure text; writing them is the app's job (it owns
the driver and the placement bookkeeping). A terminal that was guessed
wrong simply prints an escape-riddled line once -- the fallback for every
failure is the same `[image: filename]` placeholder as before.
"""

from __future__ import annotations

import base64
import io
from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL.Image import Image as PillowImage

CONFIG_ENV = "MATTERWAVE_IMAGES"
IMAGE_ID_BASE = 900
KITTY_CHUNK = 4096
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# A sixel picture larger than this is downscaled (Pillow) before encoding;
# a screenshot is welcome, a 4000-pixel camera photo is not.
SIXEL_MAX_PX = 800

OFF_VALUES = frozenset({"off", "0", "false", "no"})
FORCED_VALUES = frozenset({"kitty", "iterm2", "sixel"})
# `TERM_PROGRAM` values of terminals that speak the kitty protocol.
KITTY_PROGRAMS = frozenset({"kitty", "ghostty", "wezterm"})
# `TERM` values (and prefixes) of terminals that draw sixel.
SIXEL_TERMS = ("foot", "mlterm", "contour", "yaft")

Backend = str  # "kitty" | "iterm2" | "sixel"


def detect_backend(env: Mapping[str, str]) -> Backend | None:
    """Which graphics protocol to use, honouring the `MATTERWAVE_IMAGES`
    toggle: `off` is never, `kitty`/`iterm2`/`sixel` force one, anything
    else asks the terminal's env (see the module's note)."""
    config = env.get(CONFIG_ENV, "").strip().lower()
    if config in OFF_VALUES:
        return None
    if config in FORCED_VALUES:
        return config
    term = (env.get("TERM") or "").lower()
    term_program = (env.get("TERM_PROGRAM") or "").lower()
    if (
        env.get("KITTY_WINDOW_ID")
        or env.get("KITTY_PID")
        or env.get("GHOSTTY_RESOURCES_DIR")
        or "kitty" in term
        or "ghostty" in term
        or "kitty" in term_program
        or term_program in KITTY_PROGRAMS
    ):
        return "kitty"
    if term_program == "iterm.app" or (env.get("LC_TERMINAL") or "").lower() == "iterm2":
        return "iterm2"
    if (
        "sixel" in term
        or term.startswith(SIXEL_TERMS)
        or env.get("MLTERM")
        or env.get("KONSOLE_VERSION")
        or env.get("WT_SESSION")
        or (env.get("TERMINAL_NAME") or "").lower() == "contour"
    ):
        return "sixel"
    return None


# --- the kitty graphics protocol ------------------------------------------------


def is_png(data: bytes) -> bool:
    return data.startswith(PNG_MAGIC)


def kitty_payload(data: bytes) -> bytes | None:
    """The bytes kitty will accept as `f=100`: PNG is passed through whole,
    anything else goes through Pillow -- which is optional, so a JPEG on a
    machine without it falls back to the placeholder."""
    if is_png(data):
        return data
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            out = io.BytesIO()
            image.convert("RGBA").save(out, format="PNG")
            return out.getvalue()
    except (OSError, ValueError):
        return None


def kitty_transmit_chunks(image_id: int, png: bytes) -> list[str]:
    """The `a=T` transmission: PNG bytes as base64, 4096-byte chunks, the
    first carrying the keys and each but the last marked `m=1`. Shown where
    the cursor sits when the last chunk lands."""
    payload = base64.b64encode(png)
    chunks = [payload[i : i + KITTY_CHUNK] for i in range(0, len(payload), KITTY_CHUNK)]
    if not chunks:
        chunks = [b""]
    out: list[str] = []
    last = len(chunks) - 1
    for n, chunk in enumerate(chunks):
        keys: list[str] = []
        if n == 0:
            keys += ["a=T", "f=100", f"i={image_id}"]
        if n < last:
            keys.append("m=1")
        elif n > 0:
            keys.append("m=0")
        out.append(f"\x1b_G{','.join(keys)};{chunk.decode('ascii')}\x1b\\")
    return out


def kitty_place_at_cursor(image_id: int, columns: int, rows: int) -> str:
    """The placement for a transmitted image, sized to the cells under it.
    The cursor has to be moved first (`move_cursor_to`), so the app writes
    the pair together."""
    return f"\x1b_Ga=A,i={image_id},c={max(columns, 1)},r={max(rows, 1)}\x1b\\"


def move_cursor_to(column: int, row: int) -> str:
    """A 1-based CSI move: where the next graphics write goes."""
    return f"\x1b[{max(row, 1)};{max(column, 1)}H"


def kitty_delete(image_id: int) -> str:
    """Take one placement down (the image leaves the screen with it)."""
    return f"\x1b_Ga=d,d=i,i={image_id}\x1b\\"


def kitty_placement(
    image_id: int, column: int, row: int, columns: int, rows: int, data: bytes
) -> list[str]:
    """The writes for one kitty placement: move, transmit, place. An empty
    list means the bytes will not fly (no Pillow and not a PNG): the
    caller's signal to fall back."""
    png = kitty_payload(data)
    if png is None:
        return []
    return [
        move_cursor_to(column, row),
        *kitty_transmit_chunks(image_id, png),
        kitty_place_at_cursor(image_id, columns, rows),
    ]


# --- iTerm2 inline images --------------------------------------------------------


def iterm2_sequence(columns: int, rows: int, data: bytes) -> str:
    """The OSC 1337 `File` write: the bytes as base64, drawn inline at the
    cursor and scaled into the cells under it. iTerm2 decodes PNG, JPEG
    and GIF itself, so no Pillow."""
    payload = base64.b64encode(data).decode("ascii")
    keys = (
        f"inline=1;size={len(data)};width={max(columns, 1)};height={max(rows, 1)};"
        "preserveAspectRatio=1"
    )
    return f"\x1b]1337;File={keys}:{payload}\x07"


# --- sixel ----------------------------------------------------------------------


def sixel_stream(data: bytes) -> bytes | None:
    """The bytes as a sixel stream, or None when Pillow is missing or they
    are not an image it reads. The encoder is the plain DEC one: a
    256-colour palette, six-row bands, runs written as `!n`."""
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            rgb = image.convert("RGB")
        long_side = max(rgb.width, rgb.height)
        if long_side > SIXEL_MAX_PX:
            scale = SIXEL_MAX_PX / long_side
            rgb = rgb.resize((max(1, round(rgb.width * scale)), max(1, round(rgb.height * scale))))
        indexed = rgb.quantize(colors=256)
        return b"\x1bPq" + _sixel_body(indexed) + b"\x1b\\"
    except (OSError, ValueError):
        return None


def _sixel_body(image: PillowImage) -> bytes:
    """The quantized image as sixel: the used colours defined, then per
    six-row band, per colour, the bits of its columns. A quantized image
    is mode `P`, one palette index per pixel, so its bytes are the
    indices, row by row."""
    width, height = image.size
    indices = image.tobytes()
    used = sorted(set(indices))
    palette = image.getpalette() or []
    body = io.BytesIO()
    for colour in used:
        channel = colour * 3
        if channel + 2 < len(palette):
            red, green, blue = (palette[channel + n] * 100 // 255 for n in range(3))
            body.write(f"#{colour};2;{red};{green};{blue}".encode("ascii"))
    for top in range(0, height, 6):
        body.write(b"$")
        for colour in used:
            runs = [
                _sixel_bits(indices, width, column, top, colour, height) for column in range(width)
            ]
            if not any(runs):
                continue
            body.write(f"#{colour}".encode("ascii"))
            column = 0
            while column < width:
                bits = runs[column]
                span = 1
                while column + span < width and runs[column + span] == bits:
                    span += 1
                char = chr(0x3F + bits)
                body.write((f"!{span}{char}" if span > 3 else char * span).encode("ascii"))
                column += span
        body.write(b"-")
    return body.getvalue()


def _sixel_bits(indices: bytes, width: int, column: int, top: int, colour: int, height: int) -> int:
    """The six bits column `column` holds for `colour` in the band whose
    first row is `top`."""
    bits = 0
    for offset in range(6):
        row = top + offset
        if row < height and indices[row * width + column] == colour:
            bits |= 1 << offset
    return bits


# --- the placement the app keeps ------------------------------------------------


class Placement:
    """One image the layer has on screen: where it went and what to write
    to bring it there. `remove` is what takes it down again (empty for
    sixel, which has no delete-by-id)."""

    __slots__ = ("image_id", "remove", "sequence", "x", "y")

    def __init__(self, image_id: int, x: int, y: int, sequence: str, remove: str) -> None:
        self.image_id = image_id
        self.x = x
        self.y = y
        self.sequence = sequence
        self.remove = remove


def iterm2_placement(
    image_id: int, column: int, row: int, columns: int, rows: int, data: bytes
) -> Placement:
    """An iTerm2 placement at a cell. Like sixel, the picture has no id to
    delete by: the next redraw of those cells is what takes it down."""
    return Placement(
        image_id,
        column,
        row,
        move_cursor_to(column, row) + iterm2_sequence(columns, rows, data),
        "",
    )


def sixel_placement(image_id: int, column: int, row: int, data: bytes) -> Placement | None:
    """A sixel placement at a cell, or None when the bytes will not encode
    (no Pillow, or not an image)."""
    stream = sixel_stream(data)
    if stream is None:
        return None
    return Placement(
        image_id,
        column,
        row,
        move_cursor_to(column, row) + stream.decode("latin-1"),
        "",
    )


def new_image_id(sequence: int) -> int:
    """A kitty image id from the layer's counter, offset out of the way of
    anything a client library might mint."""
    return IMAGE_ID_BASE + sequence
