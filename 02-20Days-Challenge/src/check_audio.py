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

  # 严格模式：有任何【不合格】就返回退出码 1（可挂到提交闸门上）
  python check_audio.py "D:\\录音\\S1" --strict

它看什么（对应 SOP 第 1.2 节的要求）
------------------------------------
分两类，处理方式完全不同：

  【不合格】必须重录，脚本救不了
    · 容器     必须是 PCM WAV（m4a/mp3 可以用 --convert 转，但要占一步）
    · 采样率   44100 或 48000（16000 直接丢掉失衡特征所在的 2kHz 以上）
    · 时长     >= 8 秒（窗口 1s / 步进 0.5s，太短凑不出样本）
    · 削波     峰值触顶 = 波形被切平，高频特征被破坏
    · 响度     近乎静音说明麦克风没开 / 被遮

  【提示】导入时加 --to16 自动修，不用重录
    · 双声道   混成单声道（能改 App 设置更好，省一半体积）
    · 24/32bit 转成 16bit（体积是 16bit 的 1.5~3 倍，75 段能差出 150MB）
    · 峰值贴 0 很可能 AGC 没关（AGC 会抹平工况间的响度差，建议关掉重录）
    · 直流偏置 分析时会被减掉均值，影响有限

支持 PCM 8/16/24/32bit 与浮点 WAV；非 WAV 会调用 ffmpeg 解码后分析。
只用标准库，系统 python 也能跑（不需要 numpy / soundfile）。
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

AUDIO_EXT = (".wav", ".m4a", ".mp3", ".aac", ".amr", ".opus", ".ogg", ".flac", ".3gp", ".wma")
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


def _decode(raw: bytes, width: int) -> tuple[array, float]:
    """把 PCM 原始字节解成整型样本数组，返回 (样本, 满量程)。

    24bit 的处理：把每个 3 字节样本搬进 4 字节 int32 的**高 3 字节**
    （低位补一个 0），于是数值变成原来的 256 倍，满量程同步放大到 2^31。
    后面算的都是比值（peak/full、dc/full），所以放大不影响任何结论，
    但省掉了一次逐样本的乘法循环——44.1kHz 十秒有 44 万个点，纯 Python 循环会卡。
    """
    if width == 1:                      # 8bit 是无符号，重心在 128
        a = array("b", bytes(b - 128 & 0xFF for b in raw))
        return a, 128.0
    if width == 2:
        a = array("h")
        a.frombytes(raw)
        if sys.byteorder == "big":
            a.byteswap()
        return a, 32768.0
    if width == 3:
        n3 = len(raw) // 3
        buf = bytearray(n3 * 4)
        buf[1::4] = raw[0:n3 * 3:3]
        buf[2::4] = raw[1:n3 * 3:3]
        buf[3::4] = raw[2:n3 * 3:3]
        a = array("i")
        a.frombytes(bytes(buf))
        if sys.byteorder == "big":
            a.byteswap()
        return a, float(1 << 31)
    if width == 4:
        a = array("i")
        a.frombytes(raw)
        if sys.byteorder == "big":
            a.byteswap()
        return a, float(1 << 31)
    raise RuntimeError(f"不支持的位深 {width * 8} bit")


def analyse(path: str, ffmpeg: str | None) -> dict:
    orig_ext = os.path.splitext(path)[1].lower()
    wav, is_tmp = to_wav(path, ffmpeg)
    wav2, is_tmp2 = wav, False
    try:
        try:
            with wave.open(wav, "rb") as w:
                ch, sr, width, n = (w.getnchannels(), w.getframerate(),
                                    w.getsampwidth(), w.getnframes())
                raw = w.readframes(n)
        except (wave.Error, EOFError):
            # 浮点 WAV（format=3）wave 模块打不开，交给 ffmpeg 统一成 16bit 再判
            if not ffmpeg:
                raise RuntimeError("这个 WAV wave 模块读不了（可能是浮点格式），且没有 ffmpeg")
            wav2 = os.path.join(tempfile.gettempdir(), "_checkaudio_f_" + str(os.getpid()) + ".wav")
            subprocess.run([ffmpeg, "-y", "-v", "error", "-i", wav,
                            "-ac", "1", "-ar", "44100", "-sample_fmt", "s16", wav2],
                           check=True, capture_output=True)
            is_tmp2 = True
            with wave.open(wav2, "rb") as w:
                ch, sr, width, n = (w.getnchannels(), w.getframerate(),
                                    w.getsampwidth(), w.getnframes())
                raw = w.readframes(n)
    finally:
        for p, flag in ((wav, is_tmp), (wav2, is_tmp2)):
            if flag and os.path.isfile(p):
                os.remove(p)

    a, full = _decode(raw, width)
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
        "mb": os.path.getsize(path) / 1024 / 1024,
        "peak_db": db(peak),
        "rms_db": db(mean_abs),
        "clip_pct": 100.0 * sum(1 for v in a if abs(v) >= full - 1) / len(a),
        "dc_pct": 100.0 * abs(dc) / full,
    }


