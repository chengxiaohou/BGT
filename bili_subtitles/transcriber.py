import datetime
import html
import sys
import requests


def download_audio_directly(audio_url: str, output_path: str, show_progress: bool = True) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com/"
    }
    
    response = requests.get(audio_url, headers=headers, stream=True)
    response.raise_for_status()
    
    total_size = int(response.headers.get('content-length', 0))
    downloaded = 0
    
    with open(output_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
            downloaded += len(chunk)
            if show_progress and total_size > 0:
                percent = (downloaded / total_size) * 100
                sys.stdout.write(f"\r下载进度: {percent:.1f}%")
                sys.stdout.flush()
    
    if show_progress:
        print()  # 换行
    return output_path


def build_srt_text(segments: list) -> str:
    """把片段列表拼成 SRT 格式的完整文本（与 save_as_srt 写入内容一致）。"""
    lines = []
    for i, segment in enumerate(segments, 1):
        lines.append(str(i))
        lines.append(f'{format_timestamp(segment["start"])} --> {format_timestamp(segment["end"])}')
        lines.append(segment["text"])
        lines.append("")
    return "\n".join(lines)


def save_as_srt(segments: list, output_path: str):
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(build_srt_text(segments))


def save_as_html(srt_text: str, output_path: str, title: str = ""):
    """把原始 SRT 文本完整、原样以普通文字展示在 HTML 网页中。

    不做任何格式转换，仅 html.escape 保证安全；用 lang="ja" 便于浏览器自带翻译。
    """
    escaped_title = html.escape(title or "字幕")
    escaped_srt = html.escape(srt_text)
    html_doc = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escaped_title}</title>
<style>
  body {{ font-family: -apple-system, "PingFang SC", "Hiragino Sans", sans-serif; max-width: 720px; margin: 2rem auto; padding: 0 1rem; color: #222; }}
  h1 {{ font-size: 1.1rem; }}
  pre {{ white-space: pre-wrap; word-break: break-word; font-size: 1rem; line-height: 1.8; }}
</style>
</head>
<body>
<h1>{escaped_title}</h1>
<pre>{escaped_srt}</pre>
</body>
</html>"""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_doc)


def format_timestamp(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def sanitize_filename(filename: str) -> str:
    """移除文件名中不合法的字符"""
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        filename = filename.replace(char, '_')
    return filename


def build_output_filename(title: str, uploader: str = None, pubdate=None) -> str:
    """按「发布日期_UP名_标题」拼接输出文件名，并清理非法字符。"""
    parts = []
    if pubdate:
        parts.append(datetime.datetime.fromtimestamp(pubdate).strftime("%Y-%m-%d"))
    if uploader:
        parts.append(uploader)
    parts.append(title)
    return sanitize_filename("_".join(part for part in parts if part))


def build_txt_header(title: str, uploader: str = None, pubdate=None) -> str:
    """生成 txt 文件开头的信息头：标题 / 日期时间 / 发布者。"""
    lines = []
    if title:
        lines.append(f"标题：{title}")
    if pubdate:
        lines.append(f"日期：{datetime.datetime.fromtimestamp(pubdate).strftime('%Y-%m-%d %H:%M')}")
    if uploader:
        lines.append(f"发布者：{uploader}")
    return "\n".join(lines) + "\n\n"
