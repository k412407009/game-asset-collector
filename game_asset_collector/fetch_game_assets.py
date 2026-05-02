#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Game asset auto-collector.

Pipeline:
  P0  Store screenshots (App Store + Google Play + Steam, Tavily fallback)
  P1  Gameplay video search + download (yt-dlp YouTube/Bilibili) + ffmpeg
      frame extraction (3 modes) + pHash perceptual de-duplication
  P1+ AI labeling (Doubao Vision via ARK API, quota-based early stop)
  P2  Emit Image Resource List (Markdown) ready to paste into
      design_spec.md Section VIII

Usage (project-aware mode, recommended for PPT / review projects):
  python fetch_game_assets.py "DREDGE" \
      --project F:/Git/some-project/projects/H_深海守望者_xxx \
      --steam-id 1562430 --label --max-videos 2

Usage (legacy standalone mode, asset library style):
  python fetch_game_assets.py "Last Asylum" --label
  python fetch_game_assets.py --list

Inputs:
  - .env at game-asset-collector repo root for TAVILY_API_KEY / ARK_API_KEY
    (key lookup is case-insensitive for local compatibility; also falls back to
     current process env vars, sibling game-ppt-master/.env, sibling
     ppt-master/.env, sibling game-review/.env, and sibling
     personal-assistant/.baoyu-skills/.env)
  - System: yt-dlp, ffmpeg on PATH (install separately)
  - Optional Python dep: google-play-scraper for the GP path

Outputs (project mode):
  <project>/images/store/<game>/{appstore,googleplay,steam}/screenshot_*.jpg
  <project>/images/gameplay/<game>/<video_slug>/frame_*.jpg
  <project>/images/gameplay/labels.json
  <project>/images/gameplay/descriptions.json
  <project>/images/_game_assets_meta/<game>.metadata.json
  <project>/images/_game_assets_meta/<game>.image_resource_list.md
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

# Force UTF-8 stdout/stderr so emoji + Chinese print work on Windows GBK terminals.
# Safe no-op on macOS / Linux where the locale is already UTF-8.
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Path & env bootstrap
# ---------------------------------------------------------------------------

PACKAGE_DIR = Path(__file__).resolve().parent
COLLECTOR_ROOT = PACKAGE_DIR.parent
DEFAULT_ASSETS_ENV = "GAME_ASSET_COLLECTOR_DEFAULT_OUT"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) game-asset-collector/0.1"


def _default_assets_dir() -> Path:
    override = os.environ.get(DEFAULT_ASSETS_ENV, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return COLLECTOR_ROOT / "game_assets_library"


def _iter_dotenv_paths() -> list[Path]:
    seen: set[Path] = set()
    candidates: list[Path] = []

    override = os.environ.get("GAME_ASSET_COLLECTOR_ENV", "").strip()
    if override:
        candidates.append(Path(override).expanduser().resolve())

    candidates.append(COLLECTOR_ROOT / ".env")
    workspace_root = COLLECTOR_ROOT.parent
    candidates.append(workspace_root / "game-ppt-master" / ".env")
    candidates.append(workspace_root / "ppt-master" / ".env")
    candidates.append(workspace_root / "game-review" / ".env")
    candidates.append(workspace_root / "personal-assistant" / ".baoyu-skills" / ".env")

    deduped: list[Path] = []
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        deduped.append(path)
    return deduped


def _load_dotenv() -> None:
    """Load env files into os.environ if not already set."""
    for env_path in _iter_dotenv_paths():
        if not env_path.exists():
            continue
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
        except Exception as e:
            print(f"   ⚠ .env load skipped: {e}", file=sys.stderr)


_load_dotenv()


def _find_env_value(name: str) -> tuple[str, str | None]:
    direct = os.environ.get(name, "").strip()
    if direct:
        return direct, name
    target = name.lower()
    for key, value in os.environ.items():
        value = value.strip()
        if value and key.lower() == target:
            return value, key
    return "", None


def _env(name: str, default: str = "") -> str:
    value, _source = _find_env_value(name)
    if value:
        return value
    return default.strip()


TAVILY_API_KEY = _env("TAVILY_API_KEY")
ARK_API_KEY = _env("ARK_API_KEY") or _env("VOLCENGINE_API_KEY")


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def _download(url: str, dest: Path, timeout: int = 30) -> bool:
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT
        })
        resp = urllib.request.urlopen(req, timeout=timeout)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            f.write(resp.read())
        return True
    except Exception as e:
        print(f"   ⚠ download failed {dest.name}: {e}")
        return False


def _json_get(url: str, timeout: int = 15):
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT
        })
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"   ⚠ http json get failed: {e}")
        return None


