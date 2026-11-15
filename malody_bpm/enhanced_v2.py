"""
增强优化模式 2.0 (mode="enhanced_v2", "enhanced2", "v2") 决策引擎。

轻量级内核：分窗池化自相关 (Pooled ACF) + exact 原生 C 库追踪器。
  1. 架构定位：作为 enhanced_v2 的全新轻量级内核重构。
  2. 分窗与池化策略：
     - 固定 15 秒窗长（15,000 采样点）与 50% 步进重叠（7.5 秒步长）；
     - 全窗无差别等权累加；
     - 每个窗口独立调用 C 原生 FFT 自相关 (autocorr_fft_exact_c)，最大滞后截断至 8000ms（8000 采样点）；
     - 所有窗口自相关累加后除以窗口数得到 acf_pooled（长度为 8000）。
  3. 核心追踪与回归（无缝复用 exact C 原生库）：
     - 阶段 1：使用 find_acf_local_peaks_c 在 acf_pooled 前 4000ms 内查找局部极大值峰，生成候选；
     - 阶段 2：候选评选，选出得分最高的 best_tau；
     - 阶段 3：调用 track_full_song_exact_c 在 acf_pooled（8000ms）上执行亚像素高精度 OLS 线性回归。
  4. 门控与区间处理：
     - 若 n_matched < 3 判定为无法锁定（返回 0.0, 0.0, 999.0, 1.0）；若 n_matched >= 3 则有效输出；
     - 输出 BPM 严格限定在闭区间 [60, 300] 内，按 2^x 翻倍/折半（如 43.8 BPM 翻倍为 87.55 BPM）；
     - 维护 meta 元数据字段（如 n_matched, coverage 等）。
     
（重要说明：本方案与enhanced方案均已归档，不再在前端出现。由enhanced v3取精华去糟粕。留在这里诈尸仅仅是因为作者舍不得删。）"""

from __future__ import annotations

import ctypes
import math
import numpy as np

from . import filters as _filters

ENH_V2_CFG = {
    "transition_guard": 2500,
    "fold_min_bpm": 60.0,
    "fold_max_bpm": 300.0,
    "win_len_ms": 15000,
    "hop_len_ms": 7500,
    "max_lag_ms": 8000,
    "max_lag_cands_ms": 4000,
}


def fold_bpm_2x_60_300(bpm: float) -> float:
    """严格仅按 2^x (×2 / ÷2) 将正 BPM 折叠至 [60.0, 300.0] 闭区间。"""
    val = float(bpm)
    if not math.isfinite(val) or val <= 0.0:
        return 0.0
    lo = float(ENH_V2_CFG["fold_min_bpm"])
    hi = float(ENH_V2_CFG["fold_max_bpm"])
    while val < lo:
        val *= 2.0
    while val > hi:
        val *= 0.5
    return val


def compute_pooled_acf(
    diff_1000hz: np.ndarray,
    win_len: int = 15000,
    hop_len: int = 7500,
    max_lag: int = 8000,
) -> np.ndarray:
    """
    分窗池化自相关：
    固定 15 秒窗长（15,000 采样点）与 50% 步进重叠（7.5 秒步长），
    全窗无差别等权累加，每个窗口独立计算 FFT 自相关，最大滞后截断至 8000ms。
    """
    c_dsp = _filters.require_c_dsp(_filters._c_dsp)
    n = len(diff_1000hz)
    if n <= win_len:
        cur_len = n
        cur_max_lag = min(max_lag, cur_len)
        if cur_max_lag <= 0:
            return np.zeros(max_lag, dtype=np.float32)
        w = np.ascontiguousarray(diff_1000hz, dtype=np.float32)
        win_buf = np.zeros(cur_max_lag, dtype=np.float32)
        c_dsp.autocorr_fft_exact_c(
            w.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
            cur_len,
            cur_max_lag,
            win_buf.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
        )
        acf_sum = np.zeros(max_lag, dtype=np.float32)
        acf_sum[:cur_max_lag] = win_buf
        return acf_sum

    use_diff = np.ascontiguousarray(diff_1000hz, dtype=np.float32)
    starts = list(range(0, n - win_len + 1, hop_len))
    if not starts:
        starts = [0]

    acf_sum = np.zeros(max_lag, dtype=np.float32)
    win_buf = np.empty(max_lag, dtype=np.float32)
    for st in starts:
        w = np.ascontiguousarray(use_diff[st : st + win_len], dtype=np.float32)
        c_dsp.autocorr_fft_exact_c(
            w.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
            win_len,
            max_lag,
            win_buf.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
        )
        acf_sum += win_buf

    return np.ascontiguousarray(acf_sum / float(len(starts)), dtype=np.float32)


