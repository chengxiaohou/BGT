import os
import click
from .bilibili import (
    extract_bvid,
    get_video_info,
    get_subtitle_urls,
    fetch_subtitle,
    get_audio_url,
    download_subtitles_with_ytdlp,
    detect_installed_browsers,
    export_browser_cookies,
    srt_to_text,
)
from .transcriber import extract_audio_and_transcribe, save_as_srt, sanitize_filename


@click.command()
@click.argument("url", required=False)
@click.option("--output", "-o", help="输出文件路径（纯文本）")
@click.option("--srt", "-s", "output_srt", help="保存为SRT字幕格式文件路径")
@click.option("--cookies", "cookies_file", type=click.Path(exists=True), help="B站登录Cookie文件（Netscape格式），用于获取AI字幕")
@click.option("--browser", type=click.Choice(["chrome", "edge", "safari", "firefox", "brave", "chromium"]), help="从指定浏览器自动读取登录Cookie（不指定时自动检测）")
@click.option("--export-cookies", "export_cookies", type=click.Path(), help="把浏览器登录Cookie导出为文件后退出（之后用 --cookies 指定，无需再开完全磁盘访问权限）")
@click.option("--force-transcribe", "-f", is_flag=True, help="强制使用语音识别，不使用字幕")
@click.option("--model-size", "-m", default="base", help="语音识别模型大小: tiny, base, small, medium, large")
@click.option("--no-progress", is_flag=True, help="不显示进度信息")
def main(url: str, output: str, output_srt: str, cookies_file: str, browser: str, export_cookies: str, force_transcribe: bool, model_size: str, no_progress: bool):
    try:
        # 导出 Cookie 后直接退出，不做抓取
        if export_cookies:
            export_browser = browser or "safari"
            count = export_browser_cookies(export_browser, export_cookies)
            abs_cookies = os.path.abspath(export_cookies)
            click.echo(f"✓ 已从 {export_browser} 导出 {count} 个 Cookie 到 {abs_cookies}")
            click.echo("提示: 此文件包含登录凭据，请勿分享或提交到 git；B站登录 Cookie 通常数月后过期，过期后重新导出即可")
            return

        if not url:
            raise click.UsageError("缺少视频链接 URL")

        show_progress = not no_progress
        
        bvid = extract_bvid(url)
        click.echo(f"提取到BV号: {bvid}")
        
        video_info = get_video_info(bvid)
        title = video_info.get("title", "未知标题")
        cid = video_info.get("cid")
        click.echo(f"视频标题: {title}")
        
        segments = None  # 用于保存语音识别的时间戳信息
        srt_content = None  # 用于保存 yt-dlp 抓到的现成 SRT 字幕
        
        if not force_transcribe:
            # 1) 首选 yt-dlp：能处理 wbi 签名，带 Cookie 时还能拿到 AI 字幕
            sub = None
            attempt_warnings = []

            if cookies_file:
                attempts = [("cookiefile", cookies_file, "Cookie 文件")]
            elif browser:
                attempts = [("browser", browser, f"浏览器 {browser}")]
            else:
                detected = detect_installed_browsers()
                # 当前使用者只用 Safari 登录 B 站，默认只认 Safari，避免误读其他浏览器；
                # 需要其他浏览器时可用 --browser 显式指定
                attempts = [("browser", name, f"浏览器 {name}") for name in detected if name == "safari"]
                if not attempts:
                    attempt_warnings.append("未检测到 Safari，跳过登录状态读取")

            for kind, value, label in attempts:
                if kind == "browser":
                    click.echo(f"正在尝试从{label}读取登录状态…")
                try:
                    sub = download_subtitles_with_ytdlp(
                        bvid,
                        cookies_file=value if kind == "cookiefile" else None,
                        browser=value if kind == "browser" else None,
                    )
                except RuntimeError as exc:
                    attempt_warnings.append(f"{label}读取失败：{exc}")
                    sub = None
                if sub:
                    break

            if sub:
                click.echo(f"使用字幕: {sub['lang_name']}")
                srt_content = sub["content"]
                text = srt_to_text(srt_content)
                segments = None
            else:
                for warning in attempt_warnings:
                    click.echo(f"警告: {warning}", err=True)
                if attempts and not cookies_file:
                    click.echo("提示: 如已在浏览器登录B站但仍未读到登录态，可手动指定 --browser chrome（或 edge/safari/firefox）", err=True)
                # 2) 手动接口再试一次（针对少量无需登录即可见的 CC 字幕）
                subtitles = get_subtitle_urls(bvid, cid)
                if subtitles:
                    click.echo(f"发现字幕: {[s['lang_name'] for s in subtitles]}")
                    chinese_sub = next((s for s in subtitles if s['lang'] in ("zh-CN", "zh")), None)
                    sub = chinese_sub or subtitles[0]
                    click.echo(f"使用字幕: {sub['lang_name']}")
                    text = fetch_subtitle(sub["url"])
                else:
                    # 3) 都没有，才走语音识别
                    click.echo("未发现字幕，将使用语音识别")
                    audio_url = get_audio_url(bvid, cid)
                    if audio_url:
                        text, segments = extract_audio_and_transcribe(audio_url, model_size, show_progress)
                    else:
                        raise ValueError("无法获取音频链接")
        else:
            click.echo("强制使用语音识别")
            audio_url = get_audio_url(bvid, cid)
            if audio_url:
                text, segments = extract_audio_and_transcribe(audio_url, model_size, show_progress)
            else:
                raise ValueError("无法获取音频链接")
        
        # 使用视频标题作为默认文件名
        safe_title = sanitize_filename(title)
        current_dir = os.path.abspath(".")
        
        # 保存为纯文本
        if output:
            abs_output = os.path.abspath(output)
        else:
            abs_output = os.path.join(current_dir, f"{safe_title}.txt")
        
        with open(abs_output, "w", encoding="utf-8") as f:
            f.write(text)
        click.echo(f"✓ 文本已保存: {abs_output}")
        
        # 保存为SRT格式
        if segments:
            if output_srt:
                abs_srt = os.path.abspath(output_srt)
            else:
                abs_srt = os.path.join(current_dir, f"{safe_title}.srt")
            
            save_as_srt(segments, abs_srt)
            click.echo(f"✓ SRT字幕已保存: {abs_srt}")
        elif srt_content:
            # yt-dlp 拿到的现成字幕也顺手存一份 SRT
            if output_srt:
                abs_srt = os.path.abspath(output_srt)
            else:
                abs_srt = os.path.join(current_dir, f"{safe_title}.srt")
            
            with open(abs_srt, "w", encoding="utf-8") as f:
                f.write(srt_content)
            click.echo(f"✓ SRT字幕已保存: {abs_srt}")
        
        # 显示文本内容（如果既没有指定output也没有指定srt）
        if not output and not output_srt:
            click.echo("\n--- 视频文本内容 ---")
            click.echo(text)
            click.echo("--- 结束 ---")
    
    except Exception as e:
        click.echo(f"错误: {e}", err=True)
        raise


if __name__ == "__main__":
    main()
