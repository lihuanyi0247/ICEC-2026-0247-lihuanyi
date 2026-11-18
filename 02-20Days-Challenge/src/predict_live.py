"""
predict_live.py —— 现场诊断 / 盲测演示
------------------------------------
两种用法：

  1) 单个文件诊断（录屏做"盲测视频"就用这个）
       python src/predict_live.py raw_blind/unknown01.wav
     输出：逐窗预测 + 置信度 + 时间轴条形图 + 整段结论。
     条形图是给视频观众看的：一眼看出"第 3 秒开始判成扇叶失衡"。

  2) 批量盲测（算准确率）
       python src/predict_live.py --dir raw_blind
     盲测文件名里带真实工况（如 BLIND_F1_01.wav）时自动算准确率并落表。

⚠️ 盲测集必须来自【没参与训练的新录音】。用 raw/ 里的文件自测等于自己考自己，
   面试官一眼就能看出（同一个文件的 MD5 出现在训练目录里）。
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
import warnings

import numpy as np

warnings.filterwarnings("ignore")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from audio_io import load_audio  # noqa: E402
from build_dataset import CONDITIONS  # noqa: E402
from features import extract_segment, feature_names  # noqa: E402
from logbook import Trace  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RESULTS = os.path.join(ROOT, "results")

SEG, HOP = 1.0, 0.5
BAR = " ▁▂▃▄▅▆▇█"


def load_model(path: str):
    import joblib
    if not os.path.exists(path):
        raise SystemExit(f"[!] 找不到模型 {path}，请先运行 src/train_classify.py")
    b = joblib.load(path)
    if list(b["features"]) != feature_names():
        raise SystemExit("[!] 特征列与模型不一致，请重跑 train_classify.py")
    return b["model"], b["classes"]


def diagnose(model, classes, path: str, verbose: bool = True) -> dict:
    sr, x = load_audio(path)
    seg_len, hop_len = int(SEG * sr), int(HOP * sr)
    if len(x) < seg_len:
        raise SystemExit(f"[!] {path} 太短（{len(x)/sr:.2f}s），至少需要 {SEG}s")

    n = 1 + (len(x) - seg_len) // hop_len
    feats = np.zeros((n, len(feature_names())))
    for k in range(n):
        f = extract_segment(x[k * hop_len:k * hop_len + seg_len], sr)
        feats[k] = [f[c] for c in feature_names()]

    proba = model.predict_proba(feats) if hasattr(model, "predict_proba") else None
    pred = model.predict(feats).astype(int)
    vote = np.bincount(pred, minlength=len(classes))
    final = int(np.argmax(vote))
    conf = float(np.mean(proba[:, final])) if proba is not None else float(vote[final] / n)

    if verbose:
        print(f"\n文件：{os.path.basename(path)}   （{len(x)/sr:.1f}s · {n} 个 1s 窗）")
        print("─" * 62)
        for k in range(n):
            p = proba[k] if proba is not None else None
            c = f"{p[pred[k]]:.0%}" if p is not None else "  - "
            print(f"  t={k*HOP:>4.1f}s  → {classes[pred[k]]:<8s} 置信度 {c}")
        print("─" * 62)
        # 时间轴：每个窗一个色块字符，直观看出判读是否稳定
        marks = "".join("①②③④⑤"[int(v)] for v in pred)
        print("  时间轴：" + marks + f"   （①{classes[0]} ②{classes[1]} ③{classes[2]} ④{classes[3]} ⑤{classes[4]}）")
        print(f"\n  结论：**{classes[final]}**   置信度 {conf:.1%}   "
              f"（{vote[final]}/{n} 窗一致）")
        if proba is not None:
            top = np.argsort(proba.mean(axis=0))[::-1][:3]
            print("  Top3：" + " ｜ ".join(f"{classes[i]} {proba.mean(axis=0)[i]:.1%}" for i in top))
        print()

    return {"file": path, "pred": final, "label": classes[final],
            "conf": conf, "n_win": n, "vote": vote.tolist(),
            "windows": pred.tolist()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("file", nargs="?", help="单个音频文件")
    ap.add_argument("--dir", help="批量盲测目录")
    ap.add_argument("--model", default=os.path.join(RESULTS, "model.joblib"))
    ap.add_argument("--outdir", default=RESULTS)
    args = ap.parse_args()

    model, classes = load_model(args.model)

    with Trace("predict_live", {"target": args.file or args.dir}) as t:
        if args.dir:
            files = sorted(f for f in glob.glob(os.path.join(args.dir, "**", "*.*"), recursive=True)
                           if os.path.splitext(f)[1].lower() in (".wav", ".csv", ".m4a", ".mp3"))
            if not files:
                print(f"[!] {args.dir} 下没有音频文件")
                return 1
            rows = []
            for f in files:
                r = diagnose(model, classes, f, verbose=False)
                # 文件名里找真实工况（BLIND_F1_01 / FAN_N2_B1 —— 第二段是工况码）
                stem = os.path.splitext(os.path.basename(f))[0].upper()
                true_cond = next((p for p in stem.split("_") if p in CONDITIONS), None)
                true_label = CONDITIONS[true_cond][0] if true_cond else ""
                ok = (true_label == r["label"]) if true_label else None
                rows.append({"file": os.path.basename(f), "true": true_label,
                             "pred": r["label"], "conf": round(r["conf"], 3),
                             "n_win": r["n_win"], "correct": "" if ok is None else int(ok)})
                print(f"  {os.path.basename(f):<28s} 预测={r['label']:<8s} "
                      f"置信度={r['conf']:.0%}"
                      + (f"  真实={true_label}  {'✓' if ok else '✗'}" if true_label else ""))

            import pandas as pd
            df = pd.DataFrame(rows)
            os.makedirs(args.outdir, exist_ok=True)
            out = os.path.join(args.outdir, "blind_test.csv")
            df.to_csv(out, index=False, encoding="utf-8-sig")
            scored = df[df["correct"] != ""]
            acc = float(np.mean(scored["correct"].astype(int))) if len(scored) else float("nan")
            print(f"\n盲测：{len(df)} 个文件" +
                  (f"，有标注的 {len(scored)} 个 → 文件级准确率 **{acc:.1%}**" if len(scored) else
                   "（文件名未带真实工况，未计准确率）"))
            print(f"明细 → {out}")
            t.note(files=len(df), blind_acc=None if np.isnan(acc) else round(acc, 4))
        elif args.file:
            diagnose(model, classes, args.file)
            t.note(files=1)
        else:
            ap.print_help()
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
