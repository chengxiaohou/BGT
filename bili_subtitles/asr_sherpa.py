"""基于 sherpa-onnx + Paraformer 的中文语音识别（CPU 上又快又准）。

首次使用时会自动下载 Paraformer-zh 模型（约 230MB）到 models/ 目录。
"""

import os
import subprocess
import sys
import tarfile
import tempfile
import wave
from typing import Dict, List, Optional, Tuple

import numpy as np
import sherpa_onnx


MODEL_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-paraformer-zh-2023-09-14.tar.bz2"
MODEL_DIR_NAME = "sherpa-onnx-paraformer-zh-2023-09-14"

# SenseVoice：一个模型同时支持 中/英/日/韩/粤，靠 language 参数限定语种
SENSE_VOICE_MODEL_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2"
SENSE_VOICE_MODEL_DIR_NAME = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"

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
# 语气词 + 常见标点。若某句话每个字符都只属于这个集合（如只有"。"），
# 则视为无有效内容，丢弃，相当于什么也没听到。
FILLER_CHARS = "嗯啊呃哦喔唉哎。？！….,!?·~～-—，、；：''\"\"（）()"


def _download_and_extract_model(url: str, archive: str, show_progress: bool, label: str) -> None:
    """下载模型压缩包并解压到 archive 所在目录，完成后删除压缩包。"""
    import requests

    if show_progress:
        print(f"首次使用需要下载{label}...")
    with requests.get(url, stream=True) as resp:
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
        tar.extractall(os.path.dirname(archive))
    os.remove(archive)


def ensure_paraformer_model(models_root: str = "models", show_progress: bool = True) -> str:
    """确保 Paraformer 中文模型已下载并解压，返回模型目录路径。"""
    model_dir = os.path.join(models_root, MODEL_DIR_NAME)
    if os.path.isfile(os.path.join(model_dir, "model.int8.onnx")) and os.path.isfile(
        os.path.join(model_dir, "tokens.txt")
    ):
        return model_dir

    os.makedirs(models_root, exist_ok=True)
    _download_and_extract_model(
        MODEL_URL,
        os.path.join(models_root, MODEL_DIR_NAME + ".tar.bz2"),
        show_progress,
        "中文识别模型（约 230MB）",
    )
    return model_dir


def ensure_sense_voice_model(models_root: str = "models", show_progress: bool = True) -> str:
    """确保 SenseVoice 多语种模型已下载并解压，返回模型目录路径。"""
    model_dir = os.path.join(models_root, SENSE_VOICE_MODEL_DIR_NAME)
    if os.path.isfile(os.path.join(model_dir, "model.int8.onnx")) and os.path.isfile(
        os.path.join(model_dir, "tokens.txt")
    ):
        return model_dir

    os.makedirs(models_root, exist_ok=True)
    _download_and_extract_model(
        SENSE_VOICE_MODEL_URL,
        os.path.join(models_root, SENSE_VOICE_MODEL_DIR_NAME + ".tar.bz2"),
        show_progress,
        "SenseVoice 多语种识别模型（支持日语，约 230MB）",
    )
    return model_dir


def _get_duration(audio_path: str) -> Optional[float]:
    """用 ffprobe 取输入时长（秒），失败返回 None。"""
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                audio_path,
            ],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
        return float(out)
    except (ValueError, subprocess.SubprocessError):
        return None


def _convert_to_wav(audio_path: str, wav_path: str, show_progress: bool = True) -> None:
    """用 ffmpeg 把任意音频/视频转成 16kHz 单声道 wav，按输出时间显示转换进度。"""
    duration = _get_duration(audio_path) if show_progress else None
    cmd = [
        "ffmpeg", "-y", "-v", "error", "-nostats",
        "-progress", "pipe:1",
        "-i", audio_path,
        "-ar", "16000", "-ac", "1",
        "-c:a", "pcm_s16le",
        wav_path,
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    while True:
        line = proc.stdout.readline()
        if not line:
            break
        if duration and ("out_time_us=" in line or "out_time_ms=" in line):
            key, _, val = line.partition("=")
            try:
                us = int(val)
                if key == "out_time_ms":
                    us *= 1000
                seconds = us / 1_000_000
                percent = min(100.0, seconds / duration * 100)
                sys.stdout.write(f"\r音频转换进度: {percent:.1f}%")
                sys.stdout.flush()
            except ValueError:
                pass
    err = proc.stderr.read()
    proc.wait()
    if show_progress:
        print()
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, output=err)


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


def read_wav_to_samples(wav_path: str) -> Tuple[np.ndarray, int]:
    """直接读取一个现成的 16k 单声道 wav 为 float32 采样，返回 (samples, sample_rate)。"""
    with wave.open(wav_path, "rb") as wav:
        sample_rate = wav.getframerate()
        raw = wav.readframes(wav.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return samples, sample_rate


def extract_audio_file(file_path: str, show_progress: bool = True) -> str:
    """从本地媒体文件提取音频为同目录同名的 .wav（16k 单声道），提取后保留，可复用。

    若输入本身就是 .wav 直接返回；若同目录已存在同名 .wav，则直接复用，不再转换。
    """
    if file_path.lower().endswith(".wav"):
        return file_path
    wav_path = os.path.splitext(file_path)[0] + ".wav"
    if os.path.exists(wav_path):
        if show_progress:
            print(f"  检测到已提取的音频: {os.path.basename(wav_path)}，直接使用")
        return wav_path
    _convert_to_wav(file_path, wav_path, show_progress)
    return wav_path


def _read_wav_samples(audio_path: str, show_progress: bool = True) -> Tuple[np.ndarray, int]:
    """把任意音频/视频转成 16kHz 单声道临时 wav 并读成 float32 采样，返回 (samples, sample_rate)。"""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav_path = f.name
    try:
        _convert_to_wav(audio_path, wav_path, show_progress)
        with wave.open(wav_path, "rb") as wav:
            sample_rate = wav.getframerate()
            raw = wav.readframes(wav.getnframes())
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    finally:
        if os.path.exists(wav_path):
            os.remove(wav_path)
    return samples, sample_rate


def _transcribe_samples(
    samples: np.ndarray,
    sample_rate: int,
    recognizer: "sherpa_onnx.OfflineRecognizer",
    show_progress: bool,
) -> List[Dict]:
    """把整段采样切块送入离线识别器，再按停顿断句，返回 [{start, end, text}]。"""
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

    samples, sample_rate = _read_wav_samples(audio_path, show_progress)
    return _transcribe_samples(samples, sample_rate, recognizer, show_progress)


def transcribe_local_file_sense_voice(
    file_path: str,
    language: str = "ja",
    show_progress: bool = True,
    models_root: str = "models",
) -> List[Dict]:
    """用 SenseVoice 识别本地媒体文件，language 限定语种（默认日语 ja）。

    SenseVoice 一个模型支持 中/英/日/韩/粤，只需改 language 参数即可切换语种。
    """
    model_dir = ensure_sense_voice_model(models_root, show_progress)

    recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        os.path.join(model_dir, "model.int8.onnx"),
        os.path.join(model_dir, "tokens.txt"),
        num_threads=2,
        sample_rate=16000,
        feature_dim=80,
        decoding_method="greedy_search",
        language=language,
        use_itn=True,
    )

    samples, sample_rate = read_wav_to_samples(
        extract_audio_file(file_path, show_progress)
    )
    return _transcribe_samples(samples, sample_rate, recognizer, show_progress)


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
