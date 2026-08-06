import re
import json
import os
import glob
import shutil
import tempfile

import requests
import yt_dlp
from yt_dlp.cookies import extract_cookies_from_browser
from bs4 import BeautifulSoup
from typing import Optional, Dict, Any, List


_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# 字幕语言优先级：手动 CC 字幕优先，AI 字幕兜底
SUBTITLE_LANG_PRIORITY = ["zh-CN", "zh-Hans", "zh", "ai-zh", "zh-Hant"]

SUBTITLE_LANG_NAMES = {
    "zh-CN": "简体中文 CC 字幕",
    "zh-Hans": "简体中文字幕",
    "zh": "中文字幕",
    "ai-zh": "AI 字幕（中文）",
    "zh-Hant": "繁体中文字幕",
}

# 自动读取登录态的浏览器优先级（Safari 优先）
BROWSER_PRIORITY = ["safari", "chrome", "edge", "firefox", "brave", "chromium"]

BROWSER_INSTALL_HINTS = {
    "safari": ["/Applications/Safari.app"],
    "chrome": ["/Applications/Google Chrome.app", os.path.expanduser("~/.config/google-chrome")],
    "edge": ["/Applications/Microsoft Edge.app", os.path.expanduser("~/.config/microsoft-edge")],
    "firefox": ["/Applications/Firefox.app", os.path.expanduser("~/.mozilla/firefox")],
    "brave": ["/Applications/Brave Browser.app", os.path.expanduser("~/.config/BraveSoftware/Brave-Browser")],
    "chromium": ["/Applications/Chromium.app", os.path.expanduser("~/.config/chromium")],
}


def detect_installed_browsers() -> List[str]:
    """按优先级返回本机已安装的浏览器（用于自动读取登录态）。"""
    return [
        name for name in BROWSER_PRIORITY
        if any(os.path.exists(path) for path in BROWSER_INSTALL_HINTS[name])
    ]


def extract_bvid(url: str) -> str:
    pattern = r'BV[a-zA-Z0-9]+'
    match = re.search(pattern, url)
    if not match:
        raise ValueError("无法从URL中提取BV号")
    return match.group()


def get_video_info(bvid: str) -> Dict[str, Any]:
    url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
    headers = _HEADERS
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    data = response.json()
    if data.get("code") != 0:
        raise ValueError(f"获取视频信息失败: {data.get('message', '未知错误')}")
    return data["data"]


def get_subtitle_urls(bvid: str, cid: int) -> List[Dict[str, str]]:
    url = f"https://api.bilibili.com/x/player/v2?bvid={bvid}&cid={cid}"
    headers = _HEADERS
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    data = response.json()
    if data.get("code") != 0:
        return []
    
    subtitles = []
    if "subtitle" in data["data"] and "subtitles" in data["data"]["subtitle"]:
        for sub in data["data"]["subtitle"]["subtitles"]:
            subtitles.append({
                "lang": sub.get("lan", "unknown"),
                "lang_name": sub.get("lan_doc", "未知"),
                "url": sub.get("subtitle_url", "")
            })
    return subtitles


def fetch_subtitle(url: str) -> str:
    # B 站有时返回 "//i0.hdslb.com/..." 这种不带协议头的地址
    if url.startswith("//"):
        url = "https:" + url

    headers = _HEADERS
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    data = response.json()
    
    text_lines = []
    for item in data.get("body", []):
        if "content" in item:
            text_lines.append(item["content"])
    return "\n".join(text_lines)


def get_audio_url(bvid: str, cid: int) -> Optional[str]:
    url = f"https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&qn=120&fnval=80"
    headers = _HEADERS
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    data = response.json()
    
    if data.get("code") != 0:
        return None
    
    if "data" in data and "dash" in data["data"]:
        for stream in data["data"]["dash"].get("audio", []):
            return stream.get("baseUrl")
    return None


def download_subtitles_with_ytdlp(
    bvid: str,
    cookies_file: Optional[str] = None,
    browser: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """用 yt-dlp 抓取 B 站官方/自动字幕（可带登录 Cookie 获取 AI 字幕）。

    cookies_file 传 Cookie 文件路径，browser 传浏览器名（如 "chrome"）自动读取登录态，
    二选一，都不传则视为无登录态。返回 {"lang", "lang_name", "content"}，找不到字幕返回 None。
    """
    if cookies_file and browser:
        raise ValueError("cookies_file 和 browser 不能同时指定")

    url = f"https://www.bilibili.com/video/{bvid}"
    work_dir = tempfile.mkdtemp(prefix="bili_subs_")
    opts = {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": SUBTITLE_LANG_PRIORITY,
        "playlist_items": "1",  # 与工具其余部分一致，只处理第一 P
        "outtmpl": os.path.join(work_dir, "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "fragment_retries": 3,
        "http_headers": _HEADERS,
    }
    if cookies_file:
        opts["cookiefile"] = cookies_file
    elif browser:
        opts["cookiesfrombrowser"] = (browser,)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as exc:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise RuntimeError(f"yt-dlp 抓取字幕失败：{exc}") from exc

    srt_files = sorted(glob.glob(os.path.join(work_dir, "*.srt")))
    if not srt_files:
        shutil.rmtree(work_dir, ignore_errors=True)
        return None

    chosen = None
    for lang in SUBTITLE_LANG_PRIORITY:
        for path in srt_files:
            parts = os.path.basename(path)[:-4].split(".")
            if len(parts) >= 2 and parts[-1] == lang:
                chosen = (lang, path)
                break
        if chosen:
            break
    if not chosen:
        parts = os.path.basename(srt_files[0])[:-4].split(".")
        chosen = (parts[-1], srt_files[0])

    with open(chosen[1], encoding="utf-8") as f:
        content = f.read()
    shutil.rmtree(work_dir, ignore_errors=True)

    return {
        "lang": chosen[0],
        "lang_name": SUBTITLE_LANG_NAMES.get(chosen[0], f"字幕（{chosen[0]}）"),
        "content": content,
    }


def export_browser_cookies(browser: str, output_path: str) -> int:
    """把指定浏览器的 Cookie 导出为 Netscape 格式文件（可配合 --cookies 长期使用），返回导出条数。"""
    jar = extract_cookies_from_browser(browser)
    jar.save(filename=output_path, ignore_discard=True, ignore_expires=True)
    return len(jar)


def srt_to_text(srt_content: str) -> str:
    """把 SRT 字幕内容转成纯文本（去掉序号和时间轴）。"""
    text_lines = []
    for block in re.split(r"\n\s*\n", srt_content.strip()):
        block_lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(block_lines) < 3:
            continue
        if not block_lines[0].isdigit():
            continue
        if "-->" not in block_lines[1]:
            continue
        text = " ".join(block_lines[2:])
        if text:
            text_lines.append(text)
    return "\n".join(text_lines)
