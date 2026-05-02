from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any


CATEGORY_META: dict[str, dict[str, str]] = {
    "combat": {
        "title_zh": "战斗场景",
        "title_en": "COMBAT",
        "usage_hint": "适合冲突、夺地、枪战、强对抗页。",
    },
    "management_progress": {
        "title_zh": "经营养成",
        "title_en": "MANAGEMENT_PROGRESS",
        "usage_hint": "适合主城经营、升级扩张、玩法循环页。",
    },
    "character_story": {
        "title_zh": "角色情绪",
        "title_en": "CHARACTER_STORY",
        "usage_hint": "适合封面、角色情绪、剧情桥段页。",
    },
    "ui_economy": {
        "title_zh": "界面系统",
        "title_en": "UI_ECONOMY",
        "usage_hint": "适合系统 UI、商店、数值结构页。",
    },
    "store_promo": {
        "title_zh": "商店宣传",
        "title_en": "STORE_PROMO",
        "usage_hint": "适合对标页、官方卖点拆解页。",
    },
    "trailer_beats": {
        "title_zh": "宣传镜头",
        "title_en": "TRAILER_BEATS",
        "usage_hint": "适合章节过渡、情绪页、宣发节奏页。",
    },
    "other_misc": {
        "title_zh": "其他补充",
        "title_en": "OTHER_MISC",
        "usage_hint": "适合作为备用图，不建议优先上主视觉。",
    },
}

LABEL_TO_CATEGORY: dict[str, str] = {
    "battle": "combat",
    "main-city": "management_progress",
    "map-world": "management_progress",
    "tutorial": "management_progress",
    "loading": "management_progress",
    "character": "character_story",
    "cutscene": "character_story",
    "ui-menu": "ui_economy",
    "shop-gacha": "ui_economy",
    "store-screenshot": "store_promo",
    "ad-creative": "trailer_beats",
    "other": "other_misc",
}


def classify_business_category(label: str) -> str:
    return LABEL_TO_CATEGORY.get(label, "other_misc")


def category_display_name(category: str) -> str:
    meta = CATEGORY_META[category]
    return f"{meta['title_zh']}_{meta['title_en']}"


def infer_video_url(identifier: str) -> str | None:
    candidate = identifier.strip()
    if not candidate:
        return None
    candidate = candidate.removesuffix(".mp4")
    if "__" in candidate:
        candidate = candidate.rsplit("__", 1)[-1]
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate):
        return f"https://www.youtube.com/watch?v={candidate}"
    if re.fullmatch(r"BV[0-9A-Za-z]+", candidate):
        return f"https://www.bilibili.com/video/{candidate}"
    return None


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _infer_store_url(store_source: str, store_meta: dict[str, Any]) -> str | None:
    if store_source == "googleplay":
        app_id = str(store_meta.get("appId") or "").strip()
        if app_id:
            return f"https://play.google.com/store/apps/details?id={app_id}"
    if store_source == "appstore":
        track_id = str(store_meta.get("trackId") or store_meta.get("id") or "").strip()
        if track_id.isdigit():
            return f"https://apps.apple.com/app/id{track_id}"
    return None


def _infer_source_url(rel_path: str, metadata: dict[str, Any]) -> str | None:
    parts = Path(rel_path).parts
    if len(parts) >= 3 and parts[0] == "store":
        store_source = parts[1]
        store_meta = (metadata.get("stores") or {}).get(store_source) or {}
        return _infer_store_url(store_source, store_meta)
    if len(parts) >= 4 and parts[0] == "gameplay" and parts[1] == "frames":
        return infer_video_url(parts[2])
    if len(parts) >= 3 and parts[0] == "gameplay" and parts[1] == "videos":
        return infer_video_url(parts[2])
    return None


def _source_type(rel_path: str) -> str:
    parts = Path(rel_path).parts
    if len(parts) >= 2 and parts[0] == "store":
        return "store"
    if len(parts) >= 3 and parts[0] == "gameplay" and parts[1] == "frames":
        return "gameplay-frame"
    if len(parts) >= 3 and parts[0] == "gameplay" and parts[1] == "videos":
        return "video"
    return "other"


def _sequence_number(name: str) -> int | None:
    match = re.search(r"_(\d+)\.[A-Za-z0-9]+$", name)
    if match:
        return int(match.group(1))
    return None


def _build_secondary_tags(label: str, category: str, source_type: str) -> list[str]:
    tags: list[str] = []
    if source_type == "store":
        tags.append("official-store")
    if source_type == "gameplay-frame":
        tags.append("video-frame")
    if label in {"cutscene", "ad-creative"}:
        tags.append("transition-safe")
    if category == "management_progress":
        tags.append("养成候选")
    if category == "combat":
        tags.append("战斗候选")
    return tags


