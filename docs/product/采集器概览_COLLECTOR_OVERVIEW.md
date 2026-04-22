# 采集器概览_COLLECTOR_OVERVIEW

## 目标

把“商店截图抓取 + 视频下载抽帧 + 画面标签/描述生成”抽成一个独立模块，给：

- `game-ppt-master` 的 Skill 流
- `game-review` 的网站 / API 流
- `personal-assistant` 的历史脚本入口

提供同一份素材层能力。

## 输入

- 游戏名
- 可选的商店 ID：`--appstore-id` / `--gplay-id` / `--steam-id`
- 可选的手动视频入口：`--video <URL_OR_ID>`
- 可选的项目根目录：`--project`
- 可选的原始输出目录：`--out`

## 输出

- 商店截图
- 视频关键帧
- `labels.json`
- `descriptions.json`
- `metadata.json`
- `image_resource_list.md`

## 为什么不是直接放在 `game-review`

因为 `game-review` 只是消费素材，不应该成为素材逻辑的事实源。它未来可以单独部署成网站，但网站链路和 Skill 链路仍必须共用同一套采集规则。
