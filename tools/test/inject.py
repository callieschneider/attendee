"""
Layer 1 helper: hit /agent/debug/inject-utterance with a single utterance.

Usage:
    python tools/test/inject.py <bot_id> "what to say"
    python tools/test/inject.py --speaker "Callie" <bot_id> "..."

Reads AGENT_DEBUG_TOKEN from env (or `.env.harness` next to this file).
Reads AGENT_BASE_URL from env (defaults to prod).
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.request


HERE = pathlib.Path(__file__).resolve().parent
ENV_FILE = HERE / ".env.harness"


def _load_env_file():
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def main():
    _load_env_file()
    p = argparse.ArgumentParser()
    p.add_argument("bot_id")
    p.add_argument("text")
    p.add_argument("--speaker", default="Tester")
    p.add_argument(
        "--base",
        default=os.getenv("AGENT_BASE_URL", "https://meeting-agent-web-production.up.railway.app"),
    )
    args = p.parse_args()

    token = os.getenv("AGENT_DEBUG_TOKEN", "")
    if not token:
        print("AGENT_DEBUG_TOKEN not set in env or .env.harness", file=sys.stderr)
        sys.exit(2)

    body = json.dumps({"bot_id": args.bot_id, "text": args.text, "speaker": args.speaker}).encode()
    req = urllib.request.Request(
        f"{args.base.rstrip('/')}/agent/debug/inject-utterance",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Debug-Token": token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            print(r.status, r.read().decode())
    except urllib.error.HTTPError as e:
        print("HTTP", e.code, e.read().decode(), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
