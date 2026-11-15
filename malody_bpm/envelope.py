import ctypes
import math
import numpy as np
from .filters import (
    BAND_CONFIGS,
    SMOOTH_CONFIGS,
    BAND_DELAYS,
    BAND_WEIGHTS,
    MASTER_SMOOTH_CONFIG,
    get_filter_stages,
    _c_dsp,
    require_c_dsp,
)


def apply_zero_phase_diff(env_1000hz: np.ndarray, sample_rate: int = 44100, exact: bool = True) -> np.ndarray:
    """
    Malody 0x1000bf82e - 0x1000bf8e9 (Windows 0x474075) 步骤 8: 零相位奇对称 IIR 差分滤波
    Stage 15 (b1_b0 = 0, b2_b0 = 0, a1 = -1.4, a2 = 0.48, gain = 0.2)
    精确错位差分索引 (彻底抵消中心项 0.2 * src[i]):
      dst[0]     =  A[n - 2]            (= bwd[1])
      dst[i]     =  A[n - 2 - i] - B[i - 1] (= bwd[i + 1] - fwd[i - 1], 1 <= i <= n - 2)
      dst[n - 1] = -B[n - 2]            (= -fwd[n - 2])
    """
    n = len(env_1000hz)
    if n <= 0:
        return np.zeros(0, dtype=np.float32)
    c_dsp = require_c_dsp(_c_dsp)
    diff = np.zeros(n, dtype=np.float32)
    src_c = np.ascontiguousarray(env_1000hz, dtype=np.float32)

    if exact:
        stage15 = np.ascontiguousarray(get_filter_stages(sample_rate, 15, 1).flatten(), dtype=np.float64)
        c_dsp.apply_zero_phase_diff_exact(
            src_c.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
            n,
            diff.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
            stage15.ctypes.data_as(ctypes.POINTER(ctypes.c_double))
        )
    else:
        c_dsp.apply_zero_phase_diff(
            src_c.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
            n,
            diff.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        )
    return diff


def _extract_onset_envelope_multi_fallback(
    pcm_2d: np.ndarray,
    sample_rate: int,
    c_dsp: ctypes.CDLL,
    n_dst: int,
) -> tuple[np.ndarray, np.ndarray]:
    """
    当预编译 C 动态库尚未包含 extract_onset_envelope_multi_c 符号时（如未重编的跨平台预编译库），
    组合调用库内已有的导出 C 算子完成多声道 4 频段平方能量均值包络提取（输出与 C 快路径逐位一致）。
    """
    n_frames, n_channels = pcm_2d.shape
    all_stages_2d = get_filter_stages(sample_rate, 0, 16)
    master_env = np.zeros(n_frames, dtype=np.float32)
    master_ptr = master_env.ctypes.data_as(ctypes.POINTER(ctypes.c_float))

    for b in range(4):
        b_start, b_count = BAND_CONFIGS[b]
        s_start, s_count = SMOOTH_CONFIGS[b]
        band_stages = np.ascontiguousarray(
            all_stages_2d[b_start : b_start + b_count].flatten(),
            dtype=np.float64,
        )
        band_stages_ptr = band_stages.ctypes.data_as(ctypes.POINTER(ctypes.c_double))

        energy = np.zeros(n_frames, dtype=np.float32)
        energy_ptr = energy.ctypes.data_as(ctypes.POINTER(ctypes.c_float))

        for c in range(n_channels):
            ch_buf = np.array(pcm_2d[:, c], dtype=np.float32, copy=True, order="C")
            c_dsp.cascade_biquad_inplace(
                ch_buf.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                n_frames,
                band_stages_ptr,
                b_count,
            )
            np.multiply(ch_buf, ch_buf, out=ch_buf)
            np.add(energy, ch_buf, out=energy)

        if n_channels >= 2:
            inv_ch = np.float32(1.0) / np.float32(n_channels)
            np.multiply(energy, inv_ch, out=energy)

        if s_count > 0:
            sm_stages = np.ascontiguousarray(
                all_stages_2d[s_start : s_start + s_count].flatten(),
                dtype=np.float64,
            )
            c_dsp.cascade_biquad_inplace(
                energy_ptr,
                n_frames,
                sm_stages.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
                s_count,
            )

        median_b = float(c_dsp.compute_median_float_histogram(energy_ptr, n_frames))
        c_dsp.accumulate_band_log_novelty(
            energy_ptr,
            n_frames,
            BAND_DELAYS[b],
            ctypes.c_float(BAND_WEIGHTS[b]),
            ctypes.c_float(median_b),
            master_ptr,
            0,
        )

    mst_start, mst_count = MASTER_SMOOTH_CONFIG
    mst_stages = np.ascontiguousarray(
        all_stages_2d[mst_start : mst_start + mst_count].flatten(),
        dtype=np.float64,
    )
    c_dsp.cascade_biquad_inplace(
        master_ptr,
        n_frames,
        mst_stages.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
        mst_count,
    )

    env_1000hz = np.zeros(n_dst, dtype=np.float32)
    diff_1000hz = np.zeros(n_dst, dtype=np.float32)
    env_ptr = env_1000hz.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
    diff_ptr = diff_1000hz.ctypes.data_as(ctypes.POINTER(ctypes.c_float))

    c_dsp.resample_to_1000hz(master_ptr, n_frames, sample_rate, env_ptr, n_dst)
    c_dsp.apply_zero_phase_diff(env_ptr, n_dst, diff_ptr)
    return diff_1000hz, env_1000hz


