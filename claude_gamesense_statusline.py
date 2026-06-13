#!/usr/bin/env python3
"""
Claude Code -> SteelSeries GameSense OLED/DAC bridge.

Compact v6 layout for Arctis Nova Pro Wireless DAC/Base Station:
- Three bars only.
- The label for each limit is rendered *inside* the bar.
- Bar 1: 5h window with percent + remaining time (hours/minutes).
- Bar 2: 7d all-models window with percent + remaining time (days/hours).
- Bar 3: Sonnet-only window with percent + remaining time when Claude Code exposes it.

No third-party dependencies.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

GAME = "CLAUDE_CODE"
EVENT = "LIMITS_BARS_V6"
DISPLAY_NAME = "Claude Limits"
DEVELOPER = "adonaipedro"
# bind_game_event can take several seconds the first time the engine wires up a
# screen handler, so the timeout must be generous or the bind never completes and
# nothing ever reaches the DAC. game_event posts are fast and tolerate this too.
REQUEST_TIMEOUT_SECONDS = 6.0
REBIND_AFTER_SECONDS = 60 * 60

STATE_PATH = Path.home() / ".claude" / ".gamesense_claude_code_state.json"
# User-editable knobs for the "show bars only when usage % changes" feature.
# SteelSeries GG has no settings UI for GameSense apps, so this lives in a local
# JSON file both the statusline bridge and the standalone daemon read at runtime.
CONFIG_PATH = Path.home() / ".claude" / "dac_config.json"
# Tracks the last percentages we actually pushed plus when the bars were shown,
# so the stateless statusline process (re-invoked every ~10s) and the long-lived
# daemon share one decision about whether to show, keep, blank, or skip a frame.
DISPLAY_STATE_PATH = Path.home() / ".claude" / ".dac_display_state.json"
# Defaults when dac_config.json is missing/partial. Off => always-on behavior is
# unchanged; 30s is how long bars stay after a change before hiding.
DEFAULT_SHOW_ONLY_ON_CHANGE = False
DEFAULT_CHANGE_DISPLAY_SECONDS = 30
IMAGE_W = 128
IMAGE_H = 52  # Arctis Nova Pro DAC OLED is 128x52 (per GameSense SDK + hardware probe)
# Dynamic per-event frame key. The GameSense screen API only registers the
# documented sizes (128x36/40/48/52); 128x64 does NOT exist, so a 64-tall frame
# is silently dropped and the screen stays blank.
IMAGE_KEY = f"image-data-{IMAGE_W}x{IMAGE_H}"
DEVICE_TYPE = f"screened-{IMAGE_W}x{IMAGE_H}"
TOP_RESERVED = 2  # 52px panel has no system strip; use nearly the full height

SAMPLE_STATUS: Dict[str, Any] = {
    "model": {"display_name": "Sonnet"},
    "context_window": {
        "used_percentage": 42,
        "total_input_tokens": 82000,
        "total_output_tokens": 2000,
        "context_window_size": 200000,
    },
    "cost": {"total_cost_usd": 0.12},
    "rate_limits": {
        "five_hour": {"used_percentage": 37, "resets_at": int(time.time()) + 2 * 3600 + 35 * 60},
        "seven_day": {"used_percentage": 41, "resets_at": int(time.time()) + 4 * 86400 + 2 * 3600},
    },
}

# 5x7 font. Each row is a 5-bit value.
FONT_5X7: Dict[str, tuple[int, ...]] = {
    ' ': (0,0,0,0,0,0,0),
    '-': (0,0,0b11111,0,0,0,0),
    '%': (0b11001,0b11010,0b00100,0b01011,0b10011,0,0),
    '0': (0b01110,0b10001,0b10011,0b10101,0b11001,0b10001,0b01110),
    '1': (0b00100,0b01100,0b00100,0b00100,0b00100,0b00100,0b01110),
    '2': (0b01110,0b10001,0b00001,0b00010,0b00100,0b01000,0b11111),
    '3': (0b11110,0b00001,0b00001,0b01110,0b00001,0b00001,0b11110),
    '4': (0b00010,0b00110,0b01010,0b10010,0b11111,0b00010,0b00010),
    '5': (0b11111,0b10000,0b10000,0b11110,0b00001,0b00001,0b11110),
    '6': (0b00110,0b01000,0b10000,0b11110,0b10001,0b10001,0b01110),
    '7': (0b11111,0b00001,0b00010,0b00100,0b01000,0b01000,0b01000),
    '8': (0b01110,0b10001,0b10001,0b01110,0b10001,0b10001,0b01110),
    '9': (0b01110,0b10001,0b10001,0b01111,0b00001,0b00010,0b11100),
    'd': (0b00001,0b00001,0b01111,0b10001,0b10001,0b10011,0b01101),
    'h': (0b10000,0b10000,0b11110,0b10001,0b10001,0b10001,0b10001),
    'm': (0b00000,0b00000,0b11010,0b10101,0b10101,0b10101,0b10101),
}


def _compact_number(value: Any) -> str:
    try:
        n = float(value or 0)
    except (TypeError, ValueError):
        n = 0.0

    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 10_000:
        return f"{round(n / 1_000):.0f}k"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k".replace(".0k", "k")
    return str(int(n))


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp_pct(value: Any) -> int:
    return max(0, min(100, _safe_int(value, 0)))


def _read_json_file(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json_file(path: Path, data: Dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def core_props_candidates() -> list[Path]:
    candidates: list[Path] = []

    override = os.environ.get("STEELSERIES_CORE_PROPS")
    if override:
        candidates.append(Path(override).expanduser())

    program_data = os.environ.get("PROGRAMDATA")
    if program_data:
        candidates.append(Path(program_data) / "SteelSeries" / "SteelSeries Engine 3" / "coreProps.json")

    candidates.extend([
        Path("C:/ProgramData/SteelSeries/SteelSeries Engine 3/coreProps.json"),
        Path("/Library/Application Support/SteelSeries Engine 3/coreProps.json"),
        Path.home() / "Library/Application Support/SteelSeries Engine 3/coreProps.json",
    ])

    seen = set()
    unique = []
    for item in candidates:
        key = str(item)
        if key not in seen:
            unique.append(item)
            seen.add(key)
    return unique


def find_gamesense_base_url() -> Optional[str]:
    for path in core_props_candidates():
        data = _read_json_file(path)
        address = (data or {}).get("address")
        if isinstance(address, str) and ":" in address:
            return f"http://{address}"
    return None


def post_json(base_url: str, endpoint: str, payload: Dict[str, Any]) -> bool:
    url = base_url.rstrip("/") + "/" + endpoint.lstrip("/")
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def should_bind(base_url: str) -> bool:
    state = _read_json_file(STATE_PATH) or {}
    if state.get("base_url") != base_url or state.get("event") != EVENT:
        return True
    bound_at = _safe_float(state.get("bound_at"), 0)
    return (time.time() - bound_at) > REBIND_AFTER_SECONDS


def _blank_image_data() -> list[int]:
    return [0] * ((IMAGE_W * IMAGE_H) // 8)


def bind_gamesense_event(base_url: str) -> bool:
    post_json(base_url, "game_metadata", {
        "game": GAME,
        "game_display_name": DISPLAY_NAME,
        "developer": DEVELOPER,
        # GameSense caps this at 60000 ms (60s). The standalone daemon keeps the
        # screen alive past this by re-pushing the frame on a sub-60s keep-alive.
        "deinitialize_timer_length_ms": 60000,
    })

    payload = {
        "game": GAME,
        "event": EVENT,
        "min_value": 0,
        "max_value": 100,
        "icon_id": 0,
        "value_optional": True,
        "handlers": [
            {
                "device-type": DEVICE_TYPE,
                "zone": "one",
                "mode": "screen",
                "datas": [
                    {
                        "length-millis": 10000,
                        "repeats": True,
                        "has-text": False,
                        # Static placeholder; the real image is supplied per event
                        # via the IMAGE_KEY frame in game_event. Static bind images
                        # use the generic "image-data" field (not the sized key).
                        "image-data": _blank_image_data(),
                    }
                ],
            }
        ],
    }

    ok = post_json(base_url, "bind_game_event", payload)
    if ok:
        _write_json_file(STATE_PATH, {"base_url": base_url, "event": EVENT, "bound_at": time.time()})
    return ok


def _limit_info(window: Dict[str, Any]) -> tuple[Optional[int], int]:
    if not isinstance(window, dict) or "used_percentage" not in window:
        return None, 0
    pct = _clamp_pct(window.get("used_percentage"))
    reset_ts = _safe_int(window.get("resets_at", window.get("reset_at", 0)), 0)
    return pct, reset_ts


def _find_window(rate_limits: Dict[str, Any], preferred_keys: list[str]) -> Dict[str, Any]:
    """Find a rate-limit window even if Claude Code changes the exact key name."""
    if not isinstance(rate_limits, dict):
        return {}

    for key in preferred_keys:
        value = rate_limits.get(key)
        if isinstance(value, dict) and "used_percentage" in value:
            return value

    # Common nested shapes seen/expected around model-specific limits.
    nested_keys = ["models", "model", "model_limits", "model_specific", "weekly_model_limits", "weekly"]
    for container_key in nested_keys:
        container = rate_limits.get(container_key)
        if isinstance(container, dict):
            for key in preferred_keys:
                value = container.get(key)
                if isinstance(value, dict) and "used_percentage" in value:
                    return value

    return {}


def _remaining_brief(reset_ts: int, *, weekly: bool) -> str:
    if reset_ts <= 0:
        return "--"
    seconds = max(0, int(reset_ts - time.time()))
    if weekly:
        days = seconds // 86400
        if days >= 1:
            return f"{days}d"
        hours = max(1, seconds // 3600)
        return f"{hours}h"
    # 5h/session window
    hours = seconds // 3600
    if hours >= 1:
        return f"{hours}h"
    minutes = max(1, seconds // 60)
    return f"{minutes}m"


def _bar_label(name: str, pct: Optional[int], reset_ts: int, *, weekly: bool) -> str:
    if pct is None:
        return f"{name} --"
    remain = _remaining_brief(reset_ts, weekly=weekly)
    return f"{name} {pct}% {remain}"


def _new_canvas() -> list[list[int]]:
    return [[0 for _ in range(IMAGE_W)] for _ in range(IMAGE_H)]


def _set_px(img: list[list[int]], x: int, y: int, value: int = 1) -> None:
    if 0 <= x < IMAGE_W and 0 <= y < IMAGE_H:
        img[y][x] = 1 if value else 0


def _draw_rect(img: list[list[int]], x: int, y: int, w: int, h: int, *, fill: bool = False, value: int = 1) -> None:
    if w <= 0 or h <= 0:
        return
    if fill:
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                _set_px(img, xx, yy, value)
    else:
        for xx in range(x, x + w):
            _set_px(img, xx, y, value)
            _set_px(img, xx, y + h - 1, value)
        for yy in range(y, y + h):
            _set_px(img, x, yy, value)
            _set_px(img, x + w - 1, yy, value)


def _text_width(text: str) -> int:
    if not text:
        return 0
    return len(text) * 6 - 1


def _draw_char(img: list[list[int]], x: int, y: int, ch: str, color_for_pixel) -> None:
    glyph = FONT_5X7.get(ch, FONT_5X7[' '])
    for yy, row in enumerate(glyph):
        for xx in range(5):
            if row & (1 << (4 - xx)):
                color = color_for_pixel(x + xx, y + yy)
                _set_px(img, x + xx, y + yy, color)


def _draw_text(img: list[list[int]], x: int, y: int, text: str, color_for_pixel) -> None:
    cursor_x = x
    for ch in text:
        _draw_char(img, cursor_x, y, ch, color_for_pixel)
        cursor_x += 6


def _draw_bar_with_label(img: list[list[int]], x: int, y: int, w: int, h: int, pct: int, label: str) -> None:
    # Border and fill.
    _draw_rect(img, x, y, w, h, fill=False, value=1)
    inner_x, inner_y = x + 1, y + 1
    inner_w, inner_h = max(0, w - 2), max(0, h - 2)
    fill_w = max(0, min(inner_w, int(round(inner_w * (pct / 100.0)))))
    if fill_w > 0 and inner_h > 0:
        _draw_rect(img, inner_x, inner_y, fill_w, inner_h, fill=True, value=1)

    # Center text inside the bar. Invert text when it falls on the filled area.
    tw = _text_width(label)
    tx = x + max(1, (w - tw) // 2)
    ty = y + max(1, (h - 7) // 2)

    def color_for_pixel(px: int, py: int) -> int:
        inside_fill = (inner_x <= px < inner_x + fill_w) and (inner_y <= py < inner_y + inner_h)
        return 0 if inside_fill else 1

    _draw_text(img, tx, ty, label, color_for_pixel)


def _pack_image(img: list[list[int]]) -> list[int]:
    data: list[int] = []
    for y in range(IMAGE_H):
        x = 0
        while x < IMAGE_W:
            byte = 0
            for bit in range(8):
                byte <<= 1
                if x + bit < IMAGE_W and img[y][x + bit]:
                    byte |= 1
            data.append(byte)
            x += 8
    return data


def _extract_rate_limit_windows(status: Dict[str, Any]) -> tuple[tuple[Optional[int], int], tuple[Optional[int], int]]:
    rate_limits = status.get("rate_limits") or {}

    five_window = _find_window(rate_limits, ["five_hour", "5h", "session", "fiveHour", "five_hour_limit"])
    week_window = _find_window(rate_limits, ["seven_day", "7d", "weekly", "week", "sevenDay", "seven_day_all_models", "weekly_all_models"])

    return _limit_info(five_window), _limit_info(week_window)


def render_limits_image(status: Dict[str, Any]) -> tuple[list[int], int, int, str, str]:
    # Sonnet has no dedicated subscription rate-limit header, so only the 5h and
    # 7d windows are rendered (two bars).
    (five_pct, five_reset), (week_pct, week_reset) = _extract_rate_limit_windows(status)

    # Fallback to session context if limits are absent.
    context = status.get("context_window") or {}
    ctx_pct = _clamp_pct(context.get("used_percentage"))
    if five_pct is None and week_pct is None:
        five_pct = ctx_pct
        week_pct = 0
        label1 = f"ctx {ctx_pct}%"
        label2 = "7d --"
    else:
        five_pct = 0 if five_pct is None else five_pct
        week_pct = 0 if week_pct is None else week_pct
        label1 = _bar_label("5h", five_pct, five_reset, weekly=False)
        label2 = _bar_label("7d", week_pct, week_reset, weekly=True)

    img = _new_canvas()
    bar_x = 5
    bar_w = 118
    bar_h = 20  # two 20px bars + one gap, vertically centred in the 52px panel
    gap = 4
    bar1_y = (IMAGE_H - (bar_h * 2 + gap)) // 2
    bar2_y = bar1_y + bar_h + gap

    _draw_bar_with_label(img, bar_x, bar1_y, bar_w, bar_h, five_pct, label1)
    _draw_bar_with_label(img, bar_x, bar2_y, bar_w, bar_h, week_pct, label2)
    return _pack_image(img), five_pct, week_pct, label1, label2

def build_frame(status: Dict[str, Any]) -> Dict[str, Any]:
    image_data, five_value, week_value, label1, label2 = render_limits_image(status)
    progress = max(five_value, week_value)
    return {
        "value": progress,
        "frame": {
            IMAGE_KEY: image_data,
            "debug_label_1": label1,
            "debug_label_2": label2,
        },
    }


def _ensure_gamesense_ready() -> Optional[str]:
    """Locate the GameSense engine and (re)bind the screen event when needed.

    Factored out of send_to_gamesense so the change-gated path and blank-frame
    push reuse the same discovery + bind logic.
    @returns The engine base URL, or None when GameSense is unreachable.
    """
    base_url = find_gamesense_base_url()
    if not base_url:
        return None
    if should_bind(base_url):
        bind_gamesense_event(base_url)
    return base_url


def send_to_gamesense(status: Dict[str, Any]) -> bool:
    base_url = _ensure_gamesense_ready()
    if not base_url:
        return False
    return post_json(base_url, "game_event", {
        "game": GAME,
        "event": EVENT,
        "data": build_frame(status),
    })


def push_blank_frame(base_url: str) -> bool:
    """Push an all-zero frame so the DAC goes dark immediately.

    Used by show-only-on-change mode to hide the bars at the end of the
    configured display window, instead of waiting for GameSense's 60s deinit.
    @param base_url The GameSense engine base URL.
    @returns True if the blank frame was accepted.
    """
    return post_json(base_url, "game_event", {
        "game": GAME,
        "event": EVENT,
        "data": {"value": 0, "frame": {IMAGE_KEY: _blank_image_data()}},
    })


def load_dac_config() -> Dict[str, Any]:
    """Read user knobs from dac_config.json, applying defaults for missing keys.

    @returns Dict with `show_only_on_change` (bool) and `change_display_seconds`
        (int, clamped to >= 1).
    """
    data = _read_json_file(CONFIG_PATH) or {}
    seconds = _safe_int(data.get("change_display_seconds"), DEFAULT_CHANGE_DISPLAY_SECONDS)
    return {
        "show_only_on_change": bool(data.get("show_only_on_change", DEFAULT_SHOW_ONLY_ON_CHANGE)),
        "change_display_seconds": max(1, seconds),
    }


def gated_send(status: Dict[str, Any]) -> str:
    """Push to the DAC, honoring the show-only-on-change setting.

    When the setting is off, behaves exactly like send_to_gamesense (always
    push). When on, the bars are pushed only when the 5h/7d percentage changes,
    held for `change_display_seconds`, then blanked once and left dark until the
    next change. Decision state is persisted to DISPLAY_STATE_PATH so the
    stateless statusline process and the long-lived daemon agree.
    @param status Claude Code statusLine payload (or daemon-built equivalent).
    @returns One of "sent", "keepalive", "blanked", "skipped", or "failed".
    """
    cfg = load_dac_config()
    if not cfg["show_only_on_change"]:
        return "sent" if send_to_gamesense(status) else "failed"

    base_url = _ensure_gamesense_ready()
    if not base_url:
        return "failed"

    image_data, five_pct, week_pct, label1, label2 = render_limits_image(status)
    frame_data = {
        "value": max(five_pct, week_pct),
        "frame": {IMAGE_KEY: image_data, "debug_label_1": label1, "debug_label_2": label2},
    }

    def _post_real() -> bool:
        return post_json(base_url, "game_event", {"game": GAME, "event": EVENT, "data": frame_data})

    state = _read_json_file(DISPLAY_STATE_PATH) or {}
    changed = (state.get("five"), state.get("week")) != (five_pct, week_pct)
    showing = bool(state.get("showing"))
    shown_at = _safe_float(state.get("shown_at"), 0.0)
    now = time.time()

    if changed:
        ok = _post_real()
        _write_json_file(DISPLAY_STATE_PATH, {
            "five": five_pct, "week": week_pct, "shown_at": now, "showing": True,
        })
        return "sent" if ok else "failed"

    if showing and (now - shown_at) < cfg["change_display_seconds"]:
        # Inside the visible window: re-push so GameSense doesn't deinit early.
        return "keepalive" if _post_real() else "failed"

    if showing:
        # Window elapsed: hide once, then stay dark until the next change.
        push_blank_frame(base_url)
        _write_json_file(DISPLAY_STATE_PATH, {**state, "showing": False})
        return "blanked"

    return "skipped"


def print_statusline(status: Dict[str, Any], sent: bool) -> None:
    context = status.get("context_window") or {}
    cost = status.get("cost") or {}
    rate_limits = status.get("rate_limits") or {}
    ctx_pct = _clamp_pct(context.get("used_percentage"))
    total = _safe_int(context.get("total_input_tokens"), 0) + _safe_int(context.get("total_output_tokens"), 0)
    window_size = _safe_int(context.get("context_window_size"), 200_000)
    cost_usd = _safe_float(cost.get("total_cost_usd"), 0.0)

    (five_pct, five_ts), (week_pct, week_ts) = _extract_rate_limit_windows(status)
    five_reset = _remaining_brief(five_ts, weekly=False) if five_pct is not None else '--'
    week_reset = _remaining_brief(week_ts, weekly=True) if week_pct is not None else '--'

    dac = "DAC ok" if sent else "DAC x"
    print(
        f"5h {five_pct if five_pct is not None else '--'}% {five_reset} | "
        f"7d {week_pct if week_pct is not None else '--'}% {week_reset} | "
        f"ctx {ctx_pct}% ({_compact_number(total)}/{_compact_number(window_size)}) | ${cost_usd:.2f} | {dac}"
    )


def _read_status_from_stdin() -> Dict[str, Any]:
    raw = sys.stdin.read()
    try:
        parsed = json.loads(raw) if raw.strip() else {}
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def main() -> int:
    # Windows consoles default to cp1252; force UTF-8 so status output never
    # crashes the statusLine hook on a non-encodable character.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(description="Claude Code statusLine -> SteelSeries GameSense bridge")
    parser.add_argument("--sample", action="store_true", help="send a built-in sample payload for testing")
    parser.add_argument("--print-frame", action="store_true", help="print the GameSense frame JSON and exit")
    args = parser.parse_args()

    status = SAMPLE_STATUS if args.sample else _read_status_from_stdin()

    if args.print_frame:
        print(json.dumps(build_frame(status), indent=2, ensure_ascii=False))
        return 0

    sent = False
    try:
        sent = gated_send(status) in ("sent", "keepalive", "blanked")
    finally:
        print_statusline(status, sent)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
