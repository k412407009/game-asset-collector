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
    assert "key_sources" in report


def test_env_lookup_accepts_legacy_tavily_key_case(monkeypatch) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("Tavily_API_Key", raising=False)
    monkeypatch.delenv("Tavily_API_KEY", raising=False)
    monkeypatch.setenv("Tavily_API_Key", "demo-key")

    value, source = fetch_game_assets._find_env_value("TAVILY_API_KEY")

    assert value == "demo-key"
    assert source == "Tavily_API_Key"


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


def test_analysis_segments_prioritize_dense_intro() -> None:
    segments = fetch_game_assets._analysis_segments(1107.6)

    assert segments[0]["label"] == "intro"
    assert segments[0]["interval"] == 4
    assert segments[0]["start"] == 0.0
    assert segments[0]["end"] == 600.0
    assert segments[1]["label"] == "mid"
    assert segments[1]["interval"] == 8


def test_decide_video_type_prefers_walkthrough_for_long_gameplay_signal() -> None:
    video_type, reason = fetch_game_assets._decide_video_type(
        title="Narco Empire Gameplay Walkthrough (Android)",
        duration_sec=1107.6,
        portrait_ratio=0.8,
        unique_ratio=0.5,
    )

    assert video_type == "walkthrough"
    assert reason in {"title-keyword", "portrait-ui", "long-form"}


def test_decide_video_type_prefers_trailer_for_short_promo_signal() -> None:
    video_type, reason = fetch_game_assets._decide_video_type(
        title="Official Game Trailer",
        duration_sec=52.0,
        portrait_ratio=0.0,
        unique_ratio=0.9,
    )

    assert video_type == "trailer"
    assert reason in {"title-keyword", "short-high-cut", "short-form"}


def test_write_timeline_summary_uses_frame_index_and_labels(tmp_path) -> None:
    game_dir = tmp_path / "demo-game"
    gameplay_dir = game_dir / "gameplay"
    gameplay_dir.mkdir(parents=True)

    (gameplay_dir / "frame_index.json").write_text(
        json.dumps(
            {
                "gameplay/frames/demo/frame_t000004_0001.jpg": {
                    "timestamp_sec": 4,
                    "timestamp": "00:04",
                    "interval_sec": 4,
                    "segment": "intro",
                    "video_filename": "demo.mp4",
                    "video_slug": "demo",
                }
            }
        ),
        encoding="utf-8",
    )
    (gameplay_dir / "labels.json").write_text(
        json.dumps({"gameplay/frames/demo/frame_t000004_0001.jpg": "tutorial"}),
        encoding="utf-8",
    )
    (gameplay_dir / "descriptions.json").write_text(
        json.dumps({"gameplay/frames/demo/frame_t000004_0001.jpg": "开场引导点击教程"}),
        encoding="utf-8",
    )

    output = fetch_game_assets.write_timeline_summary(game_dir)

    assert output is not None
    text = output.read_text(encoding="utf-8")
    assert "Gameplay Timeline Summary" in text
    assert "00:04 | tutorial [intro] | 开场引导点击教程" in text
