from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from game_asset_collector import fetch_game_assets


def test_normalize_video_target_accepts_ids() -> None:
    assert fetch_game_assets._normalize_video_target("2l4DO5Z10jo") == "https://www.youtube.com/watch?v=2l4DO5Z10jo"
    assert fetch_game_assets._normalize_video_target("BV1xx411c7mD") == "https://www.bilibili.com/video/BV1xx411c7mD"


def test_select_appstore_candidate_rejects_fuzzy_match() -> None:
    results = [
        {"trackName": "Last Fortress: Underground", "bundleId": "com.more.lastfortress.appstore"},
        {"trackName": "Last Island of Survival", "bundleId": "com.herogame.ios.lastdayrules"},
    ]

    app, reason = fetch_game_assets._select_appstore_candidate("Last Beacon Survival", results)

    assert app is None
    assert "too weak" in reason or "ambiguous" in reason
