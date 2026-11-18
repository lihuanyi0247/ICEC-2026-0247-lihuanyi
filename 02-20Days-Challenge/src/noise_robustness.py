"""
noise_robustness.py —— 抗噪实验：信噪比逐级下降，准确率怎么掉
------------------------------------------------------------
交付要求里的"抗噪报告"需要一条曲线，而不是一句定性的"效果不错"。

做法：
  取每个工况的原始录音，按目标信噪比叠加两种典型干扰
    · 白噪声 white  —— 近似空调/风机底噪，全频段均匀
    · 粉噪声 pink   —— 近似人群说话/室内混响，低频更强（更接近真实宿舍）
  SNR 取 30 / 20 / 10 / 5 / 0 dB（0dB 意味着干扰和信号一样响，已经很难）
  重新切片 + 提特征 + 用已训练的模型预测，统计窗级准确率。

输出：
  results/noise_robustness.csv   逐 SNR / 逐工况的准确率
  results/noise_robustness.png   SNR-准确率曲线
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from audio_io import load_audio  # noqa: E402
from build_dataset import CONDITIONS, parse_name  # noqa: E402
from features import extract_segment, feature_names  # noqa: E402
from logbook import Trace  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RESULTS = os.path.join(ROOT, "results")

SR = 16000
SEG, HOP = 1.0, 0.5


def add_noise(x: np.ndarray, snr_db: float, kind: str, rng) -> np.ndarray:
    """按目标 SNR 叠加噪声。SNR = 10log10(P_signal / P_noise)。"""
    p_sig = float(np.mean(x ** 2)) + 1e-12
    p_noise = p_sig / (10 ** (snr_db / 10.0))
    n = len(x)
    if kind == "pink":
        w = rng.normal(size=n)
        F = np.fft.rfft(w)
        k = np.arange(1, len(F) + 1)
        w = np.fft.irfft(F / np.sqrt(k), n)
    else:
        w = rng.normal(size=n)
    w = w / (np.std(w) + 1e-12) * np.sqrt(p_noise)
    return np.clip(x + w, -1.0, 1.0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default=os.path.join(ROOT, "raw"))
    ap.add_argument("--model", default=os.path.join(RESULTS, "model.joblib"))
    ap.add_argument("--outdir", default=RESULTS)
    ap.add_argument("--snr", default="30,20,10,5,0")
    args = ap.parse_args()

    import joblib

    if not os.path.exists(args.model):
        print(f"[!] 找不到模型 {args.model}，请先运行 src/train_classify.py")
        return 1

    bundle = joblib.load(args.model)
    model, classes = bundle["model"], bundle["classes"]
    if list(bundle["features"]) != feature_names():
        print("[!] 特征列与模型不匹配，请重跑 train_classify.py")
        return 1

    files = sorted(f for f in glob.glob(os.path.join(args.raw, "**", "*.*"), recursive=True)
                   if os.path.splitext(f)[1].lower() in (".wav", ".csv", ".m4a", ".mp3"))
    snrs = [float(s) for s in args.snr.split(",")]

    with Trace("noise_robustness", {"n_files": len(files), "snr": snrs,
                                   "model": os.path.basename(args.model)}) as t:
        rows = []
        # 先跑无噪基线（只跑一次），再逐种噪声逐级降 SNR
        plan = [("none", None)] + [(k, s) for k in ("white", "pink") for s in snrs]
        for kind, snr in plan:
            rng = np.random.default_rng(0)
            hit = tot = 0
            per_cond = {}
            for fp in files:
                meta = parse_name(fp)
                if meta is None:
                    continue
                _, cond, _ = meta
                sr, x = load_audio(fp)
                if snr is not None:
                    x = add_noise(x, snr, kind, rng)
                seg_len, hop_len = int(SEG * sr), int(HOP * sr)
                if len(x) < seg_len:
                    continue
                k_max = 1 + (len(x) - seg_len) // hop_len
                for k in range(k_max):
                    feat = extract_segment(x[k * hop_len:k * hop_len + seg_len], sr)
                    v = np.array([[feat[c] for c in feature_names()]])
                    pred = int(model.predict(v)[0])
                    ok = pred == CONDITIONS[cond][1]
                    hit += ok
                    tot += 1
                    d = per_cond.setdefault(cond, [0, 0])
                    d[0] += ok
                    d[1] += 1
            acc = hit / tot if tot else float("nan")
            rows.append({"noise": kind,
                         "snr_db": "clean" if snr is None else snr,
                         "acc": round(acc, 4), "n": tot,
                         **{f"acc_{c}": round(v[0] / v[1], 3) if v[1] else None
                            for c, v in per_cond.items()}})
            print(f"  {'无噪声' if snr is None else f'{kind:5s} SNR={snr:>4.0f}dB'}"
                  f"  窗级准确率 {acc:.2%}  (n={tot})")

        df = pd.DataFrame(rows)
        os.makedirs(args.outdir, exist_ok=True)
        df.to_csv(os.path.join(args.outdir, "noise_robustness.csv"),
                  index=False, encoding="utf-8-sig")

        plot_noise(df, os.path.join(args.outdir, "noise_robustness.png"))

        # 掉到 80% 以下的临界 SNR —— 报告里最该写的一个数
        crit = {}
        for kind in ("white", "pink"):
            sub = df[(df["noise"] == kind)].sort_values("snr_db", ascending=False)
            bad = sub[sub["acc"] < 0.80]
            crit[kind] = float(bad["snr_db"].max()) if len(bad) else None
        df_clean = df[df["noise"] == "none"]["acc"].iloc[0]
        summary = {"clean_acc": float(df_clean),
                   "acc_at_snr": {k: {str(r["snr_db"]): r["acc"] for _, r in
                                      df[df["noise"] == k].iterrows()} for k in ("white", "pink")},
                   "threshold_80pct_first_below_snr": crit}
        with open(os.path.join(args.outdir, "noise_summary.json"), "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print(f"\n干净条件 {df_clean:.2%}；准确率跌破 80% 的临界 SNR："
              f"白噪 {crit['white']} dB / 粉噪 {crit['pink']} dB")
        print("→ results/noise_robustness.png ｜ noise_summary.json")
        t.note(clean_acc=round(float(df_clean), 4), crit=crit)
    return 0


def plot_noise(df: pd.DataFrame, path: str):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    fig, ax = plt.subplots(figsize=(7.0, 4.4), dpi=200)
    clean = df[df["noise"] == "none"]["acc"].iloc[0]
    ax.axhline(clean, ls="--", lw=1.2, color="#888", label=f"无噪声基线 {clean:.1%}")
    ax.axhline(0.8, ls=":", lw=1.0, color="#c0392b")
    for kind, color, marker, name, dy in (("white", "#2f6fb0", "o", "白噪声干扰", 8),
                                        ("pink", "#d9822b", "s", "粉噪声干扰", -15)):
        sub = df[df["noise"] == kind].sort_values("snr_db", ascending=False)
        if len(sub):
            ax.plot(sub["snr_db"], sub["acc"], marker=marker, color=color, lw=1.8, label=name)
            for _, r in sub.iterrows():
                ax.annotate(f"{r['acc']:.0%}", (r["snr_db"], r["acc"]),
                            textcoords="offset points", xytext=(0, dy),
                            ha="center", fontsize=7.5, color=color)
    ax.set_xlabel("信噪比 SNR (dB)   ← 干扰越来越强", fontsize=10)
    ax.set_ylabel("窗级准确率", fontsize=10)
    ax.set_title("抗噪性能：不同信噪比下的识别准确率", fontsize=11)
    ax.invert_xaxis()
    ax.set_ylim(0, 1.02)
    ax.grid(alpha=0.25)
    ax.legend(fontsize=8.5, loc="lower right")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


if __name__ == "__main__":
    raise SystemExit(main())
