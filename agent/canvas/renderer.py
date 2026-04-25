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


def render_canvas_png(bot_id: str, use_html_renderer: bool = False) -> bytes:
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

    # For HTML-spec visuals, use Selenium to render into the right pane
    visual = state.get("visual")
    html_rendered = False
    if use_html_renderer and visual:
        spec = visual.get("spec") or {}
        if spec.get("type") == "html" and spec.get("html"):
            try:
                from .html_renderer import render_html_to_png
                html_png = render_html_to_png(spec["html"])
                if html_png:
                    from PIL import Image as _Image
                    import io as _io
                    pane = _Image.open(_io.BytesIO(html_png)).convert("RGB")
                    pane = pane.resize((_WIDTH // 2, _HEIGHT), _Image.LANCZOS)
                    img.paste(pane, (_WIDTH // 2, 0))
                    html_rendered = True
            except Exception:
                log.exception("render_canvas_png: html render failed bot=%s", bot_id)

    if not html_rendered:
        _draw_viz_pane(draw, state, font_title, font_h2, font_body, font_mono_small)

    # Divider
    draw.line([(_WIDTH // 2, 0), (_WIDTH // 2, _HEIGHT)], fill=BORDER, width=1)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ── Panes ─────────────────────────────────────────────────────────────────────


def _draw_debug_pane(draw, state, font_title, font_h2, font_body, font_small, font_mono, font_mono_small):
    """Clean conversation-focused left pane."""
    L = 24
    R = _WIDTH // 2 - 24
    y = 28

    # Header
    draw.text((L, y), "Clever Star", fill=FG, font=font_title)
    if state.get("thinking"):
        # Pulsing dot indicates an in-flight action
        dot_x = R - 18
        draw.ellipse((dot_x, y + 12, dot_x + 12, y + 24), fill=WARN)
        draw.text((dot_x - 80, y + 12), "thinking…", fill=WARN, font=font_small)

    y += 50

    # Conversation feed (transcripts + tool actions interleaved)
    _draw_feed(draw, state, L, R, y, _HEIGHT - 30, font_body, font_mono_small)


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
    """
    Conversation-style feed. Speaker turns are full-width with the speaker
    label on its own line. Tool actions are compact one-liners.
    """
    rows = []
    for e in state.get("events") or []:
        rows.append({
            "ts": e.get("t"),
            "type": "turn",
            "kind": e.get("kind", "speech"),
            "who": (e.get("speaker") or "?")[:32],
            "text": (e.get("text") or "")[:400],
        })
    for a in state.get("actions") or []:
        status = a.get("status", "")
        if status == "error":
            err = (a.get("error") or "").replace("\n", " ")
            label = f"⚠ {a.get('tool','?')} failed: {err[:80]}"
            color = ERR
        elif status == "pending":
            label = f"⏳ {a.get('tool','?')} running…"
            color = WARN
        else:
            label = f"✓ {a.get('tool','?')}"
            color = OK
        rows.append({
            "ts": a.get("t"),
            "type": "action",
            "label": label,
            "color": color,
        })
    rows.sort(key=lambda r: r.get("ts") or "")

    # Prefer to fit recent items first, walk back from y_top until height runs out
    avail_w = R - L
    line_h_text = 18
    line_h_action = 18
    speaker_h = 16
    turn_gap = 6

    # Render bottom-up: take rows from the end, compute heights, stop when full
    rendered = []
    height_used = 0
    for row in reversed(rows):
        if row["type"] == "action":
            h = line_h_action + 4
        else:
            wrapped = _wrap_text(draw, row["text"], font_body, avail_w - 12)
            h = speaker_h + line_h_text * len(wrapped) + turn_gap
        if height_used + h > (y_bottom - y_top):
            break
        rendered.insert(0, (row, h, wrapped if row["type"] == "turn" else None))
        height_used += h

    y = y_top
    for row, h, wrapped in rendered:
        if row["type"] == "action":
            ts = _short_time(row["ts"]) or "--:--"
            draw.text((L, y), ts, fill=MUTED, font=font_mono)
            draw.text((L + 50, y), row["label"], fill=row["color"], font=font_body)
            y += h
        else:
            ts = _short_time(row["ts"]) or "--:--"
            who = row["who"]
            kind = row["kind"]
            who_color = SPEAKER if kind != "chat" else SPEAKER_CHAT
            draw.text((L, y), ts, fill=MUTED, font=font_mono)
            draw.text((L + 50, y), who, fill=who_color, font=font_body)
            y += speaker_h
            for line in wrapped or []:
                draw.text((L + 50, y), line, fill=FG, font=font_body)
                y += line_h_text
            y += turn_gap


def _wrap_text(draw, text, font, max_w):
    """Greedy word wrap. Returns list of lines."""
    words = text.split()
    lines = []
    line = ""
    for w in words:
        candidate = (line + " " + w).strip() if line else w
        if _text_w(draw, candidate, font) <= max_w:
            line = candidate
        else:
            if line:
                lines.append(line)
            line = w
    if line:
        lines.append(line)
    # Cap at 8 lines per turn to keep things scrollable
    return lines[:8]


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

    # Try to render the spec visually. Falls back to JSON dump if we can't
    # interpret the spec. Supported shapes:
    #   {"type": "bar"|"column", "data": [{"label": str, "value": num}, ...]}
    #   {"type": "list", "items": [str, ...]}
    #   {"type": "text", "text": str}
    #   {"type": "table", "rows": [[...], [...]]}
    chart_type = (spec.get("type") or "").lower() if isinstance(spec, dict) else ""

    try:
        if chart_type in ("bar", "column"):
            _draw_bar_chart(draw, spec, L, R, y, _HEIGHT - 30, font_body, font_mono_small)
            return
        if chart_type == "list":
            _draw_list(draw, spec, L, R, y, _HEIGHT - 30, font_body)
            return
        if chart_type == "table":
            _draw_table(draw, spec, L, R, y, _HEIGHT - 30, font_body, font_mono_small)
            return
        if chart_type == "text":
            _draw_text_card(draw, spec, L, R, y, _HEIGHT - 30, font_body)
            return
    except Exception:
        log.exception("_draw_viz_pane: render failed for spec_type=%s", chart_type)

    # Fallback: pretty-printed JSON
    try:
        body = json.dumps(spec, indent=2)[:2500]
    except Exception:
        body = str(spec)[:2500]
    max_w = R - L
    for line in body.split("\n")[:30]:
        line = _truncate_to_width(draw, line, font_mono_small, max_w)
        draw.text((L, y), line, fill=(209, 213, 219), font=font_mono_small)
        y += 16
        if y > _HEIGHT - 20:
            break


def _draw_bar_chart(draw, spec, L, R, y_top, y_bot, font_body, font_mono):
    data = spec.get("data") or []
    if not data:
        return
    # Coerce labels/values
    items = []
    for d in data[:12]:
        try:
            label = str(d.get("label", ""))[:24]
            value = float(d.get("value", 0))
            items.append((label, value))
        except Exception:
            continue
    if not items:
        return
    max_val = max(v for _, v in items) or 1.0

    # Layout
    chart_w = R - L
    chart_h = (y_bot - y_top) - 20  # leave room for labels at the bottom
    bar_count = len(items)
    gap = 6
    bar_w = max(8, (chart_w - gap * (bar_count - 1)) // bar_count)
    base_y = y_top + chart_h

    for i, (label, value) in enumerate(items):
        bar_h = int((value / max_val) * (chart_h - 30))
        x = L + i * (bar_w + gap)
        # Bar
        draw.rectangle((x, base_y - bar_h, x + bar_w, base_y), fill=ACCENT)
        # Value above bar
        val_str = _fmt_number(value)
        vw = _text_w(draw, val_str, font_mono)
        draw.text((x + (bar_w - vw) // 2, base_y - bar_h - 16), val_str, fill=FG, font=font_mono)
        # Label below
        lw = _text_w(draw, label, font_mono)
        if lw > bar_w + gap:
            label = _truncate_to_width(draw, label, font_mono, bar_w + gap)
            lw = _text_w(draw, label, font_mono)
        draw.text((x + (bar_w - lw) // 2, base_y + 4), label, fill=MUTED, font=font_mono)


def _draw_list(draw, spec, L, R, y_top, y_bot, font_body):
    items = spec.get("items") or []
    y = y_top
    line_h = 26
    max_w = R - L - 20
    for item in items[:20]:
        text = str(item)
        text = _truncate_to_width(draw, text, font_body, max_w)
        draw.ellipse((L, y + 7, L + 6, y + 13), fill=ACCENT_SOFT)
        draw.text((L + 14, y), text, fill=FG, font=font_body)
        y += line_h
        if y > y_bot:
            break


def _draw_table(draw, spec, L, R, y_top, y_bot, font_body, font_mono):
    rows = spec.get("rows") or []
    if not rows:
        return
    cols = max(len(r) for r in rows[:8])
    if cols == 0:
        return
    col_w = (R - L) // cols
    line_h = 24
    y = y_top
    for ri, row in enumerate(rows[:18]):
        is_header = ri == 0
        for ci, cell in enumerate(row[:cols]):
            x = L + ci * col_w
            text = str(cell)
            text = _truncate_to_width(draw, text, font_body if not is_header else font_body, col_w - 8)
            draw.text((x + 4, y), text, fill=FG if is_header else (209, 213, 219), font=font_body)
        if is_header:
            draw.line((L, y + line_h - 4, R, y + line_h - 4), fill=BORDER, width=1)
        y += line_h
        if y > y_bot:
            break


def _draw_text_card(draw, spec, L, R, y_top, y_bot, font_body):
    text = str(spec.get("text", ""))
    if not text:
        return
    max_w = R - L - 16
    y = y_top
    line_h = 22
    # Word wrap
    words = text.split()
    line = ""
    for word in words:
        candidate = (line + " " + word).strip()
        if _text_w(draw, candidate, font_body) <= max_w:
            line = candidate
        else:
            draw.text((L + 8, y), line, fill=FG, font=font_body)
            y += line_h
            line = word
            if y > y_bot - line_h:
                break
    if line and y <= y_bot:
        draw.text((L + 8, y), line, fill=FG, font=font_body)


def _fmt_number(v: float) -> str:
    if v == int(v):
        return str(int(v))
    return f"{v:.1f}"


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
