"""
collect_t02_adb.py —— 课题 02 自动采样器（可选，但强烈建议用）
------------------------------------------------------------------
手工每 5 分钟抄一次电量，最大的问题是：**数字是你抄的，面试官无法验证
时间点是不是事后补的**。用 adb 直接读手机电池控制器，时间戳由电脑打，
日志可回放，这是"过程留痕"最硬的形态。

前提：手机开「开发者选项 → USB 调试」，用数据线连电脑，`adb devices` 能看到设备。

用法
----
  # 0. 先确认能连上（只读，不改手机任何设置）
  python collect_t02_adb.py --check

  # 1. 录一个工况（30 分钟，每 30 秒采一次 → 61 行）
  python collect_t02_adb.py --cond 待机 --min 30 --room 22

  # 4 个工况依次执行（每两段之间静置 10 分钟回温、充电补回 80%）
  python collect_t02_adb.py --cond 短视频 --min 30 --room 22
  python collect_t02_adb.py --cond 导航拍摄 --min 30 --room 22
  python collect_t02_adb.py --cond 高帧率游戏 --min 30 --room 22

产出
----
  raw_data/记录表.csv            追加写入，同一表头，analyze_t02.py 直接吃
  raw_data/adb_raw_<工况>.txt    每次 dumpsys 的原文（万一解析错，可回溯）

为什么采样间隔用 30 秒而不是 5 分钟
----------------------------------
电量计的分辨率是 1%，5 分钟才采一次会把"电量掉 1%"的台阶抹平。
30 秒采一次，事后按 5 分钟重采样，既能算功率，又能看出掉电的台阶形状。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "raw_data")
CSV = os.path.join(RAW, "记录表.csv")
HEADER = "时间戳,工况,分钟,电量百分比,电池温度C,电压mV,屏幕亮度%,前台应用,室温C,备注"


# ---------------------------------------------------------------- adb 基础
def find_adb() -> str | None:
    p = shutil.which("adb")
    if p:
        return p
    # 常见安装位置兜底
    guesses = [
        r"D:\platform-tools\platform-tools\adb.exe",   # 本机已装在这里（37.0.1）
        r"D:\platform-tools\adb.exe",
        r"C:\platform-tools\adb.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe"),
        os.path.expandvars(r"%USERPROFILE%\AppData\Local\Android\Sdk\platform-tools\adb.exe"),
    ]
    for g in guesses:
        if os.path.exists(g):
            return g
    return None


def run_adb(adb: str, *args: str, timeout: int = 15) -> str:
    try:
        r = subprocess.run([adb, *args], capture_output=True, timeout=timeout)
        return (r.stdout or b"").decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return ""
    except OSError:
        return ""


def parse_battery(text: str) -> dict:
    """解析 `dumpsys battery`。temperature 单位是 0.1°C，voltage 单位是 mV。"""
    out: dict = {}
    m = re.search(r"level:\s*(\d+)", text)
    if m:
        out["level"] = int(m.group(1))
    m = re.search(r"temperature:\s*(\d+)", text)
    if m:
        out["temp"] = round(int(m.group(1)) / 10.0, 1)
    m = re.search(r"voltage:\s*(\d+)", text)
    if m:
        out["volt"] = int(m.group(1))
    m = re.search(r"charging:\s*(\w+)", text)
    if m:
        out["charging"] = m.group(1) == "true"
    m = re.search(r"status:\s*(\d+)", text)
    if m:
        # 2=充电中 3=放电中 4=未充电 5=已充满
        out["status"] = {"2": "充电中", "3": "放电中", "4": "未充电", "5": "已充满"}.get(m.group(1), m.group(1))
    return out


def foreground_app(adb: str) -> str:
    """尽力而为地取前台包名。取不到就返回空，不阻塞采样。"""
    for cmd in (["shell", "dumpsys", "activity", "activities"],):
        txt = run_adb(adb, *cmd, timeout=8)
        if not txt:
            continue
        # 新版 Android：topResumedActivity / mResumedActivity
        for pat in (r"topResumedActivity.*?([\w\.]+)/", r"mResumedActivity.*?([\w\.]+)/"):
            m = re.search(pat, txt)
            if m:
                return m.group(1)
    return ""


def screen_brightness(adb: str) -> str:
    txt = run_adb(adb, "shell", "settings", "get", "system", "screen_brightness", timeout=8)
    txt = txt.strip()
    if not txt or not txt.isdigit():
        return ""
    # Android 亮度范围 0~255，折算成百分比方便和记录表对齐
    return str(round(int(txt) / 255 * 100))


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只检查设备连接，不采样")
    ap.add_argument("--cond", default="", help="工况名，例如 待机 / 短视频 / 导航拍摄 / 高帧率游戏")
    ap.add_argument("--min", type=float, default=30, help="本段时长（分钟）")
    ap.add_argument("--interval", type=float, default=30, help="采样间隔（秒），默认 30")
    ap.add_argument("--room", default="", help="室温 °C（手填，没有传感器就写大概值）")
    ap.add_argument("--note", default="", help="备注，例如 '飞行模式关/通知关'")
    ap.add_argument("--app", default="", help="前台应用（可留空自动取）")
    args = ap.parse_args()

    adb = find_adb()
    if not adb:
        print("[×] 没找到 adb。三个办法：")
        print("    1) 本机已装：D:\\platform-tools\\platform-tools\\adb.exe（把它加进 PATH 或直接用绝对路径）")
        print("    2) 装 Android Platform Tools：https://developer.android.com/tools/releases/platform-tools")
        print("    3) 别折腾，直接手工抄表（示例格式见 raw_data/记录表-示例格式.csv）")
        return 2

    devices = run_adb(adb, "devices")
    lines = [l for l in devices.splitlines()[1:] if l.strip()]
    print(f"[i] adb = {adb}")
    for l in lines:
        print("    " + l)
    if not any("\tdevice" in l for l in lines):
        print("[×] 没有处于 device 状态的设备。检查：数据线是否支持传输、手机是否点了『允许 USB 调试』。")
        return 2

    bat = parse_battery(run_adb(adb, "shell", "dumpsys", "battery"))
    print(f"[i] 当前电量 {bat.get('level')}%  温度 {bat.get('temp')}°C  电压 {bat.get('volt')} mV  "
          f"状态 {bat.get('status', '?')}")
    if args.check:
        print("[√] 连接正常，可以开始采样。")
        return 0

    if not args.cond:
        print("[×] 采样需要 --cond（工况名）。用 --check 可以先只检查连接。")
        return 2

    os.makedirs(RAW, exist_ok=True)
    new_file = not os.path.exists(CSV)
    raw_log = os.path.join(RAW, f"adb_raw_{args.cond}.txt")

    total = max(1, int(round(args.min * 60 / args.interval)))
    step = 0
    t0 = time.time()
    print(f"[i] 开始采样：{args.cond} / {args.min:.0f} 分钟 / 每 {args.interval:.0f} 秒一次 → 约 {total + 1} 行")
    print("    这段时间别碰手机屏幕（会改变亮度和功耗）；手机屏幕请按实验设定保持常亮/息屏。")

    with open(CSV, "a", encoding="utf-8-sig" if new_file else "utf-8", newline="") as fcsv, \
            open(raw_log, "a", encoding="utf-8") as fraw:

        if new_file:
            fcsv.write(HEADER + "\n")

        while True:
            elapsed = time.time() - t0
            minute = int(round(elapsed / 60.0))
            dump = run_adb(adb, "shell", "dumpsys", "battery")
            b = parse_battery(dump)
            fraw.write(f"===== {datetime.now().isoformat(timespec='seconds')}  minute={minute} =====\n")
            fraw.write(dump + "\n")

            if "level" in b:
                app = args.app or foreground_app(adb)
                bright = screen_brightness(adb)
                note = args.note
                if b.get("charging"):
                    note = (note + " / 正在充电(数据作废)").strip(" /")
                row = [
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    args.cond, str(minute), str(b.get("level", "")),
                    str(b.get("temp", "")), str(b.get("volt", "")),
                    bright, app, args.room, note,
                ]
                fcsv.write(",".join(row) + "\n")
                fcsv.flush()
                print(f"  [{minute:>3d} min] {b.get('level')}%  {b.get('temp')}°C  {b.get('volt')} mV  "
                      f"亮度{bright or '?'}%  {app or '?'}", end="\r")
            else:
                print("  [!] 这轮没读到电池信息，已记入 raw log", end="\r")

            if step >= total:
                break
            step += 1
            # 对齐到下一个整间隔，避免累积漂移
            nxt = t0 + step * args.interval
            time.sleep(max(0.0, nxt - time.time()))

    print()
    print(f"[√] 完成：{args.cond}，已追加到 {CSV}")
    print(f"    原文日志：{raw_log}")
    print("    下一步：静置 10 分钟回温、把电充回 80%，再跑下一个工况。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
