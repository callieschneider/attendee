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
# We draw the layout in 1280×720 design coordinates (kept stable so font
# sizes and layout math don't have to change), then upscale the final
# PNG to OUTPUT_WIDTH × OUTPUT_HEIGHT before returning. Attendee's
# output_image endpoint feeds the bot's virtual webcam — giving it a
# higher-resolution source means the WebRTC encoder has more headroom
# and the bot's video looks meaningfully sharper in the meeting.
_WIDTH = 1280
_HEIGHT = 720
OUTPUT_WIDTH = 1920
OUTPUT_HEIGHT = 1080

# Layout — chat 25% (left), visual 75% (right). Visual is the showpiece.
TOP_BAR_H = 64
BOTTOM_BAR_H = 36
PANE_PAD_X = 28
PANE_PAD_Y = 24
MID = _WIDTH // 4  # vertical divider — chat is 0..MID, canvas is MID.._WIDTH
FEED_PAD_X = 14    # chat pane is narrow (320px); use tighter horizontal padding
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

    # Upscale to OUTPUT_WIDTH × OUTPUT_HEIGHT for a higher-fidelity virtual
    # webcam feed. LANCZOS preserves edge sharpness on text. Cost is ~10ms
    # per frame on a 1280→1920 resize, well under the pump cadence.
    if (OUTPUT_WIDTH, OUTPUT_HEIGHT) != (_WIDTH, _HEIGHT):
        img = img.resize((OUTPUT_WIDTH, OUTPUT_HEIGHT), Image.LANCZOS)

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
    L = FEED_PAD_X
    R = MID - FEED_PAD_X
    top = CONTENT_TOP + PANE_PAD_Y
    bot = CONTENT_BOTTOM - PANE_PAD_Y

    draw.text((L, top), "Conversation", fill=MUTED, font=fonts.h2)
    top += 22

    rows = _build_feed_rows(state)
    if not rows:
        _draw_feed_empty(draw, L, R, top, bot, fonts)
        return

    # Pre-measure heights bottom-up so the most recent items are guaranteed
    # to render. Older items are dropped silently when there's no room.
    # Narrow column → smaller font, tighter line height, more wrapped lines OK.
    body_font = fonts.small_b if False else fonts.body  # keep readable
    line_h = 18
    avail_w = R - L
    rendered: list[tuple[dict, int, list[str] | None]] = []
    height_used = 0
    for row in reversed(rows):
        if row["type"] == "action":
            h = 28 + 6  # chip height + gap
            wrapped = None
        else:
            wrapped = _wrap_text(draw, row["text"], body_font, avail_w - 16, max_lines=12)
            h = 26 + 4 + len(wrapped) * line_h + 12  # header + gap + lines + bottom pad
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
    """A single conversation turn rendered as a card. Narrow-column friendly."""
    card_top = y
    card_bot = y + h - 4  # tight gap so feed packs more
    pad_x = 8
    pad_y = 6

    # Card background — different tint for chat vs voice
    is_chat = row["kind"] == "chat"
    bg = BG_CARD_HI if is_chat else BG_CARD
    draw.rounded_rectangle(
        (L, card_top, R, card_bot),
        radius=8,
        fill=bg,
        outline=BORDER_SOFT,
        width=1,
    )

    # Header row: avatar + speaker (left) ··· timestamp (right)
    header_y = card_top + pad_y
    avatar_size = 14
    avatar_x = L + pad_x
    avatar_y = header_y + 2
    avatar_color = SPEAKER_CHAT if is_chat else SPEAKER_USER
    draw.ellipse(
        (avatar_x, avatar_y, avatar_x + avatar_size, avatar_y + avatar_size),
        fill=_blend(avatar_color, BG_CARD, 0.25),
        outline=avatar_color, width=1,
    )
    initial = (row["who"][:1] or "?").upper()
    iw = _text_w(draw, initial, fonts.small_b)
    draw.text(
        (avatar_x + (avatar_size - iw) // 2, avatar_y + 1),
        initial, fill=avatar_color, font=fonts.small_b,
    )

    # Timestamp (right). Use small font so it doesn't crowd the name.
    ts = _short_time(row["ts"]) or ""
    tw = _text_w(draw, ts, fonts.time)
    ts_x = R - pad_x - tw
    ts_y = header_y + 2
    draw.text((ts_x, ts_y), ts, fill=MUTED_DIM, font=fonts.time)

    # Speaker name — first name only, truncated. Use small bold so it fits.
    name_x = avatar_x + avatar_size + 6
    name_max_w = ts_x - name_x - 6
    full_name = (row["who"] or "?")
    short_name = full_name.split()[0] if full_name.split() else full_name
    who = _truncate_to_width(draw, short_name, fonts.small_b, name_max_w)
    draw.text((name_x, header_y + 2), who, fill=avatar_color, font=fonts.small_b)

    # Body — tighter line height
    body_y = header_y + 18
    line_h = 18
    for line in lines:
        if body_y + line_h > card_bot - pad_y + 4:
            break
        draw.text((L + pad_x, body_y), line, fill=FG_BODY, font=fonts.body)
        body_y += line_h


def _draw_action_chip(draw, row, L, R, y, fonts):
    """A compact one-line tool action chip — narrow-column friendly."""
    h = 22
    draw.rounded_rectangle(
        (L, y, R, y + h),
        radius=6,
        fill=BG_CARD,
        outline=BORDER_SOFT, width=1,
    )
    sym = row.get("symbol", "•")
    sym_w = _text_w(draw, sym, fonts.small_b)
    sym_x = L + 8
    draw.text((sym_x, y + 4), sym, fill=row["color"], font=fonts.small_b)

    label = row["label"]
    label_x = sym_x + sym_w + 6
    label_y = y + 4

    # Timestamp on right
    ts = _short_time(row["ts"]) or ""
    tw = _text_w(draw, ts, fonts.time)
    ts_x = R - 8 - tw
    draw.text((ts_x, y + 5), ts, fill=MUTED_DIM, font=fonts.time)

    # Truncate label to fit
    label_max_w = ts_x - label_x - 8
    text = _truncate_to_width(draw, label, fonts.small_b, label_max_w)
    draw.text((label_x, label_y), text, fill=FG_BODY, font=fonts.small_b)


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
        if chart_type == "line":
            _draw_line_chart(draw, spec, L, R, top, bot, fonts)
            return
        if chart_type == "pie":
            _draw_pie_chart(draw, spec, L, R, top, bot, fonts)
            return
        if chart_type == "kpi":
            _draw_kpi(draw, spec, L, R, top, bot, fonts)
            return
        if chart_type == "flow":
            _draw_flow(draw, spec, L, R, top, bot, fonts)
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


# ── Chart palette ────────────────────────────────────────────────────────────


def _palette(n: int) -> list[tuple[int, int, int]]:
    """Return n distinct colors from a fixed accent palette, cycling if needed."""
    base = [
        ACCENT,
        OK,
        WARN,
        SPEAKER_USER,
        SPEAKER_CHAT,
        SPEAKER_BOT,
        ERR,
    ]
    return [base[i % len(base)] for i in range(max(0, n))]


# ── Line chart ───────────────────────────────────────────────────────────────


def _draw_line_chart(draw, spec, L, R, y_top, y_bot, fonts):
    try:
        series = spec.get("series", []) or []
        if not series:
            return
        left_margin = 60
        bottom_margin = 30
        top_margin = 20
        right_margin = 20
        plot_left = L + left_margin
        plot_right = R - right_margin
        plot_top = y_top + top_margin
        plot_bot = y_bot - bottom_margin
        plot_width = plot_right - plot_left
        plot_height = plot_bot - plot_top
        all_y: list[float] = []
        for s in series[:4]:
            for p in s.get("data", []) or []:
                try:
                    all_y.append(float(p.get("y", 0)))
                except Exception:
                    continue
        if not all_y:
            return
        y_min = min(all_y)
        y_max = max(all_y)
        if y_min == y_max:
            y_min -= 1
            y_max += 1
        # Y-axis gridlines + labels
        for i in range(5):
            y = plot_bot - i * plot_height // 4
            draw.line((plot_left, y, plot_right, y), fill=BORDER_SOFT)
            val = y_min + (y_max - y_min) * i / 4
            txt = _fmt_number(val)
            w = _text_w(draw, txt, fonts.small)
            draw.text((plot_left - w - 6, y - fonts.small.size // 2), txt, font=fonts.small, fill=MUTED)
        # X-axis labels (use first series for x labels)
        first_data = (series[0].get("data") or [])
        xs = [str(p.get("x", "")) for p in first_data]
        n_pts = len(xs)
        if n_pts == 0:
            return
        step = plot_width / max(n_pts - 1, 1)
        skip = max(1, n_pts // 8)
        for i, x in enumerate(xs):
            if i % skip != 0:
                continue
            x_pos = plot_left + i * step
            txt = _truncate_to_width(draw, x, fonts.small, 80)
            w = _text_w(draw, txt, fonts.small)
            draw.text((int(x_pos - w / 2), plot_bot + 6), txt, font=fonts.small, fill=MUTED)
        # Plot each series
        colors = _palette(len(series[:4]))
        for s_idx, s in enumerate(series[:4]):
            data = s.get("data", []) or []
            if not data:
                continue
            col = colors[s_idx]
            points: list[tuple[float, float]] = []
            for i, p in enumerate(data):
                try:
                    yv = float(p.get("y", 0))
                except Exception:
                    continue
                x = plot_left + i * step
                y = plot_bot - (yv - y_min) * plot_height / (y_max - y_min)
                points.append((x, y))
            if len(points) >= 2:
                draw.line(points, fill=col, width=2)
            for (x, y) in points:
                r = 3
                draw.ellipse((x - r, y - r, x + r, y + r), fill=col, outline=col)
        # Legend (top-right inside plot)
        leg_y = plot_top + 4
        for s_idx, s in enumerate(series[:4]):
            col = colors[s_idx]
            label = s.get("label", "") or ""
            lw = _text_w(draw, label, fonts.small_b)
            r = 4
            dot_x = plot_right - lw - 14
            draw.ellipse((dot_x - r, leg_y + 4, dot_x + r, leg_y + 4 + 2 * r), fill=col)
            draw.text((dot_x + r + 6, leg_y), label, font=fonts.small_b, fill=FG_BODY)
            leg_y += fonts.small_b.size + 4
    except Exception:
        log.exception("_draw_line_chart: failed")


# ── Pie chart ────────────────────────────────────────────────────────────────


def _draw_pie_chart(draw, spec, L, R, y_top, y_bot, fonts):
    try:
        data = list(spec.get("data", []) or [])
        if not data:
            return
        total = 0.0
        for item in data:
            try:
                total += float(item.get("value", 0))
            except Exception:
                continue
        if total <= 0:
            return
        data = sorted(data, key=lambda d: float(d.get("value", 0) or 0), reverse=True)
        if len(data) > 8:
            shown = data[:7]
            other_val = sum(float(d.get("value", 0) or 0) for d in data[7:])
            shown.append({"label": "Other", "value": other_val})
            data = shown
        colors = _palette(len(data))
        width = R - L
        height = y_bot - y_top
        # Pie on the left ~45% of the pane
        pie_zone_w = int(width * 0.45)
        cx = L + pie_zone_w // 2
        cy = y_top + height // 2
        radius = min(pie_zone_w // 2 - 10, height // 2 - 10)
        if radius < 30:
            return
        bbox = (cx - radius, cy - radius, cx + radius, cy + radius)
        start = -90.0  # top
        for idx, item in enumerate(data):
            try:
                val = float(item.get("value", 0))
            except Exception:
                continue
            if val <= 0:
                continue
            end = start + 360.0 * val / total
            draw.pieslice(bbox, start, end, fill=colors[idx], outline=BG_PANE, width=2)
            start = end
        # Legend on the right half
        leg_x = L + pie_zone_w + 30
        leg_y = y_top + 10
        line_h = max(fonts.body.size + 8, 22)
        for idx, item in enumerate(data):
            col = colors[idx]
            try:
                val = float(item.get("value", 0))
            except Exception:
                val = 0.0
            label = str(item.get("label", "")) or "—"
            pct = val / total * 100 if total else 0
            txt = f"{label}  {_fmt_number(val)}  ({pct:.0f}%)"
            txt = _truncate_to_width(draw, txt, fonts.body, R - leg_x - 14)
            r = 5
            draw.ellipse((leg_x, leg_y + 5, leg_x + 2 * r, leg_y + 5 + 2 * r), fill=col)
            draw.text((leg_x + 2 * r + 8, leg_y), txt, font=fonts.body, fill=FG_BODY)
            leg_y += line_h
            if leg_y > y_bot - line_h:
                break
    except Exception:
        log.exception("_draw_pie_chart: failed")


# ── KPI cards ────────────────────────────────────────────────────────────────


def _draw_kpi(draw, spec, L, R, y_top, y_bot, fonts):
    try:
        items = list(spec.get("items", []) or [])
        if not items:
            return
        items = items[:8]
        n = len(items)
        if n <= 4:
            cols = 2 if n >= 2 else 1
        elif n <= 6:
            cols = 3
        else:
            cols = 4
        gap = 14
        width = R - L
        height = y_bot - y_top
        rows_n = (n + cols - 1) // cols
        card_w = (width - (cols - 1) * gap) // cols
        card_h = (height - (rows_n - 1) * gap) // rows_n
        for idx, item in enumerate(items):
            col_idx = idx % cols
            row_idx = idx // cols
            x0 = L + col_idx * (card_w + gap)
            y0 = y_top + row_idx * (card_h + gap)
            x1 = x0 + card_w
            y1 = y0 + card_h
            draw.rounded_rectangle(
                (x0, y0, x1, y1), radius=10,
                fill=BG_CARD, outline=BORDER_SOFT, width=1,
            )
            label = str(item.get("label", "")).upper()
            label = _truncate_to_width(draw, label, fonts.small_b, card_w - 16)
            lw = _text_w(draw, label, fonts.small_b)
            draw.text((x0 + (card_w - lw) // 2, y0 + 12), label, font=fonts.small_b, fill=MUTED)
            val = str(item.get("value", "")) or "—"
            val_font = fonts.viz_title
            val_truncated = _truncate_to_width(draw, val, val_font, card_w - 12)
            vw = _text_w(draw, val_truncated, val_font)
            draw.text(
                (x0 + (card_w - vw) // 2, y0 + (card_h - val_font.size) // 2 - 4),
                val_truncated, font=val_font, fill=FG,
            )
            delta = str(item.get("delta", "") or "")
            d_dir = (item.get("delta_dir") or "").lower()
            delta_color = OK if d_dir == "up" else (ERR if d_dir == "down" else MUTED_DIM)
            if delta:
                dw = _text_w(draw, delta, fonts.body_b)
                # Draw a small filled triangle (rendered as a polygon — works
                # regardless of which fonts have triangle glyphs available).
                arrow_w = 10 if d_dir in ("up", "down") else 0
                gap_w = 6 if arrow_w else 0
                total_w = arrow_w + gap_w + dw
                start_x = x0 + (card_w - total_w) // 2
                text_y = y1 - fonts.body_b.size - 12
                if d_dir == "up":
                    cy = text_y + fonts.body_b.size // 2
                    draw.polygon(
                        [(start_x, cy + 5), (start_x + 10, cy + 5), (start_x + 5, cy - 5)],
                        fill=OK,
                    )
                elif d_dir == "down":
                    cy = text_y + fonts.body_b.size // 2
                    draw.polygon(
                        [(start_x, cy - 5), (start_x + 10, cy - 5), (start_x + 5, cy + 5)],
                        fill=ERR,
                    )
                draw.text(
                    (start_x + arrow_w + gap_w, text_y),
                    delta, font=fonts.body_b, fill=delta_color,
                )
    except Exception:
        log.exception("_draw_kpi: failed")


# ── Flow diagram ─────────────────────────────────────────────────────────────


def _draw_flow(draw, spec, L, R, y_top, y_bot, fonts):
    try:
        nodes = list(spec.get("nodes", []) or [])
        edges = list(spec.get("edges", []) or [])
        if not nodes:
            return
        nodes = nodes[:6]
        n = len(nodes)
        width = R - L
        height = y_bot - y_top
        gap = 20
        if n <= 4:
            cols = n
            rows_n = 1
        else:
            cols = (n + 1) // 2
            rows_n = 2
        node_w = (width - (cols - 1) * gap) // max(cols, 1)
        node_h = min(80, (height - (rows_n - 1) * gap) // max(rows_n, 1))
        # Center the grid vertically
        grid_h = rows_n * node_h + (rows_n - 1) * gap
        offset_y = (height - grid_h) // 2
        pos: dict[str, tuple[int, int, int, int]] = {}
        for idx, node in enumerate(nodes):
            nid = str(node.get("id", f"_n{idx}"))
            col = idx % cols
            row = idx // cols
            x0 = L + col * (node_w + gap)
            y0 = y_top + offset_y + row * (node_h + gap)
            x1 = x0 + node_w
            y1 = y0 + node_h
            pos[nid] = (x0, y0, x1, y1)
            draw.rounded_rectangle(
                (x0, y0, x1, y1), radius=10,
                fill=BG_CARD, outline=ACCENT, width=2,
            )
            label = str(node.get("label", "")) or nid
            label = _truncate_to_width(draw, label, fonts.body_b, node_w - 12)
            tw = _text_w(draw, label, fonts.body_b)
            draw.text(
                (x0 + (node_w - tw) // 2, y0 + (node_h - fonts.body_b.size) // 2),
                label, font=fonts.body_b, fill=FG,
            )
        # Edges
        for edge in edges:
            src = str(edge.get("from", ""))
            dst = str(edge.get("to", ""))
            if src not in pos or dst not in pos:
                continue
            sx0, sy0, sx1, sy1 = pos[src]
            dx0, dy0, dx1, dy1 = pos[dst]
            scy = (sy0 + sy1) / 2
            dcy = (dy0 + dy1) / 2
            if abs(scy - dcy) < 4 and dx0 > sx1:
                # Horizontal edge: right-of-src → left-of-dst
                x_start = sx1
                x_end = dx0
                y_start = scy
                y_end = dcy
            elif sy0 == dy0 and dx0 < sx0:
                # Reversed horizontal (rare): left-of-src → right-of-dst
                x_start = sx0
                x_end = dx1
                y_start = scy
                y_end = dcy
            else:
                # General case: src bottom → dst top, or vice versa
                if dcy >= scy:
                    x_start = (sx0 + sx1) / 2
                    y_start = sy1
                    x_end = (dx0 + dx1) / 2
                    y_end = dy0
                else:
                    x_start = (sx0 + sx1) / 2
                    y_start = sy0
                    x_end = (dx0 + dx1) / 2
                    y_end = dy1
            draw.line(
                (x_start, y_start, x_end, y_end),
                fill=ACCENT_SOFT, width=2,
            )
            # Arrowhead pointing toward (x_end, y_end)
            import math
            dx = x_end - x_start
            dy = y_end - y_start
            length = max(1.0, math.hypot(dx, dy))
            ux, uy = dx / length, dy / length
            ah = 8
            # Two side points, perpendicular to direction
            px, py = -uy, ux
            tip = (x_end, y_end)
            base_x = x_end - ux * ah
            base_y = y_end - uy * ah
            left = (base_x + px * ah * 0.5, base_y + py * ah * 0.5)
            right = (base_x - px * ah * 0.5, base_y - py * ah * 0.5)
            draw.polygon([tip, left, right], fill=ACCENT_SOFT)
    except Exception:
        log.exception("_draw_flow: failed")


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
