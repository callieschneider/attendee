"""
Layer 2: synthetic Attendee audio source.

Pretends to be the Attendee bot's audio webhook. Connects to the bridge WS,
generates PCM audio from text via macOS `say` + ffmpeg/sox conversion, and
streams it as if it were Meet audio. The bridge's LiveSessionManager opens
Gemini Live, forwards the audio, and produces normal responses (TTS, tool
calls, canvas updates).

Doesn't reproduce Meet's audio-mixer echo loop — that's Layer 3 only — but
exercises the full Gemini Live audio path including VAD, gate, context
window, and tool call routing.

Usage:
    python tools/test/fake_meet_user.py <bridge_session_id> "your utterance"

bridge_session_id is the UUID returned in `metadata.bridge_session_id` from
POST /agent/api/create-meeting-bot. The bot must already be created in the
database. The bridge resolves session_id -> bot_id from Bot.metadata.

Streams audio in 50ms chunks, then sends a 700ms silence tail so VAD
end-of-speech fires and Gemini commits the turn.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

import websockets

HERE = pathlib.Path(__file__).resolve().parent
ENV_FILE = HERE / ".env.harness"

SAMPLE_RATE = 16000
CHUNK_MS = 50
TAIL_MS = 700  # trailing silence so Gemini's end-of-speech VAD fires


def _load_env_file():
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def _which_or_die(name: str) -> str:
    p = shutil.which(name)
    if not p:
        print(f"missing required CLI: {name}", file=sys.stderr)
        sys.exit(2)
    return p


def text_to_pcm16(text: str, voice: str = "Samantha") -> bytes:
    """
    macOS pipeline:
        say -v <voice> -o aiff_path "text"
        ffmpeg/afconvert -> 16k mono s16le PCM
    Returns raw PCM bytes (no header).
    """
    if sys.platform != "darwin":
        raise RuntimeError("text_to_pcm16 currently macOS-only (uses `say`).")
    say = _which_or_die("say")
    # Prefer afconvert (always installed on macOS); fall back to ffmpeg if
    # someone has stripped it out.
    afconvert = shutil.which("afconvert")
    ffmpeg = shutil.which("ffmpeg")
    if not afconvert and not ffmpeg:
        print("need afconvert (default macOS) or ffmpeg installed", file=sys.stderr)
        sys.exit(2)

    with tempfile.TemporaryDirectory() as td:
        aiff = pathlib.Path(td) / "u.aiff"
        wav = pathlib.Path(td) / "u.wav"
        subprocess.run([say, "-v", voice, "-o", str(aiff), text], check=True)
        if afconvert:
            # 16kHz, mono, 16-bit linear PCM, little-endian, .wav (so we get a
            # 44-byte header to skip).
            subprocess.run(
                [
                    afconvert,
                    "-f", "WAVE",
                    "-d", "LEI16@16000",
                    "-c", "1",
                    str(aiff),
                    str(wav),
                ],
                check=True,
            )
            data = wav.read_bytes()
            return data[44:]  # skip RIFF/WAVE/fmt/data header
        else:
            raw = pathlib.Path(td) / "u.raw"
            subprocess.run(
                [
                    ffmpeg, "-y", "-i", str(aiff),
                    "-ar", str(SAMPLE_RATE),
                    "-ac", "1",
                    "-f", "s16le",
                    str(raw),
                ],
                check=True, capture_output=True,
            )
            return raw.read_bytes()


def chunks(data: bytes, chunk_bytes: int):
    for i in range(0, len(data), chunk_bytes):
        yield data[i : i + chunk_bytes]


async def send_utterance(bridge_ws_url: str, text: str, voice: str = "Samantha", post_silence_ms: int = TAIL_MS):
    pcm = text_to_pcm16(text, voice=voice)
    silence = b"\x00\x00" * (SAMPLE_RATE * post_silence_ms // 1000)
    data = pcm + silence

    bytes_per_chunk = (SAMPLE_RATE * CHUNK_MS // 1000) * 2  # 16-bit samples = 2B each
    sleep_per_chunk = CHUNK_MS / 1000.0

    async with websockets.connect(bridge_ws_url, max_size=20 * 1024 * 1024) as ws:
        # The bridge expects realtime_audio.mixed-shaped JSON messages from
        # Attendee. See agent/live_session/manager.py:_attendee_audio_pump.
        sent = 0
        for ch in chunks(data, bytes_per_chunk):
            msg = {
                "trigger": "realtime_audio.mixed",
                "data": {
                    "chunk": base64.b64encode(ch).decode(),
                    "sample_rate": SAMPLE_RATE,
                },
            }
            await ws.send(json.dumps(msg))
            sent += 1
            await asyncio.sleep(sleep_per_chunk)
        return sent


def main():
    _load_env_file()
    p = argparse.ArgumentParser()
    p.add_argument("session_id", help="bridge_session_id UUID from create_meeting_bot response")
    p.add_argument("text")
    p.add_argument("--voice", default="Samantha")
    p.add_argument(
        "--bridge",
        default=os.getenv(
            "AGENT_BRIDGE_URL",
            "wss://meeting-agent-bridge-production.up.railway.app",
        ),
    )
    args = p.parse_args()
    url = f"{args.bridge.rstrip('/')}/audio/{args.session_id}"
    print(f"connecting -> {url}")
    sent = asyncio.run(send_utterance(url, args.text, voice=args.voice))
    print(f"sent {sent} chunks")


if __name__ == "__main__":
    main()