def _score_entry(entry: dict[str, Any]) -> int:
    score = 0
    category = entry["business_category"]
    label = entry["collector_label"]
    source_type = entry["source_type"]
    filename = entry["filename"]

    if source_type == "gameplay-frame":
        score += 20
    if source_type == "store":
        score += 10

    if category in {"combat", "character_story", "trailer_beats"} and source_type == "gameplay-frame":
        score += 30
    if category in {"store_promo", "ui_economy"} and source_type == "store":
        score += 30

    if label == "battle":
        score += 40
    elif label == "ad-creative":
        score += 35
    elif label == "character":
        score += 30
    elif label == "cutscene":
        score += 25
    elif label == "main-city":
        score += 20
    elif label == "shop-gacha":
        score += 20

    seq = _sequence_number(filename)
    if seq is not None:
        score += max(0, 12 - seq)
    return score


def _ensure_link_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _link_name(entry: dict[str, Any]) -> str:
    return f"{entry['game_slug']}__{entry['collector_label']}__{entry['filename']}"


def _safe_symlink(target: Path, link_path: Path) -> None:
    if link_path.exists() or link_path.is_symlink():
        if link_path.is_symlink():
            current = os.readlink(link_path)
            if current == os.path.relpath(target, start=link_path.parent):
                return
        link_path.unlink()
    rel_target = os.path.relpath(target, start=link_path.parent)
    link_path.symlink_to(rel_target)


def build_reference_catalog(pack_root: Path) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    games_summary: list[dict[str, Any]] = []

    for game_dir in sorted(p for p in pack_root.iterdir() if p.is_dir()):
        metadata_path = game_dir / "metadata.json"
        labels_path = game_dir / "gameplay" / "labels.json"
        descriptions_path = game_dir / "gameplay" / "descriptions.json"
        if not metadata_path.exists() or not labels_path.exists():
            continue

        metadata = _read_json(metadata_path)
        labels = _read_json(labels_path)
        descriptions = _read_json(descriptions_path)
        stores = metadata.get("stores") or {}
        gameplay = metadata.get("gameplay") or {}

        game_slug = game_dir.name
        game_title = metadata.get("game_name") or game_slug.replace("-", " ")
        source_urls: list[dict[str, str]] = []

        for store_source, store_meta in stores.items():
            if not store_meta:
                continue
            url = _infer_store_url(store_source, store_meta)
            if url:
                source_urls.append({"kind": store_source, "url": url})

        for video_meta in gameplay.get("videos") or []:
            filename = str(video_meta.get("filename") or "")
            video_url = infer_video_url(filename)
            if video_url:
                source_urls.append({"kind": "video", "url": video_url})

        games_summary.append(
            {
                "game_slug": game_slug,
                "game_title": game_title,
                "stores": sorted(k for k, v in stores.items() if v),
                "source_urls": source_urls,
                "video_count": len(gameplay.get("videos") or []),
                "frame_count": int(gameplay.get("total_frames") or 0),
                "label_distribution": metadata.get("labels", {}).get("distribution") or {},
            }
        )

        for rel_path, label in labels.items():
            abs_path = game_dir / rel_path
            if not abs_path.exists():
                continue
            business_category = classify_business_category(label)
            entry = {
                "game_slug": game_slug,
                "game_title": game_title,
                "collector_label": label,
                "business_category": business_category,
                "business_category_display": category_display_name(business_category),
                "description": descriptions.get(rel_path, ""),
                "rel_path": f"{game_slug}/{rel_path}",
                "abs_path": str(abs_path.resolve()),
                "filename": abs_path.name,
                "source_type": _source_type(rel_path),
                "source_url": _infer_source_url(rel_path, metadata),
                "secondary_tags": _build_secondary_tags(label, business_category, _source_type(rel_path)),
            }
            entry["priority_score"] = _score_entry(entry)
            entries.append(entry)

    by_category: dict[str, list[dict[str, Any]]] = {}
    for category in CATEGORY_META:
        category_entries = [e for e in entries if e["business_category"] == category]
        category_entries.sort(key=lambda item: (-item["priority_score"], item["game_slug"], item["filename"]))
        by_category[category] = category_entries

    by_game: dict[str, dict[str, Any]] = {}
    for game in games_summary:
        game_entries = [e for e in entries if e["game_slug"] == game["game_slug"]]
        counts = Counter(e["business_category"] for e in game_entries)
        game["business_counts"] = {key: counts[key] for key in sorted(counts)}
        by_game[game["game_slug"]] = game

    catalog = {
        "pack_root": str(pack_root.resolve()),
        "stats": {
            "games": len(games_summary),
            "entries": len(entries),
            "videos": sum(game["video_count"] for game in games_summary),
            "frames": sum(game["frame_count"] for game in games_summary),
        },
        "games": games_summary,
        "entries": entries,
        "by_category": by_category,
        "by_game": by_game,
    }
    return catalog


