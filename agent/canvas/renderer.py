"""
Server-side canvas renderer — produces the bot's video tile (1280×720 PNG).

Layout:

    ┌──────────────────────────────────────────────────────────────────┐
    │  TOP BAR  • app brand • thinking indicator •   [VOICE PILL]      │
    ├────────────────────────────────┬─────────────────────────────────┤
    │                                │                                 │
    │   CONVERSATION FEED            │      VISUAL CANVAS              │
    │   (cards, newest at bottom)    │   (chart / list / text / html)  │
    │                                │                                 │
    │                                │                                 │
    ├────────────────────────────────┴─────────────────────────────────┤
    │  STATUS BAR  • cursor • cost • events                            │
    └──────────────────────────────────────────────────────────────────┘

Pushed into Google Meet via Attendee's `POST /api/v1/bots/<id>/output_image`.
"""
from __future__ import annotations

import io
import json
import logging
import re
from typing import Any

from django.utils import timezone

log = logging.getLogger("agent.canvas.renderer")


# Canvas dimensions
_WIDTH = 1280
_HEIGHT = 720

# Layout
TOP_BAR_H = 64
BOTTOM_BAR_H = 36
PANE_PAD_X = 28
PANE_PAD_Y = 24
MID = _WIDTH // 2  # vertical divider
CONTENT_TOP = TOP_BAR_H
CONTENT_BOTTOM = _HEIGHT - BOTTOM_BAR_H

# Color palette — calm dark UI
BG = (10, 11, 15)
BG_PANE = (15, 17, 23)
BG_CARD = (22, 25, 33)
BG_CARD_HI = (29, 33, 44)
BORDER = (38, 43, 56)
BORDER_SOFT = (28, 32, 42)
MUTED = (124, 132, 152)
MUTED_DIM = (88, 95, 112)
FG = (232, 234, 240)
FG_BODY = (209, 213, 224)
ACCENT = (129, 140, 248)
ACCENT_SOFT = (165, 180, 252)
OK = (52, 211, 153)
WARN = (251, 191, 36)
ERR = (248, 113, 113)
SPEAKER_USER = (147, 197, 253)
SPEAKER_CHAT = (244, 114, 182)
SPEAKER_BOT = (192, 132, 252)


def render_canvas_png(bot_id: str, use_html_renderer: bool = False) -> bytes:
    try:
        from PIL import Image, ImageDraw
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

    fonts = _Fonts(
        brand=_load_font(22, bold=True),
        h1=_load_font(20, bold=True),
        h2=_load_font(14, bold=True),
        body=_load_font(15),
        body_b=_load_font(15, bold=True),
        italic=_load_font(15, italic=True),
        small=_load_font(11),
        small_b=_load_font(11, bold=True),
        time=_load_font(11, mono=True),
        mono=_load_font(12, mono=True),
        viz_title=_load_font(22, bold=True),
        list_item=_load_font(17),
        big_label=_load_font(20, bold=True),
    )

    # ── Panes ───────────────────────────────────────────────────────────────
    # Background panes (so we have a clear two-column UI rather than a flat slab)
    draw.rectangle((0, CONTENT_TOP, MID, CONTENT_BOTTOM), fill=BG_PANE)
    draw.rectangle((MID, CONTENT_TOP, _WIDTH, CONTENT_BOTTOM), fill=BG_PANE)
    # Soft divider
    draw.line((MID, CONTENT_TOP, MID, CONTENT_BOTTOM), fill=BORDER_SOFT, width=1)

    _draw_feed_pane(draw, state, fonts)

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

                    pane = _Image.open(io.BytesIO(html_png)).convert("RGB")
                    pane_w = _WIDTH - MID
                    pane_h = CONTENT_BOTTOM - CONTENT_TOP
                    pane = pane.resize((pane_w, pane_h), _Image.LANCZOS)
                    img.paste(pane, (MID, CONTENT_TOP))
                    html_rendered = True
            except Exception:
                log.exception("render_canvas_png: html render failed bot=%s", bot_id)

    if not html_rendered:
        _draw_visual_pane(draw, state, fonts)

    # ── Chrome (drawn last so it sits on top) ──────────────────────────────
    _draw_top_bar(draw, state, fonts)
    _draw_bottom_bar(draw, state, fonts)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ── Fonts container ──────────────────────────────────────────────────────────


class _Fonts:
    __slots__ = (
        "brand", "h1", "h2", "body", "body_b", "italic", "small", "small_b",
        "time", "mono", "viz_title", "list_item", "big_label",
    )

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


# ── Top bar ──────────────────────────────────────────────────────────────────


