"""
features.py —— 时域 / 频域 / 调制域特征提取
------------------------------------------
输入一段波形，输出一个固定列序的 float 字典，供 sklearn 直接喂。

特征分三层，对应"声振诊断"的三个物理线索：

  A. 时域（幅度与冲击性）
     RMS / 峰值 / 峰值因子 / 峭度 / 偏度 / 零穿越率
     → 反映"有多响、有多少冲击成分"。扇叶失衡会带来周期性冲击，
       峭度和峰值因子上升；遮挡后气流湍流增加，RMS 上升但峭度下降。

  B. 频域（能量分布与音色）
     谱质心 / 谱带宽 / 谱滚降(85%) / 谱平坦度 / 谱熵 / 谱通量
     分频段能量比（0-60Hz…8kHz 共 8 段）/ 主峰频率 / 低高频比
     → 谱质心上升 = 高频"嘶嘶"变多 = 进风受阻产生湍流噪声；
       谱平坦度上升 = 从"有调"走向"白噪"。

  C. 调制域（周期结构，本项目最有区分度的一层）
     1) 叶频估计：平均谱里最高的若干个峰的间隔中位数 → 叶片通过频率 BPF
     2) 谐波能量比：在 f0 的 1~6 次谐波处取能量 / 总能量
     3) 包络自相关：|x| 低通后的自相关峰值 → 调制周期（转频/叶频）
     4) 调制深度：包络的标准差 / 均值
     → 失衡会让"转频"处出现强边带，调制深度显著变大；
       正常工况的调制则来自叶片均匀扫过，深度较小且周期稳定。

所有帧级特征最终做 mean + std 聚合，既保留均值也保留波动性。
"""

from __future__ import annotations

import numpy as np

EPS = 1e-10

# 帧参数：16kHz 下 n_fft=4096 → 频率分辨率 3.9Hz，足够分辨 30Hz 级转频
N_FFT = 4096
HOP = 512

# 8 个频段的上界（Hz）
BAND_EDGES = [0, 60, 120, 250, 500, 1000, 2000, 4000, 8000]

# 帧级特征名（顺序固定，保证特征矩阵列顺序稳定）
FRAME_KEYS = [
    "rms", "peak", "crest", "kurt", "skew", "zcr", "duty",
    "centroid", "bandwidth", "rolloff85", "flatness", "entropy", "flux",
    "dom_freq", "dom_ratio", "tonal_ratio", "lowhigh_ratio",
] + [f"band{i}" for i in range(len(BAND_EDGES) - 1)]

SEG_KEYS = [
    "bpf", "peak_spacing", "harmonic_ratio", "envelope_mod_freq",
    "envelope_mod_depth", "envelope_periodicity", "seg_rms", "seg_kurt",
]


def _frames(x: np.ndarray) -> np.ndarray:
    """切成 50% 重叠的加窗帧，返回 (n_frames, N_FFT)。"""
    if len(x) < N_FFT:
        x = np.pad(x, (0, N_FFT - len(x)))
    n = 1 + (len(x) - N_FFT) // HOP
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n)[:, None]
    return x[idx]


def _spectrogram(x: np.ndarray) -> np.ndarray:
    fr = _frames(x)
    win = np.hanning(N_FFT).astype(np.float32)
    spec = np.abs(np.fft.rfft(fr * win, axis=1)).astype(np.float64)
    return spec  # (n_frames, N_FFT//2+1)


