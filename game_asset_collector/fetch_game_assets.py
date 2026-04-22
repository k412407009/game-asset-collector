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
    (also falls back to current process env vars, sibling ppt-master/.env,
     and sibling personal-assistant/.baoyu-skills/.env for local compatibility)
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

import argparse
import base64
import json
import os
import re
import subprocess
import sys
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
    candidates.append(workspace_root / "ppt-master" / ".env")
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


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


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

def _phash(img_path: Path, hash_size: int = 8):
    try:
        from PIL import Image
        img = Image.open(img_path).convert('L').resize(
            (hash_size + 1, hash_size), Image.LANCZOS)
        pixels = list(img.getdata())
        avg = sum(pixels) / len(pixels)
        return sum(1 << i for i, p in enumerate(pixels) if p > avg)
    except Exception:
        return None


def _hamming(h1: int, h2: int) -> int:
    return bin(h1 ^ h2).count('1')


def deduplicate_frames(frames_dir: Path, threshold: int = 8) -> int:
    print("   🧹 pHash perceptual dedup...")
    all_frames = []
    for d in sorted(frames_dir.iterdir()):
        if d.is_dir():
            all_frames.extend(sorted(d.glob("*.jpg")))
    if not all_frames:
        return 0
    hashes, removed = [], 0
    for fpath in all_frames:
        h = _phash(fpath)
        if h is None:
            continue
        if any(_hamming(h, eh) < threshold for eh in hashes):
            fpath.unlink()
            removed += 1
        else:
            hashes.append(h)
    print(f"      scanned {len(all_frames)}, removed {removed}, kept {len(all_frames) - removed}")
    return removed


# ---------------------------------------------------------------------------
# P1: Gameplay video download + frame extraction
# ---------------------------------------------------------------------------

def fetch_gameplay(game_name: str, game_dir: Path, max_videos: int = 3,
                   scene_threshold: float = 0.3, keep_video: bool = False,
                   smart: bool = True, frame_interval: int = 5,
                   scene_mode: bool = False,
                   manual_targets: list[str] | None = None) -> dict:
    """yt-dlp YouTube/Bilibili search → download → ffmpeg frame extraction.

    scene_mode=True : ffmpeg scene-detection (content-driven) + pHash dedup
    smart=True (default) : sparse sampling (1 frame per N seconds) + pHash dedup
    smart=False / no-smart : legacy ffmpeg scene-detection without dedup
    """
    if scene_mode:
        mode_str = f"scene-detect+dedup th{scene_threshold}"
    elif smart:
        mode_str = f"sparse 1/{frame_interval}s+dedup"
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

    for vpath in all_videos:
        vname = vpath.stem
        vframes_dir = frames_dir / _sanitize(vname)
        vframes_dir.mkdir(parents=True, exist_ok=True)

        if scene_mode:
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
        })
        print(f"      → {len(extracted)} frames")

    dedup_removed = 0
    if (smart or scene_mode) and total_frames > 0:
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
    mode_label = ("scene+dedup" if scene_mode
                  else ("smart" if smart else "scene"))
    return {"videos": video_info, "total_frames": total_frames,
            "dedup_removed": dedup_removed, "mode": mode_label}


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


def label_frames(game_dir: Path, force: bool = False, model: str = None,
                 smart: bool = True, quota_overrides: dict = None) -> dict:
    """AI-label gameplay frames + store screenshots.

    smart=True (default): quota-based early stop, only labels until quotas filled
    smart=False: heuristic-first, send everything uncertain to AI
    """
    mode_label = "smart-quota" if smart else "full"
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
    for img_path in all_frames:
        rel = str(img_path.relative_to(game_dir))
        if rel in existing_labels and rel in existing_descs and existing_descs.get(rel) and not force:
            skipped += 1
            continue
        h_label = _heuristic_label(img_path)
        if h_label is not None:
            heuristic_results[rel] = h_label
            desc = existing_descs.get(rel) or _default_description(rel, h_label)
            if desc:
                heuristic_descs[rel] = desc
        else:
            need_ai.append((rel, img_path))

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
        body = json.dumps({
            "model": vision_model,
            "messages": [{"role": "user", "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{_resize_for_vision(test_img)}",
                               "detail": "low"}},
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
            print(f"   ✓ AI model {vision_model} OK (256px thumb + detail:low)")
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
                     "image_url": {"url": f"data:image/jpeg;base64,{_resize_for_vision(img_path)}",
                                   "detail": "low"}},
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
                     "image_url": {"url": f"data:image/jpeg;base64,{_resize_for_vision(img_path)}",
                                   "detail": "low"}},
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
    mode_str = ("smart-quota+AI" if (use_ai and smart)
                else ("AI+heuristic" if use_ai else "heuristic"))
    print(f"\n   ✓ Labeling done ({mode_str})")
    print(f"     AI calls: {ai_calls} | tokens: {total_tokens}")
    print(f"     distribution: {dist}")
    return {"total": len(labels), "distribution": dist, "mode": mode_str,
            "ai_calls": ai_calls, "total_tokens": total_tokens,
            "descriptions_total": sum(1 for v in descriptions.values() if v)}


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

    parser.add_argument("--no-smart", action="store_true",
                        help="legacy: scene-detect full frames (no dedup)")
    parser.add_argument("--scene", action="store_true",
                        help="recommended: scene-detect + pHash dedup (default = sparse sampling)")
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
        label_meta = label_frames(game_dir, force=args.force, model=vision_model,
                                  smart=not args.no_smart,
                                  quota_overrides=quota_overrides or None)
        metadata["labels"] = label_meta
    else:
        # P0: store screenshots
        if not args.gameplay_only:
            store_meta = run_store(game_name, game_dir, args)
            metadata["stores"] = store_meta.get("stores", {})

        smart = not args.no_smart
        scene_mode = bool(args.scene)

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
                manual_targets=args.video,
            )
            metadata["gameplay"] = gp_meta

        # P1+: AI labeling
        if args.label:
            vision_model = VISION_MODEL_HEAVY if args.model == "heavy" else VISION_MODEL_LITE
            quota_overrides = {cat: getattr(args, cat.replace("-", "_"))
                               for cat in SCENE_QUOTA
                               if getattr(args, cat.replace("-", "_"), None) is not None}
            label_meta = label_frames(game_dir, force=args.force, model=vision_model,
                                      smart=smart,
                                      quota_overrides=quota_overrides or None)
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
    emit_resource_list(game_dir, project_root, game_name,
                       meta_dir / f"{_sanitize(game_name)}.image_resource_list.md")

    total_files = sum(1 for _ in game_dir.rglob("*")
                      if _.is_file() and _.name != "metadata.json")
    print(f"\n{'=' * 60}")
    print(f"✅ Done: {game_name}")
    print(f"   game_dir: {game_dir}")
    print(f"   files: {total_files}")
    print(f"   metadata: {meta_path}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
