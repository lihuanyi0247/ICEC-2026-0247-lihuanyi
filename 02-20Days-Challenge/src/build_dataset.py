"""
build_dataset.py —— 从原始录音构建特征数据集
--------------------------------------------
扫描 raw/S{1,2,3}/ 下的 WAV，按"1 秒窗 + 0.5 秒步长"切片，逐窗提特征，
输出 results/features.csv（含 file / cond / session / seg_idx / seg_t 元数据）。

为什么要切片而不是整段提特征：
  1 秒窗是"实时诊断仪"实际能拿到的决策粒度 —— 现场不可能等 10 秒出结果；
  同时把 75 个文件扩成 ~1400 个样本，小样本下模型才稳。
  文件级结论用多数投票回推，两个指标都报（窗级 + 文件级）。

命名约定（必须遵守，否则脚本认不出工况）：
  raw/S1/FAN_N1_S1_01.wav
        │   │  │   └ 同一会话内第几个（01~05）
        │   │  └ 会话号（S1/S2/S3，同一次实验连续录的一批）
        │   └ 工况代码
        └ 设备前缀（FAN=电风扇 / DRY=吹风机 / BIKE=单车）

用法：
  python src/build_dataset.py                # 扫 raw/ 全部会话
  python src/build_dataset.py --seg 1.0 --hop 0.5
"""

from __future__ import annotations

import argparse
import glob
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from audio_io import load_audio  # noqa: E402
from features import extract_segment, feature_names  # noqa: E402
from logbook import Trace  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RAW = os.path.join(ROOT, "raw")
OUT = os.path.join(ROOT, "results")

# 工况代码 → (中文名, 标签序号)。顺序即分类器类别顺序。
CONDITIONS = {
    "N1": ("正常-低档", 0),
    "N2": ("正常-高档", 1),
    "F1": ("扇叶失衡", 2),
    "O1": ("进风遮挡", 3),
    "S0": ("停机-环境基线", 4),
}


def parse_name(path: str) -> tuple[str, str, str] | None:
    """从文件名解析 (设备, 工况, 会话)。认不出返回 None。"""
    stem = os.path.splitext(os.path.basename(path))[0]
    parts = stem.split("_")
    if len(parts) < 2:
        return None
    dev, cond = parts[0], parts[1].upper()
    if cond not in CONDITIONS:
        return None
    session = parts[2].upper() if len(parts) >= 3 and parts[2].upper().startswith("S") else "S0"
    return dev, cond, session


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default=RAW)
    ap.add_argument("--seg", type=float, default=1.0, help="切片长度（秒）")
    ap.add_argument("--hop", type=float, default=0.5, help="切片步长（秒）")
    ap.add_argument("--out", default=os.path.join(OUT, "features.csv"))
    args = ap.parse_args()

    files = sorted(
        f for f in glob.glob(os.path.join(args.raw, "**", "*.*"), recursive=True)
        if os.path.splitext(f)[1].lower() in (".wav", ".csv", ".m4a", ".mp3")
    )

    with Trace("build_dataset", {"raw": os.path.relpath(args.raw, ROOT),
                                "seg": args.seg, "hop": args.hop,
                                "n_files_found": len(files)}) as t:
        if not files:
            print(f"[!] {args.raw} 下没有找到录音文件。")
            print("    先按 docs/01-录音与实验SOP.md 录好 75 个 wav，再跑这一步。")
            t.note(rows=0)
            return 1

        rows: list[dict] = []
        skipped: list[str] = []
        for i, fp in enumerate(files, 1):
            meta = parse_name(fp)
            if meta is None:
                skipped.append(os.path.basename(fp))
                continue
            dev, cond, session = meta
            try:
                sr, x = load_audio(fp)
            except Exception as e:  # 单个坏文件不该中断整批
                skipped.append(f"{os.path.basename(fp)}: {e}")
                continue

            seg_len = int(args.seg * sr)
            hop_len = int(args.hop * sr)
            if len(x) < seg_len:
                skipped.append(f"{os.path.basename(fp)}: 太短({len(x)/sr:.1f}s)")
                continue

            n = 1 + (len(x) - seg_len) // hop_len
            for k in range(n):
                chunk = x[k * hop_len: k * hop_len + seg_len]
                feat = extract_segment(chunk, sr)
                feat.update({
                    "file": os.path.basename(fp),
                    "device": dev,
                    "cond": cond,
                    "cond_cn": CONDITIONS[cond][0],
                    "label": CONDITIONS[cond][1],
                    "session": session,
                    "seg_idx": k,
                    "seg_t": round(k * args.hop, 3),
                    "sr": sr,
                    "seg_seconds": len(chunk) / sr,
                })
                rows.append(feat)

            if i % 10 == 0:
                print(f"  已处理 {i}/{len(files)} 个文件 → {len(rows)} 个样本", flush=True)

        if not rows:
            print("[!] 没有生成任何样本。检查文件名是否符合 FAN_N1_S1_01.wav 的格式。")
            t.note(rows=0)
            return 1

        df = pd.DataFrame(rows)
        cols = ["file", "device", "cond", "cond_cn", "label", "session",
                "seg_idx", "seg_t", "sr", "seg_seconds"] + feature_names()
        cols = [c for c in cols if c in df.columns]
        df = df[cols]

        os.makedirs(os.path.dirname(args.out), exist_ok=True)
        df.to_csv(args.out, index=False, encoding="utf-8-sig")

        # 覆盖率统计：哪一类样本数不足，一眼看出来
        cover = df.groupby(["cond", "cond_cn"]).agg(
            samples=("file", "size"), files=("file", "nunique")
        ).reset_index()
        print("\n=== 数据集概览 ===")
        print(cover.to_string(index=False))
        print(f"\n总样本 {len(df)} 行 · {df['file'].nunique()} 个文件 · "
              f"{df[feature_names()].shape[1]} 维特征 → {args.out}")
        if skipped:
            print(f"[!] {len(skipped)} 个文件被跳过：")
            for s in skipped[:10]:
                print("    -", s)

        t.note(rows=int(len(df)), files=int(df["file"].nunique()),
               n_features=int(len(feature_names())), skipped=len(skipped),
               out=os.path.relpath(args.out, ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