def _frame_features(x: np.ndarray, sr: int) -> dict[str, np.ndarray]:
    """逐帧特征，返回 {name: array(n_frames)}。"""
    fr = _frames(x)
    spec = _spectrogram(x)
    freqs = np.fft.rfftfreq(N_FFT, 1.0 / sr)

    out: dict[str, np.ndarray] = {}

    # ---------- A. 时域 ----------
    rms = np.sqrt(np.mean(fr ** 2, axis=1))
    peak = np.max(np.abs(fr), axis=1)
    out["rms"] = rms
    out["peak"] = peak
    out["crest"] = peak / (rms + EPS)
    m2 = np.mean(fr ** 2, axis=1)
    m4 = np.mean(fr ** 4, axis=1)
    sd = np.std(fr, axis=1) + EPS
    out["kurt"] = m4 / (m2 ** 2 + EPS)
    out["skew"] = np.mean((fr - np.mean(fr, axis=1, keepdims=True)) ** 3, axis=1) / sd ** 3
    out["zcr"] = np.mean(np.abs(np.diff(np.sign(fr), axis=1)) > 0, axis=1)
    out["duty"] = np.mean(np.abs(fr) > 0.25 * (peak[:, None] + EPS), axis=1)

    # ---------- B. 频域 ----------
    P = spec ** 2
    Psum = P.sum(axis=1) + EPS
    out["centroid"] = (P * freqs).sum(axis=1) / Psum
    out["bandwidth"] = np.sqrt((P * (freqs[None, :] - out["centroid"][:, None]) ** 2).sum(axis=1) / Psum)

    cs = np.cumsum(P, axis=1)
    thr = 0.85 * cs[:, -1:]
    out["rolloff85"] = freqs[np.argmax(cs >= thr, axis=1)]

    pn = P / Psum[:, None]
    out["flatness"] = np.exp(np.mean(np.log(pn + EPS), axis=1)) / (np.mean(pn, axis=1) + EPS)
    out["entropy"] = -np.sum(pn * np.log(pn + EPS), axis=1) / np.log(pn.shape[1])

    sn = spec / (spec.sum(axis=1, keepdims=True) + EPS)
    flux = np.sqrt(np.sum(np.diff(sn, axis=0) ** 2, axis=1))
    out["flux"] = np.concatenate([[0.0], flux])[: len(sn)]

    # 主峰频率与"纯度"：避开 0-20Hz 的直流残留
    lo = int(np.searchsorted(freqs, 20))
    for i in range(P.shape[0]):
        j = lo + int(np.argmax(P[i, lo:]))
        out.setdefault("_dom_idx", []).append(j)
    dom_idx = np.array(out.pop("_dom_idx"))
    out["dom_freq"] = freqs[dom_idx]
    out["dom_ratio"] = P[np.arange(P.shape[0]), dom_idx] / Psum
    out["tonal_ratio"] = P.max(axis=1) / (np.median(P, axis=1) + EPS)

    low = P[:, freqs <= 500].sum(axis=1)
    high = P[:, freqs > 2000].sum(axis=1)
    out["lowhigh_ratio"] = low / (high + EPS)

    for i in range(len(BAND_EDGES) - 1):
        m = (freqs >= BAND_EDGES[i]) & (freqs < BAND_EDGES[i + 1])
        out[f"band{i}"] = P[:, m].sum(axis=1) / Psum

    return out


def _peak_spacing(freqs: np.ndarray, mean_spec: np.ndarray, n_peak: int = 8) -> float:
    """平均谱里取前 n 个峰，返回峰间隔的中位数 = 叶片通过频率的估计。"""
    from scipy.signal import find_peaks

    lo = int(np.searchsorted(freqs, 25))
    hi = int(np.searchsorted(freqs, 1500))
    seg = mean_spec[lo:hi]
    if seg.size < 8 or seg.max() <= 0:
        return 0.0
    idx, props = find_peaks(seg, prominence=seg.max() * 0.05)
    if len(idx) < 2:
        return 0.0
    top = idx[np.argsort(props["prominences"])[-n_peak:]]
    top.sort()
    gaps = np.diff(freqs[lo:hi][top])
    gaps = gaps[gaps > 5]
    return float(np.median(gaps)) if gaps.size else 0.0