def _render_markdown(catalog: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# 资产索引_REFERENCE_INDEX")
    lines.append("")
    stats = catalog["stats"]
    lines.append(
        f"总计 {stats['games']} 款游戏，{stats['entries']} 条已打标素材，"
        f"{stats['videos']} 个本地视频，{stats['frames']} 张关键帧。"
    )
    lines.append("")
    lines.append("## 来源总览")
    lines.append("")
    for game in catalog["games"]:
        lines.append(f"### {game['game_title']}")
        lines.append(f"- 游戏目录：`{game['game_slug']}`")
        lines.append(f"- 商店来源：{', '.join(game['stores']) or '-'}")
        lines.append(f"- 视频数：{game['video_count']} | 关键帧数：{game['frame_count']}")
        counts = game.get("business_counts") or {}
        if counts:
            rendered = ", ".join(
                f"{CATEGORY_META[key]['title_zh']} {value}"
                for key, value in counts.items()
            )
            lines.append(f"- 业务分类：{rendered}")
        for source in game["source_urls"]:
            lines.append(f"- 源链接（{source['kind']}）：{source['url']}")
        lines.append("")

    lines.append("## 业务分类")
    lines.append("")
    for category, meta in CATEGORY_META.items():
        category_entries = catalog["by_category"].get(category) or []
        if not category_entries:
            continue
        lines.append(f"### {meta['title_zh']} · {meta['title_en']}")
        lines.append(f"- 用途：{meta['usage_hint']}")
        lines.append(f"- 素材数：{len(category_entries)}")
        preview = category_entries[:6]
        for entry in preview:
            extra = f" | {entry['description']}" if entry["description"] else ""
            source = f" | {entry['source_url']}" if entry["source_url"] else ""
            lines.append(
                f"- `{entry['rel_path']}` | {entry['game_title']} | "
                f"{entry['collector_label']}{extra}{source}"
            )
        if len(category_entries) > len(preview):
            lines.append(f"- 其余 {len(category_entries) - len(preview)} 条见 `packaged/by_category/{category}/`")
        lines.append("")

    lines.append("## 目录说明")
    lines.append("")
    lines.append("- `packaged/by_category/<category>/`：按主业务分类聚合的素材软链接。")
    lines.append("- `packaged/top_picks/<category>/`：每类按优先级挑出的精选软链接。")
    lines.append("- `packaged/by_game/<game>/<category>/`：同一游戏下再按分类细分。")
    lines.append("- `packaged/videos/<game>/`：下载到本地的原始视频软链接。")
    lines.append("")
    return "\n".join(lines)


def write_reference_pack(pack_root: Path) -> dict[str, Path]:
    catalog = build_reference_catalog(pack_root)
    index_dir = pack_root / "index"
    packaged_dir = pack_root / "packaged"
    by_category_dir = packaged_dir / "by_category"
    by_game_dir = packaged_dir / "by_game"
    top_picks_dir = packaged_dir / "top_picks"
    videos_dir = packaged_dir / "videos"

    for path in (index_dir, by_category_dir, by_game_dir, top_picks_dir, videos_dir):
        path.mkdir(parents=True, exist_ok=True)

    json_path = index_dir / "reference_pack_index.json"
    json_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")

    md_path = index_dir / "资产索引_REFERENCE_INDEX.md"
    md_path.write_text(_render_markdown(catalog), encoding="utf-8")

    for category, entries in catalog["by_category"].items():
        category_dir = by_category_dir / category
        top_dir = top_picks_dir / category
        _ensure_link_dir(category_dir)
        _ensure_link_dir(top_dir)
        for entry in entries:
            target = Path(entry["abs_path"])
            _safe_symlink(target, category_dir / _link_name(entry))
        for entry in entries[:6]:
            target = Path(entry["abs_path"])
            _safe_symlink(target, top_dir / _link_name(entry))

    for game_slug, game_summary in catalog["by_game"].items():
        game_dir = pack_root / game_slug
        labels = _read_json(game_dir / "gameplay" / "labels.json")
        for rel_path, label in labels.items():
            target = game_dir / rel_path
            if not target.exists():
                continue
            category = classify_business_category(label)
            link_dir = by_game_dir / game_slug / category
            _ensure_link_dir(link_dir)
            _safe_symlink(target, link_dir / target.name)

        gameplay_meta = _read_json(game_dir / "metadata.json").get("gameplay") or {}
        if gameplay_meta.get("videos"):
            game_videos_dir = videos_dir / game_slug
            _ensure_link_dir(game_videos_dir)
            for video_meta in gameplay_meta["videos"]:
                filename = str(video_meta.get("filename") or "")
                if not filename:
                    continue
                target = game_dir / "gameplay" / "videos" / filename
                if target.exists():
                    _safe_symlink(target, game_videos_dir / target.name)

    return {
        "json": json_path,
        "markdown": md_path,
        "packaged": packaged_dir,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a cross-game reference pack index.")
    parser.add_argument("pack_root", help="reference pack root produced by fetch_game_assets.py")
    args = parser.parse_args(argv)

    outputs = write_reference_pack(Path(args.pack_root).resolve())
    print(f"✓ json     → {outputs['json']}")
    print(f"✓ markdown → {outputs['markdown']}")
    print(f"✓ packaged → {outputs['packaged']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
