"""The image module's pure half: what terminal is detected, what kitty is
sent, and where the fallbacks bite. No terminal is involved."""

from __future__ import annotations

import base64

from matterwave_tui import images


def test_kitty_is_detected_from_its_env():
    assert images.detect_backend({"KITTY_WINDOW_ID": "1"}) == "kitty"
    assert images.detect_backend({"KITTY_PID": "42"}) == "kitty"
    assert images.detect_backend({"TERM": "xterm-kitty"}) == "kitty"
    assert images.detect_backend({"TERM": "xterm-256color",
                                  "TERM_PROGRAM": "ghostty-kitty"}) == "kitty"


def test_nothing_is_detected_without_a_tell():
    assert images.detect_backend({"TERM": "xterm-256color"}) is None
    assert images.detect_backend({}) is None


def test_the_config_toggle_decides():
    # Off wins over every tell.
    assert images.detect_backend({"MATTERWAVE_IMAGES": "off", "KITTY_WINDOW_ID": "1"}) is None
    assert images.detect_backend({"MATTERWAVE_IMAGES": "0", "TERM": "xterm-kitty"}) is None
    # Either protocol can be forced, with or without a tell.
    assert images.detect_backend({"MATTERWAVE_IMAGES": "kitty"}) == "kitty"
    assert images.detect_backend({"MATTERWAVE_IMAGES": "Sixel"}) == "sixel"
    assert images.detect_backend({"MATTERWAVE_IMAGES": "sixel"}) == "sixel"
    # Anything else auto-detects; sixel has no probe, so only kitty answers.
    assert images.detect_backend({"MATTERWAVE_IMAGES": "auto", "TERM": "xterm-kitty"}) == "kitty"
    assert images.detect_backend({"MATTERWAVE_IMAGES": "on"}) is None


def test_png_bytes_pass_through_whole():
    png = images.PNG_MAGIC + b"rest of the file"
    assert images.is_png(png)
    assert images.kitty_payload(png) == png


def test_a_jpeg_needs_pillow_which_may_not_be_there():
    jpeg = b"\xff\xd8\xff" + b"rest"
    payload = images.kitty_payload(jpeg)
    # With Pillow the bytes come back as PNG; without it the caller falls
    # back to the placeholder. Either answer is fine, but never a crash.
    assert payload is None or images.is_png(payload)


def test_a_transmission_is_chunked_at_4096_bytes_of_base64():
    png = images.PNG_MAGIC + bytes(7000)  # base64 spans three chunks
    chunks = images.kitty_transmit_chunks(7, png)
    assert len(chunks) == 3
    # The first chunk carries the keys and says more is coming; the last
    # says it is the end; the middles carry neither key nor end.
    assert chunks[0].startswith("\x1b_Ga=T,f=100,i=7,m=1;")
    assert chunks[-1].startswith("\x1b_Gm=0;")
    assert chunks[-1].endswith("\x1b\\")
    assert "a=T" not in chunks[1]
    assert chunks[1].startswith("\x1b_Gm=1;")
    # Joining the payloads back up gives the whole file again.
    payload = "".join(chunk.split(";", 1)[1].removesuffix("\x1b\\") for chunk in chunks)
    assert base64.b64decode(payload) == png


def test_a_small_image_is_one_transmission_with_no_continuation():
    chunks = images.kitty_transmit_chunks(3, images.PNG_MAGIC + b"x")
    assert len(chunks) == 1
    assert chunks[0] == (
        f"\x1b_Ga=T,f=100,i=3;{base64.b64encode(images.PNG_MAGIC + b'x').decode()}\x1b\\"
    )


def test_placement_and_delete_name_the_image():
    place = images.kitty_place_at_cursor(9, 40, 6)
    assert place == "\x1b_Ga=A,i=9,c=40,r=6\x1b\\"
    # A degenerate cell size still makes a legal request.
    assert images.kitty_place_at_cursor(9, 0, 0) == "\x1b_Ga=A,i=9,c=1,r=1\x1b\\"
    assert images.kitty_delete(9) == "\x1b_Ga=d,d=i,i=9\x1b\\"
    assert images.move_cursor_to(4, 7) == "\x1b[7;4H"


def test_kitty_placement_is_move_transmit_place_and_falls_back_to_nothing():
    png = images.PNG_MAGIC + b"tiny"
    sequence = images.kitty_placement(5, 1, 1, 40, 6, png)
    assert sequence[0] == "\x1b[1;1H"
    assert sequence[1].startswith("\x1b_Ga=T,f=100,i=5;")
    assert sequence[-1] == "\x1b_Ga=A,i=5,c=40,r=6\x1b\\"
    # Not a PNG: without Pillow there is nothing to send, and with Pillow
    # these bytes open as no image either — the caller falls back either way.
    assert images.kitty_placement(5, 1, 1, 40, 6, b"\xff\xd8not really") == []


def test_sixel_needs_pillow_and_a_readable_image():
    stream = images.sixel_stream(images.PNG_MAGIC + b"junk")
    # With Pillow a junk "PNG" fails to open (None); without Pillow nothing
    # can be encoded at all (also None). Sixel placement agrees.
    assert stream is None
    assert images.sixel_placement(1, 1, 1, images.PNG_MAGIC + b"junk") is None


def test_ids_are_offset_from_everyday_counters():
    assert images.new_image_id(0) == images.IMAGE_ID_BASE
    assert images.new_image_id(3) == images.IMAGE_ID_BASE + 3
