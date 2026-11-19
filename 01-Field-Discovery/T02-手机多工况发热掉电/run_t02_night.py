"""
run_t02_night.py —— 课题 02 今晚连挂 4 段（含无线 adb 切换）

为什么需要"无线 adb"
--------------------
数据线插着的时候，手机一直在以 500mA 左右充电。采集器虽然会把
`charging: true` 记下来并标"数据作废"，但结果是：**电量根本不掉，整段数据没了**。
所以采样期间手机必须脱离数据线 —— 用 adb over WiFi。

流程（一条命令做完）
--------------------
  1. 用 USB 先连上，自动读出手机的 WiFi IP
  2. 切到 tcpip 5555，connect 到那个 IP
  3. 提示你拔线；拔完按回车，脚本确认已经走网络、且不在充电
  4. 依次跑 待机 → 短视频 → 导航拍摄 → 高帧率游戏，段间 10 分钟回温倒计时

用法
----
  # 只做无线切换（不采样）
  python run_t02_night.py --setup

  # 切换 + 连跑 4 段（每段 30 分钟）
  python run_t02_night.py --all --room 22

  # 只跑其中两段（时间不够时）
  python run_t02_night.py --conds 待机,短视频 --room 22

为什么顺序是 待机 → 短视频 → 导航 → 游戏
----------------------------------------
这是**从低温到高温**排列。手机的机身热容很大，前一段攒的热会抬高一整段的基线；
把最凉的放前面、最烫的放最后，热累积对前 3 段的影响最小。
反过来（游戏先跑）会让后面三段的起始温度全部被污染。
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

try:                                    # Windows 终端中文不乱码
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECT = os.path.join(HERE, "collect_t02_adb.py")
PY = sys.executable

# 从低温到高温，顺序不要改（改了就破坏热累积假设）
SEGMENTS = ["待机", "短视频", "导航拍摄", "高帧率游戏"]
COOLDOWN_MIN = 10


def find_adb() -> str | None:
    p = shutil.which("adb")
    if p:
        return p
    for g in (r"D:\platform-tools\platform-tools\adb.exe",
              r"D:\platform-tools\adb.exe",
              r"C:\platform-tools\adb.exe",
              os.path.expandvars(r"%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe")):
        if os.path.exists(g):
            return g
    return None


def adb(adb_exe: str, *args: str, timeout: int = 20) -> str:
    try:
        r = subprocess.run([adb_exe, *args], capture_output=True, timeout=timeout)
        return (r.stdout or b"").decode("utf-8", "replace") + \
               (r.stderr or b"").decode("utf-8", "replace")
    except (subprocess.TimeoutExpired, OSError):
        return ""


def battery(adb_exe: str) -> dict:
    txt = adb(adb_exe, "shell", "dumpsys", "battery")
    out: dict = {}
    for key, pat in (("level", r"level:\s*(\d+)"), ("temp", r"temperature:\s*(\d+)"),
                     ("volt", r"voltage:\s*(\d+)"), ("charging", r"charging:\s*(\w+)")):
        m = re.search(pat, txt)
        if m:
            out[key] = m.group(1)
    if "temp" in out:
        out["temp"] = str(round(int(out["temp"]) / 10.0, 1))
    out["charging"] = out.get("charging", "?") == "true"
    return out


def wifi_ip(adb_exe: str) -> str:
    """从手机里读出 WiFi 的 IPv4。荣耀/MagicOS 用 `ip` 命令，老系统退化到 ifconfig。"""
    for cmd in (["shell", "ip", "-f", "inet", "addr", "show", "wlan0"],
                ["shell", "ifconfig", "wlan0"]):
        txt = adb(adb_exe, *cmd)
        m = re.search(r"inet\s+(?:addr:)?(\d+\.\d+\.\d+\.\d+)", txt)
        if m and not m.group(1).startswith("127."):
            return m.group(1)
    return ""


def list_devices(adb_exe: str) -> list[str]:
    txt = adb(adb_exe, "devices")
    return [l.split()[0] for l in txt.splitlines()[1:]
            if l.strip() and l.split()[-1] == "device"]


def setup_wifi(adb_exe: str) -> str:
    """USB → 无线。返回可用的 `<ip>:5555`，失败返回空串。"""
    devs = list_devices(adb_exe)
    usb = [d for d in devs if ":" not in d]
    if not usb:
        print("[×] 没看到 USB 连接的手机。先插数据线，并在手机上点『允许 USB 调试』。")
        return ""
    print(f"[i] USB 设备：{usb[0]}")

    ip = wifi_ip(adb_exe)
    if not ip:
        print("[×] 读不到手机的 WiFi IP。确认手机连着 WiFi（不是移动数据），然后重试。")
        return ""
    print(f"[i] 手机 WiFi IP：{ip}")

    b = battery(adb_exe)
    print(f"[i] 当前电量 {b.get('level')}%  温度 {b.get('temp')}°C")

    print("[i] 切换到网络调试模式（adb tcpip 5555）…")
    adb(adb_exe, "tcpip", "5555")
    time.sleep(2)
    print(f"[i] 连接 {ip}:5555 …")
    adb(adb_exe, "connect", f"{ip}:5555")
    time.sleep(1)

    print("\n" + "=" * 62)
    print(" 现在请拔掉数据线（手机要保持连着同一个 WiFi）。")
    print(" 拔完按回车，我来确认是不是真的走网络了。")
    print("=" * 62)
    try:
        input(" 拔好线后按回车 > ")
    except EOFError:
        pass

    for attempt in range(3):
        adb(adb_exe, "connect", f"{ip}:5555")
        time.sleep(1)
        devs = list_devices(adb_exe)
        net = [d for d in devs if d.startswith(ip)]
        if net:
            b = battery(adb_exe)
            print(f"[√] 已走无线：{net[0]}")
            print(f"[i] 电量 {b.get('level')}%  温度 {b.get('temp')}°C  "
                  f"充电中={'是（数据会作废！）' if b.get('charging') else '否 ✓'}")
            if b.get("charging"):
                print("[!] 还在充电：确认线已经拔掉、并且没有放在无线充电板上。")
            return net[0]
        print(f"[!] 第 {attempt + 1} 次没连上，重试…")
    print("[×] 无线连接失败。退路：把数据线插回去，直接用有线采（数据里会标'正在充电'，"
          "掉电不可信，只能在报告里说明局限）。")
    return ""


def countdown(minutes: float, label: str) -> None:
    end = time.time() + minutes * 60
    while True:
        left = end - time.time()
        if left <= 0:
            break
        m, s = divmod(int(left), 60)
        print(f"  ⏳ {label}：还剩 {m:02d}:{s:02d}   ", end="\r")
        time.sleep(1)
    print(f"  ⏳ {label}：完成            ")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup", action="store_true", help="只做 USB→无线切换")
    ap.add_argument("--all", action="store_true", help="连跑 4 段")
    ap.add_argument("--conds", default="", help="只跑指定工况，逗号分隔，例如 待机,短视频")
    ap.add_argument("--min", type=float, default=30, help="每段分钟数，默认 30")
    ap.add_argument("--room", default="22", help="室温 °C（自己看温度计/空调显示，写实际值）")
    ap.add_argument("--cooldown", type=float, default=COOLDOWN_MIN, help="段间回温分钟，默认 10")
    ap.add_argument("--note", default="", help="备注，会写进每一行")
    args = ap.parse_args()

    adb_exe = find_adb()
    if not adb_exe:
        print("[×] 找不到 adb。本机应在 D:\\platform-tools\\platform-tools\\adb.exe")
        return 2
    print(f"[i] adb = {adb_exe}")

    if args.setup:
        return 0 if setup_wifi(adb_exe) else 1

    conds = SEGMENTS if args.all else [c.strip() for c in args.conds.split(",") if c.strip()]
    if not conds:
        print("[i] 没指定工况，按默认顺序连跑 4 段。")
        conds = SEGMENTS

    # 已经走无线就直接用；只有 USB 才需要切换
    devs = list_devices(adb_exe)
    if not any(":" in d for d in devs):
        target = setup_wifi(adb_exe)
        if not target:
            return 1
    devs = list_devices(adb_exe)
    print(f"[i] 当前设备：{devs}")

    b = battery(adb_exe)
    try:
        lvl = int(b.get("level", "0"))
    except ValueError:
        lvl = 0
    need = 8 * len(conds)          # 经验值：每段最多掉 8%
    if lvl < need + 30:
        print(f"[!] 电量只有 {lvl}%，连跑 {len(conds)} 段可能不够（建议 ≥{need + 30}%）。")
        print("    先去充到 85% 以上再回来跑。")
        try:
            if input("    仍然要继续？(y/N) > ").strip().lower() != "y":
                return 1
        except EOFError:
            return 1
    if b.get("charging"):
        print("[×] 手机正在充电 —— 现在采出来的电量不会掉。先拔线和充电器。")
        return 1

    plan = " → ".join(f"{c}({args.min:.0f}min)" for c in conds)
    print(f"\n[i] 计划：{plan}，段间回温 {args.cooldown:.0f} 分钟")
    print(f"[i] 室温 {args.room}°C，开始时间 {datetime.now():%H:%M:%S}")
    print("[!] 每一段结束前不要碰手机屏幕（改亮度就改功耗）。\n")

    t_start = time.time()
    for i, cond in enumerate(conds, 1):
        print("=" * 62)
        print(f"  第 {i}/{len(conds)} 段：{cond}   {datetime.now():%H:%M:%S}")
        print("=" * 62)
        cmd = [PY, "-X", "utf8", COLLECT, "--cond", cond,
               "--min", str(args.min), "--room", args.room]
        if args.note:
            cmd += ["--note", args.note]
        rc = subprocess.run(cmd, cwd=HERE).returncode
        if rc != 0:
            print(f"[×] 这一段失败了（rc={rc}）。修好问题后可以单独补：")
            print(f"    python collect_t02_adb.py --cond {cond} --min {args.min:.0f} --room {args.room}")
            return rc
        if i < len(conds):
            print(f"\n[i] 回温 {args.cooldown:.0f} 分钟。手机平放桌上，别放床上、别套壳闷着。")
            countdown(args.cooldown, "回温")
            b = battery(adb_exe)
            print(f"[i] 下一段开始前：电量 {b.get('level')}%  温度 {b.get('temp')}°C\n")

    dur = (time.time() - t_start) / 60
    print("=" * 62)
    print(f"[√] {len(conds)} 段全部完成，总耗时 {dur:.0f} 分钟")
    print(f"    数据：{os.path.join(HERE, 'raw_data', '记录表.csv')}")
    print("    下一步：cd 到 T02 目录，python analyze_t02.py 出图")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
