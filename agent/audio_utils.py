"""
PCM16 audio utilities for the realtime bridge.
Uses stdlib audioop (Python 3.10) — no extra deps.
"""
import audioop
import base64

SAMPLE_WIDTH = 2   # 16-bit PCM = 2 bytes per sample
CHANNELS = 1       # mono


def pcm16_resample(pcm_bytes: bytes, from_rate: int, to_rate: int) -> bytes:
    """Resample PCM16 mono audio between sample rates using stdlib audioop."""
    if from_rate == to_rate or not pcm_bytes:
        return pcm_bytes
    # Ensure even byte count (16-bit samples must be 2-byte aligned)
    if len(pcm_bytes) % SAMPLE_WIDTH != 0:
        pcm_bytes = pcm_bytes[: len(pcm_bytes) - (len(pcm_bytes) % SAMPLE_WIDTH)]
    resampled, _ = audioop.ratecv(
        pcm_bytes,
        SAMPLE_WIDTH,
        CHANNELS,
        from_rate,
        to_rate,
        None,
    )
    return resampled


def pcm16_to_b64(pcm_bytes: bytes) -> str:
    return base64.b64encode(pcm_bytes).decode("ascii")


def b64_to_pcm16(b64_str: str) -> bytes:
    return base64.b64decode(b64_str)
