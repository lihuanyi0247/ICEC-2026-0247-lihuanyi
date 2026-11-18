"""
logbook.py —— 统一的过程日志（Telemetry Log）模块
------------------------------------------------
项目三要求"连续运行检测日志"。这个模块负责把每一次脚本运行都落成一条
JSONL 记录（时间戳、脚本名、参数、耗时、样本数、状态、异常），

设计要点：
1. 只追加（append-only），永不覆盖 —— 保证过程可溯源，无法事后"倒填"。
2. 每条记录自带 UTC 时间戳 + 主机名 + Python 版本，面试时可直接对质。
3. 异常也记录（status=fail + traceback 摘要），体现真实的排障过程。

用法：
    from logbook import Trace
    with Trace("train_classify", params={"model": "svm"}) as t:
        ...
        t.note(rows=1425, acc=0.86)
"""

from __future__ import annotations

import json
import os
import platform
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone

# 日志目录：仓库内 02-20Days-Challenge/process_logs/
_HERE = os.path.dirname(os.path.abspath(__file__))
_LOG_DIR = os.path.join(os.path.dirname(_HERE), "process_logs")
_LOG_FILE = os.path.join(_LOG_DIR, "run_log.jsonl")


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Trace:
    """一次脚本运行的追踪上下文，退出时自动写一条日志。"""

    def __init__(self, name: str, params: dict | None = None):
        self.name = name
        self.params = params or {}
        self.extra: dict = {}
        self.run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.t0 = 0.0
        self.status = "ok"
        self.err = ""

    def __enter__(self):
        os.makedirs(_LOG_DIR, exist_ok=True)
        self.t0 = time.perf_counter()
        return self

    def note(self, **kw):
        """记录运行过程中的关键指标（样本数、准确率、文件数等）。"""
        self.extra.update(kw)

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.status = "fail"
            self.err = "".join(traceback.format_exception_only(exc_type, exc)).strip()
            self.extra["traceback_tail"] = "".join(
                traceback.format_tb(tb)[-3:]
            ).replace("\n", " | ")[:800]
        rec = {
            "run_id": self.run_id,
            "ts": _now_iso(),
            "script": self.name,
            "status": self.status,
            "elapsed_s": round(time.perf_counter() - self.t0, 3),
            "params": self.params,
            "metrics": self.extra,
            "host": platform.node(),
            "python": sys.version.split()[0],
            "cwd": os.getcwd(),
            "argv": sys.argv[1:],
        }
        if self.err:
            rec["error"] = self.err
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        # 失败时让调用方拿到非零退出码
        return False


def summarize(log_path: str = _LOG_FILE) -> dict:
    """统计日志文件的基本信息，用于报告与面试自证。"""
    if not os.path.exists(log_path):
        return {"exists": False}
    n = ok = fail = 0
    days = set()
    first = last = None
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            n += 1
            ok += r.get("status") == "ok"
            fail += r.get("status") == "fail"
            ts = r.get("ts", "")
            if ts:
                days.add(ts[:10])
                first = first or ts
                last = ts
    return {
        "exists": True,
        "runs": n,
        "ok": ok,
        "fail": fail,
        "distinct_days": len(days),
        "days": sorted(days),
        "first_ts": first,
        "last_ts": last,
        "path": log_path,
    }


if __name__ == "__main__":
    print(json.dumps(summarize(), ensure_ascii=False, indent=2))
