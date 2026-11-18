"""
audio_io.py —— 音频/传感器数据读取层
------------------------------------
只做一件事：把各种来源的文件统一读成 (采样率, 单声道 float32 numpy 数组)。

支持格式：
  .wav  —— 依靠标准库 wave 模块解析（PCM 8/16/24/32bit，手机录音 App 的
           "WAV 无损"导出可直接用）。不依赖 scipy/soundfile。
  .csv  —— Phyphox / SensorLogger 等传感器 App 导出的时间序列（自动挑数值列）。
  .m4a / .mp3 / .aac / .opus —— 若本机装了 ffmpeg（或 imageio-ffmpeg）则自动转码。

为什么不用 librosa：本机是 Windows 无 GPU 环境，librosa 依赖链太重；
本项目的特征全部用 numpy 手写，反而更容易在面试里逐行讲清楚。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import wave

import numpy as np

TARGET_SR = 16000  # 统一重采样到 16k：机械噪声主能量在 4kHz 以下，16k 足够且省算力


# --------------------------------------------------------------------------
# ffmpeg 定位
# --------------------------------------------------------------------------
def _find_ffmpeg() -> str | None:
    exe = os.environ.get("FFMPEG_BINARY")
    if exe and os.path.exists(exe):
        return exe
    try:  # imageio-ffmpeg 自带静态 ffmpeg（约 39MB，装在 venv 里）
        import imageio_ffmpeg

        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.exists(p):
            return p
    except Exception:
        pass
    from shutil import which

    return which("ffmpeg")


_FFMPEG = None
_FFMPEG_CHECKED = False


def ffmpeg_available() -> bool:
    global _FFMPEG, _FFMPEG_CHECKED
    if not _FFMPEG_CHECKED:
        _FFMPEG = _find_ffmpeg()
        _FFMPEG_CHECKED = True
    return _FFMPEG is not None


# --------------------------------------------------------------------------
# WAV
# --------------------------------------------------------------------------
_WAVE_DTYPE = {1: np.uint8, 2: np.int16, 4: np.int32}


def _read_wav(path: str) -> tuple[int, np.ndarray]:
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        n = w.getnframes()
        raw = w.readframes(n)

    if sw == 3:  # 24-bit PCM：wave 模块不直接支持，手工拼
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        x = (b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)).astype(np.int64)
        x = np.where(x >= 1 << 23, x - (1 << 24), x).astype(np.float64) / float(1 << 23)
    elif sw in _WAVE_DTYPE:
        x = np.frombuffer(raw, dtype=_WAVE_DTYPE[sw]).astype(np.float64)
        if sw == 1:  # 8-bit 是无符号
            x = (x - 128.0) / 128.0
        else:
            x = x / float(np.iinfo(_WAVE_DTYPE[sw]).max)
    else:
        raise ValueError(f"不支持的 WAV 位深: {sw * 8} bit ({path})")

    if ch > 1:  # 多声道取均值 —— 声振诊断不需要空间信息
        x = x.reshape(-1, ch).mean(axis=1)
    return sr, x.astype(np.float32)


# --------------------------------------------------------------------------
# ffmpeg 转码
# --------------------------------------------------------------------------
def _read_via_ffmpeg(path: str) -> tuple[int, np.ndarray]:
    if not ffmpeg_available():
        raise RuntimeError(
            f"无法读取 {os.path.basename(path)}：本机没有 ffmpeg。\n"
            "  解决方式二选一：\n"
            "  1) 手机录音 App 里把格式设为 WAV（推荐，44.1kHz 单声道即可）；\n"
            "  2) python -m pip install imageio-ffmpeg  之后重新运行。"
        )
    tmp = os.path.join(tempfile.gettempdir(), f"_conv_{os.getpid()}.wav")
    cmd = [_FFMPEG, "-y", "-v", "error", "-i", path,
           "-ac", "1", "-ar", str(TARGET_SR), "-f", "wav", "-acodec", "pcm_s16le", tmp]
    subprocess.run(cmd, check=True, capture_output=True)
    try:
        return _read_wav(tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# --------------------------------------------------------------------------
# CSV（传感器导出）
# --------------------------------------------------------------------------
def _read_csv(path: str) -> tuple[int, np.ndarray]:
    """读 Phyphox / 通用 CSV。返回 (估算采样率, 单通道波形)。

    规则：取最像"信号"的一列或多列（跳过 time/index 列），
    多列时逐列标准化后相加，得到一条可做 FFT 的合成波形。
    """
    import csv

    with open(path, encoding="utf-8-sig", errors="ignore", newline="") as f:
        rows = [r for r in csv.reader(f) if any(c.strip() for c in r)]
    if not rows:
        raise ValueError(f"空 CSV: {path}")

    # 找到第一行"看起来是表头"的行（含非数字字段）
    header, start = None, 0
    for i, r in enumerate(rows[:5]):
        if any(_is_float(c) is False and c.strip() for c in r):
            header, start = r, i + 1
            break
    if header is None:
        header, start = [f"c{j}" for j in range(len(rows[0]))], 0

    data = []
    for r in rows[start:]:
        vals = [_is_float(c) for c in r]
        if len(vals) >= 2:
            vals = [np.nan if v is None else v for v in vals[: len(header)]]
            data.append(vals)
    arr = np.array(data, dtype=np.float64)
    if arr.size == 0:
        raise ValueError(f"CSV 里没有解析到数值列: {path}")

    # 时间列：找表头里含 time/t/s 且在单调递增的列
    sr = 0.0
    for j, name in enumerate(header):
        if j >= arr.shape[1]:
            break
        nl = str(name).lower()
        if ("time" in nl or nl.strip() in ("t", "s", "sec")) and arr.shape[0] > 2:
            dt = np.diff(arr[:, j])
            dt = dt[dt > 0]
            if dt.size:
                sr = float(1.0 / np.median(dt))
                break
    if sr <= 0 or not np.isfinite(sr) or sr > 100000:
        sr = 200.0  # Phyphox 加速度计典型值

    # 信号列：排除时间列 + 常数列
    cols = [j for j in range(arr.shape[1])
            if not (j < len(header) and ("time" in str(header[j]).lower()))]
    cols = [j for j in cols if np.std(arr[:, j]) > 0] or list(range(arr.shape[1]))
    sig = np.zeros(arr.shape[0])
    for j in cols:
        s = arr[:, j] - np.mean(arr[:, j])
        sd = np.std(s) or 1.0
        sig += s / sd
    sig = sig / max(1, len(cols))
    return int(round(sr)), sig.astype(np.float32)


def _is_float(s: str):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# 对外统一接口
# --------------------------------------------------------------------------
def load_audio(path: str, sr_target: int = TARGET_SR) -> tuple[int, np.ndarray]:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".wav":
        sr, x = _read_wav(path)
    elif ext in (".csv", ".txt"):
        sr, x = _read_csv(path)
    elif ext in (".m4a", ".mp3", ".aac", ".opus", ".ogg", ".flac", ".amr"):
        sr, x = _read_via_ffmpeg(path)
    else:
        raise ValueError(f"不支持的文件类型: {ext} ({path})")

    if sr_target and sr != sr_target:
        x = resample(x, sr, sr_target)
        sr = sr_target

    # 去掉直流偏置（手机麦克风常见 20~80Hz 以下的 DC 漂移）
    x = x - float(np.mean(x))
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    if peak > 1e-6:
        x = x / peak * 0.95  # 峰值归一，消除录音增益差异
    return sr, x


def resample(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    """多相滤波重采样，比线性插值少很多高频混叠。"""
    from math import gcd

    from scipy.signal import resample_poly

    g = gcd(int(sr_in), int(sr_out))
    return resample_poly(x, int(sr_out) // g, int(sr_in) // g).astype(np.float32)


def duration(path: str) -> float:
    try:
        sr, x = load_audio(path, sr_target=0)
        return len(x) / sr if sr else 0.0
    except Exception:
        return 0.0