def track_full_song_with_tol(
    acf: np.ndarray,
    max_lag: int,
    start_tau: float,
    tol: int = 8,
    win: int | None = None,
) -> tuple[float, float, float, float, int]:
    """
    自相关谐波回归追踪器（支持动态 tol 与 win）：
    严格采用与 c_dsp.c:track_full_song_exact_c 相同的递推统计量与抛物线插值，
    支持将容差 tol 设置为 5~25ms，搜索半宽 win 默认为 tol + 2。
    返回 (raw_bpm, raw_instab, sigma_t, rel_inst, n_matched)。
    """
    if win is None:
        win = tol + 2
    cur_tau = float(start_tau)
    target = int(round(cur_tau))
    S_kk = 0.0
    S_kt = 0.0
    S_tt = 0.0
    n_matched = 0
    k = 1.0

    while target + win < max_lag:
        base = target - win
        if base < 0:
            base = 0
        local_max_idx = base
        max_val = float(acf[base])
        for idx in range(base + 1, target + win + 1):
            val = float(acf[idx])
            if val > max_val:
                max_val = val
                local_max_idx = idx

        if max_val > 0.0 and abs(local_max_idx - target) <= tol:
            c = local_max_idx
            n_matched += 1
            ym1 = float(acf[c - 1]) if c > 0 else 0.0
            y0_d = float(acf[c])
            yp1 = float(acf[c + 1]) if c + 1 < max_lag else 0.0
            denom = (y0_d - ym1) + (y0_d - yp1)
            delta = 0.5 * (yp1 - ym1) / denom if denom > 1e-9 else 0.0
            exact_t = float(c) + delta
            if not math.isfinite(exact_t):
                return 0.0, 0.0, 999.0, 1.0, 0
            S_kk += k * k
            S_kt += k * exact_t
            S_tt += exact_t * exact_t
            cur_tau = S_kt / S_kk
            target = int(round(exact_t + cur_tau))
        else:
            target = int(round(float(target) + cur_tau))
            break
        k += 1.0

    if n_matched < 3:
        return 0.0, 0.0, 999.0, 1.0, n_matched

    raw_bpm = 60000.0 * S_kk / S_kt
    term = (((S_kk * S_tt) / S_kt) / S_kt - 1.0) / (float(n_matched) - 1.0) * float(n_matched)
    raw_instab = math.sqrt(max(0.0, term)) * raw_bpm
    var_t = (S_tt - (S_kt * S_kt) / S_kk) / (float(n_matched) - 1.0)
    sigma_t = math.sqrt(max(0.0, var_t))
    rel_inst = raw_instab / raw_bpm if raw_bpm > 0.0 else 0.0

    return raw_bpm, raw_instab, sigma_t, rel_inst, n_matched


