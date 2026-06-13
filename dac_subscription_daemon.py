#!/usr/bin/env python3
"""
Standalone DAC daemon: shows live Claude subscription limits (5h / 7d) on the
SteelSeries DAC OLED *without* Claude Code running.

It reads the Claude Code OAuth token from ~/.claude/.credentials.json, makes a
tiny authenticated request to the Anthropic API, and reads the subscription
rate-limit windows from the `anthropic-ratelimit-unified-*` response headers --
the same data Claude Code's status line shows. Then it renders the two-bar
image and pushes it to the DAC via the existing GameSense bridge.

Run once (for a scheduled task) or as a loop:
    python dac_subscription_daemon.py            # one poll + push, then exit
    python dac_subscription_daemon.py --loop 600 # poll every 600s forever
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

import claude_gamesense_statusline as gs

CRED_PATH = Path.home() / ".claude" / ".credentials.json"
LOG_PATH = Path.home() / ".claude" / "dac_daemon.log"
# SteelSeries GG stores the per-app on/off toggle in this SQLite DB
# (game_integration_games.enabled). This is the authoritative toggle state --
# the GameSense game_metadata response's "enabled" field is unrelated to it.
GG_DB_PATH = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "SteelSeries" / "GG" / "db" / "database.db"
API_URL = "https://api.anthropic.com/v1/messages"
TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
# Public Claude Code OAuth client id (same one the CLI uses).
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
PROBE_MODEL = "claude-haiku-4-5"
# While waiting for SteelSeries GameSense to come up (e.g. at logon, before the
# GG app has finished starting), re-check this often instead of the full interval.
WAIT_FOR_GG = 15
# GameSense deinitializes the game (screen reverts to default) 60s after the last
# event, so re-push the cached frame more often than that to keep the screen on.
# This is a local push only -- it does NOT call the Anthropic API.
KEEPALIVE_SECONDS = 45


def _log(msg: str) -> None:
    """Append a timestamped diagnostic line to the daemon log file."""
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def _read_creds() -> Dict[str, Any]:
    return json.loads(CRED_PATH.read_text(encoding="utf-8"))


def _write_creds(data: Dict[str, Any]) -> None:
    tmp = CRED_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(CRED_PATH)


def _refresh_token(creds: Dict[str, Any]) -> Optional[str]:
    """Refresh the OAuth access token using the stored refresh token.

    Writes the rotated tokens back atomically. Returns the new access token,
    or None on failure (in which case the user must reopen Claude Code once).
    """
    oauth = creds.get("claudeAiOauth", {})
    refresh = oauth.get("refreshToken")
    if not refresh:
        return None

    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": OAUTH_CLIENT_ID,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        method="POST",
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            payload = json.loads(r.read().decode())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"[daemon] token refresh failed: {exc}", file=sys.stderr)
        return None

    access = payload.get("access_token")
    if not access:
        return None
    oauth["accessToken"] = access
    if payload.get("refresh_token"):
        oauth["refreshToken"] = payload["refresh_token"]
    if payload.get("expires_in"):
        oauth["expiresAt"] = int((time.time() + payload["expires_in"]) * 1000)
    creds["claudeAiOauth"] = oauth
    _write_creds(creds)
    print("[daemon] token refreshed", file=sys.stderr)
    return access


def _valid_access_token() -> Optional[str]:
    """Return a usable access token, refreshing if it is expired/near expiry."""
    try:
        creds = _read_creds()
    except (OSError, ValueError):
        return None
    oauth = creds.get("claudeAiOauth", {})
    token = oauth.get("accessToken")
    expires_at = oauth.get("expiresAt", 0) / 1000.0
    # Refresh if within 60s of expiry.
    if not token or time.time() > (expires_at - 60):
        return _refresh_token(creds) or token
    return token


def _parse_unified_limits(headers: Any) -> Dict[str, Any]:
    """Build a status dict (Claude Code statusLine shape) from response headers."""
    def f(name: str) -> Optional[str]:
        return headers.get(name)

    rate_limits: Dict[str, Any] = {}

    five_util = f("anthropic-ratelimit-unified-5h-utilization")
    five_reset = f("anthropic-ratelimit-unified-5h-reset")
    if five_util is not None:
        rate_limits["five_hour"] = {
            "used_percentage": round(float(five_util) * 100),
            "resets_at": int(five_reset) if five_reset else 0,
        }

    week_util = f("anthropic-ratelimit-unified-7d-utilization")
    week_reset = f("anthropic-ratelimit-unified-7d-reset")
    if week_util is not None:
        rate_limits["seven_day"] = {
            "used_percentage": round(float(week_util) * 100),
            "resets_at": int(week_reset) if week_reset else 0,
        }

    return {"rate_limits": rate_limits}


def fetch_subscription_status() -> Optional[Dict[str, Any]]:
    """Poll the API once and return a status dict, or None on failure."""
    token = _valid_access_token()
    if not token:
        print("[daemon] no usable token (open Claude Code once to log in)", file=sys.stderr)
        return None

    body = json.dumps({
        "model": PROBE_MODEL,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "."}],
    }).encode()
    req = urllib.request.Request(
        API_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return _parse_unified_limits(r.headers)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            # Token rejected -> force a refresh next call.
            creds = _read_creds()
            if _refresh_token(creds):
                return fetch_subscription_status()
        print(f"[daemon] API error {exc.code}: {exc.read().decode()[:200]}", file=sys.stderr)
        return None
    except (urllib.error.URLError, OSError) as exc:
        print(f"[daemon] network error: {exc}", file=sys.stderr)
        return None


def _gg_toggle_enabled() -> bool:
    """Read the SteelSeries GG per-app on/off toggle from its SQLite DB.

    `game_integration_games.enabled` is "1" when the user has the app switched on
    in GG's app list, "0" when toggled off. Opened read-only + immutable so a lock
    held by GG never blocks us. Returns False on any error (treat as off).
    """
    try:
        con = sqlite3.connect(f"file:{GG_DB_PATH}?mode=ro&immutable=1", uri=True, timeout=2)
        try:
            row = con.execute(
                "SELECT enabled FROM game_integration_games WHERE game_name=?",
                (gs.GAME,),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error as exc:
        _log(f"_gg_toggle_enabled: db error {exc!r}")
        return False
    return row is not None and str(row[0]) == "1"


def _engine_up() -> bool:
    """True if the GameSense engine is reachable (GG/engine running).

    Posting game_metadata is safe -- it does NOT re-enable a toggled-off app.
    Distinguishes 'GG fully closed' (engine down) from a merely stale DB row.
    """
    base = gs.find_gamesense_base_url()
    if not base:
        return False
    return gs.post_json(base, "game_metadata", {
        "game": gs.GAME,
        "game_display_name": gs.DISPLAY_NAME,
        "developer": gs.DEVELOPER,
        "deinitialize_timer_length_ms": 60000,  # GameSense max is 60000 ms
    })


def _game_active() -> bool:
    """True only if the app is toggled on in GG AND the engine is running.

    Covers both shutdown paths the user uses:
      - app toggled OFF in GG (engine still up)  -> DB enabled=0  -> False
      - GG / engine fully closed                  -> engine down  -> False
    The DB read is cheap and offline, so it gates first; the engine is only
    pinged when the toggle is on.
    """
    if not _gg_toggle_enabled():
        return False
    return _engine_up()


def poll_once() -> bool:
    status = fetch_subscription_status()
    if status is None:
        return False
    sent = gs.send_to_gamesense(status)
    rl = status.get("rate_limits", {})
    five = rl.get("five_hour", {})
    week = rl.get("seven_day", {})
    print(
        f"5h {five.get('used_percentage', '--')}% | "
        f"7d {week.get('used_percentage', '--')}% | "
        f"DAC {'ok' if sent else 'x'}"
    )
    return sent


def main() -> int:
    parser = argparse.ArgumentParser(description="DAC daemon for Claude subscription limits")
    parser.add_argument("--loop", type=int, metavar="SECONDS",
                        help="poll forever every N seconds (default: run once and exit)")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    if args.loop:
        print(f"[daemon] refresh every {args.loop}s, keep-alive every {KEEPALIVE_SECONDS}s; "
              "exits when GameSense goes down")
        _log(f"daemon start (loop={args.loop}s, keepalive={KEEPALIVE_SECONDS}s)")
        was_up = False
        last_status: Optional[Dict[str, Any]] = None
        last_fetch = 0.0
        while True:
            if not _game_active():
                if was_up:
                    _log("game inactive after being active -> exiting")
                    # Game was active and is now off (user closed SteelSeries GG
                    # or toggled the app off in GG) -> shut down. Restarts at the
                    # next Windows logon.
                    print("[daemon] GameSense game off -> exiting")
                    break
                # Not up yet (e.g. launched at logon before GG finished starting);
                # wait for it rather than exiting immediately.
                time.sleep(WAIT_FOR_GG)
                continue

            if not was_up:
                _log("game became active")
            was_up = True
            now = time.monotonic()
            # Refresh the numbers from the Anthropic API only every args.loop seconds.
            if last_status is None or (now - last_fetch) >= args.loop:
                fetched = fetch_subscription_status()
                if fetched is not None:
                    last_status = fetched
                    last_fetch = now
                    rl = last_status.get("rate_limits", {})
                    print(f"5h {rl.get('five_hour', {}).get('used_percentage', '--')}% | "
                          f"7d {rl.get('seven_day', {}).get('used_percentage', '--')}% | refreshed")
            # Keep-alive / change-gated push: gated_send re-pushes the cached
            # frame to keep the screen on, OR (when show_only_on_change is set in
            # dac_config.json) only shows bars on a percentage change and blanks
            # them after the configured window.
            tick = KEEPALIVE_SECONDS
            if last_status is not None:
                result = gs.gated_send(last_status)
                # In change-only mode, tick faster while bars are on so the
                # blank-out fires near the configured display duration instead of
                # up to KEEPALIVE_SECONDS late.
                if result in ("sent", "keepalive"):
                    tick = min(KEEPALIVE_SECONDS, 10)
            time.sleep(tick)
    else:
        poll_once()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
