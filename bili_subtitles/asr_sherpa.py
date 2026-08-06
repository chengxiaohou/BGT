"""基于 sherpa-onnx + Paraformer 的中文语音识别（CPU 上又快又准）。

首次使用时会自动下载 Paraformer-zh 模型（约 230MB）到 models/ 目录。
"""

import os
import subprocess
import sys
import tarfile
import tempfile
import wave
from typing import Dict, List, Tuple

import numpy as np
import sherpa_onnx


MODEL_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-paraformer-zh-2023-09-14.tar.bz2"
MODEL_DIR_NAME = "sherpa-onnx-paraformer-zh-2023-09-14"

# 每块音频的长度（秒）。Paraformer 是离线模型，过长输入会拖慢速度，这里切块处理。
CHUNK_SECONDS = 25.0

# Paraformer 输出里需要忽略的特殊 token
SKIP_TOKENS = {"<s>", "</s>", "<unk>", "<blank>", "<blk>"}

# 句间停顿超过该秒数视为断句
PAUSE_SECONDS = 0.4

# 少于该字数的片段并入前后文，避免把"今天是星期三"切成一两个字
MIN_SEGMENT_CHARS = 6

# 单个片段最长时长（秒），避免长句一直不切
MAX_SEGMENT_SECONDS = 10.0

# 纯语气词片段直接丢弃
FILLER_CHARS = "嗯啊呃哦喔唉哎"


def ensure_paraformer_model(models_root: str = "models", show_progress: bool = True) -> str:
    """确保模型已下载并解压，返回模型目录路径。"""
    model_dir = os.path.join(models_root, MODEL_DIR_NAME)
    if os.path.isfile(os.path.join(model_dir, "model.int8.onnx")) and os.path.isfile(
        os.path.join(model_dir, "tokens.txt")
    ):
        return model_dir

    os.makedirs(models_root, exist_ok=True)
    archive = os.path.join(models_root, MODEL_DIR_NAME + ".tar.bz2")

    import requests

    if show_progress:
        print("首次使用需要下载中文识别模型（约 230MB）...")
    with requests.get(MODEL_URL, stream=True) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        downloaded = 0
        with open(archive, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                f.write(chunk)
                downloaded += len(chunk)
                if show_progress and total > 0:
                    percent = downloaded / total * 100
                    sys.stdout.write(f"\r模型下载: {percent:.1f}%")
                    sys.stdout.flush()
        if show_progress and total > 0:
            print()

    with tarfile.open(archive, "r:bz2") as tar:
        tar.extractall(models_root)
    os.remove(archive)
    return model_dir


def _convert_to_wav(audio_path: str, wav_path: str) -> None:
    """用 ffmpeg 把任意音频转成 16kHz 单声道 wav。"""
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-i", audio_path,
        "-ar", "16000", "-ac", "1",
        "-c:a", "pcm_s16le",
        wav_path,
    ]
    subprocess.run(cmd, check=True)


def _split_by_pause(result, chunk_start: float, chunk_end: float) -> List[Dict]:
    """按句间停顿把一段识别结果切成带时间戳的句子。"""
    tokens = result.tokens or []
    timestamps = result.timestamps or []

    if not tokens or not timestamps or len(tokens) != len(timestamps):
        text = (result.text or "").strip()
        return [{"start": chunk_start, "end": chunk_end, "text": text}] if text else []

    segments = []
    current = []  # [(token, timestamp)]
    current_start = None
    prev_ts = None

    def flush() -> None:
        nonlocal current, current_start
        text = "".join(token for token, _ in current).strip()
        if text and not all(ch in FILLER_CHARS for ch in text):
            segments.append({
                "start": chunk_start + (current_start or 0),
                "end": min(chunk_end, chunk_start + (prev_ts or 0) + 0.2),
                "text": text,
            })
        current = []
        current_start = None

    for token, ts in zip(tokens, timestamps):
        if token in SKIP_TOKENS:
            continue
        if current_start is None:
            current_start = ts
        if prev_ts is not None:
            text_len = sum(len(t) for t, _ in current)
            gap = ts - prev_ts
            duration = ts - current_start
            if (gap > PAUSE_SECONDS and text_len >= MIN_SEGMENT_CHARS) or duration > MAX_SEGMENT_SECONDS:
                flush()
                current_start = ts
        current.append((token, ts))
        prev_ts = ts

    flush()
    return segments


def transcribe_audio_paraformer(
    audio_path: str,
    show_progress: bool = True,
    models_root: str = "models",
) -> List[Dict]:
    """用 Paraformer 识别音频，返回 [{start, end, text}] 列表。"""
    model_dir = ensure_paraformer_model(models_root, show_progress)

    recognizer = sherpa_onnx.OfflineRecognizer.from_paraformer(
        os.path.join(model_dir, "model.int8.onnx"),
        os.path.join(model_dir, "tokens.txt"),
        num_threads=2,
        sample_rate=16000,
        feature_dim=80,
        decoding_method="greedy_search",
    )

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav_path = f.name
    try:
        _convert_to_wav(audio_path, wav_path)
        with wave.open(wav_path, "rb") as wav:
            sample_rate = wav.getframerate()
            raw = wav.readframes(wav.getnframes())
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    finally:
        if os.path.exists(wav_path):
            os.remove(wav_path)

    if len(samples) == 0:
        return []

    chunk_size = int(CHUNK_SECONDS * sample_rate)
    results = []
    total = len(samples)

    for offset in range(0, total, chunk_size):
        part = samples[offset:offset + chunk_size]
        stream = recognizer.create_stream()
        stream.accept_waveform(sample_rate, part)
        recognizer.decode_stream(stream)

        chunk_start = offset / sample_rate
        chunk_end = min(total, offset + chunk_size) / sample_rate
        results.extend(_split_by_pause(stream.result, chunk_start, chunk_end))

        if show_progress:
            percent = min(100.0, (offset + chunk_size) / total * 100)
            sys.stdout.write(f"\r识别进度: {percent:.1f}%")
            sys.stdout.flush()

    if show_progress:
        print()
    return results


def extract_audio_and_transcribe_paraformer(
    audio_url: str,
    show_progress: bool = True,
    models_root: str = "models",
) -> Tuple[str, List[Dict]]:
    """下载音频并用 Paraformer 识别，返回 (纯文本, 片段列表)。"""
    from .transcriber import download_audio_directly

    with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as f:
        temp_path = f.name
    try:
        audio_path = download_audio_directly(audio_url, temp_path, show_progress)
        segments = transcribe_audio_paraformer(audio_path, show_progress, models_root)
        text = "\n".join(seg["text"] for seg in segments)
        return text, segments
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
