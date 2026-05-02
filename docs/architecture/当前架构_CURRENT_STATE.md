# 当前架构_CURRENT_STATE

## 单一事实源

```text
game-asset-collector/
  game_asset_collector/fetch_game_assets.py   <- canonical implementation
  game_asset_collector/reference_pack.py      <- reference pack indexer
  game_asset_collector/scys_course.py         <- authenticated SCYS page capture
```

## 消费方式

```text
game-ppt-master wrapper
  -> import shared collector

personal-assistant wrapper
  -> import shared collector

game-review bridge
  -> subprocess shared collector
  -> fallback to game-ppt-master / ppt-master wrapper
  -> fallback to internal lightweight collector

global skill: game-asset-collector / 游戏采集器
  -> read ~/.agents/skills/game-asset-collector/SKILL.md
  -> subprocess /Users/ahs/Desktop/Git/game-asset-collector/scripts/fetch_game_assets.py

local utility CLI
  -> game-asset-reference-pack / scripts/build_reference_pack.py
  -> scys-course-capture / scripts/fetch_scys_course.py
```

## 兼容策略

- wrapper 会保留各自原来的默认输出目录
- shared collector 本身支持 `--project` 和 `--out`
- `.env` 会按这个顺序探测：
  1. `GAME_ASSET_COLLECTOR_ENV`
  2. `game-asset-collector/.env`
  3. `../game-ppt-master/.env`
  4. `../ppt-master/.env`
  5. `../game-review/.env`
  6. `../personal-assistant/.baoyu-skills/.env`

- 但推荐顺序不是“随便哪份都行”，而是：
  1. 独立使用时，优先在 `game-asset-collector/.env` 自己放一份
  2. 三仓联动时，可以临时复用同级仓的 `.env`
  3. 对外给同事时，最好统一让大家从 `.env.example` 复制生成本仓 `.env`

## 数据边界

- `game_assets_library/by_game/`：单游戏长期素材
- `game_assets_library/reference_packs/`：多游戏研究包，可用 `reference_pack.py`
  生成索引和软链接视图
- `collected_sources/scys/`：SCYS 课程章节临时采集结果，包含课程原文和签名资源
  URL，必须保持 Git 忽略，不能混入游戏素材库

## 当前已确认的优势

- App Store 搜索会拒绝模糊误命中
- 支持手动 `--video`，可接 YouTube URL / 11 位 ID、Bilibili URL / BV 号
- 没有手动视频时，自动搜索先抓 YouTube，未达到 `--max-videos` 时再用
  Bilibili 搜索补位
- 会写 `descriptions.json`
- 修掉了竖屏手游被误判成 `ui-menu` 的启发式错误
- `--store-only --label` 不会因目录缺失而崩
- 默认会自动判别 `walkthrough` vs `trailer`
- `walkthrough` 默认走时间轴 `analysis` 抽帧
- `trailer` 默认走 `scene` 抽帧
- `analysis` 输出可直接被 `game-review` 复用：
  - `gameplay/frame_index.json`
  - `gameplay/timeline_summary.md`
