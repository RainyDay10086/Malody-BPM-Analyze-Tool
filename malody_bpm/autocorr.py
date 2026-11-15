import ctypes
import numpy as np
from .filters import _c_dsp, require_c_dsp


def compute_autocorrelation_fft(env_1000hz: np.ndarray, exact: bool = True) -> np.ndarray:
    """
    通过零填充基-2 FFT 计算维纳-辛钦 (Wiener-Khinchin) 全局自相关 (严格对齐 Malody 0x1000bfa7c & 0x100191f40)：
    1. 若 n + (n // 2) >= 0x100001 (1048577)，则截断输入至 0x2aaaa8 // 4 = 699050 采样点 (~699 秒)。
    2. 有效输出自相关长度 out_len = n // 2 (仅保留重叠度 >= 50% 的前半程，消除尾部高方差伪峰)。
    3. 零填充长度 n_padded = next_pow2(n + out_len - 1)（对齐原版 0x100191f62 对 n + max_lag - 2 取位长）。
    4. 所有模式统一直接调用 C 原生单精度 float32 基-2 蝶形 FFT (0x100034599 & 0x1000344a6 2^20 旋转因子表)。
    """
    n = len(env_1000hz)
    if n <= 1:
        return np.array([0.0], dtype=np.float32)

    if n + (n // 2) >= 0x100001:
        env_1000hz = env_1000hz[: 0x2AAAA8 // 4]
        n = len(env_1000hz)

    out_len = n // 2
    if out_len <= 0:
        return np.array([0.0], dtype=np.float32)

    c_dsp = require_c_dsp(_c_dsp)
    src_c = np.ascontiguousarray(env_1000hz, dtype=np.float32)
    acf = np.empty(out_len, dtype=np.float32)
    c_dsp.autocorr_fft_exact_c(
        src_c.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
        n,
        out_len,
        acf.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
    )
    return acf



def find_tempo_peaks(
    acf: np.ndarray,
    min_bpm: float = 60.0,
    max_bpm: float = 280.0
) -> list[tuple[float, float, int]]:
    """
    在指定 BPM 区间内寻找自相关峰值，并进行抛物线二次插值。
    返回 [(raw_bpm, peak_height, lag_int), ...]，按显著度降序排列。
    """
    min_lag = int(np.floor(60000.0 / max_bpm))
    max_lag = int(np.ceil(60000.0 / min_bpm))

    min_lag = max(min_lag, 2)
    max_lag = min(max_lag, len(acf) - 2)

    candidates = []

    for lag in range(min_lag, max_lag + 1):
        y0 = acf[lag]
        ym1 = acf[lag - 1]
        yp1 = acf[lag + 1]

        if y0 > ym1 and y0 > yp1 and y0 > 0.0:
            denom = 2.0 * y0 - ym1 - yp1
            if denom > 1e-12:
                delta = 0.5 * (yp1 - ym1) / denom
                delta = max(min(delta, 1.0), -1.0)
            else:
                delta = 0.0

            refined_lag = lag + delta
            if refined_lag > 0:
                raw_bpm = 60000.0 / refined_lag
                candidates.append((raw_bpm, float(y0), lag))

    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates


def estimate_instability(
    env_1000hz: np.ndarray,
    bpm: float,
    is_quantized: bool = False,
    raw_instability: float = 0.0
) -> float:
    """
    计算节奏不稳定度 (Instability / Variance Metric，对齐 Malody 0x1000c0560 / Windows 0x474c28)：
    Malody 原版直接输出全曲线性回归的标准差，UI 界面使用 "%.2f" 格式化展示。
    """
    if raw_instability > 0.0:
        return float(raw_instability)
    return 0.0
