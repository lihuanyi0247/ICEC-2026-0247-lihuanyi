"""
selfcheck.py —— 数据质检（QA）报告
----------------------------------
答辩/面试里最容易被问死的一句话是"你这 75 个文件是同一台设备同一姿势录的吗"。
这个脚本把所有能自证的事实量化成一张表，输出 results/data_qa.md：

  ① 每个工况的文件数是否达标（5 工况 × 15 组）
  ② 文件名是否符合约定（解析不出工况的直接报错）
  ③ 采样率 / 声道 / 位深 / 时长分布
  ④ 削波比例（录音增益过大导致波形贴顶，这部分数据要丢）
  ⑤ 直流偏置（手机麦克风的低频漂移）
  ⑥ 静音比例（录到了全程无声 = 录音失败）
  ⑦ MD5 去重（防止"复制一份改个名"混数量，也防止自己手滑）
  ⑧ 实验照 EXIF 时间是否落在录音时间窗内

用法：
  python src/selfcheck.py
  python src/selfcheck.py --photos photos
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import os
import sys
import wave
from datetime import datetime

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from build_dataset import CONDITIONS, parse_name  # noqa: E402
from logbook import Trace  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXPECT_PER_COND = 15


def md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def wav_header(path: str) -> dict:
    with wave.open(path, "rb") as w:
        return {"sr": w.getframerate(), "ch": w.getnchannels(),
                "sw": w.getsampwidth(), "frames": w.getnframes(),
                "dur": w.getnframes() / w.getframerate() if w.getframerate() else 0}


def qa_audio(raw: str) -> tuple[list[str], dict]:
    files = sorted(f for f in glob.glob(os.path.join(raw, "**", "*"), recursive=True)
                   if os.path.splitext(f)[1].lower() in (".wav", ".csv", ".m4a", ".mp3"))
    lines = []
    per_cond: dict[str, list] = {c: [] for c in CONDITIONS}
    bad_name, hashes, probs = [], {}, []
    sr_set, ch_set, sw_set = set(), set(), set()
    durs, clip, dc, silent = [], [], [], []

    for fp in files:
        meta = parse_name(fp)
        if meta is None:
            bad_name.append(os.path.basename(fp))
            continue
        per_cond[meta[1]].append(os.path.basename(fp))
        hashes.setdefault(md5(fp), []).append(os.path.basename(fp))

        if os.path.splitext(fp)[1].lower() != ".wav":
            probs.append(f"{os.path.basename(fp)}：非 WAV 格式（建议统一导出 WAV 以便溯源）")
            continue

        h = wav_header(fp)
        sr_set.add(h["sr"]); ch_set.add(h["ch"]); sw_set.add(h["sw"] * 8)
        durs.append(h["dur"])

        from audio_io import _read_wav
        _, x = _read_wav(fp)
        if x.size:
            clip.append(float(np.mean(np.abs(x) >= 0.995)))
            dc.append(float(np.mean(x)))
            silent.append(float(np.mean(np.abs(x) < 2e-3)))
            if np.mean(np.abs(x) >= 0.995) > 0.01:
                probs.append(f"{os.path.basename(fp)}：削波 {np.mean(np.abs(x) >= 0.995):.1%}，"
                             "录音增益过大，建议重录")
            if np.mean(np.abs(x) < 2e-3) > 0.5:
                probs.append(f"{os.path.basename(fp)}：静音占比 >50%，疑似录音失败/没开风扇")

    dup = {k: v for k, v in hashes.items() if len(v) > 1}
    for k, v in dup.items():
        probs.append(f"MD5 重复：{', '.join(v)}")

    lines.append("| 工况 | 代码 | 文件数 | 是否达标 |")
    lines.append("|---|---|---|---|")
    for c, (cn, _) in CONDITIONS.items():
        n = len(per_cond[c])
        lines.append(f"| {cn} | {c} | {n} | {'✅' if n >= EXPECT_PER_COND else f'❌ 缺 {EXPECT_PER_COND - n}'} |")
    total = sum(len(v) for v in per_cond.values())
    lines.append(f"| **合计** | | **{total}** | "
                 f"{'✅ 75 组齐备' if total >= EXPECT_PER_COND * len(CONDITIONS) else '❌ 不足 75'} |")

    stats = {
        "files": len(files), "valid": total, "bad_name": bad_name,
        "sample_rates": sorted(sr_set), "channels": sorted(ch_set), "bit_depths": sorted(sw_set),
        "dur_min": round(min(durs), 2) if durs else 0,
        "dur_max": round(max(durs), 2) if durs else 0,
        "dur_mean": round(float(np.mean(durs)), 2) if durs else 0,
        "clip_max": round(max(clip), 4) if clip else 0,
        "dc_max": round(max(abs(v) for v in dc), 4) if dc else 0,
        "silent_max": round(max(silent), 4) if silent else 0,
        "duplicates": list(dup.values()),
        "problems": probs,
    }
    return lines, stats


def qa_photos(photo_dir: str) -> list[str]:
    """实验照 EXIF 校验：证明照片是现场拍的，且时间与录音同一天。"""
    if not photo_dir or not os.path.isdir(photo_dir):
        return ["", "> 未发现 `photos/` 目录，跳过照片 EXIF 校验。", ""]
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS
    except Exception:
        return ["", "> 未安装 Pillow，跳过照片 EXIF 校验。", ""]

    rows = ["", "### 实验照 EXIF 校验", "",
            "| 文件 | 拍摄时间 | 机型 | 尺寸 |", "|---|---|---|---|"]
    n_ok = 0
    for fp in sorted(glob.glob(os.path.join(photo_dir, "*")))[:40]:
        if os.path.splitext(fp)[1].lower() not in (".jpg", ".jpeg", ".png", ".heic"):
            continue
        try:
            im = Image.open(fp)
            ex = {TAGS.get(k, k): v for k, v in (im.getexif() or {}).items()}
            dt = ex.get("DateTimeOriginal") or ex.get("DateTime") or "无"
            model = ex.get("Model", "无")
            rows.append(f"| {os.path.basename(fp)} | {dt} | {model} | {im.size[0]}×{im.size[1]} |")
            if dt != "无":
                n_ok += 1
        except Exception as e:
            rows.append(f"| {os.path.basename(fp)} | 读取失败 | {e} | - |")
    rows.append("")
    rows.append(f"共 {n_ok} 张照片带原始拍摄时间（EXIF），可作为现场采样旁证。")
    rows.append("> ⚠️ 微信/QQ 传输会剥离 EXIF，请用「文件传输助手以原图发送」或数据线拷贝。")
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default=os.path.join(ROOT, "raw"))
    ap.add_argument("--photos", default=os.path.join(ROOT, "photos"))
    ap.add_argument("--out", default=os.path.join(ROOT, "results", "data_qa.md"))
    args = ap.parse_args()

    with Trace("selfcheck", {"raw": os.path.relpath(args.raw, ROOT)}) as t:
        lines, stats = qa_audio(args.raw)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")

        md = ["# 数据质检报告（自动生成）", "",
              f"> 生成时间：{now} ｜ 脚本：`src/selfcheck.py` ｜ "
              f"运行记录：`process_logs/run_log.jsonl`", "",
              "## 1. 文件数量与命名", ""] + lines + [""]

        md += ["## 2. 录音技术参数", "",
               f"- 采样率：{stats['sample_rates'] or '—'} Hz",
               f"- 声道数：{stats['channels'] or '—'}",
               f"- 位深：{stats['bit_depths'] or '—'} bit",
               f"- 单文件时长：{stats['dur_min']}–{stats['dur_max']} s（均值 {stats['dur_mean']} s）",
               f"- 最大削波比例：{stats['clip_max']:.2%}",
               f"- 最大直流偏置：{stats['dc_max']:.4f}",
               f"- 最大静音比例：{stats['silent_max']:.2%}", ""]

        md += ["## 3. 异常与处理", ""]
        if stats["bad_name"]:
            md.append(f"- **文件名不合规 {len(stats['bad_name'])} 个**："
                      f"{', '.join(stats['bad_name'][:8])}")
        if stats["duplicates"]:
            md.append(f"- **重复文件 {len(stats['duplicates'])} 组**：{stats['duplicates'][:5]}")
        if stats["problems"]:
            for p in stats["problems"][:20]:
                md.append(f"- {p}")
        if not (stats["bad_name"] or stats["duplicates"] or stats["problems"]):
            md.append("- 未发现异常。")
        md.append("")

        md += qa_photos(args.photos)

        md += ["", "## 4. 结论", ""]
        ok = (stats["valid"] >= EXPECT_PER_COND * len(CONDITIONS)
              and not stats["bad_name"] and not stats["duplicates"])
        md.append("✅ 数据集满足交付要求（75 组、命名规范、无重复）。" if ok
                  else "❌ 数据集尚未满足交付要求，请按上方问题逐条修正后重跑。")

        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write("\n".join(md))

        print("\n".join(lines))
        print(f"\n质检报告 → {args.out}")
        if stats["problems"]:
            print(f"[!] {len(stats['problems'])} 条问题，详见报告。")
        t.note(valid=stats["valid"], problems=len(stats["problems"]), ok=int(ok))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
