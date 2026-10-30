"""Dictation's client half: what clip names become on the wire, the WAV a
recording becomes, and the graceful half of the extras pattern."""

from __future__ import annotations

import io
import wave

from matterwave_tui import dictation


def test_media_types_are_the_server_transcribers_vocabulary():
    assert dictation.media_type_for("clip.wav") == "audio/wav"
    assert dictation.media_type_for("clip.WAV") == "audio/wav"
    assert dictation.media_type_for("note.webm") == "audio/webm"
    assert dictation.media_type_for("voice memo.m4a") == "audio/m4a"
    # A suffix the route refuses is caught here, before an upload that
    # would come back 415.
    assert dictation.media_type_for("notes.txt") is None
    assert dictation.media_type_for("no-extension") is None


def test_pcm_frames_become_a_wav_clip():
    clip = dictation.wav_bytes(b"\x00\x01" * 16)
    with wave.open(io.BytesIO(clip)) as audio:
        assert audio.getnchannels() == dictation.CHANNELS
        assert audio.getsampwidth() == dictation.SAMPLE_WIDTH
        assert audio.getframerate() == dictation.SAMPLE_RATE
        assert audio.getnframes() == 16


def test_the_microphone_is_optional_and_reports_itself():
    # The extra may be installed or not; the answer must match reality and
    # never raise, since the app asks it on the dictate path.
    assert dictation.mic_available() in (True, False)


def test_a_recorder_without_the_extra_stops_to_silence():
    # The recorder object itself works without sounddevice: a start that
    # never happened stops to an empty clip, and a discard closes nothing.
    recorder = dictation.MicRecorder()
    assert recorder.stop() == b""
    recorder.discard()
    assert recorder.stop() == b""
