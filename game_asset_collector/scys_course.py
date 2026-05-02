#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Authenticated 生财有术 (SCYS) course/chapter capture helpers.

This module reads content that the user can already access in an open Chrome
tab. It does not export browser cookies or tokens. The token, when present, is
only read inside the page JavaScript and sent back to the same scys.com API.
"""

from __future__ import annotations

import argparse
import html
import json
import mimetypes
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


COLLECTOR_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_ROOT = COLLECTOR_ROOT / "collected_sources" / "scys"
SCYS_HOSTS = {"scys.com", "www.scys.com"}
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
)


@dataclass(frozen=True)
class CourseTarget:
    course_id: int
    chapter_id: int
    url: str


def parse_course_target(url: str, course_id: int | None = None, chapter_id: int | None = None) -> CourseTarget:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    if host and host not in SCYS_HOSTS:
        raise ValueError(f"expected scys.com URL, got {host}")

    path_match = re.search(r"/course/detail/(\d+)", parsed.path)
    query = urllib.parse.parse_qs(parsed.query)

    resolved_course = course_id
    if resolved_course is None and path_match:
        resolved_course = int(path_match.group(1))

    resolved_chapter = chapter_id
    if resolved_chapter is None and query.get("chapterId"):
        resolved_chapter = int(query["chapterId"][0])

    if resolved_course is None:
        raise ValueError("course_id is required or must be present in /course/detail/<id>")
    if resolved_chapter is None:
        raise ValueError("chapter_id is required or must be present as ?chapterId=<id>")

    clean_url = f"https://scys.com/course/detail/{resolved_course}?chapterId={resolved_chapter}"
    return CourseTarget(course_id=resolved_course, chapter_id=resolved_chapter, url=clean_url)


def _run_osascript(script: str) -> str:
    result = subprocess.run(
        ["osascript", "-e", script],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "osascript failed")
    return result.stdout.strip()


def get_active_chrome_tab() -> tuple[str, str]:
    script = (
        'tell application "Google Chrome" to return '
        '(URL of active tab of front window) & "\\n" & '
        '(title of active tab of front window)'
    )
    output = _run_osascript(script)
    url, _, title = output.partition("\n")
    return url.strip(), title.strip()


def _js_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _execute_chrome_js(js: str) -> str:
    script = f'tell application "Google Chrome" to execute active tab of front window javascript {_js_string(js)}'
    return _run_osascript(script)


def fetch_chapter_via_active_chrome(target: CourseTarget, timeout_sec: int = 30) -> dict[str, Any]:
    active_url, _title = get_active_chrome_tab()
    parsed = urllib.parse.urlparse(active_url)
    if parsed.hostname not in SCYS_HOSTS:
        raise RuntimeError(f"active Chrome tab is not scys.com: {active_url}")

    endpoint = f"/search/course/getChapterContent?course_id={target.course_id}&chapter_id={target.chapter_id}"
    state_key = "__codex_scys_capture_status"
    data_key = "__codex_scys_capture_payload"
    js = f"""
window[{_js_string(data_key)}] = null;
window[{_js_string(state_key)}] = "loading";
fetch({_js_string(endpoint)}, {{
  credentials: "include",
  headers: {{
    "Accept": "application/json, text/plain, */*",
    "X-TOKEN": localStorage.getItem("__user_token.v3") || ""
  }}
}})
  .then(async (response) => {{
    const text = await response.text();
    window[{_js_string(data_key)}] = JSON.stringify({{
      ok: response.ok,
      status: response.status,
      contentType: response.headers.get("content-type"),
      fetchedAt: new Date().toISOString(),
      sourceUrl: window.location.href,
      endpoint: {_js_string(endpoint)},
      text
    }});
    window[{_js_string(state_key)}] = "done";
  }})
  .catch((error) => {{
    window[{_js_string(data_key)}] = JSON.stringify({{
      ok: false,
      error: String(error),
      fetchedAt: new Date().toISOString(),
      sourceUrl: window.location.href,
      endpoint: {_js_string(endpoint)}
    }});
    window[{_js_string(state_key)}] = "error";
  }});
