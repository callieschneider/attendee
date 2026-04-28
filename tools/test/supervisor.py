"""
Overnight test supervisor.

Loads cases.json, creates one shared bot (or reuses an env-supplied one),
waits for it to join the meeting, then for each case:

  1. Snapshot canvas state.json baseline.
  2. Inject the case's utterance via Layer 1 (POST inject-utterance).
  3. Sleep `wait_seconds`.
  4. Snapshot canvas state.json result.
  5. Diff -> check assertions -> classify pass / fail.
  6. Write a markdown report under runs/<ts>/<case_id>.md.

After the run, writes runs/<ts>/SUMMARY.md.

Stop early: `touch tools/test/runs/STOP`.

Required env (read from `.env.harness` if present):
  AGENT_DEBUG_TOKEN  — auth for inject-utterance.
  AGENT_BASE_URL     — base URL of the web service.
  TEST_MEET_URL      — the Google Meet URL to send the bot to.

Optional env:
  REUSE_BOT_ID       — skip create + join, run cases against an existing bot.
  TEST_MAX_CASES     — cap how many cases run (useful for smoke tests).
  PRIORITY_FILTER    — comma-separated subset like "priority_1_tool_reliability".
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request


HERE = pathlib.Path(__file__).resolve().parent
RUNS = HERE / "runs"
ENV_FILE = HERE / ".env.harness"
CASES_FILE = HERE / "cases.json"
STOP_FILE = RUNS / "STOP"


def _load_env_file():
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def _http_json(method: str, url: str, body: dict | None = None, headers: dict | None = None, timeout: int = 30) -> tuple[int, dict | str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def create_bot(base_url: str, meet_url: str) -> dict:
    code, body = _http_json(
        "POST",
        f"{base_url}/agent/api/create-meeting-bot",
        body={"meeting_url": meet_url, "bot_name": "Clever Star (overnight)"},
        timeout=60,
    )
    if code != 200 or not isinstance(body, dict):
        raise RuntimeError(f"create_meeting_bot failed: {code} {body!r}")
    return body


def fetch_state(base_url: str, bot_id: str) -> dict:
    code, body = _http_json("GET", f"{base_url}/agent/canvas/v2/{bot_id}/state.json", timeout=15)
    if code != 200 or not isinstance(body, dict):
        raise RuntimeError(f"state.json failed: {code} {body!r}")
    return body


def fetch_bot(base_url: str, bot_id: str) -> dict | str:
    """Read Attendee bot state via the public Attendee API."""
    api_key = os.getenv("ATTENDEE_API_KEY", "")
    if not api_key:
        return "no ATTENDEE_API_KEY"
    code, body = _http_json(
        "GET",
        f"{base_url}/api/v1/bots/{bot_id}",
        headers={"Authorization": f"Token {api_key}"},
        timeout=15,
    )
    if code != 200:
        return f"bot lookup HTTP {code}"
    return body if isinstance(body, dict) else {"raw": body}


def wait_for_join(base_url: str, bot_id: str, timeout: int = 180) -> bool:
    """Block until the canvas snapshot shows transcript activity OR action_log entries."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            s = fetch_state(base_url, bot_id)
            voice = s.get("voice", {}) or {}
            # gate_open implies the LiveSessionManager is up. We may have to wait
            # until the bot is JOINED_RECORDING for that to fire.
            if voice.get("gate_open") or s.get("transcript") or s.get("action_log"):
                return True
        except Exception:
            pass
        if STOP_FILE.exists():
            return False
        time.sleep(3)
    return False


def inject_utterance(base_url: str, token: str, bot_id: str, text: str, speaker: str = "Tester") -> bool:
    code, body = _http_json(
        "POST",
        f"{base_url}/agent/debug/inject-utterance",
        body={"bot_id": bot_id, "text": text, "speaker": speaker},
        headers={"X-Debug-Token": token},
        timeout=15,
    )
    return code == 200


def diff_state(before: dict, after: dict, *, inject_ts_iso: str | None = None) -> dict:
    """
    Compare two state.json snapshots. If `inject_ts_iso` is supplied,
    "new actions" are filtered to those whose `t` is strictly greater —
    that way an action that existed before this case (e.g., from the
    previous case running long) doesn't leak into this case's diff.
    """
    actions_after = after.get("action_log", []) or []
    if inject_ts_iso is not None:
        new_actions = [a for a in actions_after if (a.get("t") or "") > inject_ts_iso]
    else:
        actions_before = {(a.get("t"), a.get("tool")) for a in before.get("action_log", [])}
        new_actions = [a for a in actions_after if (a.get("t"), a.get("tool")) not in actions_before]

    notes_before = before.get("notes_md", "") or ""
    notes_after = after.get("notes_md", "") or ""
    notes_delta = notes_after[len(notes_before):] if notes_after.startswith(notes_before) else notes_after

    return {
        "active_tab_changed": before.get("active_tab") != after.get("active_tab"),
        "active_tab": after.get("active_tab"),
        "new_actions": new_actions,
        "notes_grew_by": len(notes_after) - len(notes_before),
        "notes_delta": notes_delta[:600],
        "dashboard_before": before.get("dashboard") or {},
        "dashboard_after": after.get("dashboard") or {},
        "focus_session_changed": (before.get("focus") or {}).get("session_id") != (after.get("focus") or {}).get("session_id"),
        "focus_text_after": (after.get("focus") or {}).get("text", "")[:600],
        "transcript_grew_by": len(after.get("transcript", []) or []) - len(before.get("transcript", []) or []),
    }


