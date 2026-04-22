# 当前架构_CURRENT_STATE

## 单一事实源

```text
game-asset-collector/
  game_asset_collector/fetch_game_assets.py   <- canonical implementation
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
```

## 兼容策略

- wrapper 会保留各自原来的默认输出目录
- shared collector 本身支持 `--project` 和 `--out`
- `.env` 会按这个顺序探测：
  1. `GAME_ASSET_COLLECTOR_ENV`
  2. `game-asset-collector/.env`
  3. `../game-ppt-master/.env`
  4. `../ppt-master/.env`
  5. `../personal-assistant/.baoyu-skills/.env`

## 当前已确认的优势

- App Store 搜索会拒绝模糊误命中
- 支持手动 `--video`
- 会写 `descriptions.json`
- 修掉了竖屏手游被误判成 `ui-menu` 的启发式错误
- `--store-only --label` 不会因目录缺失而崩
