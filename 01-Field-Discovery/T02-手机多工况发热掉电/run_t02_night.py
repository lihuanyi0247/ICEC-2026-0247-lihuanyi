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


TARGET: str = ""          # 选定要操作的设备；不指定时 adb shell 在多设备下会直接失败


def adb(adb_exe: str, *args: str, timeout: int = 20) -> str:
    cmd = [adb_exe]
    # 只有"对某台设备执行"的命令才加 -s；devices/connect/pair/tcpip 是 server 级命令
    if TARGET and args and args[0] == "shell":
        cmd += ["-s", TARGET]
    cmd += list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
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


def local_netinfo() -> tuple[list[str], list[str]]:
    """读电脑自己的 IPv4 和默认网关。

    用途：判断电脑和手机是不是同一个网段。不是的话 adb 一定连不上，
    与其反复试 IP，不如早点告诉他"去把电脑连到手机热点"。
    """
    ips: list[str] = []
    gws: list[str] = []
    try:
        r = subprocess.run(["ipconfig"], capture_output=True, timeout=10)
        raw = r.stdout or b""
        txt = ""
        for enc in ("gbk", "utf-8"):
            try:
                txt = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        lines = txt.splitlines()
        for i, line in enumerate(lines):
            if "IPv4" in line:
                m = re.search(r"\d+\.\d+\.\d+\.\d+", line)
                if m:
                    ips.append(m.group(0))
            if "默认网关" in line or "Default Gateway" in line:
                # 网关可能折到下一行（IPv6 网关占满一行时）
                for seg in (line, lines[i + 1] if i + 1 < len(lines) else ""):
                    for m in re.finditer(r"\d+\.\d+\.\d+\.\d+", seg):
                        gws.append(m.group(0))
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ips, gws


def same_subnet(a: str, b: str) -> bool:
    return a.split(".")[:3] == b.split(".")[:3]


def candidate_ips(adb_exe: str) -> list[str]:
    """列出"可能连得到手机"的 IP，按可能性排序。

    为什么不能只读 wlan0：**手机开热点时 wlan0 常常没有 IP**，
    热点接口变成了 ap0 / swlan0（192.168.43.1 之类）。只认 wlan0 会直接失败。
    所以要扫手机的所有接口，再用"电脑的默认网关"兜底 ——
    电脑连的是手机热点时，网关就是手机自己。
    """
    cands: list[str] = []

    def push(ip: str) -> None:
        if ip and not ip.startswith("127.") and ip not in cands:
            cands.append(ip)

    for cmd in (["shell", "ip", "-f", "inet", "addr", "show"],
                ["shell", "ifconfig"]):
        txt = adb(adb_exe, *cmd)
        for m in re.finditer(r"inet\s+(?:addr:)?(\d+\.\d+\.\d+\.\d+)", txt):
            push(m.group(1))
        if cands:
            break

    # 电脑默认网关兜底：电脑连手机热点时，网关 = 手机
    for gw in local_netinfo()[1]:
        push(gw)

    return cands


def device_states(adb_exe: str) -> list[tuple[str, str]]:
    """返回 [(serial, state)]。state 可能是 device / unauthorized / offline / recovery。

    为什么要区分：`adb devices` 里出现 `unauthorized` 时，设备其实**已经连上了**，
    只是手机没点"允许"。老版本只过滤 state=="device"，于是把它当成"没设备"，
    提示用户去插线/换线，方向完全错了。
    """
    txt = adb(adb_exe, "devices")
    out: list[tuple[str, str]] = []
    for line in txt.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            out.append((parts[0], parts[1]))
    return out


def list_devices(adb_exe: str) -> list[str]:
    return [s for s, st in device_states(adb_exe) if st == "device"]


def pick_target(adb_exe: str) -> str:
    """从在线设备里挑一台作为采样目标。

    为什么必须挑：Android 11+ 开「无线调试」后，系统除了 TCP 那条
    （`172.20.10.6:40203`）还会注册一条 mDNS 记录
    （`adb-XXXX-YYYY._adb-tls-connect._tcp`），`adb devices` 于是显示**两台**。
    这时任何不带 `-s` 的 `adb shell` 都只会返回
    `adb: more than one device/emulator`，解析出来的电量就是 0% ——
    今晚就踩了这个坑（脚本报"电量只有 0%"而手机明明是 100%）。
    """
    good = [s for s, st in device_states(adb_exe) if st == "device"]
    if not good:
        return ""
    tcp = [s for s in good if ":" in s and "_adb-tls-connect" not in s]
    plain = [s for s in good if "_adb-tls-connect" not in s]
    pick = (tcp or plain or good)[0]
    if len(good) > 1:
        print(f"[!] 同时在线 {len(good)} 台（无线调试会多注册一条 mDNS 别名），已指定：{pick}")
    return pick