def extract_onset_envelope(pcm: np.ndarray, sample_rate: int, exact: bool = True) -> tuple[np.ndarray, np.ndarray]:
    """
    执行 Malody 完整的 4 频段起始点突变包络检测管线 (0x1000bf52c)：
    1. 4 频段 IIR 级联双二阶滤波
    2. 能量平方化（多声道非 exact 下逐声道累加平方能量并按声道数取均值）与 Band 0 专用能量低通平滑
    3. 4096-bin IEEE-754 浮点直方图中值去噪
    4. 对数动态范围压缩与群延迟时间对齐累加
    5. 主融合包络末端 2 级双二阶低通平滑
    6. 1000 Hz (1 ms/采样点) 毫秒级重采样
    7. 步骤 8 零相位奇对称 IIR 错位差分滤波
    返回 (diff_1000hz, raw_env_1000hz)
    """
    n_samples = len(pcm)
    if n_samples <= 0:
        return np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.float32)

    c_dsp = require_c_dsp(_c_dsp)
    n_dst = int(math.ceil(n_samples * (1000.0 / float(sample_rate))))
    if n_dst <= 0:
        return np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.float32)

    if pcm.ndim == 2:
        n_channels = pcm.shape[1]
        if n_channels <= 0:
            return np.zeros(0, dtype=np.float32), np.zeros(0, dtype=np.float32)
        if not exact and n_channels >= 2:
            if hasattr(c_dsp, "extract_onset_envelope_multi_c"):
                pcm_c = np.ascontiguousarray(pcm, dtype=np.float32)
                all_stages = np.ascontiguousarray(
                    get_filter_stages(sample_rate, 0, 16).flatten(),
                    dtype=np.float64,
                )
                diff_1000hz = np.zeros(n_dst, dtype=np.float32)
                env_1000hz = np.zeros(n_dst, dtype=np.float32)
                c_dsp.extract_onset_envelope_multi_c(
                    pcm_c.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                    n_samples,
                    n_channels,
                    sample_rate,
                    all_stages.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
                    diff_1000hz.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                    env_1000hz.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                    n_dst,
                )
                return diff_1000hz, env_1000hz
            return _extract_onset_envelope_multi_fallback(pcm, sample_rate, c_dsp, n_dst)
        if n_channels == 1:
            pcm = pcm[:, 0]
        else:
            pcm = np.mean(pcm, axis=1, dtype=np.float32)

    pcm_c = np.ascontiguousarray(pcm, dtype=np.float32)
    all_stages = np.ascontiguousarray(get_filter_stages(sample_rate, 0, 16).flatten(), dtype=np.float64)
    diff_1000hz = np.zeros(n_dst, dtype=np.float32)
    env_1000hz = np.zeros(n_dst, dtype=np.float32)
    c_dsp.extract_onset_envelope_c(
        pcm_c.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
        n_samples,
        sample_rate,
        all_stages.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
        1 if exact else 0,
        diff_1000hz.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
        env_1000hz.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
        n_dst
    )
    return diff_1000hz, env_1000hz
