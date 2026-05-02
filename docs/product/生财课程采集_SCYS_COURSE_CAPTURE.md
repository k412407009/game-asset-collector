# 生财课程采集 SCYS Course Capture

这个能力用于采集用户已经登录且有权限访问的 `scys.com` 课程章节。它通过当前
Chrome 标签页在同源页面内调用课程接口，不导出浏览器 cookies 或 token；token
只在页面 JavaScript 内用于请求同一个 `scys.com` 接口。

项目内统一使用 `SCYS` 命名；如果口头写成 `SYCS`，指的也是这个 `scys.com`
课程页面采集入口。

默认输出在仓库根目录的 `collected_sources/scys/`，该目录已加入 `.gitignore`，
避免课程原文、签名图片 URL 和下载资源被误提交。

## 用法

先在 Chrome 打开并确认课程章节正文可见，然后运行：

```bash
cd /Users/ahs/Desktop/Git/game-asset-collector
python scripts/fetch_scys_course.py "https://scys.com/course/detail/148?chapterId=9614"
```

如果已经 `pip install -e .`，也可以使用安装入口：

```bash
scys-course-capture "https://scys.com/course/detail/148?chapterId=9614"
```

也可以直接采集当前 Chrome 活跃标签页：

```bash
python scripts/fetch_scys_course.py
```

输出内容包括：

- `browser_fetch_wrapper.json`：浏览器内同源接口返回包装
- `raw_response.json`：接口原始 JSON
- `chapter.json`：章节对象
- `chapter.md`：渲染后的 Markdown
- `chapter.txt`：纯文本
- `assets_manifest.json`：图片 / 媒体资源清单
- `assets/`：已下载图片文件
- `summary.md`：标题、块数、资源数量和小节目录

如果只想复用已有包装文件重新解析，不重新请求网站：

```bash
python scripts/fetch_scys_course.py \
  --from-wrapper collected_sources/scys/course-148/chapter-9614/browser_fetch_wrapper.json
```

## 边界

- 只采集当前账号已有权限访问的内容。
- 不绕过登录、会员、付费或浏览器安全限制。
- 不读取或导出浏览器 cookies。
- 不把采集结果放进 `game_assets_library/`，避免和游戏素材库混用。
