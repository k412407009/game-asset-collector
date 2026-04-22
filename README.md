# game-asset-collector

共享的游戏素材采集模块。职责只保留一件事：把商店截图、视频关键帧、标签和描述采到统一结构里，供 `ppt-master` 和 `game-review` 共同消费。

## 为什么拆出来

- `personal-assistant/丁开心的游戏观察/sources/fetch_game_assets.py` 是更早的祖先版
- `ppt-master/skills/ppt-master/scripts/game_assets/fetch_game_assets.py` 是当前最强版本
- `game-review` 想做独立网站，但又不能再维护一套分叉的抓取逻辑

这次拆分后的原则是：

- 主实现只保留在 `game-asset-collector`
- `ppt-master` 和 `personal-assistant` 只留兼容 wrapper
- `game-review` 优先桥接共享模块，找不到时才回退

## 这版为什么选 `ppt-master` 当事实源

同一批 `Last Beacon: Survival` 样本实测结果：

- `personal-assistant` 旧版 App Store 会误抓 `Day R Premium: Survival RPG`
- `personal-assistant` 旧版 `--store-only --label` 会因为缺少 `gameplay/` 目录直接报错
- `personal-assistant` 旧版没有手动 `--video` 入口，只能靠自动搜索
- `personal-assistant` 旧版没有 `descriptions.json`
- `ppt-master` 版已经补了严格 App Store 选择、手动视频入口、`descriptions.json`、竖屏误判修正、项目化输出和资源清单

所以共享模块直接以 `ppt-master` 当前实现为基线，而不是再从 `personal-assistant` 重做一遍。

## 目录

```text
game-asset-collector/
  README.md
  AGENTS.md
  pyproject.toml
  game_asset_collector/
    __init__.py
    fetch_game_assets.py
  scripts/
    fetch_game_assets.py
  docs/
    product/采集器概览_COLLECTOR_OVERVIEW.md
    architecture/当前架构_CURRENT_STATE.md
  tests/
    test_smoke.py
```

## 快速用法

```bash
cd /Users/ahs/Desktop/Git/game-asset-collector
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

```bash
# 只抓 Google Play
python scripts/fetch_game_assets.py "Last Beacon: Survival" \
  --out /tmp/game-assets \
  --store-only \
  --gplay-id com.hnhs.endlesssea.gp \
  --label
```

```bash
# 手动指定视频，跳过自动搜索
python scripts/fetch_game_assets.py "Last Beacon: Survival" \
  --out /tmp/game-assets \
  --gameplay-only \
  --video 2l4DO5Z10jo \
  --label
```

## 输出约定

```text
<out>/<game>/
  store/
    appstore/
    googleplay/
    steam/
  gameplay/
    frames/<video_slug>/
    labels.json
    descriptions.json
  metadata.json
  meta/<game>.image_resource_list.md
```

如果是 `--project <project_root>`，则落到 `<project_root>/images/_game_assets/<game>/...`，兼容 `ppt-master` 现有项目结构。
