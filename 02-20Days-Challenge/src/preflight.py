"""
preflight.py —— 提交前自检（跑一遍，就知道哪一项还缺、缺在哪）
==============================================================
考核的每一分都挂在"有没有可核验的产物"上。这个脚本把评分细则翻译成
可自动判定的检查项，逐条给出 通过 / 待补 / 不通过，并直接告诉你去补什么。

用法
----
    python src/preflight.py            # 全量检查
    python src/preflight.py --brief    # 只看结论

产出
----
    仓库根目录 PREFLIGHT.md            （可提交，本身就是"我知道要求是什么"的证据）
    终端输出                             退出码 0 = 可提交，1 = 还有阻断项

设计原则
--------
1. 只读，不改任何数据 —— 自检脚本如果会动数据，就没人敢跑第二遍。
2. 不通过的项必须给出"下一个动作"，不是只报错。
3. 分成「阻断」（必交项缺失，直接丢分）和「建议」（能加分但非硬性）。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
CH = os.path.dirname(HERE)                 # 02-20Days-Challenge
ROOT = os.path.dirname(CH)                 # 仓库根

COND_NAMES = {"N1": "正常·低档", "N2": "正常·高档", "F1": "扇叶失衡",
              "O1": "进风遮挡", "S0": "停机基线"}
COND_CODES = list(COND_NAMES)
NAME_RE = re.compile(r"^(FAN|DRY|BIKE)_(N1|N2|F1|O1|S0)_S([123])_(\d{2})\.(wav|WAV)$")
BLIND_RE = re.compile(r"^BLIND_(N1|N2|F1|O1|S0)_\d{2}\.(wav|WAV)$")

results: list[dict] = []


def add(level: str, item: str, ok: bool, detail: str, action: str = "") -> None:
    """level: BLOCK（硬性必交） / NICE（加分项）"""
    results.append({"level": level, "item": item, "ok": ok,
                    "detail": detail, "action": action if not ok else ""})


# ----------------------------------------------------------------- 工具函数
def wav_seconds(path: str) -> float | None:
    try:
        with wave.open(path, "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        return None


def _exif_time_stdlib(path: str) -> str | None:
    """不依赖 Pillow 的极简 JPEG EXIF 读取（只取拍摄时间）。

    为什么需要：本机有两个 Python，系统那个没装 Pillow。若只靠 PIL 判断，
    真实照片会被误判成"没有 EXIF"，白白扣掉一条必交项。
    """
    try:
        with open(path, "rb") as f:
            data = f.read(400_000)
    except OSError:
        return None
    if data[:2] != b"\xff\xd8":
        return None

    i, tiff = 2, None
    while i < len(data) - 4:
        if data[i] != 0xFF:
            i += 1
            continue
        m = data[i + 1]
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        seglen = int.from_bytes(data[i + 2:i + 4], "big")
        if m == 0xE1 and data[i + 4:i + 10] == b"Exif\x00\x00":
            tiff = data[i + 10:i + 2 + seglen]
            break
        i += 2 + seglen
    if not tiff or len(tiff) < 8:
        return None

    le = tiff[:2] == b"II"

    def u16(o: int) -> int:
        return int.from_bytes(tiff[o:o + 2], "little" if le else "big")

    def u32(o: int) -> int:
        return int.from_bytes(tiff[o:o + 4], "little" if le else "big")

    def ifd(off: int) -> dict:
        tags = {}
        if off + 2 > len(tiff):
            return tags
        n = u16(off)
        for k in range(n):
            e = off + 2 + k * 12
            if e + 12 > len(tiff):
                break
            tags[u16(e)] = (u16(e + 2), u32(e + 4), u32(e + 8), e + 8)
        return tags

    def ascii_of(entry) -> str | None:
        typ, cnt, valoff, inline = entry
        if typ != 2:
            return None
        raw = tiff[inline:inline + cnt] if cnt <= 4 else tiff[valoff:valoff + cnt]
        return raw.split(b"\x00")[0].decode("ascii", "ignore").strip() or None

    main = ifd(u32(4))
    # 0x9003 = DateTimeOriginal：正规写法在 Exif 子 IFD 里，
    # 但有些相机/库（包括 Pillow）直接写进 IFD0，两种都要认。
    if 0x9003 in main:
        v = ascii_of(main[0x9003])
        if v:
            return v
    if 0x8769 in main:
        sub = ifd(main[0x8769][2])
        if 0x9003 in sub:
            v = ascii_of(sub[0x9003])
            if v:
                return v
    if 0x0132 in main:                       # IFD0 的 DateTime
        return ascii_of(main[0x0132])
    return None


def exif_time(path: str) -> str | None:
    try:
        from PIL import Image
        ex = Image.open(path).getexif()
        for tag in (36867, 306):
            v = ex.get(tag)
            if v:
                return str(v)
    except Exception:
        pass
    if path.lower().endswith((".jpg", ".jpeg")):
        return _exif_time_stdlib(path)
    return None


def md_files_placeholder_count() -> tuple[int, list[str]]:
    """统计 .md 里还没填的 ▢ 占位符。

    只扫 .md：.py 里的 ▢ 是"生成模板里的提示语"，不是本人待填项；
    docs/ 下的 ▢ 是进度状态标记，也不是占位符。
    """
    hits, files = 0, []
    for path in glob.glob(os.path.join(ROOT, "**", "*.md"), recursive=True):
        rel = os.path.relpath(path, ROOT)
        if "_selftest" in rel or "示例" in rel or rel.startswith("docs" + os.sep) or os.sep + "docs" + os.sep in rel:
            continue
        if rel.replace("\\", "/").startswith("_私房"):
            continue
        if os.path.basename(path) == "PREFLIGHT.md":     # 本脚本自己的产物，跳过
            continue
        try:
            txt = open(path, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        n = txt.count("▢")
        if n:
            hits += n
            files.append((rel, n))
    files.sort(key=lambda x: -x[1])
    return hits, [f"{p}({n})" for p, n in files]


def dir_mb(path: str) -> float:
    total = 0
    for dp, _, fns in os.walk(path):
        for fn in fns:
            try:
                total += os.path.getsize(os.path.join(dp, fn))
            except OSError:
                pass
    return total / 1024 / 1024


# ----------------------------------------------------------------- 1. 目录与命名
def check_repo_identity() -> None:
    name = os.path.basename(ROOT.rstrip("\\/"))
    bad = ("XXXX" in name) or (name.lower().endswith("-pinyin"))
    add("BLOCK", "仓库命名规范", not bad,
        f"当前目录名 `{name}`",
        "改成 `ICEC-2026-<学号后四位>-<姓名拼音>`，例如 ICEC-2026-1234-zhangsan；"
        "GitHub 远端仓库必须同名")


# ----------------------------------------------------------------- 2. 原始录音
def check_raw_audio() -> None:
    per_cond: dict[str, int] = {c: 0 for c in COND_CODES}
    per_sess: dict[str, int] = {"S1": 0, "S2": 0, "S3": 0}
    bad_name: list[str] = []
    too_short: list[str] = []
    total = 0

    files = []
    for s in ("S1", "S2", "S3"):
        files += sorted(glob.glob(os.path.join(CH, "raw", s, "*.wav")))

    for fp in files:
        fn = os.path.basename(fp)
        m = NAME_RE.match(fn)
        if not m:
            bad_name.append(os.path.relpath(fp, CH))
            continue
        cond, sess = m.group(2), "S" + m.group(3)
        per_cond[cond] += 1
        per_sess[sess] += 1
        total += 1
        sec = wav_seconds(fp)
        if sec is not None and sec < 5:
            too_short.append(f"{fn}({sec:.1f}s)")

    n_ok = all(per_cond[c] >= 15 for c in COND_CODES)
    add("BLOCK", "原始录音 5 工况 × 15 组", n_ok,
        "  ".join(f"{c}:{per_cond[c]}" for c in COND_CODES) + f"　合计 {total} 个",
        "按 docs/01-录音与实验SOP.md 第 3 节补齐；每工况每个会话 5 段，共 5×3×5=75 个")

    add("BLOCK", "三次会话 S1/S2/S3 各自独立", all(per_sess[s] >= 25 for s in per_sess),
        "  ".join(f"{s}:{per_sess[s]}" for s in per_sess),
        "LOSO 交叉验证需要三次独立实验；若只有一个房间，就至少换时段并在报告里如实写明")

    add("BLOCK", "文件命名全部合规", not bad_name,
        f"不合规 {len(bad_name)} 个" + ("：" + "、".join(bad_name[:5]) if bad_name else ""),
        "严格 `FAN_<工况>_<会话>_<两位序号>.wav`，例如 FAN_N1_S1_01.wav")

    add("NICE", "单段时长 ≥5 秒", not too_short,
        f"偏短 {len(too_short)} 个" + ("：" + "、".join(too_short[:5]) if too_short else ""),
        "SOP 要求 9~12 秒；过短会导致 1 秒窗数量不足")


# ----------------------------------------------------------------- 3. 现场照片
def check_photos() -> None:
    ph = os.path.join(CH, "photos")
    imgs = [p for p in glob.glob(os.path.join(ph, "*"))
            if p.lower().endswith((".jpg", ".jpeg", ".png", ".heic"))]
    with_exif = [p for p in imgs if exif_time(p)]
    try:
        import PIL  # noqa: F401
        pil_ok = True
    except Exception:
        pil_ok = False
    detail = f"共 {len(imgs)} 张，其中 {len(with_exif)} 张带拍摄时间"
    if imgs and not pil_ok:
        detail += "（未装 Pillow，读不到 EXIF：pip install pillow）"
    add("BLOCK", "实验现场照片 ≥6 张（含 EXIF）", len(with_exif) >= 6, detail,
        "用手机原生相机拍，用数据线拷进来；经微信/QQ 传输会剥掉 EXIF")


# ----------------------------------------------------------------- 4. 跨日过程日志
def check_logs() -> None:
    sys.path.insert(0, HERE)
    try:
        from logbook import summarize
        s = summarize()
    except Exception as e:                      # noqa: BLE001
        add("NICE", "跨日运行日志", False, f"读取失败：{e}", "确认 src/logbook.py 存在")
        return
    if not s.get("exists"):
        add("BLOCK", "跨日运行日志 run_log.jsonl", False, "文件不存在",
            "跑一次 `python src/run_all.py`，日志由 logbook.py 自动追加；连续 3 天各跑一次")
        return
    add("BLOCK", "跨日运行日志 run_log.jsonl", s["distinct_days"] >= 3,
        f"{s['runs']} 次运行 / {s['distinct_days']} 个自然日 / 失败 {s['fail']} 条",
        "这是「跨多日连续过程数据」的硬证据，至少覆盖 3 个不同日期（今天起每天跑一次）")


# ----------------------------------------------------------------- 5. 结果与报告
def check_results() -> None:
    need = ["REPORT.md", "metrics.json", "data_qa.md", "confusion_matrix.png",
            "noise_robustness.png", "waveform_spectra.png", "feature_radar.png",
            "file_level_predictions.csv"]
    rd = os.path.join(CH, "results")
    missing = [f for f in need if not os.path.exists(os.path.join(rd, f))]
    add("BLOCK", "结果目录产物齐全", not missing,
        f"缺 {len(missing)} 项" + ("：" + "、".join(missing) if missing else ""),
        "跑 `python src/run_all.py`，全部图表与报告由脚本生成，不要手改")


def check_blind() -> None:
    files = sorted(glob.glob(os.path.join(CH, "raw_blind", "*.wav")))
    good = [f for f in files if BLIND_RE.match(os.path.basename(f))]
    add("BLOCK", "盲测集 ≥10 个且命名合规", len(good) >= 10,
        f"{len(good)}/{len(files)} 个合规",
        "盲测集不能参与训练；命名 BLIND_<真实工况>_<序号>.wav，5 工况各 2~3 个")
    add("NICE", "盲测结果已落表", os.path.exists(os.path.join(CH, "results", "blind_test.csv")),
        "results/blind_test.csv",
        "跑 `python src/predict_live.py --dir raw_blind`，并录一段不剪辑的视频")


# ----------------------------------------------------------------- 6. 第一阶段
def check_stage1() -> None:
    fd = os.path.join(ROOT, "01-Field-Discovery")
    t02 = os.path.join(fd, "T02-手机多工况发热掉电", "raw_data", "记录表.csv")
    t10 = os.path.join(fd, "T10-数字任务耗时审计", "raw_data", "耗时记录.csv")

    n2 = 0
    if os.path.exists(t02):
        n2 = max(0, sum(1 for _ in open(t02, encoding="utf-8-sig", errors="ignore")) - 1)
    add("BLOCK", "课题02 原始记录表 ≥24 行", n2 >= 24, f"{n2} 行",
        "用 collect_t02_adb.py 自动采（adb 读电池，时间戳由电脑打），或手工按示例格式填")

    n10 = 0
    if os.path.exists(t10):
        n10 = max(0, sum(1 for _ in open(t10, encoding="utf-8-sig", errors="ignore")) - 1)
    add("BLOCK", "课题10 耗时记录 ≥10 个样本", n10 >= 10, f"{n10} 行",
        "复制 raw_data/耗时记录-示例格式.csv → 耗时记录.csv，清空示例数字后填实测值")

    figs = os.path.join(fd, "figures")
    need = ["T02-总览.png", "T10-耗时对比.png"]
    missing = [f for f in need if not os.path.exists(os.path.join(figs, f))]
    add("BLOCK", "第一阶段图表", not missing,
        "缺：" + "、".join(missing) if missing else "两张图都在",
        "`python analyze_t02.py --mah 4500` / `python analyze_t10.py`")

    imgs = [p for p in glob.glob(os.path.join(figs, "*"))
            if p.lower().endswith((".jpg", ".jpeg", ".png")) and "T0" not in os.path.basename(p)
            and "T1" not in os.path.basename(p)]
    add("BLOCK", "第一阶段现场同框照", len(imgs) >= 2, f"{len(imgs)} 张",
        "两题各至少 1 张：拍到手 + 设备屏幕/场景，放 01-Field-Discovery/figures/")


# ----------------------------------------------------------------- 7. 文本与体积
def check_placeholders() -> None:
    n, files = md_files_placeholder_count()
    detail = f"还剩 {n} 处" + ("：" + "、".join(files[:6]) if files else "")
    add("BLOCK", "文档里的 ▢ 占位符已填完", n == 0, detail,
        "这些必须是你自己的数字与判断（准确率、局限、策略收益），不能由脚本代填")


def check_size() -> None:
    mb = dir_mb(ROOT)
    big = []
    for dp, _, fns in os.walk(ROOT):
        if ".git" in dp:
            continue
        for fn in fns:
            p = os.path.join(dp, fn)
            try:
                if os.path.getsize(p) > 40 * 1024 * 1024:
                    big.append(os.path.relpath(p, ROOT))
            except OSError:
                pass
    add("NICE", "仓库体积可控（<200MB，单文件<40MB）", mb < 200 and not big,
        f"{mb:.1f} MB" + ("，超大文件：" + "、".join(big[:3]) if big else ""),
        "GitHub 单文件上限 100MB；录音用 16kHz 单声道，或改用 Git LFS")


# ----------------------------------------------------------------- 输出
def render() -> str:
    blocks = [r for r in results if r["level"] == "BLOCK"]
    nice = [r for r in results if r["level"] == "NICE"]
    fail_b = [r for r in blocks if not r["ok"]]
    fail_n = [r for r in nice if not r["ok"]]

    L = ["# 提交前自检（PREFLIGHT）", "",
         f"> 自动生成于 `src/preflight.py`，共 {len(results)} 项检查。",
         f"> 阻断项 {len(blocks) - len(fail_b)}/{len(blocks)} 通过；"
         f"建议项 {len(nice) - len(fail_n)}/{len(nice)} 通过。", ""]

    if not fail_b and not fail_n:
        L += ["## 结论：可以提交 ✅", "", "全部检查通过。记得最后在无痕窗口打开仓库页面，确认图片能显示。", ""]
    else:
        L += ["## 结论：还不能提交 ❌" if fail_b else "## 结论：可以提交，但有可优化项 ⚠️", ""]

    def table(rows: list[dict]) -> list[str]:
        out = ["| 检查项 | 状态 | 实测 / 现状 |", "|---|---|---|"]
        for r in rows:
            out.append(f"| {r['item']} | {'✅' if r['ok'] else '❌'} | {r['detail']} |")
        return out

    L += ["## 一、硬性必交项", ""] + table(blocks) + [""]
    L += ["## 二、加分项", ""] + table(nice) + [""]

    todo = fail_b + fail_n
    if todo:
        L += ["## 三、现在该做什么（按顺序）", ""]
        for i, r in enumerate(todo, 1):
            L.append(f"{i}. **{r['item']}** —— {r['action']}")
        L.append("")
    L += ["## 四、提交命令", "",
          "```bash",
          "python src/preflight.py          # 再跑一次，确认没有 ❌",
          "git add -A",
          'git commit -m "docs: 补齐原始数据与报告，自检全绿"',
          "git push",
          "```", ""]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--brief", action="store_true", help="只输出结论")
    ap.add_argument("--out", default=os.path.join(ROOT, "PREFLIGHT.md"))
    args = ap.parse_args()

    check_repo_identity()
    check_raw_audio()
    check_photos()
    check_logs()
    check_results()
    check_blind()
    check_stage1()
    check_placeholders()
    check_size()

    md = render()
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(md)

    if args.brief:
        fails = [r for r in results if not r["ok"]]
        print(f"共 {len(results)} 项，未通过 {len(fails)} 项")
        for r in fails:
            print(f"  [{'×' if r['level'] == 'BLOCK' else '!'}] {r['item']}：{r['detail']}")
    else:
        print(md)
    print(f"\n报告 → {args.out}")
    return 0 if not any((not r["ok"]) and r["level"] == "BLOCK" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
