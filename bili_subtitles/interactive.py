"""B站视频字幕工具 - 交互模式（中文界面）。

运行方式：
    python -m bili_subtitles.interactive
"""

import json
import os
import subprocess
import sys

from .cli import process_video
from .transcriber import save_as_srt, build_output_filename, build_txt_header


OUTPUT_DIR = "output"
OUTPUT_MODES = {"txt": "仅txt", "srt": "仅srt", "both": "txt + srt"}
SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".bili-subtitles.json")


BANNER = """==============================================
    B站视频字幕工具 · 交互模式
==============================================
  1. 设置登录Cookie
  2. 设置输出格式
  3. 查看帮助
  4. 退出
=============================================="""

HELP = """可用命令:
  直接粘贴B站链接   生成字幕，保存到 output/
  1. 设置登录Cookie  指定 cookies.txt 路径，用于获取AI字幕
  2. 设置输出格式   仅txt / 仅srt / 两者都要
  3. 查看帮助
  4. 退出"""


def _default_cookies() -> str:
    """自动使用当前目录下的 cookies.txt（如果存在）。"""
    path = os.path.join(os.getcwd(), "cookies.txt")
    return path if os.path.isfile(path) else None


def _load_settings() -> dict:
    """读取本地保存的设置（输出格式、Cookie路径等）。"""
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_settings(settings: dict) -> None:
    """把设置保存到本地文件，让下次启动能记住。"""
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _reveal_in_finder(path: str) -> bool:
    """在 macOS 访达中打开并选中该文件，成功返回 True。"""
    if sys.platform != "darwin":
        return False
    try:
        subprocess.run(["open", "-R", path], check=True)
        return True
    except Exception:
        return False


def _save_results(
    text: str,
    segments,
    srt_content,
    title: str,
    uploader: str,
    pubdate,
    output_mode: str,
):
    """按输出模式保存文件，返回 (txt_path, srt_path)，不打印路径。"""
    out_dir = os.path.join(os.getcwd(), OUTPUT_DIR)
    os.makedirs(out_dir, exist_ok=True)
    safe_title = build_output_filename(title, uploader, pubdate)

    txt_path = None
    if output_mode in ("txt", "both"):
        txt_path = os.path.join(out_dir, f"{safe_title}.txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(build_txt_header(title, uploader, pubdate) + text)

    srt_path = None
    if output_mode in ("srt", "both") and (segments or srt_content):
        srt_path = os.path.join(out_dir, f"{safe_title}.srt")
        if segments:
            save_as_srt(segments, srt_path)
        else:
            with open(srt_path, "w", encoding="utf-8") as f:
                f.write(srt_content)
    return txt_path, srt_path


def main():
    print(BANNER)
    settings = _load_settings()
    output_mode = settings.get("output_mode", "both")
    if output_mode not in OUTPUT_MODES:
        output_mode = "both"
    cookies_file = settings.get("cookies_file")
    if not cookies_file or not os.path.isfile(cookies_file):
        cookies_file = _default_cookies()
    if cookies_file:
        print("  [登录] 已使用Cookie")
    else:
        print("  [登录] 未设置Cookie，AI字幕可能获取不到")
    print(f"  [输出] {OUTPUT_MODES[output_mode]}")
    print()

    while True:
        try:
            command = input("请输入指令或B站视频链接> ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n再见！")
            return

        if not command:
            continue

        lower = command.lower()
        if lower in ("4", "q", "quit", "exit", "退出"):
            print("再见！")
            return
        if lower in ("3", "help", "帮助", "h"):
            print(HELP)
            continue
        if lower == "1":
            path = input("请输入Cookie文件路径> ").strip().strip("\"'")
            if os.path.isfile(path):
                cookies_file = path
                settings["cookies_file"] = path
                _save_settings(settings)
                print("  ✓ 已设置登录Cookie")
            else:
                print(f"  ✗ 找不到文件: {path}")
            continue
        if lower == "2":
            choice = input("请选择输出格式（1=仅txt 2=仅srt 3=两者都要）> ").strip()
            mapping = {"1": "txt", "2": "srt", "3": "both"}
            if choice in mapping:
                output_mode = mapping[choice]
                settings["output_mode"] = output_mode
                _save_settings(settings)
                print(f"  ✓ 已设置输出: {OUTPUT_MODES[output_mode]}")
            else:
                print("  ✗ 无效选择，请输入 1、2 或 3")
            continue
        if command.isdigit():
            print("  ✗ 无效指令，请输入 1-4，或直接粘贴B站视频链接")
            continue

        print()
        try:
            text, segments, srt_content, title, uploader, pubdate = process_video(
                command, cookies_file=cookies_file, show_progress=True
            )
            print()
            txt_path, srt_path = _save_results(
                text, segments, srt_content, title, uploader, pubdate, output_mode
            )
            reveal_path = txt_path or srt_path
            if reveal_path and not _reveal_in_finder(reveal_path):
                print(f"  ✓ 字幕已生成: {os.path.basename(reveal_path)}（位于 {OUTPUT_DIR}/ 目录）")
            elif reveal_path:
                print(f"  ✓ 字幕已生成: {os.path.basename(reveal_path)}")
            elif output_mode == "srt":
                print("  ⚠ 该视频没有可用的SRT时间轴数据，未能生成SRT")
            print()
        except Exception as exc:
            print(f"  ✗ 处理失败: {exc}")
            print()


if __name__ == "__main__":
    main()