def poll_until_satisfied(
    base: str,
    bot_id: str,
    case: dict,
    inject_ts_iso: str,
    before: dict,
    max_wait_s: int,
):
    """
    Poll state.json every ~1.5s up to `max_wait_s`. Return as soon as
    the expectations for this case are satisfied, OR when the wait
    window elapses (whichever comes first). This is the fix for the
    flaky 6/8 we kept seeing — Gemini sometimes takes longer than the
    fixed wait_seconds and the action would land in the next case's
    window, causing a false fail here AND a false pass there.
    """
    deadline = time.monotonic() + max_wait_s
    last_diff = None
    last_eval: tuple[bool, list[str]] = (False, ["no snapshot yet"])
    while True:
        try:
            after = fetch_state(base, bot_id)
        except Exception as e:
            after = before
            last_eval = (False, [f"snapshot failed: {e}"])
        last_diff = diff_state(before, after, inject_ts_iso=inject_ts_iso)
        last_eval = evaluate(case, last_diff)
        if last_eval[0]:
            return after, last_diff, last_eval
        if time.monotonic() >= deadline:
            return after, last_diff, last_eval
        time.sleep(1.5)


def evaluate(case: dict, diff: dict) -> tuple[bool, list[str]]:
    expect = case.get("expect", {}) or {}
    fails: list[str] = []

    expected_tools = expect.get("tool_called") or []
    if expected_tools:
        called = {a.get("tool") for a in diff["new_actions"]}
        if not any(t in called for t in expected_tools):
            fails.append(f"expected one of {expected_tools}, got {sorted(called) or 'no new actions'}")

    if "notes_md_contains" in expect:
        needle = expect["notes_md_contains"].lower()
        haystack = (diff["notes_delta"] or "").lower()
        if needle not in haystack:
            fails.append(f"notes_delta missing substring {needle!r}")

    if expect.get("dashboard_changed"):
        if diff["dashboard_before"] == diff["dashboard_after"]:
            fails.append("dashboard payload unchanged")

    if "active_tab" in expect:
        want = expect["active_tab"]
        got = diff["active_tab"]
        if got != want:
            fails.append(f"active_tab expected {want!r} got {got!r}")

    if expect.get("focus_text_nonempty") and not diff["focus_text_after"]:
        fails.append("focus text empty")

    return (len(fails) == 0, fails)


def write_case_report(case: dict, diff: dict, ok: bool, fails: list[str], dest: pathlib.Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# {case['id']} — {'PASS' if ok else 'FAIL'}",
        "",
        f"- category: {case.get('category')}",
        f"- utterance: {case['utterance']!r}",
        "",
        "## Failures" if not ok else "## Notes",
    ]
    if fails:
        for f in fails:
            lines.append(f"- {f}")
    else:
        lines.append("- all expectations met")
    lines += [
        "",
        "## Diff",
        f"- active_tab_changed: {diff['active_tab_changed']} -> {diff['active_tab']}",
        f"- notes_grew_by: {diff['notes_grew_by']} bytes",
        f"- notes_delta: {(diff['notes_delta'] or '')[:200]!r}",
        f"- new_actions: {json.dumps(diff['new_actions'], indent=2)}",
        f"- focus_session_changed: {diff['focus_session_changed']}",
        f"- focus_text_after (first 200 chars): {diff['focus_text_after'][:200]!r}",
        f"- transcript_grew_by: {diff['transcript_grew_by']}",
        f"- dashboard_after: {json.dumps(diff['dashboard_after'], indent=2)[:600]}",
    ]
    dest.write_text("\n".join(lines))


def collect_cases(spec: dict, priority_filter: list[str] | None) -> list[dict]:
    out = []
    keys = list(spec.keys())
    if priority_filter:
        keys = [k for k in keys if k in priority_filter]
    for k in keys:
        v = spec.get(k)
        if isinstance(v, list):
            for c in v:
                if isinstance(c, dict) and "id" in c:
                    c.setdefault("priority", k)
                    out.append(c)
    return out


