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
    reference_pack.py
    scys_course.py
  scripts/
    fetch_game_assets.py
    build_reference_pack.py
    fetch_scys_course.py
  docs/
    product/采集器概览_COLLECTOR_OVERVIEW.md
    product/生财有术课程采集_SCYS_COURSE_CAPTURE.md
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
cp .env.example .env
```

安装后会提供三个命令：

- `game-asset-collector`：商店截图、视频抽帧、标签和描述采集
- `game-asset-reference-pack`：给多游戏研究包生成业务分类索引和软链接目录
- `scys-course-capture`：采集当前 Chrome 登录账号有权限访问的生财有术
  `SCYS` 课程章节

推荐做法：

- 如果你把它当成独立仓给同事用，**就在这个仓库根目录自己建一份 `.env`**
- 最简单就是：`cp .env.example .env`，然后把 key 填进去
- 不建议靠“记得去别的仓库 copy 一份”这种人工流程
- 现在代码虽然会回退读取同级 `game-ppt-master/.env`、`game-review/.env`，但那是兼容逻辑，不是首选

这个采集器最少建议填两项：

```env
TAVILY_API_KEY=...
ARK_API_KEY=...
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

`--video` 可以重复传入，支持：

- YouTube 11 位 ID，例如 `2l4DO5Z10jo`
- YouTube 完整 URL，例如 `https://www.youtube.com/watch?v=CWABlP9RhCA`
- Bilibili BV 号，例如 `BV1xx411c7mD`
- Bilibili 完整 URL，例如 `https://www.bilibili.com/video/BV1xx411c7mD/`

```bash
# 手动指定 Bilibili 视频并按时间轴抽帧
python scripts/fetch_game_assets.py "PlanCoach" \
  --out /tmp/game-assets \
  --gameplay-only \
  --video "https://www.bilibili.com/video/BV1n4Ytz5EnZ/" \
  --video "BV14hYBzHErr" \
  --analysis \
  --analysis-interval 5 \
  --label
```

```bash
# 默认自动判别视频类型：
# walkthrough / gameplay / guide -> analysis
# trailer / teaser / preview -> scene
python scripts/fetch_game_assets.py "Narco Empire" \
  --out /tmp/game-assets \
  --gameplay-only \
  --video KNPTUL9X9Zs \
  --label
```

```bash
# 如需强制覆盖自动判别
python scripts/fetch_game_assets.py "Narcos: Cartel Wars" \
  --out /tmp/game-assets \
  --gameplay-only \
  --video dE0kZ2SsPTA \
  --scene
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
- 手动视频入口优先走 `--video`，自动搜索则先用 YouTube `ytsearch`，未抓满
  `--max-videos` 时再用 Bilibili `bilisearch` 补位。

## 跨项目调用

这台机器已经注册了全局 Codex skill：`game-asset-collector`（显示名：
“游戏采集器”）。在其他计划、评审项目或 PPT 项目里，可以直接说：

- `用游戏采集器抓这个游戏素材`
- `用采集器抓 B 站视频抽帧`
- `跑 fetch_game_assets 生成 labels 和 image_resource_list`

触发后应读取 `~/.agents/skills/game-asset-collector/SKILL.md`，再调用本仓库的
`scripts/fetch_game_assets.py`。本仓仍然是抓取、抽帧、标注行为的事实源。

## SCYS 课程页面采集

`SCYS` 是“生财有术”的项目内缩写。`scys_course` 是独立的页面采集辅助能力，
用于用户已经登录并有权限访问的 `scys.com` 生财有术课程章节。项目内统一写作
`SCYS`，如果口头写成 `SYCS`，指的也是这个生财有术 `scys.com` 入口。

先在 Chrome 打开课程章节正文，再运行：

```bash
python scripts/fetch_scys_course.py "https://scys.com/course/detail/148?chapterId=9614"
```

输出默认落在 `collected_sources/scys/`，其中会有接口包装、原始 JSON、章节
Markdown / 纯文本、资源清单和已下载图片。这个目录已加入 `.gitignore`，不要
把课程原文或签名资源 URL 提交到 Git。详见
`docs/product/生财有术课程采集_SCYS_COURSE_CAPTURE.md`。

## 视频抽帧模式

默认不要求调用方提前声明视频类型。采集器会先看：

- 视频标题关键词
- 视频时长
- 前几帧是否明显是竖屏 UI / walkthrough
- 前几帧是否呈现高切换率的 trailer 节奏

然后自动决定抽帧策略：

- `walkthrough` / `gameplay` / `guide` / `part 1` / `实机` / `流程`
  默认走 `analysis`
- `trailer` / `teaser` / `preview` / `official trailer` / `预告` / `宣传`
  默认走 `scene`

`analysis` 模式的时间间隔：

- 前 10 分钟：每 `4` 秒 1 帧
- 10 到 30 分钟：每 `8` 秒 1 帧
- 30 分钟后：每 `15` 秒 1 帧

如果你要强制按固定密度抽 walkthrough 时间轴，可以叠加：

```bash
python scripts/fetch_game_assets.py "Fight Tycoon" \
  --project /abs/path/to/project \
  --gameplay-only \
  --video https://www.youtube.com/watch?v=CWABlP9RhCA \
  --analysis \
  --analysis-interval 2 \
  --label