def verdict(info: dict) -> tuple[list[str], list[str]]:
    """返回 (不合格, 提示)。不合格 = 必须重录；提示 = --to16 能自动修。"""
    bad, note = [], []
    if info["ext"] != ".wav":
        bad.append(f"不是 WAV（{info['ext']}）—— 分析只认 PCM WAV；"
                   "要么 App 改 WAV 重录，要么改名时加 --convert")
    if info["sr"] not in OK_SR:
        bad.append(f"采样率 {info['sr']}Hz —— 要 44100 或 48000"
                   "（16000 会丢掉扇叶失衡的高频成分）")
    if info["dur"] < MIN_DUR:
        bad.append(f"只有 {info['dur']:.1f}s —— 至少 {MIN_DUR:.0f}s")
    if info["clip_pct"] > 0.01:
        bad.append(f"削波 {info['clip_pct']:.2f}% —— 波形被切平了，高频特征已破坏，"
                   "降低录音增益或离设备远点重录")
    if info["rms_db"] < -50:
        bad.append(f"平均响度 {info['rms_db']:.0f}dBFS 近乎静音 —— 麦克风被遮 / 没开")

    if info["ch"] != 1:
        note.append(f"{info['ch']} 声道 —— 导入时会自动混成单声道，"
                    "但体积翻倍；App 里能设单声道就设上")
    if info["bits"] > 16:
        note.append(f"{info['bits']}bit —— 体积是 16bit 的 {info['bits']/16:.1f} 倍"
                    f"（本文件 {info['mb']:.1f}MB，75 段约 {info['mb']*75:.0f}MB）；"
                    "导入时加 --to16 自动转")
    if info["peak_db"] >= -0.1 and info["clip_pct"] == 0:
        note.append(f"峰值 {info['peak_db']:.1f}dBFS 贴着 0 —— AGC 自动增益可能没关，"
                    "它会把『失衡变响、遮挡变闷』的差异抹平，影响分类")
    if info["dc_pct"] > 1.0:
        note.append(f"直流偏置 {info['dc_pct']:.1f}% —— 分析时会减掉均值，影响有限")
    return bad, note


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
    print("=" * 84)
    print(f"检查目标 : {target}")
    print(f"文件数量 : {len(files)}" + ("" if ffmpeg else "    （未找到 ffmpeg，非 WAV 会直接判失败）"))
    print("=" * 84)
    print(f"{'文件':<32}{'格式':<12}{'采样率':>8}{'声道':>5}{'时长':>7}{'峰值dB':>8}{'体积':>8}")
    print("-" * 84)

    bad_files = 0
    notes: dict[str, int] = {}
    total_mb = 0.0
    for p in files:
        name = os.path.basename(p)
        try:
            info = analyse(p, ffmpeg)
            bad, note = verdict(info)
            for t in note:
                key = t.split(" —— ")[0].split("（")[0]
                notes[key] = notes.get(key, 0) + 1
            total_mb += info["mb"]
            tag = "" if not bad else "  ← 不合格"
            sr_txt = f"{info['sr']}" + ("*" if info.get("converted") else "")
            print(f"{name[:30]:<32}{str(info['bits']) + 'bit':<12}"
                  f"{sr_txt:>8}{info['ch']:>5}{info['dur']:>6.1f}s"
                  f"{info['peak_db']:>8.1f}{info['mb']:>7.1f}M{tag}")
            for pr in bad:
                print(f"      [×] {pr}")
            for pr in note:
                print(f"      [!] {pr}")
            if bad:
                bad_files += 1
        except Exception as e:
            print(f"{name[:30]:<32}读不了：{e}")
            bad_files += 1

    if any(os.path.splitext(p)[1].lower() != ".wav" for p in files):
        print("  * 该行采样率是 ffmpeg 解码时统一转成的 44100，不代表原始文件")
    print("-" * 84)
    print(f"合计 {total_mb:.1f} MB")
    if bad_files == 0:
        print(f"[√] {len(files)} 个文件全部可以进管线（不合格 0 个）。")
        if notes:
            print("[!] 但有 {} 项提示，导入时加 --to16 一步修掉：".format(len(notes)))
            for k, v in notes.items():
                print(f"      · {k} × {v}")
            print("    命令见 _私房-面试与作战/06-24bit录音怎么办.md")
        print("下一步：按 SOP 命名后放进 02-20Days-Challenge\\raw\\S1\\，")
        print("        然后跑  python 02-20Days-Challenge\\src\\selfcheck.py")
        return 0

    print(f"[×] {bad_files}/{len(files)} 个不合格，见上面每条的原因。")
    if any(not f.lower().endswith(".wav") for f in files):
        print("    如果是 m4a：用 rename_recordings.py 加 --convert 转成 WAV 再检查一次。")
    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