def _sanitize(name: str) -> str:
    """Convert game name into a filesystem-safe directory name (cross-platform)."""
    cleaned = re.sub(r'[<>:"/\\|?*\s]+', '-', name).strip('-')
    return cleaned[:80] or "unnamed"


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _find_recommended_python() -> tuple[str, str] | None:
    candidates = [
        "/opt/homebrew/bin/python3",
        "python3.14",
        "python3.13",
        "python3.12",
        "python3.11",
        "python3.10",
    ]
    for candidate in candidates:
        path = candidate if candidate.startswith("/") else shutil.which(candidate)
        if not path:
            continue
        try:
            out = subprocess.check_output(
                [path, "-c", "import sys; print(sys.version.split()[0])"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        except Exception:
            continue
        parts = tuple(int(x) for x in out.split(".")[:2])
        if parts >= (3, 10):
            return path, out
    return None


def _build_doctor_report() -> dict:
    tavily_value, tavily_source = _find_env_value("TAVILY_API_KEY")
    ark_value, ark_source = _find_env_value("ARK_API_KEY")
    if not ark_value:
        ark_value, ark_source = _find_env_value("VOLCENGINE_API_KEY")
    env_paths = [str(path) for path in _iter_dotenv_paths() if path.exists()]
    report = {
        "repo_root": str(COLLECTOR_ROOT),
        "python_version": sys.version.split()[0],
        "python_ok": sys.version_info >= (3, 10),
        "env_files": env_paths,
        "keys": {
            "TAVILY_API_KEY": bool(tavily_value),
            "ARK_API_KEY": bool(ark_value),
        },
        "key_sources": {
            "TAVILY_API_KEY": tavily_source,
            "ARK_API_KEY": ark_source,
        },
        "commands": {
            "yt-dlp": shutil.which("yt-dlp"),
            "ffmpeg": shutil.which("ffmpeg"),
        },
        "modules": {
            "google_play_scraper": _module_available("google_play_scraper"),
            "Pillow": _module_available("PIL"),
        },
    }
    return report


def _run_doctor() -> int:
    report = _build_doctor_report()
    blockers: list[str] = []
    warnings: list[str] = []

    def _line(status: str, label: str, detail: str) -> None:
        print(f"[{status}] {label}: {detail}")

    print("== game-asset-collector doctor ==")
    _line("OK", "repo", report["repo_root"])

    if report["python_ok"]:
        _line("OK", "python", report["python_version"])
    else:
        _line("MISS", "python", f"{report['python_version']}（需要 >= 3.10）")
        blockers.append("Python 版本低于 3.10")

    env_files = report["env_files"]
    if env_files:
        _line("OK", ".env", " ; ".join(env_files))
    else:
        _line("WARN", ".env", "未找到 .env 文件，将只读取当前 shell 环境变量")
        warnings.append("未找到 .env 文件")

    tavily_source = report["key_sources"]["TAVILY_API_KEY"]
    if report["keys"]["TAVILY_API_KEY"]:
        detail = "已配置（网页抓取兜底可用）"
        if tavily_source and tavily_source != "TAVILY_API_KEY":
            detail += f"；当前通过 `{tavily_source}` 读取，建议以后统一写成 `TAVILY_API_KEY`"
        _line("OK", "TAVILY_API_KEY", detail)
    else:
        _line("WARN", "TAVILY_API_KEY", "未配置，商店页文本兜底会降级")
        warnings.append("未配置 TAVILY_API_KEY")

    ark_source = report["key_sources"]["ARK_API_KEY"]
    if report["keys"]["ARK_API_KEY"]:
        detail = "已配置（AI 标签与中文描述可用）"
        if ark_source and ark_source != "ARK_API_KEY":
            detail += f"；当前通过 `{ark_source}` 读取"
        _line("OK", "ARK_API_KEY", detail)
    else:
        _line("WARN", "ARK_API_KEY", "未配置，将退化成启发式标签")
        warnings.append("未配置 ARK_API_KEY")

    for cmd, label in (("yt-dlp", "视频下载"), ("ffmpeg", "抽帧")):
        path = report["commands"][cmd]
        if path:
            _line("OK", cmd, f"{path}（{label}可用）")
        else:
            _line("WARN", cmd, f"未找到，gameplay 链路会不可用（{label}）")
            warnings.append(f"未找到 {cmd}")

    if report["modules"]["google_play_scraper"]:
        _line("OK", "google_play_scraper", "Google Play 结构化抓取可用")
    else:
        _line("WARN", "google_play_scraper", "未安装，将回退 Tavily / 网页抓取")
        warnings.append("未安装 google_play_scraper")

    if report["modules"]["Pillow"]:
        _line("OK", "Pillow", "图片读写可用")
    else:
        _line("MISS", "Pillow", "未安装，脚本无法正常处理图片")
        blockers.append("未安装 Pillow")

    print("\n总结:")
    if blockers:
        print(f"- 阻塞项 {len(blockers)} 个：")
        for item in blockers:
            print(f"  - {item}")
    else:
        print("- 没有阻塞项。")

    if warnings:
        print(f"- 提醒 {len(warnings)} 个：")
        for item in warnings:
            print(f"  - {item}")
    else:
        print("- 关键推荐项均已就绪。")

    print("\n建议:")
    print("- 只抓商店图：当前即可运行 `--store-only`")
    print("- 抓视频/抽帧：请确保 `yt-dlp` 和 `ffmpeg` 都可用")
    print("- 想要完整标签与描述：补齐 `ARK_API_KEY`")
    if blockers:
        recommended = _find_recommended_python()
        if recommended is not None:
            path, version = recommended
            print(f"- 当前默认 python 不够新，建议改用：{path} （{version}）")
    return 0 if not blockers else 2


APPSTORE_GENERIC_TOKENS = {
    "a", "an", "and", "app", "apps", "battle", "city", "day", "free", "fun",
    "game", "games", "hero", "idle", "island", "last", "legend", "legends",
    "mobile", "of", "online", "quest", "rpg", "sim", "simulator", "story",
    "survival", "the", "tycoon", "war", "world",
}


def _title_tokens(text: str) -> list[str]:
    return [tok for tok in re.split(r"[^a-z0-9]+", (text or "").lower()) if tok]


def _core_title_tokens(text: str) -> set[str]:
    return {
        tok for tok in _title_tokens(text)
        if len(tok) >= 2 and tok not in APPSTORE_GENERIC_TOKENS
    }


def _title_similarity(a: str, b: str) -> float:
    left = "".join(_title_tokens(a))
    right = "".join(_title_tokens(b))
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def _select_appstore_candidate(game_name: str, results: list[dict]) -> tuple[dict | None, str]:
    target_tokens = set(_title_tokens(game_name))
    target_core = _core_title_tokens(game_name)
    ranked = []

    for app in results:
        title = str(app.get("trackName") or app.get("trackCensoredName") or "")
        if not title:
            continue
        title_tokens = set(_title_tokens(title))
        title_core = _core_title_tokens(title)
        bundle_core = _core_title_tokens(str(app.get("bundleId") or "").replace(".", " "))
        overlap_all = len(target_tokens & title_tokens)
        overlap_core = len(target_core & (title_core | bundle_core))
        similarity = _title_similarity(game_name, title)
        contains = int(bool(title and (
            title.lower() in game_name.lower() or game_name.lower() in title.lower()
        )))
        score = similarity + (0.45 * overlap_core / max(len(target_core), 1)) + (0.12 * contains)
        if target_core and overlap_core == 0:
            score -= 0.30
        ranked.append((score, similarity, overlap_core, overlap_all, app))

    if not ranked:
        return None, "no ranked candidates"

    ranked.sort(key=lambda item: (item[0], item[1], item[2], item[3]), reverse=True)
    best_score, best_similarity, best_core, best_all, best_app = ranked[0]
    second_score = ranked[1][0] if len(ranked) > 1 else -1.0

    if target_core:
        confident = (
            (best_core == len(target_core) and best_similarity >= 0.58)
            or (best_core >= max(1, (len(target_core) + 1) // 2) and best_similarity >= 0.74)
            or best_similarity >= 0.90
        )
    else:
        confident = best_similarity >= 0.88 or (best_all >= max(1, len(target_tokens) - 1) and best_similarity >= 0.78)

    if not confident:
        return None, f"best candidate too weak (score={best_score:.2f}, similarity={best_similarity:.2f})"
    if second_score >= best_score - 0.04 and best_score < 1.15:
        return None, f"best candidate ambiguous (best={best_score:.2f}, second={second_score:.2f})"
    return best_app, f"matched by title score={best_score:.2f}, similarity={best_similarity:.2f}"


YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
BILIBILI_ID_RE = re.compile(r"^(BV[0-9A-Za-z]+)$", re.IGNORECASE)


def _normalize_video_target(target: str) -> str:
    """Normalize a user-supplied manual video target into a downloadable URL."""
    target = target.strip()
    if not target:
        return target
    if re.match(r"^https?://", target, re.IGNORECASE):
        return target
    if YOUTUBE_ID_RE.fullmatch(target):
        return f"https://www.youtube.com/watch?v={target}"
    m = BILIBILI_ID_RE.fullmatch(target)
    if m:
        return f"https://www.bilibili.com/video/{m.group(1)}"
    return target


def _run_cmd(cmd, timeout: int = 600):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, encoding="utf-8", errors="replace")
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "command timeout"
    except FileNotFoundError:
        return -2, f"command not found: {cmd[0]}"


# ---------------------------------------------------------------------------
# P0: Store screenshot collectors
# ---------------------------------------------------------------------------

def _tavily_extract_images(page_url: str):
    """Tavily Extract fallback: pull images from a public page."""
    if not TAVILY_API_KEY:
        return []
    body = json.dumps({"urls": [page_url], "include_images": True}).encode()
    req = urllib.request.Request(
        "https://api.tavily.com/extract",
        data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {TAVILY_API_KEY}"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read().decode())
        results = data.get("results", [])
        if results:
            return results[0].get("images", [])
    except Exception as e:
        print(f"   ⚠ Tavily Extract failed: {e}")
    return []


def fetch_appstore(game_name: str, game_dir: Path, app_id: str = None) -> dict:
    print("\n🍎 App Store...")
    out_dir = game_dir / "store" / "appstore"
    out_dir.mkdir(parents=True, exist_ok=True)

    if app_id:
        url = f"https://itunes.apple.com/lookup?id={app_id}&country=us&entity=software"
    else:
        term = urllib.parse.quote(game_name)
        url = f"https://itunes.apple.com/search?term={term}&entity=software&country=us&limit=10"

    data = _json_get(url)
    if not data or data.get("resultCount", 0) == 0:
        print("   ⚠ App Store no result")
        return {}

    results = data.get("results", [])
    if app_id:
        app = results[0]
        match_reason = f"lookup by app_id={app_id}"
    else:
        app, match_reason = _select_appstore_candidate(game_name, results)
        if not app:
            preview = ", ".join(
                str(item.get("trackName") or "").strip()
                for item in results[:3]
                if str(item.get("trackName") or "").strip()
            ) or "(none)"
            print(f"   ⚠ App Store search too fuzzy: {match_reason}")
            print(f"   ↳ top candidates: {preview}")
            print("   ↳ skip App Store; pass --appstore-id if you need an exact app")
            return {}
        print(f"   ✓ {match_reason}")

    info = {
        "source": "appstore",
        "trackName": app.get("trackName", ""),
        "bundleId": app.get("bundleId", ""),
        "trackId": app.get("trackId", ""),
        "sellerName": app.get("sellerName", ""),
        "price": app.get("formattedPrice", ""),
        "averageUserRating": app.get("averageUserRating", 0),
        "userRatingCount": app.get("userRatingCount", 0),
        "genres": app.get("genres", []),
        "description": app.get("description", "")[:500],
        "version": app.get("version", ""),
        "releaseDate": app.get("releaseDate", ""),
    }

    screenshots = app.get("screenshotUrls", [])
    ipad_screenshots = app.get("ipadScreenshotUrls", [])
    icon_url = app.get("artworkUrl512", "")

    count = 0
    if icon_url and _download(icon_url, out_dir / "icon.png"):
        count += 1

    if not screenshots and not ipad_screenshots:
        page_url = app.get("trackViewUrl", "")
        if page_url:
            print("   iTunes API has no screenshots, trying Tavily Extract...")
            for img_url in _tavily_extract_images(page_url):
                if "mzstatic.com" in img_url and "AppIcon" not in img_url:
                    screenshots.append(img_url)

    for i, surl in enumerate(screenshots):
        if _download(surl, out_dir / f"screenshot_{i+1:02d}.jpg"):
            count += 1
    for i, surl in enumerate(ipad_screenshots):
        if _download(surl, out_dir / f"ipad_{i+1:02d}.jpg"):
            count += 1

    info["screenshot_count"] = len(screenshots)
    info["ipad_screenshot_count"] = len(ipad_screenshots)
    print(f"   ✓ {info['trackName']} — {count} files")
    return info


def _gplay_with_timeout(func, args, timeout_sec: int = 15):
    import threading
    box = [None, None]

    def worker():
        try:
            box[0] = func(*args)
        except Exception as e:
            box[1] = e

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)
    if t.is_alive():
        print(f"   ⚠ google_play_scraper timeout ({timeout_sec}s)")
        return None
    if box[1]:
        print(f"   ⚠ google_play_scraper error: {box[1]}")
        return None
    return box[0]


def fetch_googleplay(game_name: str, game_dir: Path, gplay_id: str = None) -> dict:
    print("\n🤖 Google Play...")
    out_dir = game_dir / "store" / "googleplay"
    out_dir.mkdir(parents=True, exist_ok=True)

    result = None
    try:
        from google_play_scraper import app as gplay_app, search as gplay_search
        if gplay_id:
            result = _gplay_with_timeout(gplay_app, (gplay_id,), 15)
        else:
            results = _gplay_with_timeout(gplay_search, (game_name,), 15)
            if results:
                result = _gplay_with_timeout(gplay_app, (results[0]["appId"],), 15)
    except ImportError:
        print("   ⚠ google_play_scraper not installed (pip install google-play-scraper)")

    if not result:
        gplay_url = f"https://play.google.com/store/search?q={urllib.parse.quote(game_name)}&c=apps"
        print("   google_play_scraper unavailable, Tavily Extract fallback...")
        images = _tavily_extract_images(gplay_url)
        if images:
            good = [u for u in images if "googleusercontent" in u and "=w" in u] or images
            count = 0
            for i, img_url in enumerate(good[:10]):
                if _download(img_url, out_dir / f"screenshot_{i+1:02d}.jpg", timeout=10):
                    count += 1
            if count:
                print(f"   ✓ Tavily fallback — {count} screenshots")
                return {"source": "googleplay-tavily", "screenshot_count": count}
        print("   ⚠ Google Play no result")
        return {}

    info = {
        "source": "googleplay",
        "title": result.get("title", ""),
        "appId": result.get("appId", ""),
        "developer": result.get("developer", ""),
        "score": result.get("score", 0),
        "ratings": result.get("ratings", 0),
        "installs": result.get("installs", ""),
        "genre": result.get("genre", ""),
        "description": (result.get("description", "") or "")[:500],
        "released": result.get("released", ""),
    }

    screenshots = result.get("screenshots", [])
    icon_url = result.get("icon", "")
    video_url = result.get("video", "")
    count = 0
    if icon_url and _download(icon_url, out_dir / "icon.png"):
        count += 1
    for i, surl in enumerate(screenshots):
        if _download(surl, out_dir / f"screenshot_{i+1:02d}.jpg"):
            count += 1
    if video_url:
        info["video_url"] = video_url
    info["screenshot_count"] = len(screenshots)
    print(f"   ✓ {info['title']} — {count} files")
    return info


def fetch_steam(game_name: str, game_dir: Path, steam_id: str = None) -> dict:
    print("\n🎮 Steam...")
    out_dir = game_dir / "store" / "steam"
    out_dir.mkdir(parents=True, exist_ok=True)

    if not steam_id:
        search_url = (f"https://store.steampowered.com/api/storesearch/?"
                      f"term={urllib.parse.quote(game_name)}&l=schinese&cc=us")
        data = _json_get(search_url)
        if not data or not data.get("items"):
            print("   ⚠ Steam no result")
            return {}
        steam_id = str(data["items"][0]["id"])

    detail_url = f"https://store.steampowered.com/api/appdetails?appids={steam_id}&l=schinese"
    data = _json_get(detail_url)
    if not data or not data.get(steam_id, {}).get("success"):
        print(f"   ⚠ Steam appdetails failed (id={steam_id})")
        return {}

    d = data[steam_id]["data"]
    info = {
        "source": "steam",
        "name": d.get("name", ""),
        "steam_appid": d.get("steam_appid", ""),
        "type": d.get("type", ""),
        "developers": d.get("developers", []),
        "publishers": d.get("publishers", []),
        "genres": [g["description"] for g in d.get("genres", [])],
        "categories": [c["description"] for c in d.get("categories", [])],
        "description": (d.get("short_description", "") or "")[:500],
        "release_date": d.get("release_date", {}).get("date", ""),
        "price": d.get("price_overview", {}).get("final_formatted", "Free")
            if d.get("price_overview") else "Free",
    }

    screenshots = d.get("screenshots", [])
    movies = d.get("movies", [])
    header_url = d.get("header_image", "")

    count = 0
    if header_url and _download(header_url, out_dir / "header.jpg"):
        count += 1
    for i, s in enumerate(screenshots):
        url = s.get("path_full", "")
        if url and _download(url, out_dir / f"screenshot_{i+1:02d}.jpg"):
            count += 1

    movie_urls = []
    for i, m in enumerate(movies[:3]):
        mp4 = m.get("mp4", {})
        url = mp4.get("480", "") or mp4.get("max", "")
        if url:
            movie_urls.append(url)
            fname = f"trailer_{m.get('id', i)}.mp4"
            if _download(url, out_dir / fname, timeout=60):
                count += 1

    info["screenshot_count"] = len(screenshots)
    info["movie_count"] = len(movies)
    info["movie_urls"] = movie_urls
    print(f"   ✓ {info['name']} — {count} files")
    return info


def run_store(game_name: str, game_dir: Path, args) -> dict:
    metadata = {"game_name": game_name,
                "collected_at": datetime.now().isoformat(),
                "stores": {}}
    appstore_info = fetch_appstore(game_name, game_dir, app_id=args.appstore_id)
    if appstore_info:
        metadata["stores"]["appstore"] = appstore_info
    googleplay_info = fetch_googleplay(game_name, game_dir, gplay_id=args.gplay_id)
    if googleplay_info:
        metadata["stores"]["googleplay"] = googleplay_info
    steam_info = fetch_steam(game_name, game_dir, steam_id=args.steam_id)
    if steam_info:
        metadata["stores"]["steam"] = steam_info
    return metadata


# ---------------------------------------------------------------------------
# pHash perceptual de-duplication (pure PIL, zero extra deps)
# ---------------------------------------------------------------------------

def _region_sharpness(gray_img, box: tuple[int, int, int, int]) -> float:
    try:
        from PIL import Image
    except ImportError:
        return 0.0
    crop = gray_img.crop(box)
    crop = crop.resize((64, 64), Image.LANCZOS)
    pixels = list(crop.getdata())
    score = 0
    width = 64
    for y in range(1, 64):
        row = y * width
        prev = (y - 1) * width
        for x in range(1, width):
            p = pixels[row + x]
            score += abs(p - pixels[row + x - 1]) + abs(p - pixels[prev + x])
    return score / float(width * width)


def _active_hash_crop_box(img) -> tuple[int, int, int, int]:
    width, height = img.size
    if width <= 0 or height <= 0:
        return (0, 0, width, height)
    if width / max(height, 1) < 1.45:
        return (0, 0, width, height)

    gray = img.convert("L")
    left_score = _region_sharpness(gray, (0, 0, max(1, width // 5), height))
    center_score = _region_sharpness(
        gray,
        (int(width * 0.4), 0, int(width * 0.6), height),
    )
    right_score = _region_sharpness(
        gray,
        (int(width * 0.8), 0, width, height),
    )
    side_score = max(left_score, right_score, 1.0)
    if center_score < side_score * 1.22:
        return (0, 0, width, height)

    portrait_width = min(width, max(int(height * 9 / 16) + 24, int(width * 0.3)))
    portrait_width = min(portrait_width, int(width * 0.58))
    x0 = max(0, (width - portrait_width) // 2)
    x1 = min(width, x0 + portrait_width)
    return (x0, 0, x1, height)


def _phash(img_path: Path, hash_size: int = 12):
    try:
        from PIL import Image
        img = Image.open(img_path).convert("RGB")
        img = img.crop(_active_hash_crop_box(img)).convert("L").resize(
            (hash_size + 1, hash_size), Image.LANCZOS
        )
        pixels = list(img.getdata())
        bits = 0
        bit_index = 0
        for y in range(hash_size):
            row = y * (hash_size + 1)
            for x in range(hash_size):
                if pixels[row + x] > pixels[row + x + 1]:
                    bits |= 1 << bit_index
                bit_index += 1
        return bits
    except Exception:
        return None


def _hamming(h1: int, h2: int) -> int:
    return bin(h1 ^ h2).count('1')


def deduplicate_frames(
    frames_dir: Path,
    threshold: int = 8,
    mode: str = "global",
    recent_window: int = 6,
) -> int:
    print(f"   🧹 perceptual dedup ({mode})...")
    frame_groups: list[list[Path]] = []

    root_level = sorted(frames_dir.glob("*.jpg"))
    if root_level:
        frame_groups.append(root_level)
    for d in sorted(frames_dir.iterdir()):
        if d.is_dir():
            group = sorted(d.glob("*.jpg"))
            if group:
                frame_groups.append(group)

    if not frame_groups:
        return 0

    scanned_total = 0
    removed_total = 0
    for group in frame_groups:
        hashes: list[int] = []
        recent_hashes: list[int] = []
        for fpath in group:
            scanned_total += 1
            h = _phash(fpath)
            if h is None:
                continue
            compare_pool = recent_hashes if mode == "sequential" else hashes
            if any(_hamming(h, existing) < threshold for existing in compare_pool):
                fpath.unlink()
                removed_total += 1
                continue
            hashes.append(h)
            if mode == "sequential":
                recent_hashes.append(h)
                if len(recent_hashes) > recent_window:
                    recent_hashes = recent_hashes[-recent_window:]

    kept_total = scanned_total - removed_total
    print(f"      scanned {scanned_total}, removed {removed_total}, kept {kept_total}")
    return removed_total


def _probe_video_duration(video_path: Path) -> float | None:
    rc, output = _run_cmd(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        timeout=30,
    )
    if rc != 0:
        return None
    try:
        return float(output.strip().splitlines()[-1])
    except Exception:
        return None


def _format_timestamp(seconds: float) -> str:
    total_seconds = max(0, int(round(seconds)))
    minutes, secs = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _analysis_segments(duration_sec: float, fixed_interval_sec: int | None = None) -> list[dict[str, float | int | str]]:
    if duration_sec <= 0:
        return []
    if fixed_interval_sec is not None:
        interval = max(1, int(fixed_interval_sec))
        return [{"label": "full", "start": 0.0, "end": duration_sec, "interval": interval}]
    if duration_sec <= 300:
        return [{"label": "full", "start": 0.0, "end": duration_sec, "interval": 2}]

    plan = [
        ("intro", 600.0, 4),
        ("mid", 1800.0, 8),
        ("late", duration_sec, 15),
    ]
    segments: list[dict[str, float | int | str]] = []
    cursor = 0.0
    for label, boundary, interval in plan:
        if cursor >= duration_sec:
            break
        end = min(duration_sec, boundary)
        if end - cursor < max(2.0, interval * 0.75):
            continue
        segments.append(
            {
                "label": label,
                "start": cursor,
                "end": end,
                "interval": interval,
            }
        )
        cursor = end
    if not segments:
        segments.append({"label": "full", "start": 0.0, "end": duration_sec, "interval": 4})
    return segments


def _extract_analysis_frames(video_path: Path, frames_dir: Path,
                             fixed_interval_sec: int | None = None) -> list[dict[str, object]]:
    duration_sec = _probe_video_duration(video_path)
    if not duration_sec:
        return []

    entries: list[dict[str, object]] = []
    counter = 1
    for seg_index, segment in enumerate(
        _analysis_segments(duration_sec, fixed_interval_sec=fixed_interval_sec),
        start=1,
    ):
        start_sec = float(segment["start"])
        end_sec = float(segment["end"])
        interval_sec = int(segment["interval"])
        segment_label = str(segment["label"])
        output_pattern = str(frames_dir / f"{segment_label}_{seg_index:02d}_%04d.jpg")
        cmd = ["ffmpeg"]
        if start_sec > 0:
            cmd.extend(["-ss", str(start_sec)])
        cmd.extend(["-i", str(video_path), "-t", str(max(0.1, end_sec - start_sec))])
        cmd.extend(
            [
                "-vf",
                f"fps=1/{interval_sec},scale=1280:-2",
                "-q:v",
                "2",
                output_pattern,
                "-y",
                "-loglevel",
                "warning",
            ]
        )
        rc, _output = _run_cmd(cmd, timeout=300)
        if rc != 0:
            continue

        generated = sorted(frames_dir.glob(f"{segment_label}_{seg_index:02d}_*.jpg"))
        for item_index, img_path in enumerate(generated):
            timestamp_sec = min(duration_sec, start_sec + item_index * interval_sec)
            final_name = f"frame_t{int(round(timestamp_sec)):06d}_{counter:04d}.jpg"
            final_path = frames_dir / final_name
            img_path.rename(final_path)
            entries.append(
                {
                    "relative_path": final_path,
                    "timestamp_sec": round(timestamp_sec, 1),
                    "timestamp": _format_timestamp(timestamp_sec),
                    "interval_sec": interval_sec,
                    "segment": segment_label,
                }
            )
            counter += 1
    return entries


def _write_frame_index(game_dir: Path, entries: list[dict[str, object]]) -> Path | None:
    if not entries:
        return None
    payload: dict[str, dict[str, object]] = {}
    for entry in entries:
        rel_path = Path(entry["relative_path"]).relative_to(game_dir).as_posix()
        payload[rel_path] = {
            "timestamp_sec": entry["timestamp_sec"],
            "timestamp": entry["timestamp"],
            "interval_sec": entry["interval_sec"],
            "segment": entry["segment"],
            "video_filename": entry["video_filename"],
            "video_slug": entry["video_slug"],
        }
    frame_index_path = game_dir / "gameplay" / "frame_index.json"
    frame_index_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return frame_index_path


WALKTHROUGH_KEYWORDS = (
    "walkthrough",
    "gameplay",
    "playthrough",
    "longplay",
    "guide",
    "part 1",
    "full game",
    "full walkthrough",
    "实机",
    "流程",
    "攻略",
)

TRAILER_KEYWORDS = (
    "trailer",
    "teaser",
    "official trailer",
    "announcement",
    "preview",
    "promo",
    "commercial",
    "advert",
    "advertisement",
    "预告",
    "宣传",
    "pv",
)


def _keyword_hit_count(text: str, keywords: tuple[str, ...]) -> int:
    lowered = text.lower()
    return sum(1 for keyword in keywords if keyword in lowered)


def _portrait_probe_ratio(frame_paths: list[Path]) -> float:
    try:
        from PIL import Image
    except ImportError:
        return 0.0
    portrait_like = 0
    valid = 0
    for path in frame_paths:
        try:
            img = Image.open(path).convert("RGB")
        except Exception:
            continue
        valid += 1
        box = _active_hash_crop_box(img)
        active_width = max(1, box[2] - box[0])
        if active_width / max(img.size[0], 1) < 0.72:
            portrait_like += 1
    if valid == 0:
        return 0.0
    return portrait_like / float(valid)


def _probe_uniqueness_ratio(frame_paths: list[Path]) -> float:
    hashes: list[int] = []
    valid = 0
    unique = 0
    for path in frame_paths:
        h = _phash(path)
        if h is None:
            continue
        valid += 1
        if not hashes or all(_hamming(h, existing) >= 6 for existing in hashes):
            unique += 1
            hashes.append(h)
    if valid == 0:
        return 0.0
    return unique / float(valid)


def _decide_video_type(
    title: str,
    duration_sec: float | None,
    portrait_ratio: float,
    unique_ratio: float,
) -> tuple[str, str]:
    walkthrough_hits = _keyword_hit_count(title, WALKTHROUGH_KEYWORDS)
    trailer_hits = _keyword_hit_count(title, TRAILER_KEYWORDS)

    if trailer_hits and not walkthrough_hits:
        return "trailer", "title-keyword"
    if walkthrough_hits and not trailer_hits:
        return "walkthrough", "title-keyword"
    if portrait_ratio >= 0.5:
        return "walkthrough", "portrait-ui"
    if duration_sec and duration_sec <= 180 and unique_ratio >= 0.75 and portrait_ratio < 0.4:
        return "trailer", "short-high-cut"
    if duration_sec and duration_sec >= 300:
        return "walkthrough", "long-form"
    if duration_sec and duration_sec <= 180:
        return "trailer", "short-form"
    return "walkthrough", "fallback-default"


def detect_video_strategy(video_path: Path) -> dict[str, object]:
    duration_sec = _probe_video_duration(video_path)
    probe_frames: list[Path] = []
    sample_count = 0
    with tempfile.TemporaryDirectory(prefix="collector-probe-") as tmp_dir:
        tmp_path = Path(tmp_dir)
        span_sec = 90 if not duration_sec else min(90, max(30, int(duration_sec * 0.2)))
        cmd = [
            "ffmpeg",
            "-i",
            str(video_path),
            "-t",
            str(span_sec),
            "-vf",
            "fps=1/15,scale=640:-2",
            "-q:v",
            "5",
            str(tmp_path / "probe_%03d.jpg"),
            "-y",
            "-loglevel",
            "warning",
        ]
        rc, _output = _run_cmd(cmd, timeout=60)
        if rc == 0:
            probe_frames = sorted(tmp_path.glob("probe_*.jpg"))
            sample_count = len(probe_frames)
            portrait_ratio = _portrait_probe_ratio(probe_frames)
            unique_ratio = _probe_uniqueness_ratio(probe_frames)
        else:
            portrait_ratio = 0.0
            unique_ratio = 0.0

    video_type, reason = _decide_video_type(
        title=video_path.stem,
        duration_sec=duration_sec,
        portrait_ratio=portrait_ratio,
        unique_ratio=unique_ratio,
    )
    extraction_mode = "analysis" if video_type == "walkthrough" else "scene"
    return {
        "video_type": video_type,
        "extraction_mode": extraction_mode,
        "reason": reason,
        "duration_sec": round(duration_sec, 1) if duration_sec else None,
        "probe_samples": sample_count,
        "portrait_ratio": round(portrait_ratio, 3),
        "uniqueness_ratio": round(unique_ratio, 3),
    }


# ---------------------------------------------------------------------------
# P1: Gameplay video download + frame extraction
# ---------------------------------------------------------------------------

def fetch_gameplay(game_name: str, game_dir: Path, max_videos: int = 3,
                   scene_threshold: float = 0.3, keep_video: bool = False,
                   smart: bool = True, frame_interval: int = 5,
                   scene_mode: bool = False,
                   analysis_mode: bool = False,
                   analysis_interval: int | None = None,
                   manual_targets: list[str] | None = None) -> dict:
    """yt-dlp YouTube/Bilibili search → download → ffmpeg frame extraction.

    scene_mode=True : ffmpeg scene-detection (content-driven) + pHash dedup
    smart=True (default) : sparse sampling (1 frame per N seconds) + pHash dedup
    smart=False / no-smart : legacy ffmpeg scene-detection without dedup
    """
    auto_mode = smart and not analysis_mode and not scene_mode
    if analysis_mode:
        if analysis_interval:
            mode_str = f"analysis-timeline {analysis_interval}s/frame (forced)"
        else:
            mode_str = "analysis-timeline (forced)"
    elif scene_mode:
        mode_str = f"scene-detect+dedup th{scene_threshold} (forced)"
    elif smart:
        mode_str = "auto-detect walkthrough/trailer"
    else:
        mode_str = f"scene-detect th{scene_threshold} (legacy)"
    print(f"\n📹 Gameplay video collection (max {max_videos} videos, {mode_str})...")
    video_dir = game_dir / "gameplay" / "videos"
    frames_dir = game_dir / "gameplay" / "frames"
    video_dir.mkdir(parents=True, exist_ok=True)
    frames_dir.mkdir(parents=True, exist_ok=True)

    output_template = str(video_dir / "%(title).80s__%(id)s.%(ext)s")

    def _run_yt_dlp(target: str, label: str) -> bool:
        cmd = [
            "yt-dlp", target,
            "-f", "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
            "--merge-output-format", "mp4",
            "-o", output_template,
            "--no-playlist", "--socket-timeout", "30", "--retries", "3",
        ]
        print(f"   downloading {label}...")
        rc, output = _run_cmd(cmd, timeout=300)
        if rc != 0:
            print(f"   ⚠ yt-dlp returned {rc} for {target}")
            if "command not found" in output or "command not" in output:
                print("   ⚠ yt-dlp not installed. Install: pip install yt-dlp  (or `winget install yt-dlp`)")
            else:
                tail = output.strip().splitlines()[-3:]
                if tail:
                    print("   " + " | ".join(tail))
            return False
        return True

    manual_targets = [t for t in (manual_targets or []) if t.strip()]
    if manual_targets:
        print(f"   manual video targets: {len(manual_targets)}")
        for raw_target in manual_targets:
            normalized = _normalize_video_target(raw_target)
            label = "manual target"
            if normalized != raw_target:
                print(f"   normalize: {raw_target} -> {normalized}")
            _run_yt_dlp(normalized, label)
    else:
        # YouTube first
        yt_query = f"ytsearch{max_videos}:{game_name} gameplay"
        _run_yt_dlp(yt_query, "from YouTube search")

        yt_videos = list(video_dir.glob("*.mp4"))
        if len(yt_videos) < max_videos:
            remaining = max_videos - len(yt_videos)
            print(f"   filling with Bilibili (max {remaining})...")
            bili_query = f"bilisearch{remaining}:{game_name} 实机 gameplay"
            bili_output_template = str(video_dir / "bili_%(title).80s__%(id)s.%(ext)s")
            cmd_bili = [
                "yt-dlp", bili_query,
                "-f", "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
                "--merge-output-format", "mp4",
                "-o", bili_output_template,
                "--no-playlist", "--socket-timeout", "30", "--retries", "3",
            ]
            _run_cmd(cmd_bili, timeout=300)

    all_videos = sorted(video_dir.glob("*.mp4"))
    if not all_videos:
        print("   ⚠ no videos downloaded")
        return {}

    print(f"   ✓ downloaded {len(all_videos)} videos")
    total_frames = 0
    video_info = []
    frame_index_entries: list[dict[str, object]] = []

    for vpath in all_videos:
        vname = vpath.stem
        vframes_dir = frames_dir / _sanitize(vname)
        vframes_dir.mkdir(parents=True, exist_ok=True)
        auto_detection = detect_video_strategy(vpath)
        extraction_mode = auto_detection["extraction_mode"] if auto_mode else ("analysis" if analysis_mode else "scene")
        if auto_mode:
            print(
                "   🧭 auto-detect:"
                f" {vpath.name[:60]} -> {auto_detection['video_type']} -> {extraction_mode}"
                f" ({auto_detection['reason']}, dur={auto_detection.get('duration_sec')},"
                f" portrait={auto_detection.get('portrait_ratio')},"
                f" unique={auto_detection.get('uniqueness_ratio')})"
            )
        else:
            print(
                "   🧭 detected:"
                f" {vpath.name[:60]} -> {auto_detection['video_type']}"
                f" ({auto_detection['reason']}); using forced {extraction_mode}"
            )

        if extraction_mode == "analysis":
            print(f"   🔍 analysis extract: {vpath.name[:60]}...")
            if analysis_interval:
                print(f"      using fixed analysis interval: {analysis_interval}s/frame")
            extracted_entries = _extract_analysis_frames(
                vpath,
                vframes_dir,
                fixed_interval_sec=analysis_interval,
            )
            extracted = sorted(vframes_dir.glob("*.jpg"))
            for entry in extracted_entries:
                entry["video_filename"] = vpath.name
                entry["video_slug"] = _sanitize(vname)
                frame_index_entries.append(entry)
        elif scene_mode or extraction_mode == "scene":
            print(f"   🔍 scene-detect extract: {vpath.name[:60]}...")
            cmd_ff = [
                "ffmpeg", "-i", str(vpath),
                "-vf", f"select='gt(scene,{scene_threshold})',scale=1280:-2",
                "-vsync", "vfr", "-q:v", "2",
                str(vframes_dir / "frame_%04d.jpg"),
                "-y", "-loglevel", "warning",
            ]
        elif smart:
            print(f"   🔍 sparse extract: {vpath.name[:60]}...")
            cmd_ff = [
                "ffmpeg", "-i", str(vpath),
                "-vf", f"fps=1/{frame_interval},scale=1280:-2",
                "-q:v", "2",
                str(vframes_dir / "frame_%04d.jpg"),
                "-y", "-loglevel", "warning",
            ]
        else:
            print(f"   🔍 scene-detect (legacy): {vpath.name[:60]}...")
            cmd_ff = [
                "ffmpeg", "-i", str(vpath),
                "-vf", f"select='gt(scene,{scene_threshold})',scale=1280:-2",
                "-vsync", "vfr", "-q:v", "2",
                str(vframes_dir / "frame_%04d.jpg"),
                "-y", "-loglevel", "warning",
            ]

        if extraction_mode != "analysis":
            rc, output = _run_cmd(cmd_ff, timeout=120)
            if rc == -2:
                print("   ⚠ ffmpeg not on PATH. Install: `winget install ffmpeg` / `brew install ffmpeg`")
                return {}
            extracted = list(vframes_dir.glob("*.jpg"))
        total_frames += len(extracted)
        video_info.append({
            "filename": vpath.name,
            "size_mb": round(vpath.stat().st_size / 1024 / 1024, 1),
            "frames_extracted": len(extracted),
            "frames_dir": str(vframes_dir.relative_to(game_dir)),
            "video_type": auto_detection["video_type"],
            "detected_mode": auto_detection["extraction_mode"],
            "used_mode": extraction_mode,
            "detection_reason": auto_detection["reason"],
            "duration_sec": auto_detection["duration_sec"],
            "probe_samples": auto_detection["probe_samples"],
            "portrait_ratio": auto_detection["portrait_ratio"],
            "uniqueness_ratio": auto_detection["uniqueness_ratio"],
        })
        print(f"      → {len(extracted)} frames")

    dedup_removed = 0
    if total_frames > 0 and (analysis_mode or any(info.get("used_mode") == "analysis" for info in video_info)):
        dedup_removed = deduplicate_frames(
            frames_dir,
            threshold=5,
            mode="sequential",
            recent_window=4,
        )
        total_frames -= dedup_removed
        if frame_index_entries:
            frame_index_entries = [
                entry
                for entry in frame_index_entries
                if Path(entry["relative_path"]).exists()
            ]
    elif (smart or scene_mode) and total_frames > 0:
        dedup_removed = deduplicate_frames(frames_dir)
        total_frames -= dedup_removed

    if not keep_video:
        print("   🗑 removing source videos...")
        for vpath in all_videos:
            vpath.unlink()
        if video_dir.exists() and not list(video_dir.iterdir()):
            video_dir.rmdir()

    print(f"   ✓ kept {total_frames} frames"
          + (f" (dedup removed {dedup_removed})" if dedup_removed else ""))
    frame_index_path = _write_frame_index(game_dir, frame_index_entries)
    mode_label = (
        "analysis"
        if analysis_mode
        else ("scene+dedup" if scene_mode else ("auto" if auto_mode else ("smart" if smart else "scene")))
    )
    result = {
        "videos": video_info,
        "total_frames": total_frames,
        "dedup_removed": dedup_removed,
        "mode": mode_label,
    }
    if frame_index_path:
        result["frame_index"] = str(frame_index_path.relative_to(game_dir))
    return result


def gameplay_uses_analysis_mode(gameplay_meta: dict | None, game_dir: Path | None = None) -> bool:
    if isinstance(gameplay_meta, dict):
        if gameplay_meta.get("frame_index"):
            return True
        videos = gameplay_meta.get("videos") or []
        if any((video or {}).get("used_mode") == "analysis" for video in videos if isinstance(video, dict)):
            return True
        if gameplay_meta.get("mode") == "analysis":
            return True
    if game_dir is not None and (game_dir / "gameplay" / "frame_index.json").exists():
        return True
    return False


# ---------------------------------------------------------------------------
# P1+: AI labeling (Doubao Vision)
# ---------------------------------------------------------------------------

LABEL_CATEGORIES = [
    "ui-menu", "battle", "shop-gacha", "main-city",
    "cutscene", "loading", "character", "map-world",
    "tutorial", "social", "ad-creative", "other",
]

SCENE_QUOTA = {
    "battle": 3, "main-city": 2, "character": 2,
    "shop-gacha": 2, "ui-menu": 2, "map-world": 1,
    "cutscene": 1, "tutorial": 1, "other": 1,
}  # total quota = 15

VISION_MODEL_LITE = "doubao-seed-1-6-vision-250815"
VISION_MODEL_HEAVY = "doubao-seed-1-6-vision-250815"


def _frame_sort_key(rel_path: str) -> tuple[int, int, str]:
    match = re.search(r"frame_t(\d+)", rel_path)
    if match:
        return (0, int(match.group(1)), rel_path)
    fallback = re.search(r"frame_(\d+)", rel_path)
    if fallback:
        return (1, int(fallback.group(1)), rel_path)
    return (2, 0, rel_path)


def _get_image_info(img_path: Path):
    size_kb = img_path.stat().st_size / 1024
    w, h = 0, 0
    try:
        with open(img_path, "rb") as f:
            data = f.read(65536)
        i = 0
        while i < len(data) - 8:
            if data[i] == 0xFF and data[i + 1] in (0xC0, 0xC2):
                h = (data[i + 5] << 8) + data[i + 6]
                w = (data[i + 7] << 8) + data[i + 8]
                break
            i += 1
    except Exception:
        pass
    return w, h, size_kb


def _heuristic_label(img_path: Path):
    _w, _h, size_kb = _get_image_info(img_path)
    if "store" in str(img_path):
        return "store-screenshot"
    if size_kb < 10:
        return "loading"
    return None


def _resize_for_vision(img_path: Path, max_px: int = 256) -> str:
    try:
        from PIL import Image
        import io
        img = Image.open(img_path)
        img.thumbnail((max_px, max_px), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=60)
        return base64.b64encode(buf.getvalue()).decode()
    except ImportError:
        with open(img_path, "rb") as f:
            return base64.b64encode(f.read()).decode()


def _default_description(rel: str, label: str) -> str:
    rel_path = Path(rel)
    parts = rel_path.parts
    if "store" in parts:
        platform = "store"
        if len(parts) >= 2:
            try:
                idx = parts.index("store")
                if idx + 1 < len(parts):
                    platform = parts[idx + 1]
            except ValueError:
                pass
        m = re.search(r"(\d+)", rel_path.stem)
        if m:
            return f"{platform} 商店截图 {int(m.group(1))}"
        return f"{platform} 商店截图"
    if label == "loading":
        return "加载/转场画面"
    return ""


def _parse_label_desc(raw: str) -> tuple[str, str]:
    text = raw.strip()
    for fence in ("```json", "```JSON", "```"):
        text = text.replace(fence, "")
    text = text.strip().strip("`").strip()
    try:
        payload = json.loads(text)
        label = str(payload.get("label", "other")).strip().lower()
        desc = str(payload.get("desc", "")).strip()
    except Exception:
        lowered = text.lower()
        label = next((c for c in LABEL_CATEGORIES if c in lowered), "other")
        desc = text[:60]
    if label not in LABEL_CATEGORIES:
        label = "other"
    return label, desc


def write_timeline_summary(game_dir: Path) -> Path | None:
    gameplay_dir = game_dir / "gameplay"
    frame_index_path = gameplay_dir / "frame_index.json"
    if not frame_index_path.exists():
        return None

    try:
        frame_index = json.loads(frame_index_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(frame_index, dict) or not frame_index:
        return None

    labels = {}
    descriptions = {}
    labels_path = gameplay_dir / "labels.json"
    descriptions_path = gameplay_dir / "descriptions.json"
    if labels_path.exists():
        try:
            labels = json.loads(labels_path.read_text(encoding="utf-8"))
        except Exception:
            labels = {}
    if descriptions_path.exists():
        try:
            descriptions = json.loads(descriptions_path.read_text(encoding="utf-8"))
        except Exception:
            descriptions = {}

    ordered_entries = []
    for rel_path, meta in frame_index.items():
        if not isinstance(meta, dict):
            continue
        ordered_entries.append(
            {
                "rel_path": rel_path,
                "timestamp_sec": float(meta.get("timestamp_sec", 0)),
                "timestamp": str(meta.get("timestamp", "")),
                "video_filename": str(meta.get("video_filename", "")),
                "segment": str(meta.get("segment", "")),
                "label": labels.get(rel_path, ""),
                "description": descriptions.get(rel_path, ""),
            }
        )
    ordered_entries.sort(key=lambda item: (item["video_filename"], item["timestamp_sec"], item["rel_path"]))
    if not ordered_entries:
        return None

    out_md = gameplay_dir / "timeline_summary.md"
    lines = [
        "# Gameplay Timeline Summary",
        "",
        f"- 样本帧：{len(ordered_entries)}",
        f"- 前 15 分钟帧数：{sum(1 for item in ordered_entries if item['timestamp_sec'] <= 15 * 60)}",
        "",
        "## 标签分布",
        "",
    ]
    distribution = _count_tags({item["rel_path"]: item["label"] for item in ordered_entries if item["label"]})
    if distribution:
        for label, count in sorted(distribution.items(), key=lambda pair: (-pair[1], pair[0])):
            lines.append(f"- {label}: {count}")
    else:
        lines.append("- 还没有可用标签")

    current_video = None
    for item in ordered_entries:
        if item["video_filename"] != current_video:
            current_video = item["video_filename"]
            lines.extend(["", f"## {current_video}", ""])
        desc = item["description"] or Path(item["rel_path"]).name
        label = item["label"] or "-"
        segment = f" [{item['segment']}]" if item["segment"] else ""
        lines.append(f"- {item['timestamp']} | {label}{segment} | {desc}")

    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_md


def label_frames(game_dir: Path, force: bool = False, model: str = None,
                 smart: bool = True, quota_overrides: dict = None,
                 analysis_mode: bool = False) -> dict:
    """AI-label gameplay frames + store screenshots.

    smart=True (default): quota-based early stop, only labels until quotas filled
    smart=False: heuristic-first, send everything uncertain to AI
    """
    mode_label = "analysis-full" if analysis_mode else ("smart-quota" if smart else "full")
    print(f"\n🏷 Labeling ({mode_label})...")
    frames_dir = game_dir / "gameplay" / "frames"

    all_frames = []
    if frames_dir.exists():
        for d in sorted(frames_dir.iterdir()):
            if d.is_dir():
                all_frames.extend(sorted(d.glob("*.jpg")))

    store_dir = game_dir / "store"
    store_frames = []
    if store_dir.exists():
        for platform in ("appstore", "googleplay", "steam"):
            pdir = store_dir / platform
            if pdir.exists():
                store_frames.extend(sorted(pdir.glob("screenshot_*.jpg")))

    if not all_frames and not store_frames:
        print("   ⚠ no frames to label")
        return {}

    labels_path = game_dir / "gameplay" / "labels.json"
    descriptions_path = game_dir / "gameplay" / "descriptions.json"
    existing_labels = {}
    existing_descs = {}
    if labels_path.exists() and not force:
        try:
            existing_labels = json.loads(labels_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    if descriptions_path.exists() and not force:
        try:
            existing_descs = json.loads(descriptions_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    heuristic_results = {}
    heuristic_descs = {}
    for img_path in store_frames:
        rel = str(img_path.relative_to(game_dir))
        heuristic_results[rel] = "store-screenshot"
        heuristic_descs[rel] = existing_descs.get(rel) or _default_description(rel, "store-screenshot")

    need_ai = []
    skipped = 0
    ordered_frames = sorted(all_frames, key=lambda path: _frame_sort_key(str(path.relative_to(game_dir))))
    for img_path in ordered_frames:
        rel = str(img_path.relative_to(game_dir))
        if rel in existing_labels and rel in existing_descs and existing_descs.get(rel) and not force:
            skipped += 1
            continue
        h_label = None if analysis_mode else _heuristic_label(img_path)
        if h_label is not None:
            heuristic_results[rel] = h_label
            desc = existing_descs.get(rel) or _default_description(rel, h_label)
            if desc:
                heuristic_descs[rel] = desc
        else:
            need_ai.append((rel, img_path))

    need_ai.sort(key=lambda item: _frame_sort_key(item[0]))

    total = len(all_frames) + len(store_frames)
    print(f"   total {total} | cached {skipped} | heuristic {len(heuristic_results)} | need-AI {len(need_ai)}")

    if skipped == len(all_frames) and not store_frames:
        print("   ✓ all already labeled (use --force to relabel)")
        return {"total": len(existing_labels),
                "distribution": _count_tags(existing_labels),
                "mode": "cached", "ai_calls": 0}

    if not ARK_API_KEY:
        print("   ⚠ ARK_API_KEY not set — fallback to heuristic-only labels")

    vision_model = model or VISION_MODEL_LITE
    use_ai, ai_calls, total_tokens = False, 0, 0

    if ARK_API_KEY and need_ai:
        test_rel, test_img = need_ai[0]
        image_max_px = 512 if analysis_mode else 256
        image_detail = "high" if analysis_mode else "low"
        body = json.dumps({
            "model": vision_model,
            "messages": [{"role": "user", "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{_resize_for_vision(test_img, max_px=image_max_px)}",
                               "detail": image_detail}},
                {"type": "text", "text": "Describe this in one word."},
            ]}],
            "max_tokens": 10,
        }).encode()
        req = urllib.request.Request(
            "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
            data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {ARK_API_KEY}"},
        )
        try:
            urllib.request.urlopen(req, timeout=15)
            use_ai = True
            print(f"   ✓ AI model {vision_model} OK ({image_max_px}px thumb + detail:{image_detail})")
        except Exception as e:
            print(f"   ⚠ AI model unavailable ({e}), heuristic-only")

    cats_str = ", ".join(LABEL_CATEGORIES)
    ai_labels = {}
    ai_descs = {}

    if use_ai and smart:
        quota = dict(SCENE_QUOTA)
        if quota_overrides:
            quota.update(quota_overrides)
        filled = {cat: 0 for cat in quota}
        total_quota = sum(quota.values())
        early_stopped = False

        for i, (rel, img_path) in enumerate(need_ai):
            if sum(filled.values()) >= total_quota:
                early_stopped = True
                print(f"   ⏹ quota full, skip remaining {len(need_ai) - i}")
                break

            print(f"   AI [{i+1}/{len(need_ai)}] {img_path.name}...", end="", flush=True)
            body = json.dumps({
                "model": vision_model,
                "messages": [{"role": "user", "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{_resize_for_vision(img_path, max_px=image_max_px)}",
                                   "detail": image_detail}},
                    {"type": "text",
                     "text": (
                         "你在看一张手机游戏截图。严格只回答 JSON，格式："
                         '{"label":"<分类>","desc":"<中文一句话描述, 不超过30字>"}。'
                         f"label 只能从以下列表中选一个：{cats_str}。"
                         "desc 要具体指出这是哪个玩法/界面/场景。"
                     )},
                ]}],
                "max_tokens": 120,
            }).encode()
            req = urllib.request.Request(
                "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
                data=body,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {ARK_API_KEY}"},
            )
            try:
                resp = urllib.request.urlopen(req, timeout=30)
                result = json.loads(resp.read().decode())
                tag, desc = _parse_label_desc(result["choices"][0]["message"]["content"])
                total_tokens += result.get("usage", {}).get("total_tokens", 0)
                ai_calls += 1
            except Exception:
                tag = "other"
                desc = existing_descs.get(rel) or _default_description(rel, tag)

            mapped = tag if tag in quota else "other"
            if filled.get(mapped, 0) < quota.get(mapped, 0):
                filled[mapped] = filled.get(mapped, 0) + 1
                ai_labels[rel] = tag
                if desc:
                    ai_descs[rel] = desc
                print(f" → {tag} ✓ ({filled[mapped]}/{quota[mapped]})")
            else:
                print(f" → {tag} (quota full, drop)")
            time.sleep(0.2)

        print(f"   filled: {dict(filled)}")
    elif use_ai:
        for i, (rel, img_path) in enumerate(need_ai):
            print(f"   AI [{i+1}/{len(need_ai)}] {img_path.name}...", end="", flush=True)
            body = json.dumps({
                "model": vision_model,
                "messages": [{"role": "user", "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{_resize_for_vision(img_path, max_px=image_max_px)}",
                                   "detail": image_detail}},
                    {"type": "text",
                     "text": (
                         "你在看一张手机游戏截图。严格只回答 JSON，格式："
                         '{"label":"<分类>","desc":"<中文一句话描述, 不超过30字>"}。'
                         f"label 只能从以下列表中选一个：{cats_str}。"
                         "desc 要具体指出这是哪个玩法/界面/场景。"
                     )},
                ]}],
                "max_tokens": 120,
            }).encode()
            req = urllib.request.Request(
                "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
                data=body,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {ARK_API_KEY}"},
            )
            try:
                resp = urllib.request.urlopen(req, timeout=30)
                result = json.loads(resp.read().decode())
                tag, desc = _parse_label_desc(result["choices"][0]["message"]["content"])
                total_tokens += result.get("usage", {}).get("total_tokens", 0)
                ai_calls += 1
            except Exception:
                tag = "other"
                desc = existing_descs.get(rel) or _default_description(rel, tag)
            print(f" → {tag}")
            ai_labels[rel] = tag
            if desc:
                ai_descs[rel] = desc
            time.sleep(0.2)
    else:
        for rel, _img_path in need_ai:
            ai_labels[rel] = existing_labels.get(rel, "other")
            desc = existing_descs.get(rel) or _default_description(rel, ai_labels[rel])
            if desc:
                ai_descs[rel] = desc

    labels = {}
    labels.update(existing_labels)
    labels.update(heuristic_results)
    labels.update(ai_labels)

    descriptions = {}
    descriptions.update(existing_descs)
    descriptions.update(heuristic_descs)
    descriptions.update(ai_descs)
    for rel in labels:
        descriptions.setdefault(rel, "")

    labels_path.parent.mkdir(parents=True, exist_ok=True)
    labels_path.write_text(json.dumps(labels, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    descriptions_path.write_text(json.dumps(descriptions, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    dist = _count_tags(labels)
    timeline_summary_path = write_timeline_summary(game_dir)
    mode_str = (
        "analysis-full+AI"
        if analysis_mode and use_ai
        else ("smart-quota+AI" if (use_ai and smart)
              else ("AI+heuristic" if use_ai else "heuristic"))
    )
    print(f"\n   ✓ Labeling done ({mode_str})")
    print(f"     AI calls: {ai_calls} | tokens: {total_tokens}")
    print(f"     distribution: {dist}")
    result = {"total": len(labels), "distribution": dist, "mode": mode_str,
              "ai_calls": ai_calls, "total_tokens": total_tokens,
              "descriptions_total": sum(1 for v in descriptions.values() if v)}
    if timeline_summary_path:
        result["timeline_summary"] = str(timeline_summary_path.relative_to(game_dir))
    return result


def _count_tags(labels: dict) -> dict:
    by_tag = {}
    for t in labels.values():
        by_tag[t] = by_tag.get(t, 0) + 1
    return by_tag


# ---------------------------------------------------------------------------
# P2: Image Resource List emitter (paste-ready Markdown for design_spec.md §VIII)
# ---------------------------------------------------------------------------

def emit_resource_list(game_dir: Path, project_root: Path,
                       game_name: str, out_md: Path) -> None:
    """Build a Markdown table that drops straight into design_spec.md §VIII."""
    images_root = project_root / "images" if project_root else game_dir
    rows = []

    labels_path = game_dir / "gameplay" / "labels.json"
    descriptions_path = game_dir / "gameplay" / "descriptions.json"
    labels = {}
    descriptions = {}
    if labels_path.exists():
        try:
            labels = json.loads(labels_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    if descriptions_path.exists():
        try:
            descriptions = json.loads(descriptions_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Walk store/ + gameplay/frames/
    for sub in ("store", "gameplay/frames"):
        root = game_dir / sub
        if not root.exists():
            continue
        for img in sorted(root.rglob("*.jpg")):
            rel_to_game = img.relative_to(game_dir).as_posix()
            label = labels.get(rel_to_game, "")
            try:
                from PIL import Image
                w, h = Image.open(img).size
                ratio = round(w / h, 2) if h else 0
            except Exception:
                w = h = ratio = 0
            try:
                rel_to_images = img.relative_to(images_root).as_posix()
            except ValueError:
                rel_to_images = rel_to_game
            note_parts = [game_name]
            if label:
                note_parts.append(label)
            desc = descriptions.get(rel_to_game, "").strip()
            if desc:
                note_parts.append(desc)
            rows.append({
                "filename": rel_to_images,
                "dimensions": f"{w}x{h}" if w else "?",
                "ratio": str(ratio) if ratio else "?",
                "purpose": "(to fill)",
                "type": "Photography",
                "status": "Existing",
                "notes": " / ".join(note_parts),
            })

    if not rows:
        print(f"   ⚠ no images to emit for {game_name}")
        return

    out_md.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"## VIII. Image Resource List — {game_name} (auto-collected)",
        "",
        f"> Generated by `scripts/game_assets/fetch_game_assets.py` on "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M')}.",
        f"> Source: store APIs + yt-dlp gameplay videos + Doubao Vision labels.",
        "",
        "| Filename | Dimensions | Ratio | Purpose | Type | Status | Notes |",
        "|----------|-----------|-------|---------|------|--------|-------|",
    ]
    for r in rows:
        lines.append(
            f"| `{r['filename']}` | {r['dimensions']} | {r['ratio']} | "
            f"{r['purpose']} | {r['type']} | {r['status']} | {r['notes']} |"
        )
    lines.append("")
    lines.append("> Paste this table directly into your project's `design_spec.md` Section VIII, "
                 "then fill in the **Purpose** column per slide.")
    out_md.write_text("\n".join(lines), encoding="utf-8")
    print(f"   ✓ resource list → {out_md}  ({len(rows)} rows)")


def _count_files_by_suffix(root: Path, suffixes: tuple[str, ...]) -> int:
    if not root.exists():
        return 0
    return sum(1 for path in root.rglob("*") if path.is_file() and path.suffix.lower() in suffixes)


def _load_json_len(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    return len(payload) if isinstance(payload, dict) else 0


def write_collection_summary(
    game_dir: Path,
    project_root: Path | None,
    game_name: str,
    metadata: dict[str, object],
    out_md: Path,
    resource_list_path: Path,
) -> None:
    store_root = game_dir / "store"
    gameplay_root = game_dir / "gameplay"
    videos_root = gameplay_root / "videos"
    frames_root = gameplay_root / "frames"
    labels_path = gameplay_root / "labels.json"
    descriptions_path = gameplay_root / "descriptions.json"
    frame_index_path = gameplay_root / "frame_index.json"
    timeline_summary_path = gameplay_root / "timeline_summary.md"

    store_counts: dict[str, int] = {}
    for source_dir in sorted(store_root.iterdir()) if store_root.exists() else []:
        if source_dir.is_dir():
            store_counts[source_dir.name] = _count_files_by_suffix(
                source_dir,
                (".jpg", ".jpeg", ".png", ".webp", ".mp4"),
            )

    video_count = _count_files_by_suffix(videos_root, (".mp4", ".webm", ".mkv", ".mov"))
    frame_count = _count_files_by_suffix(frames_root, (".jpg", ".jpeg", ".png"))
    labels_total = _load_json_len(labels_path)
    descriptions_total = _load_json_len(descriptions_path)

    missing: list[str] = []
    if not store_counts:
        missing.append("没有抓到任何商店素材")
    else:
        for source in ("appstore", "googleplay", "steam"):
            if store_counts.get(source, 0) == 0:
                missing.append(f"{source} 没有抓到素材")
    if video_count == 0:
        missing.append("没有下载到 gameplay 视频")
    if frame_count == 0:
        missing.append("没有抽到 gameplay 关键帧")
    if labels_total == 0:
        missing.append("没有生成 labels.json")
    if descriptions_total == 0:
        missing.append("没有生成 descriptions.json")

    lines = [
        f"# 采集摘要：{game_name}",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 素材目录：`{game_dir}`",
        f"- metadata：`{game_dir / 'metadata.json'}`",
        f"- 资源清单：`{resource_list_path}`",
    ]
    if frame_index_path.exists():
        lines.append(f"- 帧时间轴索引：`{frame_index_path}`")
    if timeline_summary_path.exists():
        lines.append(f"- 时间轴摘要：`{timeline_summary_path}`")
    if project_root is not None:
        lines.append(f"- 项目目录：`{project_root}`")

    lines.extend([
        "",
        "## 抓到了什么",
        "",
        f"- 商店来源数量：{len(store_counts)}",
    ])
    if store_counts:
        for source, count in store_counts.items():
            lines.append(f"- {source}：{count} 个文件")
    lines.extend([
        f"- gameplay 视频：{video_count} 个",
        f"- gameplay 关键帧：{frame_count} 张",
        f"- labels.json 条目：{labels_total}",
        f"- descriptions.json 条目：{descriptions_total}",
    ])
    if frame_index_path.exists():
        lines.append(f"- frame_index.json 条目：{_load_json_len(frame_index_path)}")

    stores_meta = metadata.get("stores", {})
    gameplay_meta = metadata.get("gameplay", {})
    labels_meta = metadata.get("labels", {})
    if isinstance(stores_meta, dict) and stores_meta:
        lines.extend(["", "## 来源记录", ""])
        for source, info in stores_meta.items():
            if isinstance(info, dict):
                title = (
                    info.get("trackName")
                    or info.get("title")
                    or info.get("name")
                    or "(未命名)"
                )
                lines.append(f"- {source}：{title}")
    if isinstance(gameplay_meta, dict) and gameplay_meta:
        lines.extend(["", "## 视频链路", ""])
        total_frames = gameplay_meta.get("total_frames", frame_count)
        lines.append(f"- 总关键帧：{total_frames}")
        videos = gameplay_meta.get("videos") or []
        if isinstance(videos, list):
            lines.append(f"- 视频条目：{len(videos)}")
            for item in videos:
                if not isinstance(item, dict):
                    continue
                video_name = item.get("filename", "(unknown)")
                detected = item.get("video_type", "-")
                used_mode = item.get("used_mode", item.get("detected_mode", "-"))
                reason = item.get("detection_reason", "-")
                duration = item.get("duration_sec", "-")
                lines.append(
                    f"  - {video_name} | type={detected} | mode={used_mode} | reason={reason} | duration={duration}"
                )
    if isinstance(labels_meta, dict) and labels_meta:
        lines.extend(["", "## 标签链路", ""])
        lines.append(f"- 模式：{labels_meta.get('mode', '-')}")
        lines.append(f"- 标签总数：{labels_meta.get('total', labels_total)}")
        lines.append(f"- 描述总数：{labels_meta.get('descriptions_total', descriptions_total)}")

    lines.extend(["", "## 还缺什么", ""])
    if missing:
        for item in missing:
            lines.append(f"- {item}")
    else:
        lines.append("- 关键素材已经齐全。")

    lines.extend(["", "## 下一步建议", ""])
    if frame_count == 0 and video_count == 0:
        lines.append("- 如果自动搜不到视频，下一次直接加 `--video <URL_OR_ID>`。")
    if descriptions_total == 0:
        lines.append("- 如果你需要中文画面描述，请确认 `ARK_API_KEY` 已配置并重新加 `--label`。")
    if store_counts.get("googleplay", 0) == 0:
        lines.append("- 如果目标是 Google Play，建议显式传 `--gplay-id`。")
    if not missing:
        lines.append("- 可以直接把这批素材交给 `game-review` 或 `game-ppt-master` 继续使用。")

    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"   ✓ summary → {out_md}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def list_games(assets_dir: Path):
    if not assets_dir.exists():
        print("📂 asset library is empty")
        return
    games = sorted(d for d in assets_dir.iterdir() if d.is_dir())
    if not games:
        print("📂 asset library is empty")
        return
    print(f"\n📂 {len(games)} games collected at {assets_dir}:\n")
    for gdir in games:
        meta_path = gdir / "metadata.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text("utf-8"))
                stores = list(meta.get("stores", {}).keys())
                gameplay = meta.get("gameplay", {})
                frames = gameplay.get("total_frames", 0)
                label_count = meta.get("labels", {}).get("total", 0)
                print(f"  🎮 {gdir.name}")
                print(f"     stores: {', '.join(stores) if stores else '-'}")
                print(f"     frames: {frames}  labels: {label_count}")
                print(f"     collected: {meta.get('collected_at', '?')}")
            except Exception as e:
                print(f"  🎮 {gdir.name}  (metadata broken: {e})")
        else:
            print(f"  🎮 {gdir.name}  (no metadata)")
        print()


def _resolve_game_dir(args, game_name: str) -> tuple[Path, Path]:
    """Resolve where to land assets for this game.

    Returns (game_dir, project_root_or_None).
    """
    dir_name = _sanitize(game_name)
    if args.project:
        project_root = Path(args.project).resolve()
        if not project_root.exists():
            sys.exit(f"[fatal] --project not found: {project_root}")
        # land in <project>/images/_game_assets/<game>/
        game_dir = project_root / "images" / "_game_assets" / dir_name
    elif args.out:
        out_root = Path(args.out).resolve()
        game_dir = out_root / dir_name
        project_root = None
    else:
        game_dir = _default_assets_dir() / dir_name
        project_root = None
    game_dir.mkdir(parents=True, exist_ok=True)
    return game_dir, project_root


def main():
    parser = argparse.ArgumentParser(description="Game asset auto-collector")
    parser.add_argument("game", nargs="?", help="game name")
    parser.add_argument("--list", action="store_true", help="list collected games")
    parser.add_argument("--doctor", action="store_true", help="检查环境、依赖和 API key")
    parser.add_argument("--project",
                        help="consumer project root (lands assets in <project>/images/_game_assets/<game>/)")
    parser.add_argument("--out",
                        help="custom output root (overrides --project layout, used for batch shared library)")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--store-only", action="store_true", help="store screenshots only")
    mode.add_argument("--gameplay-only", action="store_true", help="gameplay video frames only")
    mode.add_argument("--label-only", action="store_true", help="(re-)label existing files only")
    mode.add_argument("--emit-list-only", action="store_true",
                      help="just emit Image Resource List from existing files")

    parser.add_argument("--label", action="store_true", help="run AI labeling after collection")
    parser.add_argument("--force", action="store_true", help="ignore existing labels.json")
    parser.add_argument("--model", choices=["lite", "heavy"], default="lite",
                        help="vision model: lite=token-saving (default), heavy=high-precision")
    parser.add_argument("--keep-video", action="store_true", help="keep source mp4")
    parser.add_argument(
        "--analysis",
        action="store_true",
        help="force walkthrough-style dense timeline extraction + full gameplay labeling",
    )
    parser.add_argument(
        "--analysis-interval",
        type=int,
        default=None,
        help="when analysis mode is used, override the adaptive timeline cadence with a fixed 1 frame per N seconds",
    )

    parser.add_argument("--no-smart", action="store_true",
                        help="legacy: scene-detect full frames (no dedup)")
    parser.add_argument("--scene", action="store_true",
                        help="force trailer-style scene-detect + pHash dedup")
    parser.add_argument("--frame-interval", type=int, default=5,
                        help="sparse mode: 1 frame per N seconds (default 5)")

    for cat in SCENE_QUOTA:
        parser.add_argument(f"--{cat}", type=int, default=None, metavar="N",
                            help=f"quota override for {cat} (default {SCENE_QUOTA[cat]})")

    parser.add_argument("--appstore-id", help="App Store ID")
    parser.add_argument("--gplay-id", help="Google Play package name")
    parser.add_argument("--steam-id", help="Steam App ID")
    parser.add_argument("--max-videos", type=int, default=3,
                        help="max gameplay videos (default 3)")
    parser.add_argument(
        "--video",
        action="append",
        default=[],
        metavar="URL_OR_ID",
        help="manual gameplay video target (repeatable). Accepts full URL, YouTube ID, or Bilibili BV id. When set, skip auto-search.",
    )
    parser.add_argument("--scene-threshold", type=float, default=0.3,
                        help="scene-detect threshold (default 0.3)")

    args = parser.parse_args()
    if args.analysis_interval is not None and args.analysis_interval < 1:
        parser.error("--analysis-interval must be >= 1")

    if args.doctor:
        return _run_doctor()

    # --list resolves with project / out / default fallback
    if args.list:
        if args.project:
            list_games(Path(args.project).resolve() / "images" / "_game_assets")
        elif args.out:
            list_games(Path(args.out).resolve())
        else:
            list_games(_default_assets_dir())
        return

    if not args.game:
        parser.error("specify a game name, or use --list")

    game_name = args.game
    game_dir, project_root = _resolve_game_dir(args, game_name)
    print(f"🎮 Collecting: {game_name}")
    print(f"   game_dir: {game_dir}")
    if project_root:
        print(f"   project: {project_root}")

    metadata = {"game_name": game_name,
                "collected_at": datetime.now().isoformat(),
                "stores": {}}

    if args.emit_list_only:
        meta_dir = (project_root / "images" / "_game_assets_meta") if project_root else (game_dir / "meta")
        emit_resource_list(game_dir, project_root, game_name,
                           meta_dir / f"{_sanitize(game_name)}.image_resource_list.md")
        return

    if args.label_only:
        vision_model = VISION_MODEL_HEAVY if args.model == "heavy" else VISION_MODEL_LITE
        quota_overrides = {cat: getattr(args, cat.replace("-", "_"))
                           for cat in SCENE_QUOTA
                           if getattr(args, cat.replace("-", "_"), None) is not None}
        effective_analysis_mode = args.analysis or gameplay_uses_analysis_mode({}, game_dir=game_dir)
        label_meta = label_frames(game_dir, force=args.force, model=vision_model,
                                  smart=not args.no_smart and not effective_analysis_mode,
                                  quota_overrides=quota_overrides or None,
                                  analysis_mode=effective_analysis_mode)
        metadata["labels"] = label_meta
    else:
        # P0: store screenshots
        if not args.gameplay_only:
            store_meta = run_store(game_name, game_dir, args)
            metadata["stores"] = store_meta.get("stores", {})

        smart = not args.no_smart
        scene_mode = bool(args.scene)
        effective_analysis_mode = bool(args.analysis)

        # P1: gameplay video frames
        if not args.store_only:
            gp_meta = fetch_gameplay(
                game_name, game_dir,
                max_videos=args.max_videos,
                scene_threshold=args.scene_threshold,
                keep_video=args.keep_video,
                smart=smart,
                frame_interval=args.frame_interval,
                scene_mode=scene_mode,
                analysis_mode=args.analysis,
                analysis_interval=args.analysis_interval,
                manual_targets=args.video,
            )
            metadata["gameplay"] = gp_meta
            effective_analysis_mode = effective_analysis_mode or gameplay_uses_analysis_mode(gp_meta, game_dir=game_dir)

        # P1+: AI labeling
        if args.label:
            vision_model = VISION_MODEL_HEAVY if args.model == "heavy" else VISION_MODEL_LITE
            quota_overrides = {cat: getattr(args, cat.replace("-", "_"))
                               for cat in SCENE_QUOTA
                               if getattr(args, cat.replace("-", "_"), None) is not None}
            label_meta = label_frames(game_dir, force=args.force, model=vision_model,
                                      smart=smart and not effective_analysis_mode,
                                      quota_overrides=quota_overrides or None,
                                      analysis_mode=effective_analysis_mode)
            metadata["labels"] = label_meta

    # Persist metadata
    meta_path = game_dir / "metadata.json"
    meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2),
                         encoding="utf-8")

    # P2: emit Image Resource List
    if project_root:
        meta_dir = project_root / "images" / "_game_assets_meta"
    else:
        meta_dir = game_dir / "meta"
    resource_list_path = meta_dir / f"{_sanitize(game_name)}.image_resource_list.md"
    emit_resource_list(game_dir, project_root, game_name, resource_list_path)
    write_collection_summary(
        game_dir,
        project_root,
        game_name,
        metadata,
        meta_dir / f"{_sanitize(game_name)}.collection_summary.md",
        resource_list_path,
    )

    total_files = sum(1 for _ in game_dir.rglob("*")
                      if _.is_file() and _.name != "metadata.json")
    print(f"\n{'=' * 60}")
    print(f"✅ Done: {game_name}")
    print(f"   game_dir: {game_dir}")
    print(f"   files: {total_files}")
    print(f"   metadata: {meta_path}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    raise SystemExit(main())
