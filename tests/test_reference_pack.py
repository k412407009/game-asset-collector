from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from game_asset_collector import reference_pack


def test_infer_video_url_accepts_youtube_and_bilibili_ids() -> None:
    assert reference_pack.infer_video_url("KNPTUL9X9Zs") == "https://www.youtube.com/watch?v=KNPTUL9X9Zs"
    assert reference_pack.infer_video_url("demo__N33E7R9cSmM.mp4") == "https://www.youtube.com/watch?v=N33E7R9cSmM"
    assert reference_pack.infer_video_url("BV1xx411c7mD") == "https://www.bilibili.com/video/BV1xx411c7mD"


def test_classify_business_category_maps_core_labels() -> None:
    assert reference_pack.classify_business_category("battle") == "combat"
    assert reference_pack.classify_business_category("main-city") == "management_progress"
    assert reference_pack.classify_business_category("character") == "character_story"
    assert reference_pack.classify_business_category("ui-menu") == "ui_economy"
    assert reference_pack.classify_business_category("store-screenshot") == "store_promo"
    assert reference_pack.classify_business_category("other") == "other_misc"


def test_write_reference_pack_generates_index_and_symlinks(tmp_path) -> None:
    pack_root = tmp_path / "demo-pack"
    game_dir = pack_root / "Demo-Game"
    (game_dir / "store" / "googleplay").mkdir(parents=True)
    (game_dir / "gameplay" / "frames" / "demo__KNPTUL9X9Zs").mkdir(parents=True)
    (game_dir / "gameplay" / "videos").mkdir(parents=True)
    (game_dir / "store" / "googleplay" / "screenshot_01.jpg").write_bytes(b"x")
    (game_dir / "gameplay" / "frames" / "demo__KNPTUL9X9Zs" / "frame_0001.jpg").write_bytes(b"x")
    (game_dir / "gameplay" / "videos" / "Demo Video__KNPTUL9X9Zs.mp4").write_bytes(b"x")

    metadata = {
        "game_name": "Demo Game",
        "stores": {"googleplay": {"appId": "com.demo.game"}},
        "gameplay": {
            "videos": [{"filename": "Demo Video__KNPTUL9X9Zs.mp4"}],
            "total_frames": 1,
        },
        "labels": {"distribution": {"store-screenshot": 1, "battle": 1}},
    }
    (game_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    (game_dir / "gameplay" / "labels.json").write_text(
        json.dumps(
            {
                "store/googleplay/screenshot_01.jpg": "store-screenshot",
                "gameplay/frames/demo__KNPTUL9X9Zs/frame_0001.jpg": "battle",
            }
        ),
        encoding="utf-8",
    )
    (game_dir / "gameplay" / "descriptions.json").write_text(
        json.dumps(
            {
                "store/googleplay/screenshot_01.jpg": "商店图",
                "gameplay/frames/demo__KNPTUL9X9Zs/frame_0001.jpg": "战斗图",
            }
        ),
        encoding="utf-8",
    )

    outputs = reference_pack.write_reference_pack(pack_root)

    assert outputs["json"].exists()
    assert outputs["markdown"].exists()
    assert (pack_root / "packaged" / "by_category" / "combat").exists()
    assert (pack_root / "packaged" / "top_picks" / "combat").exists()
    assert any((pack_root / "packaged" / "videos" / "Demo-Game").iterdir())
