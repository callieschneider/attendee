"""
Audio chimes played to the meeting on agent state transitions.

Two short tones are synthesized at module load (16-bit mono PCM
@ 16 kHz, the same format Attendee expects on
`realtime_audio.bot_output`). They are sent through the same audio
pipeline as Gemini's TTS so every meeting participant hears them.

  WAKE_CHIME  — ascending two-note, played when the audio gate
                opens (bot starts listening).
  SLEEP_CHIME — descending two-note, played when the audio gate
                closes (bot stops listening).
"""
from __future__ import annotations

import math
import struct

ATTENDEE_SAMPLE_RATE = 16000  # Hz, must match manager.py


def _envelope(t: float, dur: float, attack: float = 0.015, release: float = 0.04) -> float:
    """ADSR-lite: linear attack, full sustain, smooth release."""
    if t < attack:
        return t / attack
    if t > dur - release:
        x = (dur - t) / release
        return max(0.0, x)
    return 1.0


def _tone(freq: float, dur_s: float, sample_rate: int = ATTENDEE_SAMPLE_RATE,
          amp: float = 0.35) -> bytes:
    """Generate a sine tone with a soft envelope as little-endian PCM16."""
    n_samples = int(dur_s * sample_rate)
    samples = bytearray(n_samples * 2)
    two_pi_f = 2.0 * math.pi * freq
    for i in range(n_samples):
        t = i / sample_rate
        env = _envelope(t, dur_s)
        v = math.sin(two_pi_f * t) * env * amp
        s = max(-1.0, min(1.0, v))
        struct.pack_into("<h", samples, i * 2, int(s * 32767))
    return bytes(samples)


def _silence(dur_s: float, sample_rate: int = ATTENDEE_SAMPLE_RATE) -> bytes:
    return b"\x00\x00" * int(dur_s * sample_rate)


def _build_wake_chime() -> bytes:
    # Ascending: A4 (440) → E5 (660). Bright, "I'm listening".
    return (
        _tone(440.0, 0.090)
        + _silence(0.020)
        + _tone(660.0, 0.140)
    )


def _build_sleep_chime() -> bytes:
    # Descending: E5 (660) → A4 (440). Soft, "I'm out".
    return (
        _tone(660.0, 0.090)
        + _silence(0.020)
        + _tone(440.0, 0.140)
    )


WAKE_CHIME: bytes = _build_wake_chime()
SLEEP_CHIME: bytes = _build_sleep_chime()
