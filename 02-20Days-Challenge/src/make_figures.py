"""
make_figures.py —— 出报告用的图
------------------------------
  1) results/waveform_spectra.png  5 工况 × (时域波形 + 平均频谱)，一图看懂"为什么能分"
  2) results/feature_radar.png     5 工况的关键特征雷达图（归一化后对比）
  3) results/feature_box.png       最能区分工况的 4 个特征箱线图

这些图直接贴进 README 和报告，也是面试时"你凭什么说失衡特征是调制深度"
这个问题的答案。
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
from build_dataset import CONDITIONS, parse_name  # noqa: E402
from features import N_FFT, feature_names  # noqa: E402
from logbook import Trace  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RESULTS = os.path.join(ROOT, "results")

ORDER = list(CONDITIONS.keys())
RADAR_KEYS = [
    ("seg_rms", "响度", 1),
    ("crest_mean", "峰值因子", 1),
    ("kurt_mean", "峭度", 1),
    ("centroid_mean", "谱质心", 1),
    ("flatness_mean", "谱平坦度", 1),
    ("rolloff85_mean", "高频滚降", 1),
    ("lowhigh_ratio_mean", "低/高频比", "log"),
    ("envelope_mod_depth", "调制深度", 1),
]


def _style():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    return plt


def fig_waveform_spectra(raw_dir: str, path: str):
    plt = _style()
    picks = {}
    for f in sorted(glob.glob(os.path.join(raw_dir, "**", "*.*"), recursive=True)):
        meta = parse_name(f)
        if meta and meta[1] not in picks:
            picks[meta[1]] = f

    fig, axes = plt.subplots(len(ORDER), 2, figsize=(9.6, 1.55 * len(ORDER) + 1), dpi=200)
    for i, cond in enumerate(ORDER):
        ax_t, ax_f = axes[i]
        fp = picks.get(cond)
        if fp is None:
            ax_t.axis("off"); ax_f.axis("off")
            continue
        sr, x = load_audio(fp)
        t = np.arange(len(x)) / sr
        ax_t.plot(t, x, lw=0.35, color="#2f6fb0")
        ax_t.set_xlim(0, t[-1] if len(t) else 1)
        ax_t.set_ylim(-1.05, 1.05)
        ax_t.set_ylabel(CONDITIONS[cond][0], fontsize=7.5)
        ax_t.tick_params(labelsize=6)
        if i == 0:
            ax_t.set_title("时域波形（归一化）", fontsize=9)

        spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
        fr = np.fft.rfftfreq(len(x), 1 / sr)
        m = fr <= 2000
        ax_f.plot(fr[m], spec[m] / (spec.max() + 1e-9), lw=0.5, color="#d9822b")
        ax_f.set_xlim(0, 2000)
        ax_f.tick_params(labelsize=6)
        if i == 0:
            ax_f.set_title("平均频谱（0–2000 Hz）", fontsize=9)
    fig.suptitle("5 种工况的时域/频域形态对照", fontsize=11.5, y=0.995)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def fig_radar(df: pd.DataFrame, path: str):
    plt = _style()
    feats, labels = [], []
    for key, name, mode in RADAR_KEYS:
        col = "seg_rms" if key == "seg_rms" else key
        if col not in df.columns:
            continue
        v = df.groupby("cond")[col].mean()
        v = np.log(v + 1e-6) if mode == "log" else v
        lo, hi = v.min(), v.max()
        v = (v - lo) / (hi - lo) if hi > lo else v * 0 + 0.5
        feats.append([float(v.get(c, 0.5)) for c in ORDER])
        labels.append(name)
    if not feats:
        return

    ang = np.linspace(0, 2 * np.pi, len(labels), endpoint=False).tolist()
    ang += ang[:1]
    fig = plt.figure(figsize=(5.6, 5.6), dpi=200)
    ax = fig.add_subplot(111, polar=True)
    colors = ["#2f6fb0", "#4a9b8e", "#c0392b", "#d9822b", "#7b6fb0"]
    for i, cond in enumerate(ORDER):
        vals = [f[i] for f in feats]
        vals += vals[:1]
        ax.plot(ang, vals, lw=1.6, color=colors[i], label=CONDITIONS[cond][0])
        ax.fill(ang, vals, color=colors[i], alpha=0.07)
    ax.set_xticks(ang[:-1], labels, fontsize=8.5)
    ax.set_yticklabels([])
    ax.set_ylim(0, 1.05)
    ax.set_title("关键特征雷达图（各类均值，min-max 归一化）", fontsize=10.5, pad=16)
    ax.legend(loc="upper left", bbox_to_anchor=(1.16, 1.06), fontsize=7.6,
              framealpha=0.9, borderpad=0.5)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def fig_box(df: pd.DataFrame, path: str, cols=None):
    plt = _style()
    cols = cols or [("centroid_mean", "谱质心 Hz"), ("flatness_mean", "谱平坦度"),
                    ("envelope_mod_depth", "调制深度"), ("kurt_mean", "峭度")]
    cols = [c for c in cols if c[0] in df.columns]
    fig, axes = plt.subplots(1, len(cols), figsize=(3.1 * len(cols), 3.8), dpi=200)
    if len(cols) == 1:
        axes = [axes]
    short = [CONDITIONS[c][0].replace("-", "\n") for c in ORDER]
    for ax, (col, name) in zip(axes, cols):
        data = [df.loc[df["cond"] == c, col].replace([np.inf, -np.inf], np.nan).dropna()
                for c in ORDER]
        bp = ax.boxplot(data, tick_labels=short, showfliers=False, patch_artist=True,
                        medianprops={"color": "#1a1a1a", "lw": 1.2})
        for patch, color in zip(bp["boxes"], ["#2f6fb0", "#4a9b8e", "#c0392b", "#d9822b", "#7b6fb0"]):
            patch.set_facecolor(color); patch.set_alpha(0.55); patch.set_edgecolor("#333")
        ax.set_title(name, fontsize=9.5)
        ax.tick_params(labelsize=7)
        ax.grid(axis="y", alpha=0.25)
    fig.suptitle("四个最具判别力的特征分布", fontsize=11)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default=os.path.join(ROOT, "raw"))
    ap.add_argument("--features", default=os.path.join(RESULTS, "features.csv"))
    ap.add_argument("--outdir", default=RESULTS)
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    with Trace("make_figures") as t:
        if glob.glob(os.path.join(args.raw, "**", "*.*"), recursive=True):
            fig_waveform_spectra(args.raw, os.path.join(args.outdir, "waveform_spectra.png"))
            print("  ✓ waveform_spectra.png")
        if os.path.exists(args.features):
            df = pd.read_csv(args.features)
            fig_radar(df, os.path.join(args.outdir, "feature_radar.png"))
            fig_box(df, os.path.join(args.outdir, "feature_box.png"))
            print("  ✓ feature_radar.png / feature_box.png")
        t.note(ok=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
