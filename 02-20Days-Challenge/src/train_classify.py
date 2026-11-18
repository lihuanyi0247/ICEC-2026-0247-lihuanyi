"""
train_classify.py —— 5 工况分类：训练、交叉验证、混淆矩阵、特征重要性
--------------------------------------------------------------------
评估策略（这一点面试必问，务必理解）：

  [主指标] 留一会话交叉验证 LOSO（Leave-One-Session-Out）
      同一次实验连续录的一批音频共享同一个环境（同一房间、同一背景噪声、
      同一手机摆放位置）。如果随机切分，训练集和测试集里会有"同一段录音
      切出来的相邻窗"，模型可以靠"背景噪声指纹"作弊，报出的准确率虚高。
      所以主指标按会话分组：用 S1+S2 训练、S3 测试，轮换三次取平均。
      —— 这才是"拿到一台没见过的风扇现场盲测"的真实水平。

  [参考指标] 分层 5 折（随机切分窗口）
      只用来观察上限，不作对外宣称。

  [最终盲测] 单独录制的盲测集（raw_blind/），完全独立，对应交付要求里的
      "盲测准确率视频"。

用法：
  python src/train_classify.py
  python src/train_classify.py --model svm
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from features import describe, feature_names  # noqa: E402
from logbook import Trace  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RESULTS = os.path.join(ROOT, "results")

CLASS_NAMES = ["正常-低档", "正常-高档", "扇叶失衡", "进风遮挡", "停机-基线"]


def make_models() -> dict:
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.neighbors import KNeighborsClassifier
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.svm import SVC

    return {
        "svm": Pipeline([
            ("sc", StandardScaler()),
            ("clf", SVC(C=10, gamma="scale", kernel="rbf",
                        probability=True, class_weight="balanced", random_state=0)),
        ]),
        "rf": RandomForestClassifier(
            n_estimators=400, max_depth=None, min_samples_leaf=2,
            class_weight="balanced", n_jobs=-1, random_state=0),
        "knn": Pipeline([
            ("sc", StandardScaler()),
            ("clf", KNeighborsClassifier(n_neighbors=9, weights="distance")),
        ]),
        "logreg": Pipeline([
            ("sc", StandardScaler()),
            ("clf", LogisticRegression(max_iter=3000, C=1.0,
                                       class_weight="balanced")),
        ]),
    }


def load_features(path: str):
    df = pd.read_csv(path)
    missing = [c for c in feature_names() if c not in df.columns]
    if missing:
        raise SystemExit(f"特征列缺失 {len(missing)} 个，请重跑 build_dataset.py：{missing[:5]}")
    X = df[feature_names()].to_numpy(dtype=np.float64)
    y = df["label"].to_numpy()
    groups = df["session"].astype(str).to_numpy()
    return df, X, y, groups


def file_level_vote(df: pd.DataFrame, pred: np.ndarray) -> tuple[float, pd.DataFrame]:
    """窗级预测 → 文件级多数投票，返回 (文件级准确率, 明细表)。"""
    tmp = df[["file", "label", "cond_cn"]].copy()
    tmp["pred"] = pred
    rows = []
    for f, g in tmp.groupby("file", sort=False):
        vc = g["pred"].value_counts()
        rows.append({"file": f, "true": int(g["label"].iloc[0]),
                     "pred": int(vc.index[0]), "n_win": len(g),
                     "vote_frac": float(vc.iloc[0] / len(g))})
    det = pd.DataFrame(rows)
    return float((det["true"] == det["pred"]).mean()), det


def plot_confusion(cm: np.ndarray, acc: float, path: str, title: str):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    fig, ax = plt.subplots(figsize=(6.4, 5.4), dpi=200)
    im = ax.imshow(cm, cmap="Blues")
    ax.set_xticks(range(len(CLASS_NAMES)), CLASS_NAMES, rotation=35, ha="right", fontsize=9)
    ax.set_yticks(range(len(CLASS_NAMES)), CLASS_NAMES, fontsize=9)
    ax.set_xlabel("预测", fontsize=10)
    ax.set_ylabel("真实", fontsize=10)
    ax.set_title(f"{title}\n窗级准确率 {acc:.2%}", fontsize=11)
    thresh = cm.max() / 2 if cm.max() else 0.5
    for i in range(cm.shape[0]):
        for j in range(cm.shape[1]):
            ax.text(j, i, str(cm[i, j]), ha="center", va="center", fontsize=9,
                    color="white" if cm[i, j] > thresh else "#1a1a1a")
    fig.colorbar(im, ax=ax, fraction=0.045)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def plot_importance(names: np.ndarray, vals: np.ndarray, path: str, top: int = 15):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    order = np.argsort(vals)[::-1][:top][::-1]
    fig, ax = plt.subplots(figsize=(7.2, 5.0), dpi=200)
    ax.barh(range(len(order)), vals[order], color="#2f6fb0")
    ax.set_yticks(range(len(order)), [names[i] for i in order], fontsize=8.5)
    ax.set_xlabel("重要性（不纯度下降）", fontsize=10)
    ax.set_title(f"Top {top} 判别特征", fontsize=11)
    for k, i in enumerate(order):
        ax.text(vals[i], k, f" {vals[i]:.3f}", va="center", fontsize=7.5)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--features", default=os.path.join(RESULTS, "features.csv"))
    ap.add_argument("--model", default="auto", choices=["auto", "svm", "rf", "knn", "logreg"])
    ap.add_argument("--outdir", default=RESULTS)
    args = ap.parse_args()

    if not os.path.exists(args.features):
        print(f"[!] 找不到特征表 {args.features}，请先运行 src/build_dataset.py")
        return 1

    from sklearn.metrics import (accuracy_score, classification_report,
                                 confusion_matrix, f1_score)

    os.makedirs(args.outdir, exist_ok=True)
    df, X, y, groups = load_features(args.features)

    with Trace("train_classify", {"n_samples": len(y), "n_features": X.shape[1],
                                 "sessions": sorted(set(groups.tolist())),
                                 "model_arg": args.model}) as t:
        models = make_models()
        sess = sorted(set(groups.tolist()))
        cv_result = {}

        print(f"样本 {len(y)} · 特征 {X.shape[1]} · 会话 {sess}\n")

        # ---------------- 1. 留一会话交叉验证（主指标） ----------------
        print("=== 留一会话交叉验证 LOSO（主指标，面试以这个为准）===")
        for name, mdl in models.items():
            if len(sess) >= 2:
                y_true_all, y_pred_all, file_accs = [], [], []
                for s in sess:
                    te = groups == s
                    tr = ~te
                    from sklearn.base import clone
                    m = clone(mdl)
                    m.fit(X[tr], y[tr])
                    p = m.predict(X[te])
                    y_true_all.append(y[te])
                    y_pred_all.append(p)
                    fa, _ = file_level_vote(df[te], p)
                    file_accs.append(fa)
                yt = np.concatenate(y_true_all)
                yp = np.concatenate(y_pred_all)
                win_acc = accuracy_score(yt, yp)
                macro_f1 = f1_score(yt, yp, average="macro")
                file_acc = float(np.mean(file_accs))
            else:
                win_acc = macro_f1 = file_acc = float("nan")

            # 分层 K 折（参考上限）
            from sklearn.model_selection import StratifiedKFold, cross_val_score
            n_splits = min(5, int(np.bincount(y).min()))
            skf = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=0)
            ref = float(np.mean(cross_val_score(mdl, X, y, cv=skf,
                                                scoring="f1_macro", n_jobs=-1)))
            cv_result[name] = {
                "loso_window_acc": round(float(win_acc), 4),
                "loso_macro_f1": round(float(macro_f1), 4),
                "loso_file_acc": round(float(file_acc), 4),
                "kfold5_macro_f1_reference": round(ref, 4),
            }
            print(f"  {name:7s} LOSO 窗级 {win_acc:.2%} | 文件级 {file_acc:.2%} | "
                  f"macro-F1 {macro_f1:.3f} || 5折(参考) {ref:.3f}")

        # ---------------- 2. 选主模型，出混淆矩阵 ----------------
        if args.model == "auto":
            best = max(cv_result, key=lambda k: (cv_result[k]["loso_macro_f1"],
                                                 cv_result[k]["kfold5_macro_f1_reference"]))
        else:
            best = args.model
        print(f"\n=== 主模型：{best}（按 LOSO macro-F1 选出）===")

        # 用全量数据重训一次给现场演示用，同时用 LOSO 拼接结果画混淆矩阵（才诚实）
        from sklearn.base import clone
        yt_all, yp_all = [], []
        for s in sess:
            te = groups == s
            m = clone(models[best])
            m.fit(X[~te], y[~te])
            yt_all.append(y[te]); yp_all.append(m.predict(X[te]))
        yt = np.concatenate(yt_all); yp = np.concatenate(yp_all)

        cm = confusion_matrix(yt, yp, labels=list(range(len(CLASS_NAMES))))
        plot_confusion(cm, accuracy_score(yt, yp),
                       os.path.join(args.outdir, "confusion_matrix.png"),
                       f"混淆矩阵 · {best} · LOSO 拼接")
        print("\n分类报告（LOSO 拼接）：")
        rep = classification_report(yt, yp, labels=list(range(len(CLASS_NAMES))),
                                   target_names=CLASS_NAMES, zero_division=0, digits=3)
        print(rep)

        per_class = {}
        for i, nm in enumerate(CLASS_NAMES):
            tp = cm[i, i]
            per_class[nm] = {
                "support": int(cm[i].sum()),
                "recall": round(float(tp / cm[i].sum()), 3) if cm[i].sum() else None,
                "precision": round(float(tp / cm[:, i].sum()), 3) if cm[:, i].sum() else None,
            }

        # 最容易混淆的一对（面试会追问）
        off = cm.copy(); np.fill_diagonal(off, 0)
        i, j = np.unravel_index(np.argmax(off), off.shape)
        top_confusion = {"true": CLASS_NAMES[i], "pred": CLASS_NAMES[j],
                         "count": int(off[i, j])}

        # ---------------- 3. 特征重要性 ----------------
        rf = models["rf"].fit(X, y)
        imp = rf.feature_importances_
        names = np.array(feature_names())
        plot_importance(names, imp, os.path.join(args.outdir, "feature_importance.png"))
        top_feats = [
            {"feature": str(names[k]), "importance": round(float(imp[k]), 4),
             "meaning": describe(str(names[k]))}
            for k in np.argsort(imp)[::-1][:12]
        ]

        # ---------------- 4. 存档 ----------------
        metrics = {
            "n_samples": int(len(y)),
            "n_features": int(X.shape[1]),
            "sessions": sess,
            "cv": cv_result,
            "best_model": best,
            "per_class": per_class,
            "most_confused_pair": top_confusion,
            "top_features": top_feats,
        }
        with open(os.path.join(args.outdir, "metrics.json"), "w", encoding="utf-8") as f:
            json.dump(metrics, f, ensure_ascii=False, indent=2)

        # 文件级明细（面试时可以直接指着说"这 3 个样本判错了，原因是…"）
        fa, det = file_level_vote(df, yp)
        det.to_csv(os.path.join(args.outdir, "file_level_predictions.csv"),
                   index=False, encoding="utf-8-sig")
        print(f"\n文件级（多数投票）准确率：{fa:.2%}")
        bad = det[det["true"] != det["pred"]]
        if len(bad):
            print(f"判错的 {len(bad)} 个文件（面试要能解释）：")
            for _, r in bad.head(8).iterrows():
                print(f"  {r['file']}  真实={CLASS_NAMES[int(r['true'])]}  "
                      f"预测={CLASS_NAMES[int(r['pred'])]}  投票占比={r['vote_frac']:.2f}")

        # 留一份全量模型给 predict_live / 盲测用
        try:
            import joblib
            full = clone(models[best]).fit(X, y)
            joblib.dump({"model": full, "classes": CLASS_NAMES,
                         "features": feature_names()},
                        os.path.join(args.outdir, "model.joblib"))
            print(f"\n模型已保存 → {os.path.join(args.outdir, 'model.joblib')}")
        except Exception as e:
            print(f"[!] 模型保存失败（不影响结论）：{e}")

        t.note(best_model=best, loso_macro_f1=cv_result[best]["loso_macro_f1"],
               loso_file_acc=round(fa, 4), n_samples=int(len(y)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
