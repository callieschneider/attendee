"""
Server-side canvas renderer — draws a PNG snapshot of what Clever Star is
doing right now, to be pushed into the meeting as the bot's image/video feed
via Attendee's POST /api/v1/bots/<id>/output_image endpoint.

Attendee's webpage streamer requires a k8s sidecar that we don't have on
Railway, so this is how we get "something on screen" without that.

The rendered image is a 1280×720 PNG with:
  - Left half: live debug info (gate, cursor, recent transcript, actions)
  - Right half: visualization panel (chart spec or Clever Star branding)
"""
from __future__ import annotations

import io
import json
import logging
from typing import Any

from django.utils import timezone

log = logging.getLogger("agent.canvas.renderer")


_WIDTH = 1280
_HEIGHT = 720

# Color palette — mirrors the HTML canvas
BG = (10, 11, 15)
BG_DIM = (15, 17, 23)
BORDER = (31, 41, 55)
MUTED = (107, 114, 128)
FG = (229, 231, 235)
ACCENT = (99, 102, 241)
ACCENT_SOFT = (165, 180, 252)
OK = (110, 231, 183)
WARN = (251, 191, 36)
ERR = (252, 165, 165)
SPEAKER = (147, 197, 253)
SPEAKER_CHAT = (249, 168, 212)
SPEAKER_ACTION = (252, 211, 77)