def _envelope_features(x: np.ndarray, sr: int) -> tuple[float, float, float]:
    """包络自相关 → (调制频率 Hz, 调制深度, 周期性得分)。"""
    # 包络：整流 + 低通（抽取到 1kHz 再做自相关，省算力）
    env = np.abs(x)
    ds = max(1, sr // 1000)
    env = env[: len(env) // ds * ds].reshape(-1, ds).mean(axis=1)
    env_sr = sr / ds
    env = env - env.mean()
    if env.size < 64 or np.std(env) < EPS:
        return 0.0, 0.0, 0.0

    depth = float(np.std(env) / (np.mean(np.abs(x)) + EPS))

    n = 1 << int(np.ceil(np.log2(2 * env.size)))
    ac = np.fft.irfft(np.abs(np.fft.rfft(env, n)) ** 2, n)[: env.size]
    ac = ac / (ac[0] + EPS)

    # 只在 5~250Hz 的调制频率范围内找峰
    lag_lo = max(1, int(env_sr / 250))
    lag_hi = min(env.size - 1, int(env_sr / 5))
    if lag_hi <= lag_lo + 2:
        return 0.0, depth, 0.0
    seg = ac[lag_lo:lag_hi]
    k = int(np.argmax(seg))
    mod_f = env_sr / (lag_lo + k)
    periodicity = float(seg[k])
    return float(mod_f), depth, periodicity


def _harmonic_ratio(freqs: np.ndarray, mean_spec: np.ndarray, f0: float) -> float:
    """f0 的 1~6 次谐波处能量占总能量的比例（衡量"有调性"）。"""
    if f0 < 20:
        return 0.0
    total = mean_spec.sum() + EPS
    acc = 0.0
    for k in range(1, 7):
        f = k * f0
        if f > freqs[-1]:
            break
        j = int(np.searchsorted(freqs, f))
        j = min(max(j, 0), len(mean_spec) - 1)
        acc += mean_spec[max(0, j - 2): j + 3].sum()
    return float(acc / total)


def extract_segment(x: np.ndarray, sr: int) -> dict[str, float]:
    """一段波形（建议 1 秒）→ 特征字典（列序固定）。整个数据集共用这一套列。"""
    f = _frame_features(x, sr)
    freqs = np.fft.rfftfreq(N_FFT, 1.0 / sr)
    mean_spec = _spectrogram(x).mean(axis=0)

    feat: dict[str, float] = {}
    for k in FRAME_KEYS:
        v = np.asarray(f[k], dtype=np.float64)
        v = np.nan_to_num(v, nan=0.0, posinf=0.0, neginf=0.0)
        feat[f"{k}_mean"] = float(np.mean(v))
        feat[f"{k}_std"] = float(np.std(v))

    # ---------- C. 调制域（段级） ----------
    bpf = _peak_spacing(freqs, mean_spec)
    f0 = feat.get("dom_freq_mean", 0.0)
    mod_f, mod_d, period = _envelope_features(x, sr)

    feat["bpf"] = bpf
    feat["peak_spacing"] = bpf
    feat["harmonic_ratio"] = _harmonic_ratio(freqs, mean_spec, bpf / 3.0 if bpf else f0)
    feat["envelope_mod_freq"] = mod_f
    feat["envelope_mod_depth"] = mod_d
    feat["envelope_periodicity"] = period
    feat["seg_rms"] = float(np.sqrt(np.mean(x ** 2)))
    feat["seg_kurt"] = float(np.mean((x - x.mean()) ** 4) / (np.var(x) ** 2 + EPS))

    return feat


FEATURE_NAMES = (
    [f"{k}_{s}" for k in FRAME_KEYS for s in ("mean", "std")] + SEG_KEYS
)


def feature_names() -> list[str]:
    """统一的特征列顺序（58 列 = 25 个帧级特征 × mean/std + 8 个段级特征）。"""
    return list(FEATURE_NAMES)


# 面试时讲"哪些特征最重要"用：给每一列一个人话解释。
# 键 = 去掉 _mean/_std 后缀的基础特征名，describe() 负责拼回来。
FEATURE_DOC_BASE = {
    # ---- 时域 ----
    "rms": "均方根：整体响度。换手机/换距离就变，属于最脆弱的特征",
    "peak": "峰值幅度：瞬时最大冲击",
    "crest": "峰值因子=峰值/有效值：冲击性强弱，失衡的周期性撞击会抬高它",
    "kurt": "峭度：>3 表示有冲击成分，正常稳定转动接近 3",
    "skew": "偏度：波形上下不对称程度（叶片偏心会带来偏斜）",
    "zcr": "零穿越率：与主频正相关，转速越高越高",
    "duty": "占空比：超过 25% 峰值的采样点比例，反映波形的「饱满」程度",
    # ---- 频域 ----
    "centroid": "谱质心：能量重心。遮挡/湍流↑，叶片失衡几乎不变",
    "bandwidth": "谱带宽：能量分布的分散程度",
    "rolloff85": "85% 能量滚降频率：高频噪声多则升高",
    "flatness": "谱平坦度：接近 1 = 白噪，接近 0 = 有明确音调",
    "entropy": "谱熵：能量分布的混乱度",
    "flux": "谱通量：相邻帧频谱的变化速度，反映时变剧烈程度",
    "dom_freq": "主峰频率：正常工况 ≈ 叶片通过频率",
    "dom_ratio": "主峰能量占比：单一音调的强度",
    "tonal_ratio": "最强峰 / 中位谱 = 音调纯度，失衡会分散到边带",
    "lowhigh_ratio": "0-500Hz 与 >2kHz 能量比：低频机械振动 vs 高频气流",
    "band0": "0–60Hz 能量占比：转频基频",
    "band1": "60–120Hz 能量占比：叶频（3 叶 × 20~40Hz 转频）",
    "band2": "120–250Hz 能量占比：低次谐波",
    "band3": "250–500Hz 能量占比：中低频机械振动",
    "band4": "500–1kHz 能量占比：机械/气流过渡带",
    "band5": "1–2kHz 能量占比：气流噪声起始带",
    "band6": "2–4kHz 能量占比：湍流噪声主带（遮挡最敏感）",
    "band7": "4–8kHz 能量占比：高频嘶声，遮挡/漏风时明显上升",
}

FEATURE_DOC = {
    **{f"{k}_mean": v for k, v in FEATURE_DOC_BASE.items()},
    "bpf": "叶片通过频率：谱峰间隔中位数，转速的间接测量",
    "peak_spacing": "同 bpf（保留别名便于对照）",
    "harmonic_ratio": "谐波能量占比：结构规则性，越接近纯音越高",
    "envelope_mod_freq": "调制频率：包络起伏的速率（≈转频或其倍频）",
    "envelope_mod_depth": "包络调制深度：失衡最敏感的特征之一",
    "envelope_periodicity": "包络自相关峰值：周期调制的确定性",
    "seg_rms": "整段响度（未做帧聚合的原始 RMS）",
    "seg_kurt": "整段峭度：冲击性的段级估计",
}


def describe(name: str) -> str:
    """给任意特征列名一个人话解释（找不到就返回空串，不报错）。"""
    if name in FEATURE_DOC:
        return FEATURE_DOC[name]
    for suf in ("_mean", "_std"):
        if name.endswith(suf) and name[: -len(suf)] in FEATURE_DOC_BASE:
            base = FEATURE_DOC_BASE[name[: -len(suf)]]
            return base if suf == "_mean" else f"{base}（帧间 std）"
    return ""
