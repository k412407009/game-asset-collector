# 采集器概览_COLLECTOR_OVERVIEW

## 目标

把“商店截图抓取 + 视频下载抽帧 + 画面标签/描述生成”抽成一个独立模块，给：

- `game-ppt-master` 的 Skill 流
- `game-review` 的网站 / API 流
- `personal-assistant` 的历史脚本入口

提供同一份素材层能力。

仓库内还有两个辅助能力：

- `reference_pack`：为多游戏研究包生成 `index/` 索引和 `packaged/` 分类软链接
- `scys_course`：采集当前 Chrome 登录账号已有权限访问的 `scys.com` 生财有术
  `SCYS` 课程章节，输出到 `collected_sources/scys/`

## 输入

- 游戏名
- 可选的商店 ID：`--appstore-id` / `--gplay-id` / `--steam-id`
- 可选的手动视频入口：`--video <URL_OR_ID>`，可重复传入，支持 YouTube
  完整 URL / 11 位视频 ID，以及 Bilibili 完整 URL / BV 号
- 可选的项目根目录：`--project`
- 可选的原始输出目录：`--out`

## 输出

- 商店截图
- 视频关键帧
- `labels.json`
- `descriptions.json`
- `frame_index.json`（analysis 模式）
- `timeline_summary.md`（analysis 模式）
- `metadata.json`
- `image_resource_list.md`
- 多游戏研究包可额外生成 `reference_pack_index.json`、
  `资产索引_REFERENCE_INDEX.md` 和按业务分类组织的软链接目录

## 视频模式判别

默认情况下，调用方不需要预先告诉系统这是 walkthrough 还是 trailer。

如果传了 `--video`，采集器会跳过自动搜索并直接下载这些目标；如果没有手动
视频，自动搜索会先走 YouTube `ytsearch`，未达到 `--max-videos` 时再用
Bilibili `bilisearch` 补位。

采集器会结合：

- 视频标题关键词
- 视频时长
- 前几帧的 UI / 竖屏特征
- 前几帧的切换密度

自动选择：

- walkthrough / gameplay / guide -> `analysis`
- trailer / teaser / preview -> `scene`

`analysis` 的目标不是只挑“漂亮帧”，而是保留 onboarding / 经营 / 战斗 / UI / 地图 / 养成这些可供评审复盘的时间轴样本。

## 为什么不是直接放在 `game-review`

因为 `game-review` 只是消费素材，不应该成为素材逻辑的事实源。它未来可以单独部署成网站，但网站链路和 Skill 链路仍必须共用同一套采集规则。

## SCYS 页面采集边界

`SCYS` 是“生财有术”的项目内缩写。SCYS 课程采集不属于游戏素材库，不写入
`game_assets_library/`。它只在用户已经打开并可阅读对应课程章节时，通过同源页面
请求课程接口；不导出 cookies / token，输出目录 `collected_sources/scys/` 必须保持
Git 忽略。
