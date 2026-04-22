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