def _draw_top_bar(draw, state, fonts):
    draw.rectangle((0, 0, _WIDTH, TOP_BAR_H), fill=BG_PANE)
    draw.line((0, TOP_BAR_H, _WIDTH, TOP_BAR_H), fill=BORDER, width=1)

    # Brand: pulsing dot + name on left
    bx = PANE_PAD_X
    by = TOP_BAR_H // 2
    dot_r = 7
    draw.ellipse((bx, by - dot_r, bx + dot_r * 2, by + dot_r), fill=ACCENT)
    draw.text((bx + dot_r * 2 + 12, by - 14), "Clever Star", fill=FG, font=fonts.brand)

    # Subtitle (small uppercase tag)
    sub = "meeting agent"
    sub_x = bx + dot_r * 2 + 12 + _text_w(draw, "Clever Star", fonts.brand) + 14
    draw.text((sub_x, by - 5), sub.upper(), fill=MUTED_DIM, font=fonts.small_b)

    # Thinking indicator (centered)
    if state.get("thinking"):
        label = "thinking…"
        lw = _text_w(draw, label, fonts.small_b)
        cx = _WIDTH // 2 - lw // 2
        # Subtle pill
        pad = 12
        draw.rounded_rectangle(
            (cx - pad - 6, by - 12, cx + lw + pad, by + 12),
            radius=12, fill=BG_CARD, outline=BORDER, width=1,
        )
        # Dot
        draw.ellipse((cx - pad - 1, by - 4, cx - pad + 7, by + 4), fill=WARN)
        draw.text((cx, by - 6), label, fill=WARN, font=fonts.small_b)

    # Voice pill on the right
    _draw_voice_pill(draw, state, fonts)


def _draw_voice_pill(draw, state, fonts):
    """Right-aligned voice indicator inside the top bar."""
    voice = state.get("voice_state") or {}
    label = voice.get("label", "IDLE")
    color_name = voice.get("color", "gray")
    accent = {
        "green": OK,
        "red": ERR,
        "gray": MUTED,
    }.get(color_name, MUTED)

    pill_h = 36
    text_w = _text_w(draw, label, fonts.h2)
    pill_w = text_w + 56
    pill_y = (TOP_BAR_H - pill_h) // 2
    pill_x = _WIDTH - PANE_PAD_X - pill_w

    # Subtle tinted background of the pill (10% accent over BG_PANE)
    draw.rounded_rectangle(
        (pill_x, pill_y, pill_x + pill_w, pill_y + pill_h),
        radius=pill_h // 2,
        fill=BG_CARD,
        outline=accent,
        width=2,
    )
    # Status dot
    dot_r = 6
    dot_cx = pill_x + 18
    dot_cy = pill_y + pill_h // 2
    # Outer halo for "alive" feel
    draw.ellipse(
        (dot_cx - dot_r - 3, dot_cy - dot_r - 3, dot_cx + dot_r + 3, dot_cy + dot_r + 3),
        outline=accent, width=1,
    )
    draw.ellipse(
        (dot_cx - dot_r, dot_cy - dot_r, dot_cx + dot_r, dot_cy + dot_r),
        fill=accent,
    )
    # Label
    label_y = pill_y + (pill_h - fonts.h2.size) // 2 - 2
    draw.text((dot_cx + dot_r + 10, label_y), label, fill=accent, font=fonts.h2)


# ── Bottom bar ───────────────────────────────────────────────────────────────


def _draw_bottom_bar(draw, state, fonts):
    y0 = _HEIGHT - BOTTOM_BAR_H
    draw.rectangle((0, y0, _WIDTH, _HEIGHT), fill=BG_PANE)
    draw.line((0, y0, _WIDTH, y0), fill=BORDER, width=1)

    cursor = state.get("cursor") or {}
    events_n = len(state.get("events") or [])
    actions_n = len(state.get("actions") or [])
    cost = float(cursor.get("total_cost_usd") or 0)
    cap = float(cursor.get("budget_cap_usd") or 0)
    last_turn = _short_time(cursor.get("last_turn_at")) or "—"
    cur_t = _short_time(cursor.get("cursor_event_time")) or "—"

    cells = [
        ("CURSOR", cur_t),
        ("LAST TURN", last_turn),
        ("EVENTS", str(events_n)),
        ("ACTIONS", str(actions_n)),
        ("COST", f"${cost:.3f} / ${cap:.0f}"),
    ]

    x = PANE_PAD_X
    cy = y0 + BOTTOM_BAR_H // 2
    for i, (k, v) in enumerate(cells):
        draw.text((x, cy - 11), k, fill=MUTED_DIM, font=fonts.small_b)
        kw = _text_w(draw, k, fonts.small_b)
        draw.text((x + kw + 8, cy - 12), v, fill=FG_BODY, font=fonts.mono)
        x += kw + 8 + _text_w(draw, v, fonts.mono) + 28


