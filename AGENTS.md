# AGENTS

## Goal

这个仓库只维护一套共享的游戏素材采集逻辑，避免 `ppt-master`、`game-review`、`personal-assistant` 三边继续分叉。

## First Read

开始修改前先看：

1. `README.md`
2. `docs/product/采集器概览_COLLECTOR_OVERVIEW.md`
3. `docs/architecture/当前架构_CURRENT_STATE.md`
4. `game_asset_collector/fetch_game_assets.py`

## Working Rules

- 优先保证三个消费者看到的是同一套抓取 / 抽帧 / 标注行为。
- 如果要改默认输出结构，先确认 `ppt-master` wrapper 和 `game-review` bridge 都还能兼容。
- 涉及 App Store / Google Play / 视频搜索规则的改动，至少补一条 smoke 验证。
- 不在这里做评审打分、PPT 生成或网站页面，只做素材采集。

## Asset Library 目录约定（2026-04-25 重组后）

`game_assets_library/` 下统一只放两类东西：

```
game_assets_library/
  by_game/                          ← 单游戏权威素材（推荐）
    Last-Outbreak/
      store/  gameplay/  metadata.json  meta/
      _repro/                       ← 同游戏的历史复现快照（可清理）
    Last-Beacon-Survival/
  reference_packs/                  ← 多游戏研究包（带日期）
    2026-04-23_crime_reference_pack_auto/
    2026-04-24_crazy_plants_reference_pack/
```

新增采集结果时的硬规则：

- **单游戏长期素材** → `by_game/<GameName>/`，目录名跟 `metadata.json` 里的 `game_name` 对齐
- **某次研究/对标的多游戏包** → `reference_packs/<YYYY-MM-DD>_<topic>_reference_pack/`
- 评审产出（`.docx`/`.xlsx`/`.json`/`.md`）**不要**塞进来，那些归 `game-review/projects/`
- 评审项目要看素材时，由 `game-review/projects/<Game>/raw_assets/<game_slug>` **软链接**回这里，不复制

## 与 game-review 的协作

- `game-review` 项目目录 `projects/<Game>/raw_assets/<game_slug>` 是软链接，指向本仓库 `by_game/<Game>/`
- 这个软链接保证 `game-review review --with-visuals` 不需要改源代码就能找到图
- 如果你在本仓库重命名 `by_game/<Game>/`，必须同步去 `game-review/projects/<Game>/raw_assets/` 修软链接

