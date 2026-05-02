from __future__ import annotations

from game_asset_collector import scys_course


def test_parse_course_target_from_url() -> None:
    target = scys_course.parse_course_target("https://scys.com/course/detail/148?chapterId=9614")

    assert target.course_id == 148
    assert target.chapter_id == 9614
    assert target.url == "https://scys.com/course/detail/148?chapterId=9614"


def test_render_markdown_and_collect_assets() -> None:
    blocks = [
        {
            "block_type": 6,
            "heading4": {
                "elements": [
                    {"text_run": {"content": "1. 需求挖掘"}},
                ]
            },
            "block_id": "h1",
        },
        {
            "block_type": 12,
            "bullet": {
                "elements": [
                    {"text_run": {"content": "从自身痛点出发"}},
                ]
            },
            "block_id": "b1",
        },
        {
            "block_type": 27,
            "block_id": "img1",
            "image": {"token": "abc", "width": 10, "height": 20},
            "file_url": "https://example.com/a.jpg",
        },
    ]

    assets = scys_course.collect_assets(blocks)
    markdown = scys_course.markdown_for_blocks(blocks, {"img1": {"token": "abc", "local_path": "assets/001_abc.jpg"}})

    assert assets[0]["kind"] == "image"
    assert assets[0]["token"] == "abc"
    assert "## 1. 需求挖掘" in markdown
    assert "- 从自身痛点出发" in markdown
    assert "![abc](assets/001_abc.jpg)" in markdown
