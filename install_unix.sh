#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/.claude"
cp "$DIR/claude_gamesense_statusline.py" "$HOME/.claude/claude_gamesense_statusline.py"
chmod +x "$HOME/.claude/claude_gamesense_statusline.py"

# Seed default display knobs without clobbering an existing user-edited copy.
if [ -f "$DIR/dac_config.json" ] && [ ! -f "$HOME/.claude/dac_config.json" ]; then
  cp "$DIR/dac_config.json" "$HOME/.claude/dac_config.json"
fi
python3 "$DIR/update_settings.py" \
  "$HOME/.claude/settings.json" \
  python3 \
  "$HOME/.claude/claude_gamesense_statusline.py"
