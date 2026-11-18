"""
check_audio.py —— 录完（或开录前试录）先跑这个，确认"能进管线"
=================================================================
为什么需要它：
  75 个录音如果格式设错（采样率不对 / 双声道 / 开了 AGC / 其实是 m4a），
  要等全部录完、跑分析时才报错——那时重录成本极高。
  这个脚本只做一件事：3 秒告诉你这个文件能不能用。

用法
----
  # 检查一个文件
  python check_audio.py "D:\\录音\\试录.wav"

  # 检查整个目录（录完一个会话后跑）
  python check_audio.py "D:\\录音\\S1"

  # 严格模式：任何一项不合格就返回退出码 1（可挂到提交闸门上）
  python check_audio.py "D:\\录音\\S1" --strict

它看什么（对应 SOP 第 1.2 节的要求）
------------------------------------
  容器     必须是 PCM WAV（m4a/mp3 不能直接进分析，需要 --convert 转）
  采样率   44100 或 48000
  声道     单声道（双声道会让左右相位差混进特征）
  时长     >= 8 秒（分段窗口 1s/0.5s 步进，太短凑不出样本）
  峰值     不能削波（>= -0.1 dBFS 就是削波，AGC 会掩盖这个问题）
  响度     不能近乎静音（均值 < -50 dBFS 说明麦克风没开或被遮）
  直流偏置 偏离 0 太多说明有 DC offset（部分安卓机前置放大器的毛病）

只用标准库，系统 python 也能跑。非 WAV 文件会调用 ffmpeg 解码后分析。
"""

from __future__ import annotations

import argparse
import glob
import math
import os
import shutil
import subprocess
import sys
import tempfile
import wave
from array import array

AUDIO_EXT = (".wav", ".m4a", ".mp3", ".aac", ".amr", ".opus", ".ogg", ".flac", ".3gp")
OK_SR = (44100, 48000)
MIN_DUR = 8.0

_FF_GLOBS = [
    r"C:\Users\Baron\.workbuddy\binaries\python\envs\default\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg*.exe",
    r"C:\Users\Baron\.workbuddy\binaries\python\envs\*\Lib\site-packages\imageio_ffmpeg\binaries\ffmpeg*.exe",
    r"C:\Users\Baron\AppData\Roaming\Python\*\site-packages\imageio_ffmpeg\binaries\ffmpeg*.exe",
    r"C:\ffmpeg\bin\ffmpeg.exe",
]


def find_ffmpeg() -> str | None:
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg                                    # noqa: PLC0415
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    for pat in _FF_GLOBS:
        hit = sorted(glob.glob(pat))
        if hit:
            return hit[-1]
    return None


def to_wav(path: str, ffmpeg: str | None) -> tuple[str, bool]:
    """返回 (可供 wave 打开的 wav 路径, 是否为临时文件)。"""
    if path.lower().endswith(".wav"):
        return path, False
    if not ffmpeg:
        raise RuntimeError("不是 WAV，且找不到 ffmpeg 无法解码")
    tmp = os.path.join(tempfile.gettempdir(), "_checkaudio_" + str(os.getpid()) + ".wav")
    subprocess.run([ffmpeg, "-y", "-v", "error", "-i", path,
                    "-ac", "1", "-ar", "44100", "-sample_fmt", "s16", tmp],
                   check=True, capture_output=True)
    return tmp, True


def analyse(path: str, ffmpeg: str | None) -> dict:
    orig_ext = os.path.splitext(path)[1].lower()
    wav, is_tmp = to_wav(path, ffmpeg)
    try:
        with wave.open(wav, "rb") as w:
            ch, sr, width, n = (w.getnchannels(), w.getframerate(),
                                w.getsampwidth(), w.getnframes())
            raw = w.readframes(n)
    finally:
        if is_tmp and os.path.isfile(wav):
            os.remove(wav)

    if width == 1:                      # 8bit 是无符号，重心在 128
        a = array("b", bytes(b - 128 & 0xFF for b in raw))
        full, zero = 128.0, 0
    elif width == 2:
        a = array("h")
        a.frombytes(raw)
        if sys.byteorder == "big":
            a.byteswap()
        full, zero = 32768.0, 0
    else:                               # 24/32bit 极少见，只报不支持
        raise RuntimeError(f"不支持的位深 {width*8} bit")

    if not a:
        raise RuntimeError("文件里没有采样点（空的）")

    peak = max(max(a), -min(a))
    mean_abs = sum(abs(v) for v in a) / len(a)
    dc = sum(a) / len(a)

    def db(x: float) -> float:
        return -999.0 if x <= 0 else 20 * math.log10(x / full)

    return {
        "ext": orig_ext,
        "converted": is_tmp,
        "ch": ch, "sr": sr, "bits": width * 8,
        "dur": len(a) / (sr * ch),
        "peak_db": db(peak),
        "rms_db": db(mean_abs),
        "clip_pct": 100.0 * sum(1 for v in a if abs(v) >= full - 1) / len(a),
        "dc_pct": 100.0 * abs(dc) / full,
    }


