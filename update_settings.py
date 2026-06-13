#!/usr/bin/env python3
"""Install helper: point Claude Code's statusLine at the GameSense bridge.

Shared by install_windows.ps1 and install_unix.sh so the settings.json merge
logic lives in one place instead of being duplicated (and drifting) across both
installers. PowerShell 5.1's native JSON is unreliable for this -- it rewrites
`hooks` arrays into objects (the "Expected array, but received object" error)
and truncates at a default depth -- so both installers delegate the
read/merge/write to this script. Python is already the runtime dependency.

Usage:
    python update_settings.py <settings_path> <python_cmd> <script_path>
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path


def main() -> int:
    settings_path = Path(sys.argv[1])
    python_cmd = sys.argv[2]
    script_path = sys.argv[3].replace("\\", "/")

    settings: dict = {}
    if settings_path.exists():
        raw = settings_path.read_text(encoding="utf-8-sig")
        if raw.strip():
            try:
                settings = json.loads(raw)
            except json.JSONDecodeError:
                backup = settings_path.with_suffix(settings_path.suffix + ".broken-backup")
                shutil.copy2(settings_path, backup)
                print(f"settings.json had invalid JSON; backup saved at {backup}")
                settings = {}

    if not isinstance(settings, dict):
        settings = {}

    # Repair compatibility issue caused by older installer versions / PowerShell JSON:
    # Claude Code requires each hooks.<EventName> value to be an array of matcher entries.
    hooks = settings.get("hooks")
    if isinstance(hooks, dict):
        for event_name, event_value in list(hooks.items()):
            if isinstance(event_value, dict):
                hooks[event_name] = [event_value]

    settings["statusLine"] = {
        "type": "command",
        "command": f'{python_cmd} "{script_path}"',
        "padding": 2,
        "refreshInterval": 10,
    }

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Settings updated at: {settings_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