```

这会对 analysis 模式改成全程 `2` 秒 1 帧，适合做逐镜头流程复盘。

`scene` 模式：

- 用 `ffmpeg select='gt(scene,threshold)'` 按场景切换抽帧
- 更适合预告片、广告片、PV

两个强制开关仍然保留：

- `--analysis`：无视自动判别，强制走 walkthrough 分析模式
- `--scene`：无视自动判别，强制走 trailer 场景模式

自动判别结果会落到：

- `metadata.json` 的 `gameplay.videos[*]`
- `meta/<game>.collection_summary.md`
- 如果走 `analysis`，还会额外生成 `gameplay/frame_index.json` 和 `gameplay/timeline_summary.md`

跑完采集后，还会自动生成一份“结果单”，告诉你：

- 抓到了哪些商店图
- 抓到了几个视频和多少张关键帧
- 标签和中文描述有没有生成
- 还缺什么
- 下一步建议做什么

## 输出约定

每个游戏的素材目录约定：

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
    frame_index.json
    timeline_summary.md
  metadata.json
  meta/<game>.image_resource_list.md
  meta/<game>.collection_summary.md
```

如果是 `--project <project_root>`，则落到 `<project_root>/images/_game_assets/<game>/...`，兼容 `game-ppt-master` 现有项目结构。

## Asset Library 目录约定（2026-04-25 起）

`game_assets_library/` 是这个仓库的"长期素材仓库"，**所有游戏资产**最终都应该落在这里。
约定分两类：

```text
game_assets_library/
  by_game/                          ← 推荐：单游戏权威素材
    Last-Outbreak/
      store/  gameplay/  metadata.json
      _repro/                       ← 同游戏的历史复现快照（可清理）
        gameplay_only/
        ppt_master/
        store_only/
    Last-Beacon-Survival/
  reference_packs/                  ← 某次研究/对标的多游戏包（带日期）
    2026-04-23_crime_reference_pack_auto/
    2026-04-23_narco_reference_pack/
    2026-04-24_crazy_plants_reference_pack/
```

什么时候用哪个：

- **单游戏长期素材** → `by_game/<GameName>/`
  ```bash
  python scripts/fetch_game_assets.py "Last Outbreak" \
    --out ./game_assets_library/by_game \
    --gameplay-only --video <id>
  ```
- **多游戏研究包** → `reference_packs/<日期>_<主题>_reference_pack/`
  ```bash
  python scripts/fetch_game_assets.py "Crazy Plants TD" \
    --out ./game_assets_library/reference_packs/2026-04-25_td_reference_pack \
    --gameplay-only --video <id>
  ```
  采完多个游戏后，可以为这个研究包生成业务分类索引：
  ```bash
  python scripts/build_reference_pack.py ./game_assets_library/reference_packs/2026-04-25_td_reference_pack
  ```
  生成内容包括 `index/reference_pack_index.json`、
  `index/资产索引_REFERENCE_INDEX.md` 和 `packaged/` 下的分类软链接目录。

评审产出（`.docx` / `.xlsx` / `review.json`）**不放这里**，归 `game-review/projects/`。
评审项目要看图时，由 `game-review/projects/<Game>/raw_assets/<game_slug>` 软链接回 `by_game/<Game>/`。