def write_summary(run_dir: pathlib.Path, results: list[tuple[dict, bool, list[str]]]):
    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    failed = total - passed
    by_cat: dict[str, list[tuple[str, bool]]] = {}
    for case, ok, _ in results:
        cat = case.get("category", "uncategorized")
        by_cat.setdefault(cat, []).append((case["id"], ok))

    lines = [
        "# Overnight Run — SUMMARY",
        "",
        f"- total: {total}",
        f"- passed: {passed}",
        f"- failed: {failed}",
        f"- run dir: {run_dir.name}",
        "",
        "## By category",
    ]
    for cat, items in sorted(by_cat.items()):
        passed_n = sum(1 for _, ok in items if ok)
        lines.append(f"### {cat} ({passed_n}/{len(items)})")
        for cid, ok in items:
            lines.append(f"- {'PASS' if ok else 'FAIL'} {cid}")
        lines.append("")

    lines += [
        "## Failures (top 20)",
    ]
    fails = [(c["id"], reasons) for c, ok, reasons in results if not ok][:20]
    if not fails:
        lines.append("- none")
    else:
        for cid, reasons in fails:
            lines.append(f"### {cid}")
            for r in reasons:
                lines.append(f"- {r}")
            lines.append("")

    (run_dir / "SUMMARY.md").write_text("\n".join(lines))


def main():
    _load_env_file()
    p = argparse.ArgumentParser()
    p.add_argument("--cases", default=str(CASES_FILE))
    p.add_argument("--max", type=int, default=int(os.getenv("TEST_MAX_CASES") or 0))
    p.add_argument("--priorities", default=os.getenv("PRIORITY_FILTER", "priority_1_tool_reliability"))
    p.add_argument("--reuse-bot", default=os.getenv("REUSE_BOT_ID", ""))
    args = p.parse_args()

    base = os.getenv("AGENT_BASE_URL", "https://meeting-agent-web-production.up.railway.app").rstrip("/")
    token = os.getenv("AGENT_DEBUG_TOKEN", "")
    meet = os.getenv("TEST_MEET_URL", "")
    if not token:
        print("AGENT_DEBUG_TOKEN missing", file=sys.stderr)
        sys.exit(2)

    spec = json.loads(pathlib.Path(args.cases).read_text())
    cases = collect_cases(spec, [c.strip() for c in args.priorities.split(",") if c.strip()])
    if args.max and len(cases) > args.max:
        cases = cases[: args.max]

    ts = dt.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    run_dir = RUNS / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"run dir: {run_dir}")

    if args.reuse_bot:
        bot_id = args.reuse_bot
        print(f"reusing bot {bot_id}")
    else:
        if not meet:
            print("TEST_MEET_URL missing and no --reuse-bot supplied", file=sys.stderr)
            sys.exit(2)
        b = create_bot(base, meet)
        bot_id = b["id"]
        print(f"created bot {bot_id} session {b.get('bridge_session_id')}")
        print("waiting for join...")
        if not wait_for_join(base, bot_id, timeout=240):
            print("bot never showed live activity (no transcript / no gate_open)", file=sys.stderr)
            sys.exit(3)
        print("bot is live")
        # tiny grace period so the system prompt is fully wired
        time.sleep(5)

    results: list[tuple[dict, bool, list[str]]] = []
    for i, case in enumerate(cases):
        if STOP_FILE.exists():
            print("STOP file present; exiting")
            break
        print(f"[{i+1}/{len(cases)}] {case['id']} — {case['utterance']!r}")
        try:
            before = fetch_state(base, bot_id)
        except Exception as e:
            print(f"  pre-snapshot failed: {e}")
            results.append((case, False, [f"pre-snapshot failed: {e}"]))
            continue
        # Capture inject timestamp BEFORE injecting so any action whose
        # `t` is strictly greater belongs to this case.
        inject_ts_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        if not inject_utterance(base, token, bot_id, case["utterance"], speaker=case.get("speaker", "Tester")):
            results.append((case, False, ["inject_utterance returned non-200"]))
            continue
        wait_s = int(case.get("expect", {}).get("wait_seconds", 12))
        # Poll until satisfied or deadline — return early on PASS so we
        # don't waste the rest of the budget waiting once Gemini's already
        # acted. Bounded by wait_seconds + 50% padding for slow turns.
        max_wait = max(wait_s + (wait_s // 2), wait_s + 5)
        after, diff, (ok, fails) = poll_until_satisfied(
            base, bot_id, case, inject_ts_iso, before, max_wait
        )
        # Brief drain so the next case's "before" snapshot doesn't include
        # actions still settling from this one. ~0.5s is enough; the
        # inject_ts filter prevents most leakage anyway.
        time.sleep(0.5)
        write_case_report(case, diff, ok, fails, run_dir / f"{case['id']}.md")
        results.append((case, ok, fails))
        print(f"  -> {'PASS' if ok else 'FAIL'}  {' | '.join(fails) if fails else ''}")

    write_summary(run_dir, results)
    print(f"\nSUMMARY: {sum(1 for _, ok, _ in results if ok)}/{len(results)} passed")
    print(f"reports under {run_dir}")


if __name__ == "__main__":
    main()
