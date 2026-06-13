"""Make the repo root importable so ``import claude_gamesense_statusline`` works
when tests run from any working directory (pytest or ``python -m unittest``)."""
import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
