"""B站视频字幕工具 - Web 应用。

提供网页界面，支持粘贴 B站链接获取 AI 字幕，支持 Cookie 上传以获取登录态。
"""

import os
import tempfile
import shutil

from flask import Flask, render_template, request, jsonify

from .bilibili import (
    extract_bvid,
    get_video_info,
    get_subtitle_urls,
    fetch_subtitle,
    get_audio_url,
    download_subtitles_with_ytdlp,
    detect_installed_browsers,
    srt_to_text,
)
from .transcriber import build_output_filename, build_txt_header

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024  # Cookie 文件最大 1MB


def _process_video_web(url: str, cookies_file: str = None) -> dict:
    """Web 版视频处理逻辑，返回结果字典，不依赖 click.echo。

    返回:
        {
            "success": bool,
            "messages": [str, ...],     # 处理过程中的日志信息
            "text": str,                # 纯文本字幕
            "srt_content": str,         # SRT 格式字幕
            "title": str,
            "uploader": str,
            "pubdate": int,
            "error": str,               # 错误信息（仅 success=False 时）
        }
    """
    messages = []
    result = {
        "success": True,
        "messages": messages,
        "text": "",
        "srt_content": "",
        "title": "",
        "uploader": "",
        "pubdate": None,
        "error": "",
    }

    try:
        bvid = extract_bvid(url)
        messages.append(f"提取到 BV 号: {bvid}")

        video_info = get_video_info(bvid)
        title = video_info.get("title", "未知标题")
        cid = video_info.get("cid")
        messages.append(f"视频标题: {title}")
        result["title"] = title
        result["uploader"] = (video_info.get("owner") or {}).get("name", "")
        result["pubdate"] = video_info.get("pubdate")

        # 1) 优先尝试 yt-dlp 抓取 AI 字幕
        sub = None
        if cookies_file:
            messages.append("正在使用上传的 Cookie 尝试获取 AI 字幕…")
            try:
                sub = download_subtitles_with_ytdlp(bvid, cookies_file=cookies_file)
            except RuntimeError as exc:
                messages.append(f"Cookie 方式失败：{exc}")
        else:
            # 检测本机浏览器（在服务器上通常无可用浏览器，跳过）
            messages.append("未提供 Cookie，尝试获取公开字幕…")

        if sub:
            messages.append(f"使用字幕: {sub['lang_name']}")
            result["srt_content"] = sub["content"]
            result["text"] = srt_to_text(sub["content"])
        else:
            # 2) 尝试手动 CC 字幕接口
            subtitles = get_subtitle_urls(bvid, cid)
            if subtitles:
                lang_names = [s["lang_name"] for s in subtitles]
                messages.append(f"发现公开字幕: {', '.join(lang_names)}")
                chinese_sub = next(
                    (s for s in subtitles if s["lang"] in ("zh-CN", "zh")), None
                )
                sub = chinese_sub or subtitles[0]
                messages.append(f"使用字幕: {sub['lang_name']}")
                result["text"] = fetch_subtitle(sub["url"])
            else:
                # 3) 尝试语音识别
                messages.append("未发现字幕，尝试语音识别…")
                audio_url = get_audio_url(bvid, cid)
                if audio_url:
                    try:
                        from .asr_sherpa import extract_audio_and_transcribe_paraformer

                        text, segments = extract_audio_and_transcribe_paraformer(
                            audio_url, show_progress=False
                        )
                        result["text"] = text
                        # 有 segments 时可以生成 SRT
                        if segments:
                            from .transcriber import format_timestamp

                            srt_lines = []
                            for i, seg in enumerate(segments, 1):
                                start = format_timestamp(seg["start"])
                                end = format_timestamp(seg["end"])
                                srt_lines.append(f"{i}\n{start} --> {end}\n{seg['text']}\n")
                            result["srt_content"] = "\n".join(srt_lines) + "\n"
                        messages.append("语音识别完成")
                    except ImportError:
                        messages.append("语音识别模块未安装（sherpa-onnx），跳过")
                        result["error"] = "没有可用的字幕，且语音识别模块未安装"
                        result["success"] = False
                else:
                    result["error"] = "无法获取音频链接，且没有可用的字幕"
                    result["success"] = False

    except ValueError as e:
        result["success"] = False
        result["error"] = str(e)
        messages.append(f"输入解析失败: {e}")
    except Exception as e:
        result["success"] = False
        result["error"] = f"处理出错: {e}"
        messages.append(f"处理出错: {e}")

    return result


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/extract", methods=["POST"])
def extract():
    url = request.form.get("url", "").strip()
    if not url:
        return jsonify({"success": False, "error": "请输入 B站视频链接"})

    # 处理上传的 Cookie 文件
    cookies_file = None
    cleanup_cookies = False
    uploaded = request.files.get("cookies")
    if uploaded and uploaded.filename:
        tmp = tempfile.NamedTemporaryFile(
            prefix="bili_cookies_", suffix=".txt", delete=False, mode="wb"
        )
        uploaded.save(tmp.name)
        tmp.close()
        cookies_file = tmp.name
        cleanup_cookies = True

    try:
        result = _process_video_web(url, cookies_file=cookies_file)
        if result["success"] and result["text"]:
            # 构建下载文件名
            safe_name = build_output_filename(
                result["title"], result["uploader"], result["pubdate"]
            )
            result["download_name"] = safe_name
        return jsonify(result)
    finally:
        if cleanup_cookies and cookies_file:
            try:
                os.unlink(cookies_file)
            except OSError:
                pass


def main():
    """启动 Web 服务器（开发用）。"""
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)


if __name__ == "__main__":
    main()