# ── Conversation feed (left pane) ────────────────────────────────────────────


def _draw_feed_pane(draw, state, fonts):
    L = PANE_PAD_X
    R = MID - PANE_PAD_X
    top = CONTENT_TOP + PANE_PAD_Y
    bot = CONTENT_BOTTOM - PANE_PAD_Y

    draw.text((L, top), "Conversation", fill=MUTED, font=fonts.h2)
    top += 26

    rows = _build_feed_rows(state)
    if not rows:
        _draw_feed_empty(draw, L, R, top, bot, fonts)
        return

    # Pre-measure heights bottom-up so the most recent items are guaranteed
    # to render. Older items are dropped silently when there's no room.
    avail_w = R - L
    # Rendered tuples: (row, height, lines_or_none)
    rendered: list[tuple[dict, int, list[str] | None]] = []
    height_used = 0
    for row in reversed(rows):
        if row["type"] == "action":
            h = 30 + 8  # chip height + gap
            wrapped = None
        else:
            wrapped = _wrap_text(draw, row["text"], fonts.body, avail_w - 24)
            h = 28 + 6 + len(wrapped) * 22 + 14  # header + gap + body lines + bottom pad
        if height_used + h > (bot - top):
            break
        rendered.insert(0, (row, h, wrapped))
        height_used += h

    y = top
    for row, h, wrapped in rendered:
        if row["type"] == "action":
            _draw_action_chip(draw, row, L, R, y, fonts)
            y += h
        else:
            _draw_speaker_card(draw, row, wrapped or [], L, R, y, h, fonts)
            y += h