def diagnose_no_device(adb_exe: str) -> None:
    """没有可用 USB 设备时，按 state 给出针对性排查（而不是笼统一句"插线"）。"""
    states = device_states(adb_exe)
    unauth = [s for s, st in states if st == "unauthorized"]
    offline = [s for s, st in states if st == "offline"]

    if unauth:
        print("[×] 手机连上了，但**没有授权**（state=unauthorized）。")
        print("    → 看手机屏幕，应该有个『允许 USB 调试吗？』的弹窗，勾『一律允许』再点『允许』。")
        print("    → 没弹窗就去：设置 → 系统和更新 → 开发人员选项 → 『撤销 USB 调试授权』，")
        print("      然后拔线重插，弹窗会重新出现。")
        return
    if offline:
        print("[×] 手机处于 offline 状态（连接不稳或 adb server 状态错乱）。")
        print("    → 终端里跑：adb kill-server   然后    adb devices")
        print("    → 还不行就换一根线、换主板后置 USB 口。")
        return

    print("[×] adb 完全看不到设备（列表是空的）。按顺序排查，每做一步就重跑本命令：")
    print("    1) 设置 → 系统和更新 → 开发人员选项 → 把『USB 调试』**关掉再打开**")
    print("    2) 同页面 → 『撤销 USB 调试授权』→ 拔线重插 → 手机上点『允许』")
    print("    3) 同页面若有『HDB 调试』→ **关掉它**（荣耀特有，它会抢走调试通道）")
    print("    4) 换一根**能传数据**的线（很多线只能充电），换主板后置 USB 口（别用前面板/扩展坞）")
    print("    5) 终端里跑：adb kill-server ; adb devices")
    print("    6) 手机下拉通知栏，把 USB 用途从『仅充电』改成『传输文件』")


def setup_wifi(adb_exe: str) -> str:
    """USB → 无线。返回可用的 `<ip>:5555`，失败返回空串。"""
    devs = list_devices(adb_exe)
    usb = [d for d in devs if ":" not in d]
    if not usb:
        diagnose_no_device(adb_exe)
        return ""
    print(f"[i] USB 设备：{usb[0]}")

    cands = candidate_ips(adb_exe)
    if not cands:
        print("[×] 一个可用 IP 都没读到。确认手机连着 WiFi、或者热点已经打开。")
        return ""
    print("[i] 候选 IP：" + "、".join(cands))
    print("    （手机开热点时，热点接口一般是 192.168.43.1 这类地址；"
          "电脑连热点时它就是网关）")

    pc_ips, pc_gws = local_netinfo()
    if pc_ips:
        print(f"[i] 电脑 IPv4：{'、'.join(pc_ips)}   网关：{'、'.join(pc_gws) or '（无）'}")
        reachable = [ip for ip in cands if any(same_subnet(ip, p) for p in pc_ips)]
        if reachable:
            print(f"[√] 手机和电脑在同一网段：{reachable[0]}")
            cands = reachable + [ip for ip in cands if ip not in reachable]
        else:
            print("[!] ⚠ 手机和电脑**不在同一网段**，adb 大概率连不上。")
            print(f"    手机 {cands[0]} ↔ 电脑 {pc_ips[0]}")
            print("    先把电脑连到手机热点（不要一个连热点、一个连校园 WiFi），再重跑这一步。")

    b = battery(adb_exe)
    print(f"[i] 当前电量 {b.get('level')}%  温度 {b.get('temp')}°C")

    print("[i] 切换到网络调试模式（adb tcpip 5555）…")
    adb(adb_exe, "tcpip", "5555")
    time.sleep(2)

    print("\n" + "=" * 62)
    print(" 现在请拔掉数据线（手机别关机，热点 / WiFi 保持开着）。")
    print(" 拔完按回车，我来逐个试这些 IP。")
    print("=" * 62)
    try:
        input(" 拔好线后按回车 > ")
    except EOFError:
        pass

    for ip in cands:
        print(f"[i] 试着连 {ip}:5555 …")
        adb(adb_exe, "connect", f"{ip}:5555")
        time.sleep(1.2)
        devs = list_devices(adb_exe)
        hit = [d for d in devs if d.startswith(ip)]
        if hit:
            b = battery(adb_exe)
            print(f"[√] 已走无线：{hit[0]}")
            print(f"[i] 电量 {b.get('level')}%  温度 {b.get('temp')}°C  "
                  f"充电中={'是（数据会作废！）' if b.get('charging') else '否 ✓'}")
            if b.get("charging"):
                print("[!] 还在充电：确认线已经拔掉、并且没有放在无线充电板上。")
            return hit[0]

    print("[×] 所有候选 IP 都没连上。")
    print("    退路 1：让**电脑和手机连同一个网络**（要么都连宿舍 WiFi，要么都用热点），")
    print("            不要一个连热点、一个连 WiFi。")
    print("    退路 2：把数据线插回去，改用有线采（数据会标'正在充电'，"
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
    ap.add_argument("--serial", default="", help="目标设备（无线调试时会看到两台，用 --serial 指定，"
                                                 "如 172.20.10.6:40203；留空则自动挑 TCP 那条）")
    args = ap.parse_args()

    adb_exe = find_adb()
    if not adb_exe:
        print("[×] 找不到 adb。本机应在 D:\\platform-tools\\platform-tools\\adb.exe")
        return 2
    print(f"[i] adb = {adb_exe}")

    global TARGET
    TARGET = args.serial.strip() or pick_target(adb_exe)
    if TARGET:
        print(f"[i] 目标设备：{TARGET}")

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
        if TARGET:
            cmd += ["--serial", TARGET]
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
