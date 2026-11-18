"""
batch_rename.py —— 可复用的批量重命名工具（课题 10 的"AI 批处理"那一半）
----------------------------------------------------------------------
真实痛点：手机/相机导出的文件叫 `IMG_20261118_203312.jpg` `微信图片_2026...jpg`，
交作业、归档时得按「日期_课程_序号」改一遍，10 个文件手动改要 2 分钟以上。

用法（默认只预览，不会动文件）：

  python batch_rename.py --dir "D:/照片/待整理"                 # 预览
  python batch_rename.py --dir "D:/照片/待整理" --apply          # 真的改名
  python batch_rename.py --dir "D:/作业" --pattern "{date}_{course}_{seq:02d}{ext}" --course 通信原理

安全设计（这几条就是"工程素养"的体现，面试可以直接讲）：
  1. 默认 dry-run，必须显式 --apply 才动手；
  2. 先写 rename_log.csv 再改名，日志里记 old→new，可一键回滚；
  3. 目标名已存在时自动加 `_2` `_3` 后缀，绝不覆盖；
  4. 单个文件失败不中断整批，最后汇报成功/失败数。
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from datetime import datetime

IMG_EXT = {".jpg", ".jpeg", ".png", ".heic", ".mp4", ".mov", ".wav", ".m4a", ".mp3",
           ".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".md"}


def guess_date(path: str) -> str:
    """优先用文件名里的日期（20261118 / 2026-11-18），没有就用修改时间。"""
    stem = os.path.basename(path)
    m = re.search(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})", stem)
    if m:
        return f"{m.group(1)}{m.group(2)}{m.group(3)}"
    return datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y%m%d")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--pattern", default="{date}_{course}_{seq:02d}{ext}")
    ap.add_argument("--course", default="未分类")
    ap.add_argument("--apply", action="store_true", help="真的改名（默认只预览）")
    ap.add_argument("--recursive", action="store_true")
    args = ap.parse_args()

    if not os.path.isdir(args.dir):
        print(f"[!] 目录不存在：{args.dir}")
        return 1

    files = []
    if args.recursive:
        for r, _, fs in os.walk(args.dir):
            files += [os.path.join(r, f) for f in fs]
    else:
        files = [os.path.join(args.dir, f) for f in os.listdir(args.dir)
                 if os.path.isfile(os.path.join(args.dir, f))]
    files = [f for f in files if os.path.splitext(f)[1].lower() in IMG_EXT]
    files.sort(key=lambda p: os.path.getmtime(p))

    if not files:
        print("[!] 没找到可处理的文件（支持 " + " ".join(sorted(IMG_EXT)) + "）")
        return 1

    used: set[str] = set()
    plan = []
    for i, fp in enumerate(files, 1):
        ext = os.path.splitext(fp)[1].lower()
        new = args.pattern.format(date=guess_date(fp), course=args.course,
                                  seq=i, ext=ext, old=os.path.splitext(os.path.basename(fp))[0])
        new = re.sub(r'[\\/:*?"<>|]', "_", new)      # Windows 非法字符
        base, e = os.path.splitext(new)
        k = 2
        while new in used or os.path.exists(os.path.join(os.path.dirname(fp), new)):
            new = f"{base}_{k}{e}"
            k += 1
        used.add(new)
        plan.append((fp, os.path.join(os.path.dirname(fp), new)))

    print(f"{'[预览]' if not args.apply else '[执行]'} 共 {len(plan)} 个文件，目录 {args.dir}\n")
    for old, new in plan:
        mark = "•" if os.path.basename(old) != os.path.basename(new) else "="
        print(f"  {mark} {os.path.basename(old):<42s} → {os.path.basename(new)}")

    if not args.apply:
        print("\n以上为预览。确认无误后加 --apply 真正执行。")
        return 0

    log_path = os.path.join(args.dir, "rename_log.csv")
    with open(log_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["old", "new", "ts"])
        for old, new in plan:
            w.writerow([old, new, datetime.now().isoformat(timespec="seconds")])

    ok = fail = 0
    for old, new in plan:
        if old == new:
            continue
        try:
            os.rename(old, new)
            ok += 1
        except OSError as e:
            print(f"  ✗ {os.path.basename(old)}: {e}")
            fail += 1
    print(f"\n完成：成功 {ok}，失败 {fail}。回滚日志：{log_path}")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
