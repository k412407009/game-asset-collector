#!/usr/bin/env python3
"""CLI shim for the shared collector package."""

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from game_asset_collector.fetch_game_assets import main


if __name__ == "__main__":
    main()
