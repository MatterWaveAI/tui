"""Dictation's client half: the microphone, when an extra provides it, and
always the file a user may name instead.

The server route (`POST /api/speech/transcribe`, one clip in, its words
out) is what the web app's composer posts to, so the request shape here is
that one: multipart `file`, optional `language`. The transcript is text
for the composer -- nothing here sends it.

The microphone needs `sounddevice`, an optional dependency
(`pip install matterwave-tui[dictation]`): absent, dictation falls back to
transcribing an audio file the user names, which is the graceful half of
the extras pattern. A recording is held in memory as WAV bytes and nowhere
else, matching what the server promises about its side.
"""

from __future__ import annotations

import io
import wave
from importlib.util import find_spec
from pathlib import Path
from typing import Any

# 16 kHz mono PCM16: what every transcriber expects of a voice clip, and a
# fifth of the server's 10 MiB cap for a full minute.
SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2
MAX_RECORDING_S = 60

# The suffixes the server's transcriber accepts, with the media type each
# becomes. Anything else is refused here, before an upload that would 415.
MEDIA_TYPES: dict[str, str] = {
    ".webm": "audio/webm",
    ".ogg": "audio/ogg",
    ".mp4": "audio/mp4",
    ".m4a": "audio/m4a",
    ".mp3": "audio/mp3",
    ".mpeg": "audio/mpeg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".aac": "audio/aac",
}


def mic_available() -> bool:
    """Whether the optional microphone extra is installed."""
    return find_spec("sounddevice") is not None


def media_type_for(filename: str) -> str | None:
    """The media type a clip of this name is sent as, or None when the
    server would refuse it."""
    suffix = Path(filename).suffix.lower()
    return MEDIA_TYPES.get(suffix)


def wav_bytes(pcm: bytes) -> bytes:
    """Raw mono PCM16 frames as a WAV clip the transcribe route accepts."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as clip:
        clip.setnchannels(CHANNELS)
        clip.setsampwidth(SAMPLE_WIDTH)
        clip.setframerate(SAMPLE_RATE)
        clip.writeframes(pcm)
    return buffer.getvalue()


class MicRecorder:
    """The microphone from `start()` to `stop()`, as WAV bytes. Frames
    arrive on sounddevice's own thread and are only ever appended, so the
    event loop is not touched."""

    def __init__(self) -> None:
        # The RawInputStream, typed loosely: the extra is optional and
        # ships no type information, and this class touches nothing on it
        # but start, stop and close.
        self._stream: Any = None
        self._frames: list[bytes] = []
        self.overflowed = False

    def _on_frames(self, indata: Any, frames: int, state: Any, time: Any) -> None:
        del frames, time
        self._frames.append(bytes(indata))
        if getattr(state, "input_overflow", False):
            self.overflowed = True

    def start(self) -> None:
        import sounddevice  # noqa: PLC0415

        self._frames = []
        self._stream = sounddevice.RawInputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            callback=self._on_frames,
        )
        self._stream.start()

    def stop(self) -> bytes:
        """End the recording and return the clip. An empty clip means
        nothing was captured (or the device never delivered a frame)."""
        stream = self._stream
        self._stream = None
        if stream is not None:
            stream.stop()
            stream.close()
        if not self._frames:
            return b""
        return wav_bytes(b"".join(self._frames))

    def discard(self) -> None:
        """Abandon a recording: close the microphone, keep nothing."""
        stream = self._stream
        self._stream = None
        self._frames = []
        if stream is not None:
            stream.stop()
            stream.close()