def _draw_feed_empty(draw, L, R, y_top, y_bot, fonts):
    cx = (L + R) // 2
    cy = (y_top + y_bot) // 2 - 12
    msg = "waiting for the first turn"
    mw = _text_w(draw, msg, fonts.body)
    draw.text((cx - mw // 2, cy), msg, fill=MUTED_DIM, font=fonts.body)


def _build_feed_rows(state) -> list[dict]:
    """Combine speech turns and actions into a single time-ordered list."""
    rows: list[dict] = []
    for e in state.get("events") or []:
        rows.append({
            "ts": e.get("t"),
            "type": "turn",
            "kind": e.get("kind", "speech"),
            "who": (e.get("speaker") or "").strip() or "Speaker",
            "text": (e.get("text") or "").strip(),
        })
    for a in state.get("actions") or []:
        status = a.get("status", "ok")
        if status == "error":
            err_msg = (a.get("error") or "").replace("\n", " ").strip()
            label = f"{a.get('tool', '?')} failed"
            sub = err_msg[:120] if err_msg else ""
            color = ERR
            symbol = "✕"
        elif status == "pending":
            label = f"{a.get('tool', '?')} running"
            sub = ""
            color = WARN
            symbol = "•"
        elif status == "deferred":
            label = f"{a.get('tool', '?')} deferred"
            sub = ""
            color = MUTED
            symbol = "›"
        else:
            label = a.get("tool", "?")
            sub = ""
            color = OK
            symbol = "✓"
        rows.append({
            "ts": a.get("t"),
            "type": "action",
            "label": label,
            "sub": sub,
            "color": color,
            "symbol": symbol,
        })
    rows.sort(key=lambda r: r.get("ts") or "")
    return rows


def _draw_speaker_card(draw, row, lines, L, R, y, h, fonts):
    """A single conversation turn rendered as a card."""
    card_top = y
    card_bot = y + h - 6  # leave a 6px gap before next card
    pad_x = 12
    pad_y = 8

    # Card background — different tint for chat vs voice
    is_chat = row["kind"] == "chat"
    bg = BG_CARD_HI if is_chat else BG_CARD
    draw.rounded_rectangle(
        (L, card_top, R, card_bot),
        radius=10,
        fill=bg,
        outline=BORDER_SOFT,
        width=1,
    )

    # Header row: avatar + speaker (left) ··· timestamp (right-aligned)
    header_y = card_top + pad_y
    avatar_size = 18
    avatar_x = L + pad_x
    avatar_y = header_y + 1
    avatar_color = SPEAKER_CHAT if is_chat else SPEAKER_USER
    draw.ellipse(
        (avatar_x, avatar_y, avatar_x + avatar_size, avatar_y + avatar_size),
        fill=_blend(avatar_color, BG_CARD, 0.25),
        outline=avatar_color, width=1,
    )
    initial = (row["who"][:1] or "?").upper()
    iw = _text_w(draw, initial, fonts.small_b)
    draw.text(
        (avatar_x + (avatar_size - iw) // 2, avatar_y + 2),
        initial, fill=avatar_color, font=fonts.small_b,
    )

    # Timestamp (right) — measure first so we know where it starts
    ts = _short_time(row["ts"]) or ""
    tw = _text_w(draw, ts, fonts.time)
    ts_x = R - pad_x - tw
    ts_y = header_y + 4
    draw.text((ts_x, ts_y), ts, fill=MUTED_DIM, font=fonts.time)

    # Speaker name — truncate to fit between avatar and timestamp
    name_x = avatar_x + avatar_size + 10
    name_max_w = ts_x - name_x - 10
    who = _truncate_to_width(draw, row["who"], fonts.body_b, name_max_w)
    draw.text((name_x, header_y + 1), who, fill=avatar_color, font=fonts.body_b)

    # Tag for chat
    if is_chat:
        tag = "chat"
        tag_x = name_x + _text_w(draw, who, fonts.body_b) + 8
        if tag_x + _text_w(draw, tag, fonts.small_b) + 12 < ts_x:
            tag_w = _text_w(draw, tag, fonts.small_b) + 10
            draw.rounded_rectangle(
                (tag_x, header_y + 4, tag_x + tag_w, header_y + 17),
                radius=6, fill=BG_PANE, outline=SPEAKER_CHAT, width=1,
            )
            draw.text((tag_x + 5, header_y + 4), tag, fill=SPEAKER_CHAT, font=fonts.small_b)

    # Body
    body_y = header_y + 26
    line_h = 22
    for line in lines:
        if body_y + line_h > card_bot - pad_y + 6:
            break
        draw.text((L + pad_x, body_y), line, fill=FG_BODY, font=fonts.body)
        body_y += line_h


def _draw_action_chip(draw, row, L, R, y, fonts):
    """A compact one-line tool action chip."""
    h = 26
    draw.rounded_rectangle(
        (L, y, R, y + h),
        radius=8,
        fill=BG_CARD,
        outline=BORDER_SOFT, width=1,
    )
    sym = row.get("symbol", "•")
    sym_w = _text_w(draw, sym, fonts.body_b)
    sym_x = L + 12
    draw.text((sym_x, y + 4), sym, fill=row["color"], font=fonts.body_b)

    label = row["label"]
    label_x = sym_x + sym_w + 10
    label_y = y + 5

    # Timestamp on right
    ts = _short_time(row["ts"]) or ""
    tw = _text_w(draw, ts, fonts.time)
    ts_x = R - 12 - tw
    draw.text((ts_x, y + 7), ts, fill=MUTED_DIM, font=fonts.time)

    # Sub message (errors): if it fits, append it after the label in muted
    label_max_w = ts_x - label_x - 10
    sub = row.get("sub") or ""
    text = label if not sub else f"{label} — {sub}"
    text = _truncate_to_width(draw, text, fonts.body, label_max_w)
    draw.text((label_x, label_y), text, fill=FG_BODY, font=fonts.body)


# ── Visual pane (right) ──────────────────────────────────────────────────────


def _draw_visual_pane(draw, state, fonts):
    L = MID + PANE_PAD_X
    R = _WIDTH - PANE_PAD_X
    top = CONTENT_TOP + PANE_PAD_Y
    bot = CONTENT_BOTTOM - PANE_PAD_Y

    visual = state.get("visual")

    draw.text((L, top), "Canvas", fill=MUTED, font=fonts.h2)
    top += 26

    if not visual:
        _draw_visual_empty(draw, L, R, top, bot, fonts, state)
        return

    title = (visual.get("title") or "Visual")[:80]
    draw.text((L, top), title, fill=FG, font=fonts.viz_title)
    top += 38

    spec = visual.get("spec") or {}
    chart_type = (spec.get("type") or "").lower() if isinstance(spec, dict) else ""
    try:
        if chart_type in ("bar", "column"):
            _draw_bar_chart(draw, spec, L, R, top, bot, fonts)
            return
        if chart_type == "list":
            _draw_list(draw, spec, L, R, top, bot, fonts)
            return
        if chart_type == "table":
            _draw_table(draw, spec, L, R, top, bot, fonts)
            return
        if chart_type == "text":
            _draw_text_card(draw, spec, L, R, top, bot, fonts)
            return
    except Exception:
        log.exception("_draw_visual_pane: render failed for spec_type=%s", chart_type)

    # Fallback: pretty JSON
    try:
        body = json.dumps(spec, indent=2)[:2500]
    except Exception:
        body = str(spec)[:2500]
    y = top
    for line in body.split("\n")[:30]:
        line = _truncate_to_width(draw, line, fonts.mono, R - L)
        draw.text((L, y), line, fill=MUTED, font=fonts.mono)
        y += 16
        if y > bot:
            break


def _draw_visual_empty(draw, L, R, y_top, y_bot, fonts, state):
    cx = (L + R) // 2
    cy = (y_top + y_bot) // 2

    # Concentric halos
    for r, alpha in ((220, 0.08), (160, 0.18), (110, 0.30), (70, 0.55)):
        col = _blend(ACCENT, BG_PANE, alpha)
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=col)

    # Center dot
    draw.ellipse((cx - 14, cy - 14, cx + 14, cy + 14), fill=ACCENT_SOFT)

    # Status text
    voice = state.get("voice_state") or {}
    label = voice.get("label", "IDLE")
    sub_map = {
        "LISTENING": "ready when you are",
        "ASLEEP": "on hold — say something to wake me",
        "IDLE": "waiting for the meeting to start",
    }
    sub = sub_map.get(label, "")
    big = "Ready" if label == "LISTENING" else ("Sleeping" if label == "ASLEEP" else "Idle")

    bw = _text_w(draw, big, fonts.big_label)
    draw.text((cx - bw // 2, cy + 50), big, fill=FG, font=fonts.big_label)
    if sub:
        sw = _text_w(draw, sub, fonts.body)
        draw.text((cx - sw // 2, cy + 80), sub, fill=MUTED, font=fonts.body)


def _draw_bar_chart(draw, spec, L, R, y_top, y_bot, fonts):
    data = spec.get("data") or []
    if not data:
        return
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

    chart_w = R - L
    chart_h = (y_bot - y_top) - 36
    n = len(items)
    gap = 10
    bar_w = max(10, (chart_w - gap * (n - 1)) // n)
    base_y = y_top + chart_h

    for i, (label, value) in enumerate(items):
        bar_h = int((value / max_val) * (chart_h - 30))
        x = L + i * (bar_w + gap)
        # Bar
        draw.rounded_rectangle(
            (x, base_y - bar_h, x + bar_w, base_y),
            radius=4, fill=ACCENT,
        )
        # Value above bar
        val_str = _fmt_number(value)
        vw = _text_w(draw, val_str, fonts.mono)
        draw.text((x + (bar_w - vw) // 2, base_y - bar_h - 18), val_str, fill=FG, font=fonts.mono)
        # Label below
        lw = _text_w(draw, label, fonts.mono)
        if lw > bar_w + gap:
            label = _truncate_to_width(draw, label, fonts.mono, bar_w + gap)
            lw = _text_w(draw, label, fonts.mono)
        draw.text((x + (bar_w - lw) // 2, base_y + 6), label, fill=MUTED, font=fonts.mono)


def _draw_list(draw, spec, L, R, y_top, y_bot, fonts):
    items = spec.get("items") or []
    y = y_top
    line_h = 38
    max_w = R - L - 30
    for i, item in enumerate(items[:12]):
        # Markdown-aware items: strip inline markers for length budget but render styled.
        text = str(item)
        # Bullet circle with index
        idx = str(i + 1)
        circle_x = L
        circle_y = y + 4
        circle_d = 22
        draw.ellipse(
            (circle_x, circle_y, circle_x + circle_d, circle_y + circle_d),
            fill=_blend(ACCENT, BG_PANE, 0.30), outline=ACCENT, width=1,
        )
        iw = _text_w(draw, idx, fonts.small_b)
        draw.text(
            (circle_x + (circle_d - iw) // 2, circle_y + 4),
            idx, fill=ACCENT_SOFT, font=fonts.small_b,
        )
        # Plain text path: keep the existing list-item font (17pt) for legibility.
        # Inline **bold** / *italic* / `code` get respected via stripped fallback when present.
        if any(m in text for m in ("**", "*", "`", "_")):
            runs = _md_parse_inline(text)
            cur_x = circle_x + circle_d + 12
            for run_txt, st in runs:
                if st == "bold":
                    f = fonts.body_b
                elif st == "italic":
                    f = getattr(fonts, "italic", None) or fonts.list_item
                elif st == "mono":
                    f = fonts.mono
                else:
                    f = fonts.list_item
                seg = _truncate_to_width(draw, run_txt, f, max(0, R - cur_x - 8))
                draw.text((cur_x, y + 3), seg, fill=FG, font=f)
                cur_x += _text_w(draw, seg, f)
                if cur_x >= R - 8:
                    break
        else:
            text = _truncate_to_width(draw, text, fonts.list_item, max_w)
            draw.text((circle_x + circle_d + 12, y + 3), text, fill=FG, font=fonts.list_item)
        y += line_h
        if y > y_bot - 10:
            break


def _draw_table(draw, spec, L, R, y_top, y_bot, fonts):
    rows = spec.get("rows") or []
    if not rows:
        return
    cols = max(len(r) for r in rows[:8])
    if cols == 0:
        return
    col_w = (R - L) // cols
    line_h = 28
    y = y_top
    for ri, row in enumerate(rows[:18]):
        is_header = ri == 0
        if is_header:
            draw.rectangle((L, y, R, y + line_h), fill=BG_CARD)
        for ci, cell in enumerate(row[:cols]):
            x = L + ci * col_w
            text = str(cell)
            text = _truncate_to_width(draw, text, fonts.body_b if is_header else fonts.body, col_w - 16)
            color = FG if is_header else FG_BODY
            font = fonts.body_b if is_header else fonts.body
            draw.text((x + 10, y + 6), text, fill=color, font=font)
        if is_header:
            draw.line((L, y + line_h, R, y + line_h), fill=BORDER, width=1)
        else:
            draw.line((L, y + line_h, R, y + line_h), fill=BORDER_SOFT, width=1)
        y += line_h
        if y > y_bot:
            break


def _draw_text_card(draw, spec, L, R, y_top, y_bot, fonts):
    text = str(spec.get("text", "")).strip()
    if not text:
        return
    pad = 18
    card_h = y_bot - y_top
    draw.rounded_rectangle(
        (L, y_top, R, y_top + card_h),
        radius=12, fill=BG_CARD, outline=BORDER_SOFT, width=1,
    )
    _md_draw(
        draw, text,
        L + pad, R - pad,
        y_top + pad, y_top + card_h - pad,
        fonts, body_fill=FG,
    )


# ── Helpers ──────────────────────────────────────────────────────────────────


def _fmt_number(v: float) -> str:
    if v == int(v):
        return str(int(v))
    return f"{v:.1f}"


def _wrap_text(draw, text, font, max_w, max_lines: int = 8) -> list[str]:
    """Greedy word wrap. Returns up to `max_lines` lines (last truncated)."""
    text = (text or "").replace("\r", " ").replace("\n", " ")
    words = text.split()
    lines: list[str] = []
    line = ""
    for w in words:
        candidate = (line + " " + w).strip() if line else w
        if _text_w(draw, candidate, font) <= max_w:
            line = candidate
        else:
            if line:
                lines.append(line)
            # If the single word is too long, hard-cut it
            if _text_w(draw, w, font) > max_w:
                lines.append(_truncate_to_width(draw, w, font, max_w))
                line = ""
            else:
                line = w
        if len(lines) >= max_lines:
            break
    if line and len(lines) < max_lines:
        lines.append(line)
    if len(lines) >= max_lines and line:
        # mark truncation on last line
        last = lines[-1]
        lines[-1] = _truncate_to_width(draw, last + "…", font, max_w)
    return lines


def _text_w(draw, text: str, font) -> int:
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]
    except Exception:
        return len(text) * (font.size // 2 if hasattr(font, "size") else 7)


def _truncate_to_width(draw, text: str, font, max_w: int) -> str:
    if max_w <= 0:
        return ""
    if _text_w(draw, text, font) <= max_w:
        return text
    # Binary search to find longest prefix that fits with an ellipsis
    lo, hi = 0, len(text)
    best = ""
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = text[:mid].rstrip() + "…"
        if _text_w(draw, candidate, font) <= max_w:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return best or text[:1]


def _short_time(iso: Any) -> str:
    if not iso:
        return ""
    try:
        s = str(iso)
        # Expect ISO; hh:mm portion is at chars 11..16
        if len(s) >= 16 and s[10] == "T":
            return s[11:16]
        return s[:5]
    except Exception:
        return ""


def _blend(fg: tuple[int, int, int], bg: tuple[int, int, int], alpha: float) -> tuple[int, int, int]:
    """Return fg over bg with the given alpha (0..1)."""
    a = max(0.0, min(1.0, alpha))
    return tuple(int(fg[i] * a + bg[i] * (1 - a)) for i in range(3))  # type: ignore[return-value]


# ── Markdown / simple-HTML inline rendering ──────────────────────────────────
# Lets the agent ship `**bold**`, `*italic*`, `# headings`, `- bullets`, and a
# small set of HTML tags (<b>/<i>/<code>/<br>/<p>/<h1-3>/<ul>/<ol>/<li>) inside
# any text-style visual.


def _md_html_to_md(text: str) -> str:
    text = re.sub(r'(?i)<br\s*/?>', '\n', text)
    text = re.sub(r'(?i)<p[^>]*>', '\n\n', text)
    text = re.sub(r'(?i)</p>', '', text)
    text = re.sub(r'(?i)<h1[^>]*>(.*?)</h1>', r'# \1', text, flags=re.DOTALL)
    text = re.sub(r'(?i)<h2[^>]*>(.*?)</h2>', r'## \1', text, flags=re.DOTALL)
    text = re.sub(r'(?i)<h3[^>]*>(.*?)</h3>', r'### \1', text, flags=re.DOTALL)
    text = re.sub(r'(?i)<(?:b|strong)[^>]*>(.*?)</(?:b|strong)>', r'**\1**', text, flags=re.DOTALL)
    text = re.sub(r'(?i)<(?:i|em)[^>]*>(.*?)</(?:i|em)>', r'*\1*', text, flags=re.DOTALL)
    text = re.sub(r'(?i)<(?:code|tt)[^>]*>(.*?)</(?:code|tt)>', r'`\1`', text, flags=re.DOTALL)

    def _replace_ol(m):
        inner = m.group(1)
        idx = [1]

        def _li_num(li_match):
            content = li_match.group(1)
            res = f"\n{idx[0]}. {content}"
            idx[0] += 1
            return res

        return re.sub(r'(?i)<li[^>]*>(.*?)</li>', _li_num, inner, flags=re.DOTALL)

    text = re.sub(r'(?i)<ol[^>]*>(.*?)</ol>', _replace_ol, text, flags=re.DOTALL)

    def _replace_ul(m):
        inner = m.group(1)
        return re.sub(r'(?i)<li[^>]*>(.*?)</li>', r'\n- \1', inner, flags=re.DOTALL)

    text = re.sub(r'(?i)<ul[^>]*>(.*?)</ul>', _replace_ul, text, flags=re.DOTALL)
    text = re.sub(r'(?i)<li[^>]*>', '\n- ', text)
    text = re.sub(r'(?i)</li>', '', text)
    text = re.sub(r'(?i)<[^>]+>', '', text)
    return text


def _md_parse_inline(text: str) -> list[tuple[str, str]]:
    runs: list[tuple[str, str]] = []
    i = 0
    pattern = re.compile(r'(`[^`]*`)|(\*\*[^*]+\*\*)|(\*[^*\s][^*]*\*)|(_[^_\s][^_]*_)')
    while i < len(text):
        m = pattern.search(text, i)
        if not m:
            runs.append((text[i:], "normal"))
            break
        if m.start() > i:
            runs.append((text[i:m.start()], "normal"))
        token = m.group(0)
        if token.startswith('`'):
            runs.append((token[1:-1], "mono"))
        elif token.startswith('**'):
            runs.append((token[2:-2], "bold"))
        else:
            start, end = m.start(), m.end()
            before = text[start - 1] if start > 0 else ''
            after = text[end] if end < len(text) else ''
            if (not before.isalnum()) and (not after.isalnum()):
                runs.append((token[1:-1], "italic"))
            else:
                runs.append((token, "normal"))
        i = m.end()
    merged: list[tuple[str, str]] = []
    for txt, st in runs:
        if not txt:
            continue
        if merged and merged[-1][1] == st:
            merged[-1] = (merged[-1][0] + txt, st)
        else:
            merged.append((txt, st))
    return merged


def _md_parse_blocks(text: str) -> list[dict]:
    md = _md_html_to_md(text or "")
    lines = md.splitlines()
    blocks: list[dict] = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        hm = re.match(r'^(#{1,3})\s+(.*)', line)
        if hm:
            blocks.append({
                "kind": "heading",
                "level": len(hm.group(1)),
                "runs": _md_parse_inline(hm.group(2)),
            })
            i += 1
            continue
        if re.match(r'^[-*•]\s+', line):
            content = re.sub(r'^[-*•]\s+', '', line)
            blocks.append({
                "kind": "list_item",
                "ordered": False,
                "n": None,
                "runs": _md_parse_inline(content),
            })
            i += 1
            continue
        om = re.match(r'^(\d+)\.\s+(.*)', line)
        if om:
            blocks.append({
                "kind": "list_item",
                "ordered": True,
                "n": int(om.group(1)),
                "runs": _md_parse_inline(om.group(2)),
            })
            i += 1
            continue
        para_parts: list[str] = []
        while i < len(lines):
            cur = lines[i].strip()
            if not cur:
                break
            if (re.match(r'^(#{1,3})\s+', cur)
                    or re.match(r'^[-*•]\s+', cur)
                    or re.match(r'^(\d+)\.\s+', cur)):
                break
            para_parts.append(cur)
            i += 1
        blocks.append({
            "kind": "paragraph",
            "runs": _md_parse_inline(" ".join(para_parts)),
        })
    return blocks


def _md_font_for(fonts, style: str, base: str = "body"):
    if base == "h1":
        return fonts.h1
    if base == "h2":
        return fonts.h2
    if base == "h3":
        return fonts.body_b
    if style == "bold":
        return fonts.body_b
    if style == "italic":
        return getattr(fonts, "italic", None) or fonts.body
    if style == "mono":
        return fonts.mono
    return fonts.body


def _md_draw_runs(draw, runs, x: int, y: int, max_w: int, fonts,
                  base: str = "body", fill=None, line_h: int = 22,
                  max_lines: int = 999) -> int:
    if fill is None:
        fill = FG_BODY
    cur_y = y
    line_num = 0
    line_tokens: list[tuple[str, str]] = []
    line_width = 0
    done = False

    def flush_line():
        nonlocal cur_y, line_num, line_tokens, line_width, done
        if done or not line_tokens:
            return
        is_last = (line_num + 1 >= max_lines)
        if is_last:
            ell_font = _md_font_for(fonts, "normal", base)
            ell_w = _text_w(draw, "…", ell_font)
            while line_tokens and line_width + ell_w > max_w:
                txt, st = line_tokens.pop()
                line_width -= _text_w(draw, txt, _md_font_for(fonts, st, base))
            cx = x
            for txt, st in line_tokens:
                f = _md_font_for(fonts, st, base)
                draw.text((cx, cur_y), txt, font=f, fill=fill)
                cx += _text_w(draw, txt, f)
            draw.text((cx, cur_y), "…", font=ell_font, fill=fill)
            cur_y += line_h
            line_num += 1
            line_tokens.clear()
            line_width = 0
            done = True
            return
        cx = x
        for txt, st in line_tokens:
            f = _md_font_for(fonts, st, base)
            draw.text((cx, cur_y), txt, font=f, fill=fill)
            cx += _text_w(draw, txt, f)
        cur_y += line_h
        line_num += 1
        line_tokens.clear()
        line_width = 0

    for txt, st in runs:
        if done:
            break
        for token in re.findall(r'\S+|\s+', txt):
            if done:
                break
            font = _md_font_for(fonts, st, base)
            tw = _text_w(draw, token, font)
            if token.isspace():
                if not line_tokens:
                    continue
                if line_width + tw > max_w:
                    flush_line()
                    continue
                line_tokens.append((token, st))
                line_width += tw
                continue
            if line_width + tw > max_w:
                if tw > max_w:
                    if line_tokens:
                        flush_line()
                        if done:
                            break
                    truncated = _truncate_to_width(draw, token, font, max_w)
                    draw.text((x, cur_y), truncated, font=font, fill=fill)
                    cur_y += line_h
                    line_num += 1
                    if line_num >= max_lines:
                        done = True
                    continue
                flush_line()
                if done:
                    break
            line_tokens.append((token, st))
            line_width += tw
    flush_line()
    return cur_y


def _md_draw(draw, text: str, L: int, R: int, y_top: int, y_bot: int, fonts,
             body_fill=None) -> int:
    if body_fill is None:
        body_fill = FG_BODY
    y = y_top
    for blk in _md_parse_blocks(text or ""):
        if y >= y_bot - 10:
            break
        kind = blk["kind"]
        avail_lines = max(1, (y_bot - y) // 22)
        if kind == "heading":
            level = blk["level"]
            base = {1: "h1", 2: "h2", 3: "h3"}[level]
            line_h = {1: 30, 2: 24, 3: 22}[level]
            pad = {1: 6, 2: 5, 3: 4}[level]
            y = _md_draw_runs(draw, blk["runs"], L, y, R - L, fonts,
                              base=base, fill=FG, line_h=line_h, max_lines=avail_lines)
            y += pad
        elif kind == "paragraph":
            y = _md_draw_runs(draw, blk["runs"], L, y, R - L, fonts,
                              base="body", fill=body_fill, line_h=22, max_lines=avail_lines)
            y += 6
        elif kind == "list_item":
            bullet_font = fonts.body_b
            if blk["ordered"]:
                btxt = f"{blk['n']}."
                bw = _text_w(draw, btxt, bullet_font)
                draw.text((L + max(0, 18 - bw), y), btxt, font=bullet_font, fill=MUTED)
            else:
                draw.text((L + 4, y - 2), "•", font=bullet_font, fill=ACCENT)
            text_x = L + 22
            y = _md_draw_runs(draw, blk["runs"], text_x, y, R - text_x, fonts,
                              base="body", fill=body_fill, line_h=22, max_lines=avail_lines)
            y += 2
    return y


def _load_font(size: int, bold: bool = False, mono: bool = False, italic: bool = False):
    from PIL import ImageFont

    candidates: list[str]
    if mono:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
        ]
    elif italic and bold:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-BoldItalic.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
    elif italic:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
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
