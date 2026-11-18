"""
analyze_t02.py —— 课题 02：手机多工况发热与掉电
------------------------------------------------
读 raw_data/记录表.csv，输出：
  figures/T02-电量曲线.png
  figures/T02-温度曲线.png
  figures/T02-工况功率对比.png
  raw_data/分析结果.csv        （每工况汇总：耗电率、功率、温升）
  T02-结论.md                  （带真实数字的结论草稿）

用法（把手机电池容量换成你自己的，设错了功率全错）：
  python analyze_t02.py --mah 4500 --vnom 3.85

电池容量查法：手机设置 → 关于手机 → 电池容量；或搜"<你的机型> battery capacity"。
额定电压一般是 3.85V（部分机型 3.87/4.4，误差 5% 以内可以接受，报告里写明假设即可）。
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "raw_data")
FIG = os.path.join(os.path.dirname(HERE), "figures")

COL = {"cond": "工况", "min": "分钟", "soc": "电量百分比", "temp": "电池温度C",
       "volt": "电压mV", "bright": "屏幕亮度%", "app": "前台应用", "room": "室温C"}


def plt_setup():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    return plt


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=os.path.join(RAW, "记录表.csv"))
    ap.add_argument("--mah", type=float, default=5200,
                    help="电池容量 mAh（荣耀200 典型值 5200 / 额定 5100）")
    ap.add_argument("--vnom", type=float, default=3.87, help="电芯额定电压 V（荣耀200 取 3.87）")
    args = ap.parse_args()

    if not os.path.exists(args.csv):
        # 兼容只有示例文件的情况
        alt = os.path.join(RAW, "记录表-示例格式.csv")
        if os.path.exists(alt):
            print(f"[!] 没找到 {args.csv}，先按示例格式新建并填入实测数据。")
            print(f"    示例文件：{alt}（里面的数字是编的，只示范格式）")
            args.csv = alt
        else:
            print(f"[!] 找不到 {args.csv}")
            return 1

    df = pd.read_csv(args.csv)
    df = df.rename(columns={v: k for k, v in COL.items()})
    for c in ("min", "soc", "temp", "volt"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["min", "soc"])
    ORDER = df["cond"].drop_duplicates().tolist()   # 保持记录表里出现的先后顺序
    df["cond"] = pd.Categorical(df["cond"], categories=ORDER, ordered=True)
    df = df.sort_values(["cond", "min"])
    os.makedirs(FIG, exist_ok=True)

    # ---------------- 汇总表 ----------------
    rows = []
    for cond, g in df.groupby("cond", sort=False, observed=True):
        g = g.sort_values("min")
        dur_h = (g["min"].iloc[-1] - g["min"].iloc[0]) / 60.0
        dsoc = float(g["soc"].iloc[0] - g["soc"].iloc[-1])
        rate = dsoc / dur_h if dur_h > 0 else np.nan           # %/小时
        mah = dsoc / 100.0 * args.mah                          # 消耗的 mAh
        mA = mah / dur_h if dur_h > 0 else np.nan               # 平均电流
        W = mA / 1000.0 * args.vnom                            # 平均功率
        Wh = mah * args.vnom / 1000.0
        t_rise = float(g["temp"].iloc[-1] - g["temp"].iloc[0])
        rows.append({
            "工况": cond, "时长min": round(dur_h * 60), "耗电%": dsoc,
            "耗电率%/h": round(rate, 1), "消耗mAh": round(mah),
            "平均电流mA": round(mA), "平均功率W": round(W, 2),
            "能量Wh": round(Wh, 2), "温升C": round(t_rise, 1),
            "峰值温度C": round(float(g["temp"].max()), 1),
            "满电续航h": round(100 / rate, 1) if rate and rate > 0 else None,
        })
    summ = pd.DataFrame(rows)
    summ.to_csv(os.path.join(RAW, "分析结果.csv"), index=False, encoding="utf-8-sig")
    print(summ.to_string(index=False))

    # ---------------- 图 ----------------
    plt = plt_setup()
    colors = ["#2f6fb0", "#4a9b8e", "#c0392b", "#d9822b"]

    fig, axes = plt.subplots(1, 3, figsize=(14.5, 4.2), dpi=200)
    for i, (cond, g) in enumerate(df.groupby("cond", sort=False, observed=True)):
        c = colors[i % len(colors)]
        g = g.sort_values("min")
        axes[0].plot(g["min"], g["soc"], "o-", color=c, lw=1.8, ms=4, label=cond)
        axes[1].plot(g["min"], g["temp"], "o-", color=c, lw=1.8, ms=4, label=cond)
    axes[0].set_xlabel("时间 (min)"); axes[0].set_ylabel("电量 (%)")
    axes[0].set_title("电量下降曲线", fontsize=11)
    axes[1].set_xlabel("时间 (min)"); axes[1].set_ylabel("电池温度 (°C)")
    axes[1].set_title("温度变化曲线", fontsize=11)
    for ax in axes[:2]:
        ax.grid(alpha=0.25); ax.legend(fontsize=8)

    axes[2].bar(range(len(summ)), summ["平均功率W"],
                color=[colors[i % len(colors)] for i in range(len(summ))])
    axes[2].set_xticks(range(len(summ)), summ["工况"], rotation=20, ha="right", fontsize=8.5)
    axes[2].set_ylabel("平均功率 (W)")
    axes[2].set_title("各工况平均功耗", fontsize=11)
    for i, v in enumerate(summ["平均功率W"]):
        axes[2].text(i, v, f"{v:.1f}", ha="center", va="bottom", fontsize=8)
    axes[2].grid(axis="y", alpha=0.25)

    fig.tight_layout()
    out = os.path.join(FIG, "T02-总览.png")
    fig.savefig(out)
    plt.close(fig)
    print(f"\n图 → {out}")

    # ---------------- 结论草稿 ----------------
    base = summ.iloc[0]
    worst = summ.loc[summ["平均功率W"].idxmax()]
    md_tbl = ["| 工况 | 耗电% | 耗电率%/h | 平均电流mA | 平均功率W | 温升C | 峰值温度C | 满电续航h |",
              "|---|---|---|---|---|---|---|---|"]
    for _, r in summ.iterrows():
        md_tbl.append(f"| {r['工况']} | {r['耗电%']} | {r['耗电率%/h']} | {r['平均电流mA']} | "
                      f"{r['平均功率W']:.2f} | {r['温升C']} | {r['峰值温度C']} | {r['满电续航h']} |")

    L = ["# 课题 02 结论：手机多工况发热与掉电", "",
         "> 测试机：荣耀 200（HONOR 200，第三代骁龙 7 / 6.7\" OLED 120Hz / "
         "青海湖电池 100W 快充）",
         f"> 折算参数：容量 {args.mah:.0f} mAh（官方典型值 5200，额定 5100，差 2%）、"
         f"额定电压 {args.vnom} V",
         "> 功率是**算**出来的不是测出来的：ΔSoC% × 容量 ÷ 时长 → 平均电流 → × 电压，"
         "误差来源见 设备档案.md 第 3 节。", "",
         "## 实测结果", ""] + md_tbl + ["", "## 关键发现", "",
         f"1. **功耗跨度 {worst['平均功率W'] / max(base['平均功率W'], 0.01):.1f} 倍**："
         f"待机 {base['平均功率W']:.2f} W，{worst['工况']} {worst['平均功率W']:.2f} W。",
         f"2. **温度是短板**：{worst['工况']} 30 分钟温升 {worst['温升C']:.1f} °C，"
         f"峰值 {worst['峰值温度C']:.1f} °C。锂电池长期高于 40 °C 会加速容量衰减。",
         f"3. **折算续航**：满电下 {worst['工况']} 只能撑 {worst['满电续航h']} 小时，"
         f"待机 {base['满电续航h']} 小时。", "",
         "## 高功耗硬件模块归因", "",
         "▢ 必写。按这条线索展开（有实测数据支撑才可信）：", "",
         "- **屏幕**：待机 30% 亮度 vs 拍摄 80% 亮度 —— 亮度是线性的直流功耗，"
         "拆开看「关屏但保持运行」这一工况就能量化屏幕的占比（建议补测这一组）",
         "- **SoC/GPU**：高帧率游戏时温升最快、电流最大，说明 GPU 长期跑在高频；"
         "降帧到 60fps 或锁 45fps 通常能省 20~30%",
         "- **摄像头 + 定位 + 编码**：导航/拍摄是三个模块同时工作（CMOS、ISP、GNSS、视频编码器），"
         "这一组的意义是「多模块叠加」而不是单模块",
         "- **射频**：4G/5G 弱信号下发射功率会拉满，是待机功耗被低估的常见原因", "",
         "本机（荣耀 200）特有线索，归因至少用上两条：", "",
         "- **OLED + 120Hz 可变刷新率**：像素自发光，深色界面天然省电；但高刷会让"
         "显示链路常驻，\"帧率\"和\"屏幕刷新率\"是两个变量，别混成一句「高刷费电」",
         "- **VC 散热 5029 mm²** 是温升曲线「前陡后平」的原因：热量先从 SoC 传到均热板，"
         "约 10 分钟后进入准稳态。所以只报 30 分钟末温会低估瞬时峰值，两个数都要给",
         "- **100W 快充的充电温升**会污染下一段基线（见 README 3.2），归因前先排除这一项",
         "- 硅碳电池低 SoC 段电压平台更平，`电压mV` 列在 20% 以下会明显变陡 —— "
         "可以拿它交叉验证电量计读数有没有跳变", "",
         "## 系统省电策略（可落地，不要写「少玩手机」）", "",
         "| 策略 | 预期收益 | 代价 | 依据 |", "|---|---|---|---|",
         "| 高刷场景锁 60fps | 省 ▢%（需实测） | 画面流畅度 | 游戏工况功耗最高 |",
         "| 拍摄时降屏幕亮度到 50% | 省 ▢ W | 取景可读性 | 亮度-功耗近似线性 |",
         "| 待机时限制后台定位/推送 | 省 ▢ mA | 消息延迟 | 待机基线与前台差值 |",
         "| 充电时避免高负载 | 降低峰值温度 ▢ °C | 使用体验 | 温度峰值已接近阈值 |", "",
         "> 每条策略后面必须挂一个你实测的数字，否则就是套话。", ""]

    md = os.path.join(HERE, "T02-结论.md")
    with open(md, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"结论草稿 → {md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
