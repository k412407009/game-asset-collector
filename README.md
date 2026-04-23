# game-asset-collector

共享的游戏素材采集模块。职责只保留一件事：把商店截图、视频关键帧、标签和描述采到统一结构里，供 `game-ppt-master` 和 `game-review` 共同消费。

## 在三仓架构里的位置

推荐和下面两个仓库放在同级目录：

- `game-ppt-master`
- `game-asset-collector`
- `game-review`

其中：

- `game-ppt-master` 负责主工作流和最终 PPT
- `game-asset-collector` 负责素材抓取与视觉证据
- `game-review` 负责结构化评审报告

总入口说明见：

- `../game-ppt-master/docs/三仓协同架构_THREE_REPO_STACK.md`
- 兼容旧本地目录：`../ppt-master/docs/三仓协同架构_THREE_REPO_STACK.md`

公开仓库地址：

- [https://github.com/k412407009/game-asset-collector](https://github.com/k412407009/game-asset-collector)

## 为什么拆出来

- `personal-assistant/丁开心的游戏观察/sources/fetch_game_assets.py` 是更早的祖先版
- `game-ppt-master/skills/ppt-master/scripts/game_assets/fetch_game_assets.py` 是当前最强版本
- `game-review` 想做独立网站，但又不能再维护一套分叉的抓取逻辑

这次拆分后的原则是：

- 主实现只保留在 `game-asset-collector`
- `game-ppt-master` 和 `personal-assistant` 只留兼容 wrapper
- `game-review` 优先桥接共享模块，找不到时才回退

## 这版为什么选 `game-ppt-master` 当事实源

同一批 `Last Beacon: Survival` 样本实测结果：

- `personal-assistant` 旧版 App Store 会误抓 `Day R Premium: Survival RPG`
- `personal-assistant` 旧版 `--store-only --label` 会因为缺少 `gameplay/` 目录直接报错
- `personal-assistant` 旧版没有手动 `--video` 入口，只能靠自动搜索
- `personal-assistant` 旧版没有 `descriptions.json`
- `game-ppt-master` 版已经补了严格 App Store 选择、手动视频入口、`descriptions.json`、竖屏误判修正、项目化输出和资源清单

所以共享模块直接以 `game-ppt-master` 当前实现为基线，而不是再从 `personal-assistant` 重做一遍。

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
git clone https://github.com/k412407009/game-asset-collector.git
cd /Users/ahs/Desktop/Git/game-asset-collector
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

先体检一遍环境：

```bash
python scripts/fetch_game_assets.py --doctor
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

`--doctor` 会检查：

- Python 版本
- `.env` 是否被发现
- `TAVILY_API_KEY` / `ARK_API_KEY`
- `yt-dlp` / `ffmpeg`
- `google_play_scraper`

补充说明：

- 现在兼容历史写法 `Tavily_API_Key` / `Tavily_API_KEY`
- 但对外仍然建议统一写成 `TAVILY_API_KEY`，避免团队里有人误判成“没配”

跑完采集后，还会自动生成一份“结果单”，告诉你：

- 抓到了哪些商店图
- 抓到了几个视频和多少张关键帧
- 标签和中文描述有没有生成
- 还缺什么
- 下一步建议做什么

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
  meta/<game>.collection_summary.md
```

如果是 `--project <project_root>`，则落到 `<project_root>/images/_game_assets/<game>/...`，兼容 `game-ppt-master` 现有项目结构。