def render_canvas_png(bot_id: str) -> bytes:
    """
    Produce a PNG (bytes) representing the current canvas state for `bot_id`.
    Returns a minimal branded image if Pillow or the state snapshot is
    unavailable, so the bot always has something to show.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        log.exception("render_canvas_png: Pillow not installed")
        return b""

    try:
        from .views import _snapshot_state

        state = _snapshot_state(bot_id)
    except Exception:
        log.exception("render_canvas_png: snapshot failed bot=%s", bot_id)
        state = {"cursor": {"present": False}, "events": [], "actions": [], "thinking": False, "visual": None}

    img = Image.new("RGB", (_WIDTH, _HEIGHT), BG)
    draw = ImageDraw.Draw(img)

    # Load fonts
    font_title = _load_font(24, bold=True)
    font_h2 = _load_font(13, bold=True)
    font_body = _load_font(14)
    font_small = _load_font(11)
    font_mono = _load_font(12, mono=True)
    font_mono_small = _load_font(11, mono=True)

    _draw_debug_pane(draw, state, font_title, font_h2, font_body, font_small, font_mono, font_mono_small)
    _draw_viz_pane(draw, state, font_title, font_h2, font_body, font_mono_small)

    # Divider
    draw.line([(_WIDTH // 2, 0), (_WIDTH // 2, _HEIGHT)], fill=BORDER, width=1)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ── Panes ─────────────────────────────────────────────────────────────────────


def _draw_debug_pane(draw, state, font_title, font_h2, font_body, font_small, font_mono, font_mono_small):
    L = 24
    R = _WIDTH // 2 - 24
    y = 28

    # Header
    draw.text((L, y), "Clever Star", fill=FG, font=font_title)
    gate = (state.get("cursor") or {}).get("audio_gate_open", False)
    gate_reason = (state.get("cursor") or {}).get("audio_gate_reason", "")
    gate_label = f"gate: OPEN ({gate_reason})" if gate else "gate: closed"
    gate_color = OK if gate else MUTED
    draw.text((R - _text_w(draw, gate_label, font_small), y + 6), gate_label, fill=gate_color, font=font_small)

    y += 42

    # Thinking pill
    if state.get("thinking"):
        draw.ellipse((L, y + 2, L + 10, y + 12), fill=WARN)
        draw.text((L + 18, y), "processing…", fill=WARN, font=font_small)
        y += 20

    # Stats grid (4 cells)
    y = _draw_stats(draw, state, L, R, y, font_small, font_mono_small)

    # Feed section
    draw.text((L, y), "TRANSCRIPT + ACTIONS", fill=MUTED, font=font_h2)
    y += 22

    _draw_feed(draw, state, L, R, y, _HEIGHT - 40, font_body, font_mono_small)

    # Footer
    now_label = timezone.now().strftime("%H:%M:%S")
    draw.text((L, _HEIGHT - 22), f"bot: {state.get('bot_id','')}", fill=MUTED, font=font_small)
    draw.text((R - _text_w(draw, now_label, font_small), _HEIGHT - 22), now_label, fill=MUTED, font=font_small)


def _draw_stats(draw, state, L, R, y, font_small, font_mono):
    cursor = state.get("cursor") or {}
    cells = [
        ("Cursor", _short_time(cursor.get("cursor_event_time")) or "—"),
        ("Last turn", _short_time(cursor.get("last_turn_at")) or "—"),
        ("Cost", f"${float(cursor.get('total_cost_usd') or 0):.3f} / ${float(cursor.get('budget_cap_usd') or 0):.0f}"),
        ("Events", str(len(state.get("events") or []))),
    ]
    cell_w = (R - L - 18) // 4
    for i, (k, v) in enumerate(cells):
        x = L + i * (cell_w + 6)
        draw.rounded_rectangle((x, y, x + cell_w, y + 52), radius=4, fill=BG_DIM, outline=BORDER)
        draw.text((x + 8, y + 6), k.upper(), fill=MUTED, font=font_small)
        draw.text((x + 8, y + 24), v, fill=FG, font=font_mono)
    return y + 68


def _draw_feed(draw, state, L, R, y_top, y_bottom, font_body, font_mono):
    # Merge events + actions, sort, take the tail
    rows = []
    for e in state.get("events") or []:
        rows.append({
            "ts": e.get("t"),
            "kind": e.get("kind", "speech"),
            "who": (e.get("speaker") or "?")[:28],
            "text": (e.get("text") or "")[:160],
        })
    for a in state.get("actions") or []:
        msg = a.get("tool", "?")
        if a.get("status") == "error":
            msg += " — " + (a.get("error") or "")[:60]
        elif a.get("latency_ms"):
            msg += f" ({a['latency_ms']}ms)"
        rows.append({
            "ts": a.get("t"),
            "kind": "action " + (a.get("status") or ""),
            "who": a.get("tool", "?")[:28],
            "text": msg,
        })
    rows.sort(key=lambda r: r.get("ts") or "")

    # Fit rows that actually fit in the visible area (leave bottom 36px for footer)
    line_h = 22
    max_rows = max(1, (y_bottom - y_top) // line_h)
    rows = rows[-max_rows:]

    y = y_top
    for row in rows:
        ts = _short_time(row["ts"]) or "--:--:--"
        kind = row["kind"]
        color = SPEAKER
        if "chat" in kind:
            color = SPEAKER_CHAT
        elif "action" in kind:
            color = OK if "ok" in kind else ERR if "error" in kind else SPEAKER_ACTION
        draw.text((L, y), ts, fill=MUTED, font=font_mono)
        draw.text((L + 60, y), row["who"], fill=color, font=font_body)
        text_x = L + 60 + _text_w(draw, row["who"], font_body) + 8
        text = row["text"]
        avail = R - text_x
        text = _truncate_to_width(draw, text, font_body, avail)
        draw.text((text_x, y), text, fill=FG, font=font_body)
        y += line_h


def _draw_viz_pane(draw, state, font_title, font_h2, font_body, font_mono_small):
    mid = _WIDTH // 2
    L = mid + 24
    R = _WIDTH - 24
    y = 28

    draw.text((L, y), "Canvas", fill=MUTED, font=font_h2)
    y += 24

    visual = state.get("visual")
    if not visual:
        # Empty state — branded
        title = "Clever Star"
        title_w = _text_w(draw, title, font_title)
        center_x = (L + R) // 2
        center_y = _HEIGHT // 2 - 30
        # Halo
        draw.ellipse(
            (center_x - 240, center_y - 140, center_x + 240, center_y + 140),
            fill=(30, 27, 75),
            outline=None,
        )
        draw.ellipse(
            (center_x - 150, center_y - 80, center_x + 150, center_y + 80),
            fill=BG,
            outline=None,
        )
        draw.text((center_x - title_w // 2, center_y - 18), title, fill=ACCENT_SOFT, font=font_title)
        sub = "ready"
        sub_w = _text_w(draw, sub, font_mono_small)
        draw.text((center_x - sub_w // 2, center_y + 20), sub, fill=MUTED, font=font_mono_small)
        return

    draw.text((L, y), visual.get("title", "Visual")[:80], fill=FG, font=font_title)
    y += 40
    spec = visual.get("spec")
    if not spec:
        return
    try:
        body = json.dumps(spec, indent=2)[:2500]
    except Exception:
        body = str(spec)[:2500]
    # Simple monospace wrap
    max_w = R - L
    for line in body.split("\n")[:30]:
        line = _truncate_to_width(draw, line, font_mono_small, max_w)
        draw.text((L, y), line, fill=(209, 213, 219), font=font_mono_small)
        y += 16
        if y > _HEIGHT - 20:
            break


# ── Helpers ───────────────────────────────────────────────────────────────────


def _load_font(size: int, bold: bool = False, mono: bool = False):
    from PIL import ImageFont

    # Try common system fonts; fall back to Pillow's default.
    candidates = []
    if mono:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
        ]
    elif bold:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]
    else:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _text_w(draw, text: str, font) -> int:
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]
    except Exception:
        return len(text) * (font.size // 2 if hasattr(font, "size") else 7)


def _truncate_to_width(draw, text: str, font, max_w: int) -> str:
    if _text_w(draw, text, font) <= max_w:
        return text
    # Binary-ish shrink; chars are ~7-9px
    approx = max(1, max_w // 7)
    if len(text) <= approx:
        return text
    return text[: approx - 1] + "…"


def _short_time(iso: Any) -> str:
    if not iso:
        return ""
    try:
        return str(iso)[11:19]
    except Exception:
        return ""
