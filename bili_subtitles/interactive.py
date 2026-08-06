"""B站视频字幕工具 - 交互模式（中文界面）。

运行方式：
    python -m bili_subtitles.interactive
"""

import os
import subprocess
import sys

from .cli import process_video
from .transcriber import save_as_srt, sanitize_filename


OUTPUT_DIR = "output"


BANNER = """==============================================
    B站视频字幕工具 · 交互模式
==============================================
  1. 设置登录Cookie
  2. 查看帮助
  3. 退出
=============================================="""

HELP = """可用命令:
  直接粘贴B站链接   生成字幕，保存到 output/
  1. 设置登录Cookie  指定 cookies.txt 路径，用于获取AI字幕
  2. 查看帮助
  3. 退出"""


def _default_cookies() -> str:
    """自动使用当前目录下的 cookies.txt（如果存在）。"""
    path = os.path.join(os.getcwd(), "cookies.txt")
    return path if os.path.isfile(path) else None


def _reveal_in_finder(path: str) -> bool:
    """在 macOS 访达中打开并选中该文件，成功返回 True。"""
    if sys.platform != "darwin":
        return False
    try:
        subprocess.run(["open", "-R", path], check=True)
        return True
    except Exception:
        return False


def _save_results(text: str, segments, srt_content, title: str):
    """把结果保存为 txt 和 srt，返回 (txt_path, srt_path)，不打印路径。"""
    out_dir = os.path.join(os.getcwd(), OUTPUT_DIR)
    os.makedirs(out_dir, exist_ok=True)
    safe_title = sanitize_filename(title)
    txt_path = os.path.join(out_dir, f"{safe_title}.txt")
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(text)

    srt_path = None
    if segments:
        srt_path = os.path.join(out_dir, f"{safe_title}.srt")
        save_as_srt(segments, srt_path)
    elif srt_content:
        srt_path = os.path.join(out_dir, f"{safe_title}.srt")
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(srt_content)
    return txt_path, srt_path


def main():
    print(BANNER)
    cookies_file = _default_cookies()
    if cookies_file:
        print("  [登录] 已使用Cookie")
    else:
        print("  [登录] 未设置Cookie，AI字幕可能获取不到")
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
        if lower in ("3", "q", "quit", "exit", "退出"):
            print("再见！")
            return
        if lower in ("2", "help", "帮助", "h"):
            print(HELP)
            continue
        if lower == "1":
            path = input("请输入Cookie文件路径> ").strip().strip("\"'")
            if os.path.isfile(path):
                cookies_file = path
                print("  ✓ 已设置登录Cookie")
            else:
                print(f"  ✗ 找不到文件: {path}")
            continue
        if command.isdigit():
            print("  ✗ 无效指令，请输入 1-3，或直接粘贴B站视频链接")
            continue

        print()
        try:
            text, segments, srt_content, title = process_video(
                command, cookies_file=cookies_file, show_progress=True
            )
            print()
            txt_path, srt_path = _save_results(text, segments, srt_content, title)
            if txt_path and not _reveal_in_finder(txt_path):
                print(f"  ✓ 字幕已生成: {os.path.basename(txt_path)}（位于 {OUTPUT_DIR}/ 目录）")
            elif txt_path:
                print(f"  ✓ 字幕已生成: {os.path.basename(txt_path)}")
            print()
        except Exception as exc:
            print(f"  ✗ 处理失败: {exc}")
            print()


if __name__ == "__main__":
    main()
