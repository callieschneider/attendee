"""
Layer 3 helper: speak text into Meet via BlackHole virtual mic.

Generates TTS via macOS `say`, converts to a PCM WAV, then plays it
to a chosen output device. The expected setup:

  - Default audio output: any (we don't change it)
  - Cursor browser's Meet tab: mic = "BlackHole 2ch"
  - Playback target: "Multi-Output Device" (so BlackHole captures it
    AND your speakers play it so you can hear the test)

Usage:
    python tools/test/speak_to_meet.py "what to say"
    python tools/test/speak_to_meet.py --device "BlackHole 2ch" "..."
    python tools/test/speak_to_meet.py --voice Daniel "..."

Requires `SwitchAudioSource` (brew install switchaudio-osx) so we can
flip the output device just for the duration of playback and restore
the user's previous default afterwards.
"""
from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import sys
import tempfile


def _which_or_die(name: str) -> str:
    p = shutil.which(name)
    if not p:
        print(f"missing: {name}", file=sys.stderr)
        sys.exit(2)
    return p


def current_output() -> str:
    sas = _which_or_die("SwitchAudioSource")
    return subprocess.check_output([sas, "-c", "-t", "output"]).decode().strip()


def set_output(device: str) -> None:
    sas = _which_or_die("SwitchAudioSource")
    subprocess.run([sas, "-s", device, "-t", "output"], check=True)


def speak(text: str, *, voice: str = "Samantha", device: str = "Multi-Output Device", restore: bool = True):
    """Generate TTS, route audio to `device`, play, optionally restore output."""
    say = _which_or_die("say")
    afplay = _which_or_die("afplay")

    with tempfile.TemporaryDirectory() as td:
        aiff = pathlib.Path(td) / "u.aiff"
        subprocess.run([say, "-v", voice, "-o", str(aiff), text], check=True)
        prev = current_output() if restore else None
        set_output(device)
        try:
            subprocess.run([afplay, str(aiff)], check=True)
        finally:
            if prev:
                try:
                    set_output(prev)
                except Exception:
                    pass


def main():
    p = argparse.ArgumentParser()
    p.add_argument("text")
    p.add_argument("--voice", default="Samantha")
    p.add_argument("--device", default="Multi-Output Device")
    p.add_argument("--no-restore", action="store_true", help="don't restore previous output")
    args = p.parse_args()
    speak(args.text, voice=args.voice, device=args.device, restore=not args.no_restore)


if __name__ == "__main__":
    main()
