"""
make_report.py —— 自动生成实验报告（数字全部从结果文件里读，不手抄）
--------------------------------------------------------------------
面试时最忌讳"报告里的数和代码跑出来的数对不上"。这个脚本把
  results/metrics.json / noise_summary.json / data_qa.md / process_logs/run_log.jsonl
里的真实数字注入报告模板，保证报告与产物永远一致。

输出：results/REPORT.md

⚠️ 报告里带「▢」的段落是必须由你补写的判断性内容（错例归因、局限、感想）。
   自动生成的部分只负责事实，不负责观点。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from logbook import summarize  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RESULTS = os.path.join(ROOT, "results")


def load_json(p, default=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default or {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default=RESULTS)
    args = ap.parse_args()

    m = load_json(os.path.join(args.outdir, "metrics.json"))
    nz = load_json(os.path.join(args.outdir, "noise_summary.json"))
    lg = summarize(os.path.join(ROOT, "process_logs", "run_log.jsonl"))

    if not m:
        print("[!] 缺少 metrics.json，请先运行 train_classify.py")
        return 1

    cv = m.get("cv", {})
    best = m.get("best_model", "?")
    b = cv.get(best, {})

    L = []
    L += ["# 基于手机内置传感器的日常旋转电器声振异常诊断",
          "", "> ICEC-2026 第二阶段工坊项目 · 项目三", ""]

    # ---------- 摘要 ----------
    _acc5 = _acc_at(nz, 5)
    _noise_sent = (f"在 5 dB 强干扰下仍保持 {_acc5:.0%} 的准确率。"
                   if _acc5 == _acc5 else           # nan 检查
                   "抗噪实验：▢（先运行 noise_robustness.py）")
    L += ["## 摘要", "",
          f"用一部手机（麦克风）采集电风扇在 5 种工况下的声音，提取时域、频域、"
          f"调制域共 {m.get('n_features', 0)} 维特征，训练分类器区分"
          f"「正常低档 / 正常高档 / 扇叶失衡 / 进风遮挡 / 停机基线」。",
          "",
          f"在**留一会话交叉验证**（用两次实验的数据训练、第三次实验的数据测试，"
          f"杜绝同段录音泄漏）下，窗级准确率 **{b.get('loso_window_acc', 0):.2%}**、"
          f"文件级（多数投票）准确率 **{b.get('loso_file_acc', 0):.2%}**、"
          f"macro-F1 **{b.get('loso_macro_f1', 0):.3f}**。"
          f"{_noise_sent}",
          ""]

    # ---------- 1 问题与动机 ----------
    L += ["## 1. 问题定义与动机", "",
          "宿舍里的电风扇、吹风机出问题，往往是「先有异响、后才有故障」。",
          "专业声振诊断设备（加速度传感器 + 频谱分析仪）价格在千元级以上，",
          "而每台手机都自带麦克风和加速度计。本项目要验证一件事："
          "**用普通手机的麦克风，能不能把「转得好好的」和「快要坏了的」分开？**", "",
          "三种异常在物理上各有独立的线索，这也是特征设计的依据：", "",
          "| 工况 | 物理变化 | 预期特征响应 |", "|---|---|---|",
          "| 扇叶失衡 | 质心偏移 → 每转一次周期性冲击 | 包络调制深度↑、峭度↑、转频边带↑ |",
          "| 进风遮挡 | 气流受阻 → 湍流噪声 | 谱质心↑、谱平坦度↑、高频段能量↑ |",
          "| 档位变化 | 转速变化 → 叶频平移 | 主峰频率↑、叶频↑、总声压↑ |",
          "| 停机 | 只剩环境噪声 | 全频段能量↓、谱平坦度接近白噪 |", ""]

    # ---------- 2 方法 ----------
    L += ["## 2. 材料与采样方法", "",
          "- 采集设备：荣耀 200（HONOR 200，青海湖电池 5200mAh）/ 系统「录音机」/"
          "原始 24bit·48kHz·双声道，导入时转 16bit·44.1kHz·单声道",
          "- 被测对象：▢（风扇型号/功率/档位数/叶片数）",
          "- 采集距离与方位：▢（如：出风口正前方 30cm、高度与扇心齐平、手机固定不动）",
          "- 工况定义：",
          "  - `N1` 正常低档 ｜ `N2` 正常高档",
          "  - `F1` 扇叶失衡：▢（具体做法，如在其中一片叶片外缘贴 2g 胶带，位置固定）",
          "  - `O1` 进风遮挡：▢（如网罩后半部覆盖 A4 纸，遮住一半进风面积）",
          "  - `S0` 停机基线：关停后 5 秒开始录，保留环境底噪",
          "- 规模：5 工况 × 3 次实验（会话 S1/S2/S3）× 每组 5 段 = **75 个 10 秒录音**", "",
          "**为什么分成 3 个会话而不是一次性录完 75 个？**",
          "同一次连续录制的所有文件共享同一个房间混响、同一段背景噪声、同一个手机摆放位置。",
          "如果按文件随机切分训练/测试，模型可以靠「背景噪声指纹」作弊，准确率会虚高。",
          "所以在设计阶段就按会话分组，评估时用「留一会话」——这也是本实验最容易被问到的一点。",
          ""]

    # ---------- 3 特征 ----------
    L += ["## 3. 特征工程（三层，共 {} 维）".format(m.get("n_features", 0)), "",
          "| 层 | 特征 | 物理含义 |", "|---|---|---|",
          "| 时域 | RMS / 峰值 / 峰值因子 / 峭度 / 偏度 / 零穿越率 / 占空比 | 有多响、有多少冲击 |",
          "| 频域 | 谱质心 / 带宽 / 滚降 / 平坦度 / 熵 / 通量 / 8 段能量占比 / 主峰频率 / 低高频比 | 能量如何分布、是否有明确音调 |",
          "| 调制域 | 叶频（谱峰间隔中位数）/ 谐波能量比 / 包络调制频率 / 调制深度 / 包络周期性 | 有没有周期性的机械缺陷 |",
          "",
          "每一维都做 mean + std 聚合，既保留均值也保留波动。", "",
          "### 判别力最强的特征（随机森林不纯度下降）", "",
          "| 排名 | 特征 | 重要性 | 物理含义 |", "|---|---|---|---|"]
    for i, f in enumerate(m.get("top_features", [])[:10], 1):
        L.append(f"| {i} | `{f['feature']}` | {f['importance']:.3f} | {f.get('meaning', '')} |")
    L += ["", "![特征重要性](feature_importance.png)", ""]

    # ---------- 4 评估 ----------
    L += ["## 4. 评估方案", "",
          "| 方案 | 用途 | 说明 |", "|---|---|---|",
          "| 分层 5 折（随机切窗） | 只作参考上限 | 同一段录音的相邻窗会同时进训练和测试，乐观偏差明显 |",
          "| **留一会话 LOSO** | **对外宣称的唯一指标** | 用 S1+S2 训练、S3 测试，轮换 3 次 |",
          "| 独立盲测集 | 交付要求③的验证视频 | 重新录一批没参与训练的文件 |", "",
          "### 模型对比", "",
          "| 模型 | LOSO 窗级准确率 | LOSO 文件级准确率 | LOSO macro-F1 | 5 折 macro-F1（参考） |",
          "|---|---|---|---|---|"]
    for name, r in cv.items():
        star = "**" if name == best else ""
        L.append(f"| {star}{name}{star} | {star}{r['loso_window_acc']:.2%}{star} | "
                 f"{r['loso_file_acc']:.2%} | {r['loso_macro_f1']:.3f} | "
                 f"{r['kfold5_macro_f1_reference']:.3f} |")
    L += ["",
          f"主模型选 `{best}`（按 LOSO macro-F1 选出）。",
          "",
          "> 注意 5 折与 LOSO 之间的差距：这正是「数据泄漏」的量化证据 —— "
          "随机切分把同一段录音的相邻窗分到了训练和测试两侧，模型记住了录音环境的指纹。"
          "两个数字都报出来，比只报一个好数字更可信。",
          "",
          "### 混淆矩阵（LOSO 拼接）", "",
          "![混淆矩阵](confusion_matrix.png)", "",
          "| 工况 | 样本数 | 召回率 | 精确率 |", "|---|---|---|---|"]
    for nm, r in m.get("per_class", {}).items():
        L.append(f"| {nm} | {r['support']} | "
                 f"{r['recall']:.1%} | {r['precision']:.1%} |" if r["recall"] is not None
                 else f"| {nm} | {r['support']} | - | - |")
    mc = m.get("most_confused_pair", {})
    L += ["",
          f"最主要的混淆发生在 **{mc.get('true')} → {mc.get('pred')}**（{mc.get('count')} 个窗）。",
          "",
          "**错例归因**：▢（自己写。可用的角度：两个工况的叶频差只有 X Hz，而 1 秒窗的频率分辨率是 3.9 Hz；"
          "或者高档位下扇叶转速波动导致叶频抖动，单窗不足以下结论，需要更长的观测窗）", ""]

    # ---------- 5 抗噪 ----------
    if nz:
        L += ["## 5. 抗噪性能", "",
              "![抗噪曲线](noise_robustness.png)", "",
              f"- 无噪声基线：{nz.get('clean_acc', 0):.2%}", ""]
        for kind, cn in (("white", "白噪声（近似空调/风机底噪）"), ("pink", "粉噪声（近似人声/室内混响）")):
            d = nz.get("acc_at_snr", {}).get(kind, {})
            if d:
                s = " ｜ ".join(f"{k}dB: {v:.0%}" for k, v in sorted(d.items(), key=lambda x: -float(x[0])))
                L.append(f"- {cn}：{s}")
        crit = nz.get("threshold_80pct_first_below_snr", {})
        L += ["",
              f"准确率跌破 80% 的临界信噪比：白噪 {crit.get('white')} dB ／ 粉噪 {crit.get('pink')} dB。",
              "",
              "工程含义：▢（用一句人话解释这个数对现场使用的意义，"
              "例如「相当于手机距风扇 30cm 时，背景里还有一台空调全力运转仍可用」）", ""]

    # ---------- 6 图 ----------
    L += ["## 6. 波形与频谱对照", "",
          "![波形与频谱](waveform_spectra.png)", "",
          "![特征雷达图](feature_radar.png)", "",
          "![特征箱线图](feature_box.png)", ""]

    # ---------- 7 过程 ----------
    L += ["## 7. 过程留痕", "",
          f"- 脚本运行记录：`process_logs/run_log.jsonl`，共 **{lg.get('runs', 0)}** 次运行，"
          f"覆盖 **{lg.get('distinct_days', 0)}** 个自然日"
          f"（{lg.get('first_ts', '—')} → {lg.get('last_ts', '—')}）",
          f"- 失败运行 {lg.get('fail', 0)} 次 —— 排障过程同样记录在案，未做删除",
          f"- Git 提交：▢（`git log --oneline --graph` 的输出，至少 3 次迭代）",
          "- 数据质检：见 `results/data_qa.md`", ""]

    # ---------- 8 局限 ----------
    L += ["## 8. 局限与下一步", "",
          "▢ 必写，至少 3 条，且要具体。可以直接用下面这些角度改写：", "",
          "1. **单一设备**：只测了一台风扇，换一台不同叶片数/转速的机器，叶频整体平移，"
          "模型很可能失效。→ 下一步应做「与设备无关」的特征（叶频归一化后的谐波结构）。",
          "2. **工况是模拟的**：贴胶带模拟失衡，与真实轴承磨损的频谱形态并不相同。",
          "3. **依赖手机摆位**：距离从 30cm 变成 1m，低频衰减明显。→ 需要 AGC 或距离归一化。",
          "4. **只有 3 个会话**：LOSO 只有 3 折，置信区间很宽。",
          "", "---", "",
          f"*本报告由 `src/make_report.py` 于 {datetime.now():%Y-%m-%d %H:%M} 自动生成，"
          "所有数字直接读取 `results/*.json` 与 `process_logs/run_log.jsonl`，未经手抄。*"]

    out = os.path.join(args.outdir, "REPORT.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print(f"报告已生成 → {out}")
    return 0


def _acc_at(nz: dict, snr: float) -> float:
    d = nz.get("acc_at_snr", {})
    vals = []
    for k in ("white", "pink"):
        v = d.get(k, {}).get(str(int(snr)))
        if v is not None:
            vals.append(float(v))
    return min(vals) if vals else float("nan")


if __name__ == "__main__":
    raise SystemExit(main())
