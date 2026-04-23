from __future__ import annotations

import json
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


def test_build_doctor_report_exposes_core_fields() -> None:
    report = fetch_game_assets._build_doctor_report()

    assert report["repo_root"].endswith("game-asset-collector")
    assert "python_ok" in report
    assert "commands" in report
    assert "keys" in report


def test_write_collection_summary_creates_human_readable_report(tmp_path) -> None:
    game_dir = tmp_path / "demo-game"
    (game_dir / "store" / "googleplay").mkdir(parents=True)
    (game_dir / "gameplay" / "frames" / "vid1").mkdir(parents=True)
    (game_dir / "gameplay" / "videos").mkdir(parents=True)
    (game_dir / "store" / "googleplay" / "screenshot_01.jpg").write_bytes(b"x")
    (game_dir / "gameplay" / "frames" / "vid1" / "frame_0001.jpg").write_bytes(b"x")
    (game_dir / "gameplay" / "videos" / "vid1.mp4").write_bytes(b"x")
    (game_dir / "gameplay" / "labels.json").write_text(json.dumps({"a": "cover"}), encoding="utf-8")
    (game_dir / "gameplay" / "descriptions.json").write_text(json.dumps({"a": "desc"}), encoding="utf-8")

    summary_path = tmp_path / "meta" / "demo.collection_summary.md"
    resource_list_path = tmp_path / "meta" / "demo.image_resource_list.md"
    resource_list_path.parent.mkdir(parents=True)
    resource_list_path.write_text("demo", encoding="utf-8")

    fetch_game_assets.write_collection_summary(
        game_dir=game_dir,
        project_root=None,
        game_name="Demo Game",
        metadata={"stores": {"googleplay": {"title": "Demo Game"}}},
        out_md=summary_path,
        resource_list_path=resource_list_path,
    )

    text = summary_path.read_text(encoding="utf-8")
    assert "采集摘要：Demo Game" in text
    assert "抓到了什么" in text
    assert "还缺什么" in text