"started";
"""
    _execute_chrome_js(js)

    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        time.sleep(0.5)
        status = _execute_chrome_js(f"window[{_js_string(state_key)}] || ''")
        if status in {"done", "error"}:
            payload_text = _execute_chrome_js(f"window[{_js_string(data_key)}] || ''")
            if not payload_text:
                raise RuntimeError("Chrome capture finished without payload")
            payload = json.loads(payload_text)
            if not payload.get("ok"):
                raise RuntimeError(f"生财有术 SCYS capture failed: {payload}")
            return payload

    raise TimeoutError(f"生财有术 SCYS capture timed out after {timeout_sec}s")


def load_wrapper(path: Path) -> dict[str, Any]:
    wrapper = json.loads(path.read_text(encoding="utf-8"))
    if "text" not in wrapper:
        raise ValueError(f"wrapper missing text: {path}")
    return wrapper


def unwrap_response(wrapper: dict[str, Any]) -> dict[str, Any]:
    text = wrapper.get("text")
    if not isinstance(text, str):
        raise ValueError("wrapper text is not a string")
    response = json.loads(text)
    if response.get("status") not in {0, None} and "data" not in response:
        raise ValueError(f"unexpected 生财有术 SCYS response status: {response.get('status')}")
    return response


def _elements_to_text(elements: list[dict[str, Any]] | None) -> str:
    parts: list[str] = []
    for element in elements or []:
        text_run = element.get("text_run")
        if text_run:
            parts.append(str(text_run.get("content", "")))
            continue
        link = element.get("link")
        if link:
            parts.append(str(link.get("text") or link.get("url") or ""))
            continue
        mention = element.get("mention_user")
        if mention:
            parts.append(str(mention.get("name") or mention.get("user_id") or ""))
    return "".join(parts)


def block_text(block: dict[str, Any]) -> str:
    for key in (
        "text",
        "heading1",
        "heading2",
        "heading3",
        "heading4",
        "heading5",
        "heading6",
        "heading7",
        "heading8",
        "heading9",
        "bullet",
        "ordered",
        "todo",
        "quote",
    ):
        value = block.get(key)
        if isinstance(value, dict):
            text = _elements_to_text(value.get("elements"))
            if text:
                return text
    html_block = block.get("sc_html")
    if isinstance(html_block, dict) and html_block.get("content"):
        return re.sub(r"<[^>]+>", "", html.unescape(str(html_block["content"]))).strip()
    return ""


def iter_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []

    def visit(block: dict[str, Any]) -> None:
        flattened.append(block)
        for child in block.get("children_blocks") or []:
            if isinstance(child, dict):
                visit(child)

    for block in blocks:
        visit(block)
    return flattened


def collect_assets(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    assets: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, block in enumerate(iter_blocks(blocks), start=1):
        file_url = block.get("file_url")
        image = block.get("image") if isinstance(block.get("image"), dict) else {}
        token = image.get("token") or block.get("token") or ""
        if isinstance(file_url, str) and file_url and file_url not in seen:
            seen.add(file_url)
            assets.append({
                "kind": "image" if image else "file",
                "index": len(assets) + 1,
                "block_id": block.get("block_id", ""),
                "token": token,
                "url": file_url,
                "width": image.get("width"),
                "height": image.get("height"),
            })

        xiaoe = block.get("sc_xiaoe_tech")
        if isinstance(xiaoe, dict) and xiaoe.get("url") and xiaoe["url"] not in seen:
            seen.add(xiaoe["url"])
            assets.append({
                "kind": "xiaoe_tech",
                "index": len(assets) + 1,
                "block_id": block.get("block_id", ""),
                "token": "",
                "url": xiaoe["url"],
                "title": xiaoe.get("title", ""),
                "cover_image": xiaoe.get("cover_image", ""),
            })
    return assets


def _extension_from_response(url: str, headers: Any, data: bytes) -> str:
    content_type = headers.get("content-type", "").split(";")[0].strip().lower()
    guessed = mimetypes.guess_extension(content_type) if content_type else None
    if guessed:
        return ".jpg" if guessed == ".jpe" else guessed
    suffix = Path(urllib.parse.urlparse(url).path).suffix
    if suffix:
        return suffix[:12]
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"GIF8"):
        return ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    return ".bin"


def download_assets(assets: list[dict[str, Any]], out_dir: Path) -> list[dict[str, Any]]:
    assets_dir = out_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    updated: list[dict[str, Any]] = []
    for asset in assets:
        copied = dict(asset)
        url = str(asset.get("url", ""))
        if asset.get("kind") not in {"image", "file"} or not url:
            updated.append(copied)
            continue
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = response.read()
                ext = _extension_from_response(url, response.headers, data)
            token = re.sub(r"[^A-Za-z0-9_-]+", "", str(asset.get("token") or "asset"))
            filename = f"{int(asset['index']):03d}_{token[:32] or 'asset'}{ext}"
            path = assets_dir / filename
            path.write_bytes(data)
            copied["local_path"] = str(path.relative_to(out_dir))
            copied["bytes"] = len(data)
        except Exception as exc:
            copied["download_error"] = str(exc)
        updated.append(copied)
    return updated


def markdown_for_blocks(blocks: list[dict[str, Any]], asset_by_block_id: dict[str, dict[str, Any]]) -> str:
    lines: list[str] = []

    def add_text(prefix: str, text: str) -> None:
        if text.strip():
            lines.append(f"{prefix}{text.strip()}")
            lines.append("")

    def render(block: dict[str, Any], depth: int = 0) -> None:
        block_type = block.get("block_type")
        text = block_text(block)
        block_id = str(block.get("block_id", ""))

        if block_type == 27:
            asset = asset_by_block_id.get(block_id)
            label = asset.get("token") if asset else block_id
            src = asset.get("local_path") or asset.get("url") if asset else block.get("file_url", "")
            if src:
                lines.append(f"![{label}]({src})")
                lines.append("")
            return

        if block.get("heading1"):
            add_text("# ", text)
        elif block.get("heading2"):
            add_text("## ", text)
        elif block.get("heading3"):
            add_text("### ", text)
        elif block.get("heading4"):
            add_text("## ", text)
        elif block.get("heading5"):
            add_text("### ", text)
        elif block.get("heading6") or block.get("heading7") or block.get("heading8") or block.get("heading9"):
            add_text("#### ", text)
        elif block.get("bullet"):
            add_text("- ", text)
        elif block.get("ordered"):
            add_text("1. ", text)
        elif block_type == 19:
            if text:
                add_text("> ", text)
        elif text:
            add_text("", text)

        for child in block.get("children_blocks") or []:
            if isinstance(child, dict):
                render(child, depth + 1)

    for block in blocks:
        render(block)

    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def plain_text_for_blocks(blocks: list[dict[str, Any]]) -> str:
    chunks = [block_text(block).strip() for block in iter_blocks(blocks)]
    return "\n".join(chunk for chunk in chunks if chunk)


def write_capture_outputs(
    wrapper: dict[str, Any],
    out_dir: Path,
    download: bool = True,
) -> dict[str, Path | int | str]:
    response = unwrap_response(wrapper)
    chapter = response["data"]["chapter"]
    blocks = chapter.get("content") or []
    out_dir.mkdir(parents=True, exist_ok=True)

    raw_response_path = out_dir / "raw_response.json"
    chapter_path = out_dir / "chapter.json"
    wrapper_path = out_dir / "browser_fetch_wrapper.json"
    assets_path = out_dir / "assets_manifest.json"
    md_path = out_dir / "chapter.md"
    text_path = out_dir / "chapter.txt"
    summary_path = out_dir / "summary.md"

    wrapper_path.write_text(json.dumps(wrapper, ensure_ascii=False, indent=2), encoding="utf-8")
    raw_response_path.write_text(json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8")
    chapter_path.write_text(json.dumps(chapter, ensure_ascii=False, indent=2), encoding="utf-8")

    assets = collect_assets(blocks)
    if download:
        assets = download_assets(assets, out_dir)
    assets_path.write_text(json.dumps(assets, ensure_ascii=False, indent=2), encoding="utf-8")

    asset_by_block_id = {
        str(asset.get("block_id")): asset
        for asset in assets
        if asset.get("block_id")
    }
    markdown = markdown_for_blocks(blocks, asset_by_block_id)
    plain_text = plain_text_for_blocks(blocks)
    md_path.write_text(f"# {chapter.get('title', '生财有术 SCYS Chapter')}\n\n{markdown}", encoding="utf-8")
    text_path.write_text(plain_text + "\n", encoding="utf-8")

    headings = [
        block_text(block)
        for block in iter_blocks(blocks)
        if any(block.get(key) for key in ("heading1", "heading2", "heading3", "heading4", "heading5"))
    ]
    downloaded = sum(1 for asset in assets if asset.get("local_path"))
    failed = sum(1 for asset in assets if asset.get("download_error"))
    summary = [
        f"# 生财有术 SCYS Capture Summary: {chapter.get('title', '')}",
        "",
        f"- course_id: {chapter.get('course_id')}",
        f"- chapter_id: {chapter.get('id')}",
        f"- content_blocks: {len(blocks)}",
        f"- flattened_blocks: {len(iter_blocks(blocks))}",
        f"- assets: {len(assets)}",
        f"- assets_downloaded: {downloaded}",
        f"- assets_failed: {failed}",
        f"- learner_count: {chapter.get('learner_count', '')}",
        f"- updated_at: {chapter.get('updated_at', '')}",
        "",
        "## Headings",
        "",
    ]
    summary.extend(f"- {heading}" for heading in headings if heading)
    summary_path.write_text("\n".join(summary).strip() + "\n", encoding="utf-8")

    return {
        "out_dir": out_dir,
        "chapter_title": str(chapter.get("title", "")),
        "content_blocks": len(blocks),
        "flattened_blocks": len(iter_blocks(blocks)),
        "assets": len(assets),
        "assets_downloaded": downloaded,
        "assets_failed": failed,
        "markdown": md_path,
        "summary": summary_path,
    }


def default_output_dir(target: CourseTarget) -> Path:
    return DEFAULT_OUT_ROOT / f"course-{target.course_id}" / f"chapter-{target.chapter_id}"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Capture an authenticated 生财有术 SCYS course chapter from Chrome.")
    parser.add_argument("url", nargs="?", help="生财有术 SCYS course URL, e.g. https://scys.com/course/detail/148?chapterId=9614")
    parser.add_argument("--course-id", type=int, default=None)
    parser.add_argument("--chapter-id", type=int, default=None)
    parser.add_argument("--out", type=Path, default=None, help="Output directory")
    parser.add_argument("--from-wrapper", type=Path, default=None, help="Parse an existing browser_fetch_wrapper.json")
    parser.add_argument("--no-download-assets", action="store_true", help="Only write URLs, do not download images/files")
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args(argv)

    if args.from_wrapper:
        wrapper = load_wrapper(args.from_wrapper)
        response = unwrap_response(wrapper)
        chapter = response["data"]["chapter"]
        target = CourseTarget(
            course_id=int(chapter["course_id"]),
            chapter_id=int(chapter["id"]),
            url=f"https://scys.com/course/detail/{chapter['course_id']}?chapterId={chapter['id']}",
        )
    else:
        active_url = args.url
        if not active_url:
            active_url, _title = get_active_chrome_tab()
        target = parse_course_target(active_url, args.course_id, args.chapter_id)
        wrapper = fetch_chapter_via_active_chrome(target, timeout_sec=args.timeout)

    out_dir = args.out or default_output_dir(target)
    result = write_capture_outputs(wrapper, out_dir, download=not args.no_download_assets)
    print(json.dumps(
        {key: str(value) if isinstance(value, Path) else value for key, value in result.items()},
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