def verdict(info: dict) -> list[str]:
    bad = []
    if info["ext"] != ".wav":
        bad.append(f"不是 WAV（{info['ext']}）—— 分析脚本只认 PCM WAV，"
                   "要么 App 改设置重录，要么之后加 --convert 转码")
    if info["sr"] not in OK_SR:
        bad.append(f"采样率 {info['sr']}Hz —— 要 44100 或 48000")
    if info["ch"] != 1:
        bad.append(f"{info['ch']} 声道 —— 要单声道")
    if info["dur"] < MIN_DUR:
        bad.append(f"只有 {info['dur']:.1f}s —— 至少 {MIN_DUR:.0f}s")
    if info["clip_pct"] > 0.01:
        bad.append(f"削波 {info['clip_pct']:.2f}% —— 离设备远点或降增益")
    if info["peak_db"] >= -0.1 and info["clip_pct"] == 0:
        bad.append(f"峰值 {info['peak_db']:.1f}dBFS 贴着 0 —— AGC 可能没关")
    if info["rms_db"] < -50:
        bad.append(f"平均响度 {info['rms_db']:.0f}dBFS 近乎静音 —— 麦克风被遮/没开")
    if info["dc_pct"] > 1.0:
        bad.append(f"直流偏置 {info['dc_pct']:.1f}% —— 部分安卓机的通病，会影响低频特征")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="一个音频文件，或一个目录")
    ap.add_argument("--strict", action="store_true", help="有任何不合格就返回 1")
    args = ap.parse_args()

    target = os.path.abspath(args.path)
    if os.path.isdir(target):
        files = sorted(p for p in glob.glob(os.path.join(target, "*"))
                       if p.lower().endswith(AUDIO_EXT) and os.path.isfile(p))
    elif os.path.isfile(target):
        files = [target]
    else:
        print(f"[×] 路径不存在：{target}")
        return 2

    if not files:
        print(f"[×] 里面没有音频文件（找过 {' '.join(AUDIO_EXT)}）")
        return 2

    ffmpeg = find_ffmpeg()
    print("=" * 74)
    print(f"检查目标 : {target}")
    print(f"文件数量 : {len(files)}" + ("" if ffmpeg else "    （未找到 ffmpeg，非 WAV 会直接判失败）"))
    print("=" * 74)
    print(f"{'文件':<34}{'格式':<12}{'采样率':>8}{'声道':>5}{'时长':>7}{'峰值dB':>8}")
    print("-" * 74)

    bad_files = 0
    for p in files:
        name = os.path.basename(p)
        try:
            info = analyse(p, ffmpeg)
            probs = verdict(info)
            tag = "" if not probs else "  ← 不合格"
            sr_txt = f"{info['sr']}" + ("*" if info.get("converted") else "")
            print(f"{name[:32]:<34}{str(info['bits']) + 'bit':<12}"
                  f"{sr_txt:>8}{info['ch']:>5}{info['dur']:>6.1f}s"
                  f"{info['peak_db']:>8.1f}{tag}")
            for pr in probs:
                print(f"      · {pr}")
            if probs:
                bad_files += 1
        except Exception as e:
            print(f"{name[:32]:<34}读不了：{e}")
            bad_files += 1

    if any(os.path.splitext(p)[1].lower() != ".wav" for p in files):
        print("  * 该行采样率是 ffmpeg 解码时统一转成的 44100，不代表原始文件")
    print("-" * 74)
    if bad_files == 0:
        print(f"[√] {len(files)} 个文件全部合格，可以直接进特征提取。")
        print("下一步：把录音按 SOP 命名后放进 02-20Days-Challenge\\raw\\S1\\，")
        print("        然后跑  python 02-20Days-Challenge\\src\\selfcheck.py")
        return 0

    print(f"[×] {bad_files}/{len(files)} 个不合格，见上面每条的原因。")
    if any(not f.lower().endswith(".wav") for f in files):
        print("    如果是 m4a：用 rename_recordings.py 加 --convert 转成 WAV 再检查一次。")
    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
