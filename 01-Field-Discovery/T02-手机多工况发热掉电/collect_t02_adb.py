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


SERIAL: str = ""          # 目标设备序列号；多设备时必须指定，否则 adb shell 会报错


def run_adb(adb: str, *args: str, timeout: int = 15) -> str:
    cmd = [adb]
    # 只有"对某台设备执行"的命令才加 -s；devices/connect/pair/tcpip 是 server 级命令，
    # 加了 -s 会直接报错。
    if SERIAL and args and args[0] == "shell":
        cmd += ["-s", SERIAL]
    cmd += list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
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
    # 必须锚到行首的 "voltage:"。否则会先匹配到 "Max charging voltage: 5000000"
    # （充电器输出电压），把 4.4V 的电池电压读成 5000V。
    m = re.search(r"^\s*voltage:\s*(\d+)", text, re.M)
    if m:
        out["volt"] = int(m.group(1))

    # 新版 dumpsys 没有 "charging: true" 这一行，只有四条 powered 标志。
    # 少读这一项，插着充电器的数据就不会被标"作废"，一路污染到结论里。
    powered = bool(re.search(r"^\s*(?:AC|USB|Wireless|Dock) powered:\s*true", text, re.M))
    out["powered"] = powered

    code = 0
    m = re.search(r"^\s*status:\s*(\d+)", text, re.M)
    if m:
        code = int(m.group(1))
        # 2=充电中 3=放电中 4=未充电 5=已充满
        out["status"] = {2: "充电中", 3: "放电中", 4: "未充电", 5: "已充满"}.get(code, str(code))

    # 只要外部供电就算"在充电"——100% 时 status 可能是 5（已充满），
    # 但电量一样不会掉，这类数据同样作废。
    out["charging"] = powered or code == 2
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
def clean_csv() -> int:
    """删掉"插着充电器采的"废行。

    判据两条，任一条命中即删：
      1) 备注里带"作废"（采集时就知道在充电）；
      2) 电压 > 6000 mV —— 真机电池电压不可能这么高，
         这是把 `Max charging voltage: 5000000` 读成电池电压的典型特征。
    """
    if not os.path.exists(CSV):
        print(f"[i] {CSV} 还不存在，不用清。")
        return 0
    with open(CSV, encoding="utf-8-sig") as f:
        rows = [r.rstrip("\n") for r in f if r.strip()]
    if not rows:
        print("[i] 文件是空的。")
        return 0

    head, body = rows[0], rows[1:]
    keep, drop = [], []
    for r in body:
        cells = r.split(",")
        bad = "作废" in r
        if not bad and len(cells) > 5:
            try:
                bad = float(cells[5]) > 6000
            except ValueError:
                pass
        (drop if bad else keep).append(r)

    if not drop:
        print(f"[i] 没有废行，现有 {len(keep)} 行都是有效数据。")
        return 0
    with open(CSV, "w", encoding="utf-8-sig", newline="") as f:
        f.write(head + "\n")
        for r in keep:
            f.write(r + "\n")
    print(f"[√] 删掉 {len(drop)} 行废数据（充电中 / 电压异常），保留 {len(keep)} 行有效数据。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只检查设备连接，不采样")
    ap.add_argument("--clean", action="store_true", help="清掉记录表里充电时采的废行")
    ap.add_argument("--cond", default="", help="工况名，例如 待机 / 短视频 / 导航拍摄 / 高帧率游戏")
    ap.add_argument("--min", type=float, default=30, help="本段时长（分钟）")
    ap.add_argument("--interval", type=float, default=30, help="采样间隔（秒），默认 30")
    ap.add_argument("--room", default="", help="室温 °C（手填，没有传感器就写大概值）")
    ap.add_argument("--note", default="", help="备注，例如 '飞行模式关/通知关'")
    ap.add_argument("--app", default="", help="前台应用（可留空自动取）")
    ap.add_argument("--serial", default="", help="目标设备序列号（多设备时必须给，例如 172.20.10.6:40203）")
    args = ap.parse_args()

    if args.clean:                      # 不需要连手机，先处理
        return clean_csv()

    global SERIAL
    SERIAL = args.serial or os.environ.get("ANDROID_SERIAL", "").strip()

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

    # 多设备时自动挑一台：优先 TCP/IP（含冒号），并且避开 mDNS 别名
    # （Android 11+ 的无线调试会额外注册一条 adb-XXXX-YYYY._adb-tls-connect._tcp，
    #   于是 devices 里出现"两台"，不带 -s 的 adb shell 会报 more than one device，
    #   读到的电量就是空的 —— 表现为"电量 0%"）
    if not SERIAL:
        cand = [l.split()[0] for l in lines if l.endswith("device")]
        tcp = [s for s in cand if ":" in s and "_adb-tls-connect" not in s]
        plain = [s for s in cand if "_adb-tls-connect" not in s]
        SERIAL = (tcp or plain or cand or [""])[0]
    if SERIAL:
        print(f"[i] 目标设备：{SERIAL}")
    if len([l for l in lines if l.endswith("device")]) > 1:
        print(f"[!] 同时在线 {len([l for l in lines if l.endswith('device')])} 台（无线调试会多注册一条 mDNS 别名），"
              f"已自动指定 {SERIAL}")

    bat = parse_battery(run_adb(adb, "shell", "dumpsys", "battery"))
    print(f"[i] 当前电量 {bat.get('level')}%  温度 {bat.get('temp')}°C  电压 {bat.get('volt')} mV  "
          f"状态 {bat.get('status', '?')}  外部供电 {'是 <- 数据会作废' if bat.get('charging') else '否 OK'}")
    if args.check:
        if bat.get("charging"):
            print("[×] 手机正在被充电（插着数据线 / 充电头 / 无线充电板）。")
            print("    充电时电量不会掉，30 分钟采出来是一条平线，整段作废。")
            print("    请拔掉线再重跑 --check。")
            return 1
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
                # 无线调试下息屏久了可能被系统掐掉 TCP，这里自动重连一次再报错
                if SERIAL and ":" in SERIAL:
                    run_adb(adb, "connect", SERIAL, timeout=8)
                print("  [!] 这轮没读到电池信息（无线可能断了，已在后台尝试重连）", end="\r")

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
