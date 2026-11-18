"""
make_demo.py —— 合成数据，用于在"还没录音"时先把整条流水线跑通
----------------------------------------------------------------
⚠️ 这个脚本生成的是【合成测试数据】，只用于验证代码没有 bug，
   绝对不能当作实验数据提交。它默认写到仓库外的 _selftest/，
   且 .gitignore 已把 _selftest/ 排除。

  真实的 75 个录音必须由你按 docs/01-录音与实验SOP.md 亲手采。

用法：
  python src/make_demo.py                 # 生成 75 个合成 wav 到 _selftest/raw/
  python src/make_demo.py --dir D:/tmp/demo
"""

from __future__ import annotations

import argparse
import os

import numpy as np

SR = 16000
DUR = 10.0  # 每个文件 10 秒

COND = ["N1", "N2", "F1", "O1", "S0"]
SESS = ["S1", "S2", "S3"]


def _pink(n: int, rng: np.random.Generator) -> np.ndarray:
    w = rng.normal(size=n)
    f = np.fft.rfft(w)
    k = np.arange(1, len(f) + 1)
    return np.fft.irfft(f / np.sqrt(k), n)


def synth(cond: str, session: str, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(SR * DUR)
    t = np.arange(n) / SR

    if session == "S1":
        rot = 22.0
    elif session == "S2":
        rot = 26.0
    else:
        rot = 24.0

    if cond == "S0":  # 停机：只有环境底噪
        x = 0.02 * _pink(n, rng)
        return x / (np.max(np.abs(x)) + 1e-9) * 0.05

    gain = {"N1": 0.35, "N2": 0.55, "F1": 0.55, "O1": 0.55}[cond]
    bpf = rot * 3  # 3 叶

    # 转频 + 1~5 次谐波（含叶频）
    x = np.zeros(n)
    for k in range(1, 6):
        amp = 0.6 / k
        if abs(k * rot - bpf) < 1e-6:
            amp *= 2.0  # 叶频更强
        x += amp * np.sin(2 * np.pi * k * rot * t + rng.uniform(0, 6.28))

    # 宽频气流噪声
    x += 0.35 * _pink(n, rng)

    if cond == "F1":
        # 失衡：幅度调制（周期性）+ 每转一次机械撞击
        mod = 1.0 + 0.55 * np.sin(2 * np.pi * rot * t)
        x *= mod
        clicks = np.zeros(n)
        for c in np.arange(0, DUR, 1.0 / rot):
            i0 = int(c * SR)
            k_len = int(0.004 * SR)
            env = np.exp(-np.linspace(0, 6, k_len))
            clicks[i0:i0 + k_len] += env[: max(0, min(k_len, n - i0))] * rng.normal()
        x += 0.9 * clicks

    if cond == "O1":
        # 遮挡：高频湍流噪声显著上升，低频基频反而被闷住
        x *= 0.75
        flow = rng.normal(size=n)
        f = np.fft.rfft(flow)
        fr = np.fft.rfftfreq(n, 1 / SR)
        f[(fr < 1800) | (fr > 7000)] = 0
        x += 0.55 * np.fft.irfft(f, n) / (np.std(np.fft.irfft(f, n)) + 1e-9)

    if cond == "N1":
        x *= 0.75

    # 房间底噪（同一会话共享，故意做成"会话指纹"，检验分组切分是否必要）
    room = {"S1": 0.012, "S2": 0.02, "S3": 0.03}[session]
    x = gain * x / (np.max(np.abs(x)) + 1e-9) + room * _pink(n, rng)
    return x / (np.max(np.abs(x)) + 1e-9) * 0.9


def write_wav(path: str, x: np.ndarray):
    import wave

    pcm = np.clip(x, -1, 1)
    pcm = (pcm * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def main():
    ap = argparse.ArgumentParser()
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--dir", default=os.path.join(os.path.dirname(here), "_selftest", "raw"))
    args = ap.parse_args()

    n = 0
    for si, s in enumerate(SESS):
        d = os.path.join(args.dir, s)
        os.makedirs(d, exist_ok=True)
        for ci, c in enumerate(COND):
            for j in range(1, 6):
                seed = 1000 * si + 10 * ci + j
                fp = os.path.join(d, f"FAN_{c}_{s}_{j:02d}.wav")
                write_wav(fp, synth(c, s, seed))
                n += 1
    print(f"[demo] 已生成 {n} 个合成 wav → {args.dir}")
    print("[demo] 合成数据仅用于跑通流水线，不可提交为实验数据。")


if __name__ == "__main__":
    main()
