import os
import sys
import tempfile
import requests
from faster_whisper import WhisperModel


def get_model_cache_path(model_size: str) -> str:
    """获取模型缓存路径"""
    home = os.path.expanduser("~")
    cache_dir = os.path.join(home, ".cache", "huggingface", "hub")
    
    # 查找对应的模型目录
    for root, dirs, files in os.walk(cache_dir):
        for d in dirs:
            if f"faster-whisper-{model_size}" in d:
                return os.path.join(root, d)
    return cache_dir


def download_audio_directly(audio_url: str, output_path: str) -> str:
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
            if total_size > 0:
                percent = (downloaded / total_size) * 100
                sys.stdout.write(f"\r下载进度: {percent:.1f}%")
                sys.stdout.flush()
    
    print()  # 换行
    return output_path


def transcribe_audio(audio_path: str, model_size: str = "base", show_progress: bool = True) -> list:
    if show_progress:
        print(f"正在加载 Whisper {model_size} 模型...")
    
    model = WhisperModel(model_size, device="auto", compute_type="int8")
    
    # 显示模型缓存路径
    if show_progress:
        model_path = get_model_cache_path(model_size)
        print(f"模型路径: {model_path}")
        print("开始语音识别...")
    
    segments, info = model.transcribe(
        audio_path, 
        language="zh",
        word_timestamps=True
    )
    
    results = []
    total_segments = sum(1 for _ in segments)
    
    # 重新获取生成器
    segments, _ = model.transcribe(audio_path, language="zh", word_timestamps=True)
    
    for i, segment in enumerate(segments):
        if show_progress:
            percent = ((i + 1) / total_segments) * 100 if total_segments > 0 else 0
            sys.stdout.write(f"\r识别进度: {percent:.1f}% ({i + 1}/{total_segments})")
            sys.stdout.flush()
        
        results.append({
            "start": segment.start,
            "end": segment.end,
            "text": segment.text.strip()
        })
    
    if show_progress:
        print()  # 换行
        print(f"识别完成！共识别 {len(results)} 个片段")
    
    return results


def extract_audio_and_transcribe(audio_url: str, model_size: str = "base", show_progress: bool = True) -> tuple:
    with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as temp_file:
        temp_path = temp_file.name
    
    try:
        audio_path = download_audio_directly(audio_url, temp_path)
        segments = transcribe_audio(audio_path, model_size, show_progress)
        
        # 转换为纯文本
        text = "\n".join([seg["text"] for seg in segments])
        
        return text, segments
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def save_as_srt(segments: list, output_path: str):
    with open(output_path, "w", encoding="utf-8") as f:
        for i, segment in enumerate(segments, 1):
            start_time = format_timestamp(segment["start"])
            end_time = format_timestamp(segment["end"])
            text = segment["text"]
            f.write(f"{i}\n")
            f.write(f"{start_time} --> {end_time}\n")
            f.write(f"{text}\n")
            f.write("\n")


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
