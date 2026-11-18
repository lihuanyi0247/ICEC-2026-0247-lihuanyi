"""
submit.py —— 每天收工时的"一键提交"（带自检闸门）
=================================================
为什么需要它：
  * 人会忘记跑自检，直接 `git add -A` 把半成品推上去 —— 面试官点开看到
    占位符，比没有仓库更糟。
  * 提交信息（commit message）是最便宜的过程证据。写"改了点东西"等于浪费一次留痕机会。

它做三件事：
  1. 先跑 preflight 自检；有阻断项就**拒绝提交**并列出缺什么
  2. 打印这次改动清单，让你确认没把无关大文件带进去
  3. 提交 + 推送

用法
----
  python submit.py -m "feat: 补齐 S1 会话 25 个录音与现场照"
  python submit.py -m "docs: 补完课题02 归因与省电策略" --force   # 明知有缺项也要提
  python submit.py -m "..." --no-push                            # 只提交不推送

提交信息建议用前缀：feat / fix / data / docs / chore
  data: 新增原始数据    feat: 加功能    fix: 修 bug
  docs: 文档报告        chore: 目录结构、依赖
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CH = os.path.join(HERE, "02-20Days-Challenge")
PREFLIGHT = os.path.join(CH, "src", "preflight.py")


def sh(cmd: list[str], cwd: str) -> tuple[int, str]:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True)
    out = (r.stdout or b"").decode("utf-8", "replace")
    err = (r.stderr or b"").decode("utf-8", "replace")
    return r.returncode, (out + err).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-m", "--message", required=True, help="提交信息")
    ap.add_argument("--force", action="store_true", help="跳过自检闸门")
    ap.add_argument("--no-push", action="store_true", help="只提交不推送")
    ap.add_argument("--quiet", action="store_true", help="不打印改动清单")
    args = ap.parse_args()

    git = shutil.which("git")
    if not git:
        print("[×] 没找到 git，先执行：winget install --id Git.Git -e")
        return 3

    # ---------- 1. 自检 ----------
    if not args.force:
        print("[1/3] 自检 …")
        code, out = sh([sys.executable, "-X", "utf8", PREFLIGHT, "--brief"], HERE)
        print("\n".join("    " + l for l in out.splitlines()[-14:]))
        if code != 0:
            print("\n[×] 有阻断项，不提交。补齐后再来，或确认要硬推就加 --force。")
            print("    完整清单见仓库根目录 PREFLIGHT.md")
            return 1
        print("    [√] 自检通过")
    else:
        print("[1/3] 跳过自检（--force）")

    # ---------- 2. 改动清单 ----------
    print("[2/3] 本次改动 …")
    sh([git, "add", "-A"], HERE)
    _, stat = sh([git, "status", "--short"], HERE)
    if not stat:
        print("    没有改动，无需提交。")
        return 0
    lines = stat.splitlines()
    if not args.quiet:
        for l in lines[:40]:
            print("    " + l)
        if len(lines) > 40:
            print(f"    ... 共 {len(lines)} 项")
    else:
        print(f"    共 {len(lines)} 项")

    # 体积提醒：防止把几百 MB 录音一次推爆
    _, numstat = sh([git, "diff", "--cached", "--numstat"], HERE)
    total_mb = 0.0
    for l in numstat.splitlines():
        parts = l.split("\t")
        if len(parts) == 3 and parts[0].isdigit():
            total_mb += int(parts[0]) / 1024 / 1024
    print(f"    体积约 {total_mb:.1f} MB")
    if total_mb > 80:
        print("    [!] 本次超过 80MB。若是原始录音，建议 16kHz 单声道，或改用 Git LFS：")
        print("        git lfs track \"*.wav\"  →  git add .gitattributes")

    # ---------- 3. 提交与推送 ----------
    print("[3/3] 提交 …")
    code, out = sh([git, "commit", "-m", args.message], HERE)
    if code != 0:
        print("    [!] 提交失败：")
        print("\n".join("    " + l for l in out.splitlines()[-8:]))
        return 1
    print("    " + (out.splitlines()[0] if out else "已提交"))

    if args.no_push:
        print("    已跳过推送（--no-push）")
    else:
        code, out = sh([git, "push"], HERE)
        if code != 0:
            print("    [!] 推送失败（常见原因：没登录 / 远端有冲突 / 代理）：")
            print("\n".join("    " + l for l in out.splitlines()[-10:]))
            print("    解决办法见 _私房-面试与作战/03-建仓与提交指南.md 第 6 节")
            return 1
        print("    [√] 已推送到 GitHub")

    _, log = sh([git, "log", "--oneline"], HERE)
    print(f"\n当前提交历史（共 {len(log.splitlines())} 条）：")
    print("\n".join("  " + l for l in log.splitlines()[:8]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