def select_best_enhanced_v2(
    acf: np.ndarray | None = None,
    candidates: list[tuple[float, float]] | None = None,
    best_tau: float = 0.0,
    best_cand_idx: int = 0,
    octave_norm: bool = False,
    meta: dict | None = None,
    rescue_candidates: list[tuple[float, float]] | None = None,
    peaks: list[int] | None = None,
    diff_1000hz: np.ndarray | None = None,
    pcm_duration_ms: float | None = None,
    transition_guard: float | int | None = None,
    transition_miss_beats: int = 4,
    transition_resume_beats: int = 3,
    track_tol: int = 8,
    comb_tol: int | None = None,
) -> tuple[float, float, float, float]:
    """
    Enhanced V2 核心决策：分窗池化自相关 (Pooled ACF) + exact 原生 C 库追踪器。
    :param track_tol: 自相关谐波回归容差 (5~25ms 整数，默认 8ms)
    :param comb_tol: 时域脉冲梳节拍匹配窗口半宽 (10~50ms 整数，若为 None 默认 35ms)
    """
    if not isinstance(track_tol, int) or isinstance(track_tol, bool) or not (5 <= track_tol <= 25):
        raise ValueError(f"track_tol 必须为 5 到 25 之间的整数，当前传入: {track_tol!r}")

    if comb_tol is not None:
        if not isinstance(comb_tol, int) or isinstance(comb_tol, bool) or not (10 <= comb_tol <= 50):
            raise ValueError(f"comb_tol 必须为 10 到 50 之间的整数，当前传入: {comb_tol!r}")

    if transition_guard is not None:
        if isinstance(transition_guard, bool):
            raise ValueError(f"transition_guard 不能为布尔类型，当前传入: {transition_guard!r}")
        elif isinstance(transition_guard, float):
            if not (0.0 <= transition_guard <= 3.0001):
                raise ValueError(f"transition_guard (秒) 必须在 0.0 到 3.0 之间，当前传入: {transition_guard!r}")
        elif isinstance(transition_guard, int):
            if not (0 <= transition_guard <= 3000):
                raise ValueError(f"transition_guard (毫秒) 必须在 0 到 3000 之间，当前传入: {transition_guard!r}")
        else:
            raise ValueError(f"transition_guard 必须为 None、float (0.0~3.0s) 或 int (0~3000ms)，当前传入: {transition_guard!r}")

    if not isinstance(transition_miss_beats, int) or isinstance(transition_miss_beats, bool) or not (2 <= transition_miss_beats <= 10):
        raise ValueError(f"transition_miss_beats 必须为 2 到 10 之间的整数，当前传入: {transition_miss_beats!r}")
    if not isinstance(transition_resume_beats, int) or isinstance(transition_resume_beats, bool) or not (2 <= transition_resume_beats <= 10):
        raise ValueError(f"transition_resume_beats 必须为 2 到 10 之间的整数，当前传入: {transition_resume_beats!r}")

    c_dsp = _filters.require_c_dsp(_filters._c_dsp)

    if meta is not None:
        meta.setdefault("suspected_triple_meter", False)
        meta["track_tol"] = track_tol
        if comb_tol is not None:
            meta["comb_tol"] = comb_tol
        if transition_guard is not None:
            meta["transition_guard"] = transition_guard

    # 1. 计算或获取池化自相关序列 (8000ms)
    win_len = int(ENH_V2_CFG["win_len_ms"])
    hop_len = int(ENH_V2_CFG["hop_len_ms"])
    max_lag = int(ENH_V2_CFG["max_lag_ms"])

    if diff_1000hz is not None and len(diff_1000hz) > 0:
        acf_pooled = compute_pooled_acf(diff_1000hz, win_len=win_len, hop_len=hop_len, max_lag=max_lag)
        # 若外部未显式传入全曲自相关 acf 但提供了有效 diff_1000hz，自动补全全曲自相关
        # 确保独立 API 调用时第二阶全曲长程回归与 detect_bpm 100% 对齐触发
        if (acf is None or len(acf) <= max_lag) and len(diff_1000hz) > max_lag:
            from .autocorr import compute_autocorrelation_fft
            acf = compute_autocorrelation_fft(diff_1000hz, exact=False)
    elif acf is not None and len(acf) > 0:
        if len(acf) >= max_lag:
            acf_pooled = np.ascontiguousarray(acf[:max_lag], dtype=np.float32)
        else:
            acf_pooled = np.pad(np.ascontiguousarray(acf, dtype=np.float32), (0, max_lag - len(acf)))
    else:
        if meta is not None:
            meta.update({"stage": "fail", "bpm": 0.0, "n_matched": 0, "coverage": 0.0})
        return 0.0, 0.0, 999.0, 1.0

    # 2. 阶段 1: 查找前 4000ms 局部极大值峰并生成候选
    max_lag_cands = min(len(acf_pooled), int(ENH_V2_CFG["max_lag_cands_ms"]))
    if max_lag_cands <= 32:
        if meta is not None:
            meta.update({"stage": "fail", "bpm": 0.0, "n_matched": 0, "coverage": 0.0})
        return 0.0, 0.0, 999.0, 1.0

    peaks_buf = (ctypes.c_int * max_lag_cands)()
    n_peaks = int(c_dsp.find_acf_local_peaks_c(
        acf_pooled.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
        max_lag_cands,
        peaks_buf,
    ))
    peaks_list = [int(peaks_buf[i]) for i in range(n_peaks)]
    if not peaks_list:
        if meta is not None:
            meta.update({"stage": "fail", "bpm": 0.0, "n_matched": 0, "coverage": 0.0})
        return 0.0, 0.0, 999.0, 1.0

    cands: list[tuple[float, float]] = []
    b_cand_idx = 0
    b_score = np.float32(0.0)

    for rbx, p_lag in enumerate(peaks_list):
        if p_lag >= max_lag_cands:
            break
        if len(cands) > 0 and acf_pooled[p_lag] <= 0.0:
            continue

        tau = float(p_lag)
        target = int(round(tau))
        win_cand = track_tol + 2
        if target >= max_lag_cands - win_cand:
            continue

        sum_h = np.float32(0.0)
        n_matched = 0
        S_kt = 0.0
        S_kk = 0.0
        k = 1.0
        p_idx = rbx

        while True:
            while p_idx < len(peaks_list) and peaks_list[p_idx] < target - win_cand:
                p_idx += 1
            if p_idx < len(peaks_list) and peaks_list[p_idx] <= target + win_cand:
                c = peaks_list[p_idx]
                n_matched += 1
                y0 = acf_pooled[c]
                sum_h = np.float32(sum_h + y0)
                ym1 = float(acf_pooled[c - 1])
                y0_d = float(y0)
                yp1 = float(acf_pooled[c + 1])
                denom = (y0_d - ym1) + (y0_d - yp1)
                with np.errstate(divide="ignore", invalid="ignore"):
                    delta = float(np.float64(0.5 * (yp1 - ym1)) / np.float64(denom)) if denom != 0 else 0.0
                exact_t = float(c) + delta
                S_kk += k * k
                S_kt += k * exact_t
                tau = S_kt / S_kk
                matched = True
                next_t = exact_t + tau
            else:
                matched = False
                next_t = float(target) + tau

            if math.isfinite(next_t):
                target = int(round(next_t))
            else:
                target = -(1 << 63)
            if target >= max_lag_cands - win_cand:
                break
            k += 1.0
            if not matched:
                break

        if n_matched == 0 or target <= max_lag_cands // 2:
            continue

        mean_h = np.float32(sum_h / np.float32(n_matched))
        if mean_h <= b_score:
            continue
        if mean_h <= np.float32(acf_pooled[p_lag] * np.float32(0.699999988079071)):
            continue

        cands.append((S_kt / S_kk, float(mean_h)))
        if mean_h > np.float32(b_score * np.float32(1.25)):
            b_cand_idx = len(cands) - 1
        b_score = mean_h

    if not cands or b_score <= 0.0:
        if meta is not None:
            meta.update({
                "stage": "fail",
                "bpm": 0.0,
                "n_matched": 0,
                "coverage": 0.0,
                "peaks": [{"lag": int(p), "val": float(acf_pooled[p])} for p in peaks_list],
                "candidates": [],
            })
        return 0.0, 0.0, 999.0, 1.0

    # 3. 阶段 2: 候选评选与 220 BPM 亚谐波消歧
    sel_tau = cands[b_cand_idx][0]
    TAU_MIN = 272.72727272727275  # 220.0 BPM
    TAU_MAX = 545.4545454545455   # 110.0 BPM

    if sel_tau < TAU_MIN:
        while b_cand_idx < len(cands) - 1:
            if cands[b_cand_idx + 1][0] >= TAU_MAX:
                break
            found = False
            for j in range(b_cand_idx + 1, len(cands)):
                cand_tau = cands[j][0]
                if cand_tau >= TAU_MAX:
                    break
                if abs(math.remainder(cand_tau, sel_tau)) <= 10.0:
                    b_cand_idx = j
                    sel_tau = cand_tau
                    found = True
                    break
            if not found or sel_tau >= TAU_MIN:
                break

    # 4. 阶段 3: 双阶递进回归追踪
    def _run_track(target_acf, target_len, target_tau):
        if track_tol == 8:
            target_acf_c = np.ascontiguousarray(target_acf[:target_len], dtype=np.float32)
            stats = (ctypes.c_double * 4)()
            n = int(c_dsp.track_full_song_exact_c(
                target_acf_c.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                target_len,
                target_tau,
                stats,
            ))
            if n >= 4:
                return float(stats[0]), float(stats[1]), float(stats[2]), float(stats[3]), n
            elif n == 3:
                # 兼容 C 动态库历史硬编码 n < 4 返回 0.0，调用 Python 镜像计算 3 拍高精度 OLS 统计量
                return track_full_song_with_tol(target_acf, target_len, target_tau, tol=8)
            else:
                return float(stats[0]), float(stats[1]), float(stats[2]), float(stats[3]), n
        else:
            return track_full_song_with_tol(target_acf, target_len, target_tau, tol=track_tol)

    # 4.1 第一阶: 调用追踪器在 acf_pooled (8000ms) 上回归 (确立门控与稳态基础)
    raw_bpm, raw_instab, sigma_t, rel_inst, n_m = _run_track(acf_pooled, len(acf_pooled), sel_tau)

    # 超低速 (BPM < 60 / tau > 1000ms) 候选回溯兜底：
    # 若被选中的候选周期由于 8000ms 截断物理容量不足导致 n_m < 3 熔断，
    # 尝试在已有候选池与原始极大值峰中回溯其落入 [60, 300] BPM (200ms ~ 1000ms) 内的真实基频/谐波候选（匹配拍数 >= 3）
    if n_m < 3 and sel_tau > 1000.0:
        for factor in (8, 4, 2):
            sub_target = sel_tau / float(factor)
            if 200.0 <= sub_target <= 1000.0:
                best_sub_idx = None
                min_diff = 10.0
                for idx in range(b_cand_idx):
                    cand_tau = cands[idx][0]
                    diff = abs(cand_tau - sub_target)
                    if diff < min_diff:
                        min_diff = diff
                        best_sub_idx = idx

                cand_tau_to_try = cands[best_sub_idx][0] if best_sub_idx is not None else None
                # 二级兜底：若 cands 初筛池中未匹配到候选（例如被阶段 1 单调打分过滤），
                # 则在原始极大值峰 peaks_list 中查找最接近 sub_target 的物理峰
                if cand_tau_to_try is None:
                    p_min_diff = 10.0
                    for p_lag in peaks_list:
                        diff = abs(float(p_lag) - sub_target)
                        if diff < p_min_diff:
                            p_min_diff = diff
                            cand_tau_to_try = float(p_lag)

                if cand_tau_to_try is not None:
                    try_bpm, try_inst, try_sig, try_rel, try_n = _run_track(
                        acf_pooled, len(acf_pooled), cand_tau_to_try
                    )
                    if try_n >= 3 and try_bpm > 0.0:
                        if best_sub_idx is not None:
                            b_cand_idx = best_sub_idx
                        sel_tau = cand_tau_to_try
                        raw_bpm = try_bpm
                        raw_instab = try_inst
                        sigma_t = try_sig
                        rel_inst = try_rel
                        n_m = try_n
                        break

    if math.isnan(raw_instab) or raw_instab < 0.0:
        raw_instab = 0.0
    if math.isnan(sigma_t) or sigma_t < 0.0:
        sigma_t = 0.0
    if math.isnan(rel_inst) or rel_inst < 0.0:
        rel_inst = 0.0

    # 5. 门控与区间处理：全曲统一要求 n_matched >= 3 作为有效性门槛（若 n_matched < 3 则无法锁定返回 0.0）
    if n_m < 3 or raw_bpm <= 0.0:
        if meta is not None:
            meta.update({
                "stage": "fail",
                "best_tau": sel_tau,
                "bpm": 0.0,
                "raw_bpm": 0.0,
                "instab": 1.0,
                "steady_instab": 0.0,
                "fold_scale": 1.0,
                "sigma_t": 999.0,
                "rel_inst": 1.0,
                "n_matched": n_m,
                "coverage": 0.0,
                "len_acf": len(acf_pooled),
                "acf_pooled": acf_pooled,
                "peaks": [{"lag": int(p), "val": float(acf_pooled[p])} for p in peaks_list],
                "candidates": [
                    {"tau": float(t), "bpm": round(60000.0 / float(t), 3), "score": float(s)}
                    for t, s in cands
                ],
                "suspected_triple_meter": False,
            })
        return 0.0, 0.0, 999.0, 1.0

    # 4.2 第二阶: 全曲长程自相关高精度回归追踪 (扩充匹配拍数至全曲数百拍，消除微漂移与指标虚高)
    if acf is not None and len(acf) > len(acf_pooled):
        raw_bpm_f, raw_instab_f, sigma_t_f, rel_inst_f, n_full = _run_track(acf, len(acf), sel_tau)
        # 仅当全曲追踪成功且匹配拍数不少于池化切片时采纳
        if n_full >= n_m and raw_bpm_f > 0.0:
            n_m = n_full
            raw_bpm = raw_bpm_f
            raw_instab = raw_instab_f
            sigma_t = sigma_t_f
            rel_inst = rel_inst_f
            if math.isnan(raw_instab) or raw_instab < 0.0:
                raw_instab = 0.0
            if math.isnan(sigma_t) or sigma_t < 0.0:
                sigma_t = 0.0
            if math.isnan(rel_inst) or rel_inst < 0.0:
                rel_inst = 0.0

    final_bpm = fold_bpm_2x_60_300(raw_bpm)
    fold_scale = final_bpm / raw_bpm if raw_bpm > 0.0 else 1.0
    final_instab = raw_instab * fold_scale
    steady_instab = final_bpm * rel_inst
    tracked_tau = 60000.0 / raw_bpm

    # 4.3 第三阶: 100% 全曲时间轴时域脉冲梳投影 (贯穿歌曲第 0 秒至末尾毫秒，突破 ACF N/2 与 8s 窗口物理限制)
    comb_n_matched = n_m
    comb_total_beats = 0
    comb_cov = float(n_m) * tracked_tau / float(len(acf_pooled)) if len(acf_pooled) > 0 else 0.0
    comb_rmse = 0.0
    comb_phi = 0.0
    comb_meta = {}

    if diff_1000hz is not None and len(diff_1000hz) > 0 and final_bpm > 0.0:
        from .force_v3 import compute_best_phase_and_rmse
        target_tau = 60000.0 / final_bpm
        win_r = comb_tol if comb_tol is not None else 35
        comb_phi, comb_rmse, _ = compute_best_phase_and_rmse(
            diff_1000hz,
            target_tau,
            win_radius=win_r,
            meta=comb_meta,
            transition_guard=transition_guard,
            transition_miss_beats=transition_miss_beats,
            transition_resume_beats=transition_resume_beats,
            comb_tol=comb_tol,
        )
        if comb_meta.get("total_beats", 0) > 0:
            comb_n_matched = int(comb_meta["n_matched"])
            comb_total_beats = int(comb_meta["total_beats"])
            comb_cov = float(comb_meta["coverage"])

    beat_display = (
        f"{comb_n_matched} / {comb_total_beats} 拍 ({comb_cov * 100.0:.1f}%)"
        if comb_total_beats > 0
        else f"{comb_n_matched} 拍 ({comb_cov * 100.0:.1f}%)"
    )

    if meta is not None:
        meta.update({
            "stage": "v2_pooled",
            "best_tau": sel_tau,
            "best_phi": comb_phi,
            "bpm": final_bpm,
            "raw_bpm": raw_bpm,
            "instab": final_instab,
            "steady_instab": steady_instab,
            "fold_scale": fold_scale,
            "sigma_t": sigma_t,
            "rel_inst": rel_inst,
            "n_matched": comb_n_matched,
            "total_beats": comb_total_beats,
            "coverage": min(1.0, max(0.0, comb_cov)),
            "rmse_ms": comb_rmse,
            "beat_display": beat_display,
            "len_acf": len(acf_pooled),
            "acf_pooled": acf_pooled,
            "peaks": [{"lag": int(p), "val": float(acf_pooled[p])} for p in peaks_list],
            "candidates": [
                {"tau": float(t), "bpm": round(60000.0 / float(t), 3), "score": float(s)}
                for t, s in cands
            ],
            "suspected_triple_meter": False,
        })
        if comb_meta:
            for k in (
                "transition_guard_ms",
                "transition_miss_beats",
                "transition_resume_beats",
                "transition_spans",
                "raw_n_matched",
                "raw_total_beats",
                "raw_coverage",
            ):
                if k in comb_meta:
                    meta[k] = comb_meta[k]

    return final_bpm, final_instab, sigma_t, rel_inst
