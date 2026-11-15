"""
malody_bpm.force_v3
~~~~~~~~~~~~~~~~~~~

Force Fit (V3 / 全局梳状网格强行拟合) 独立内核模块。
严格遵循 2.txt 设计规范：
1. 全曲时间轴贯穿分析，无视局部丢拍或瞬时漂移，强制拟合全局宏观平均 BPM；
2. 采用全曲时间轴等距梳状网格能量投影（Global Comb Projection），彻底避免离散逐拍提取中的相位错位；
3. 纯客观能量驱动，不设主观速度先验，由全局投影内积能量绝对值最大决定最佳周期；
4. 严格 2^x 规则折叠至 [59.99, 300.01] 闭区间（为 [60.00, 300.00] 保留 ±0.01 BPM 余量）；
5. 依据理论节拍网格与全曲起音点之间的均方根残差 (RMSE) 归一化输出真实不稳定度。
"""

from __future__ import annotations

import math
import numpy as np

from .autocorr import compute_autocorrelation_fft

# V3 双模式统一八度折叠闭区间 [59.99, 300.01] BPM 及周期搜索边界
# - enhanced_v3 内部前置阈值使用 199.99~1000.2 ms（略宽于 60000/300.01 ~ 60000/59.99）
# - force_v3 梳状投影搜索端点严格对齐 60000.0/300.01 ~ 60000.0/59.99 ms，防止粗步长吸附超界触发翻倍/折半
V3_BPM_MIN = 59.99
V3_BPM_MAX = 300.01
V3_TAU_MIN_MS = 199.99
V3_TAU_MAX_MS = 1000.2
V3_FORCE_TAU_MIN_MS = 60000.0 / V3_BPM_MAX
V3_FORCE_TAU_MAX_MS = 60000.0 / V3_BPM_MIN


def fold_bpm_2x_range(
    val: float,
    min_bpm: float = V3_BPM_MIN,
    max_bpm: float = V3_BPM_MAX,
) -> tuple[float, float]:
    """严格按照 2^x 规则将原始 BPM 折叠到 [min_bpm, max_bpm] 闭区间内，返回 (折叠后BPM, 倍率)。"""
    if val <= 0.0 or not math.isfinite(val):
        return 0.0, 1.0

    orig = float(val)
    cur = orig
    lo = float(min_bpm)
    hi = float(max_bpm)
    while cur < lo:
        cur *= 2.0
    while cur > hi:
        cur *= 0.5

    scale = cur / orig
    return float(cur), float(scale)


def fold_bpm_2x_60_300(val: float) -> tuple[float, float]:
    """严格按照 2^x 规则将原始 BPM 折叠到 [60.0, 300.0] 闭区间内（保留兼容入口）。"""
    return fold_bpm_2x_range(val, 60.0, 300.0)


def fold_bpm_2x_v3_with_scale(val: float) -> tuple[float, float]:
    """V3 专用 2^x 八度折叠（[59.99, 300.01] BPM 闭区间），返回 (折叠后BPM, 倍率)。"""
    return fold_bpm_2x_range(val, V3_BPM_MIN, V3_BPM_MAX)


def fold_bpm_2x_v3(val: float) -> float:
    """V3 专用 2^x 八度折叠（[59.99, 300.01] BPM 闭区间），仅返回折叠后 BPM。"""
    return fold_bpm_2x_range(val, V3_BPM_MIN, V3_BPM_MAX)[0]


def align_bpm_2x_to_reference(
    cand_bpm: float,
    ref_bpm: float,
    rel_tol: float = 0.035,
    min_bpm: float = V3_BPM_MIN,
    max_bpm: float = V3_BPM_MAX,
) -> float:
    """
    若候选 BPM 与参考 BPM（如第一轮全曲 BPM）在 2^x 倍频/分频意义下同源（相对偏差 <= rel_tol），
    且折算回参考八度后的值仍位于 [min_bpm, max_bpm] 闭区间内（默认 V3 区间 [59.99, 300.01]），
    则将其无损映射回参考八度；否则保持候选值原八度不变。
    """
    if (
        cand_bpm <= 0.0
        or ref_bpm <= 0.0
        or not math.isfinite(cand_bpm)
        or not math.isfinite(ref_bpm)
    ):
        return float(cand_bpm) if (math.isfinite(cand_bpm) and cand_bpm > 0.0) else 0.0

    best_val = float(cand_bpm)
    best_rel = float("inf")
    lo = float(min_bpm)
    hi = float(max_bpm)
    for exp_k in (-3, -2, -1, 0, 1, 2, 3):
        scaled = float(cand_bpm) * (2.0 ** exp_k)
        if lo <= scaled <= hi:
            rel_diff = abs(scaled - float(ref_bpm)) / float(ref_bpm)
            if rel_diff <= rel_tol and rel_diff < best_rel:
                best_rel = rel_diff
                best_val = scaled
    return best_val


def compute_comb_projection_spectrum(
    diff_1000hz: np.ndarray,
    acf: np.ndarray | None = None,
    tau_min: float = V3_FORCE_TAU_MIN_MS,
    tau_max: float = V3_FORCE_TAU_MAX_MS,
    tau_step: float = 0.5,
    end_fade_ms: float = 250.0,
    _prevalidated_acf: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """
    计算全曲时间轴梳状网格能量投影谱。
    结合全局自相关 (ACF) 谐波链加权与差分包络全局时间积分，
    输出 (candidate_taus, energy_scores)。
    [优化 2026-11-15] 按候选周期分块执行二维向量化线性插值，并对滞后上限前
    end_fade_ms (默认 250ms) 窗口内的末端谐波施加线性渐隐加权，消除阶数 k 跳变台阶。
    """
    if _prevalidated_acf is not None:
        acf_used = _prevalidated_acf
    else:
        N = len(diff_1000hz) if diff_1000hz is not None else 0
        if N <= 0:
            return np.array([]), np.array([])

        diff_max = float(np.max(diff_1000hz)) if N > 0 else 0.0
        diff_min = float(np.min(diff_1000hz)) if N > 0 else 0.0
        if (
            not math.isfinite(diff_max)
            or not math.isfinite(diff_min)
            or diff_max <= 1e-6
            or (diff_max - diff_min) <= 1e-6
        ):
            return np.array([]), np.array([])

        # 如果未提供或 acf 过短，从 diff_1000hz 提取，复用 C/FFT 统一实现
        if acf is None or len(acf) < 1000:
            acf_used = compute_autocorrelation_fft(diff_1000hz, exact=False)
        else:
            acf_used = np.ascontiguousarray(acf, dtype=np.float32)

        if len(acf_used) < 600 or not np.all(np.isfinite(acf_used)):
            return np.array([]), np.array([])

    max_acf_lag = min(len(acf_used) - 1, 30000)
    if max_acf_lag <= 10:
        return np.array([]), np.array([])
    # 使用对齐右端点的均匀网格，确保 tau_max (如 1000.2ms) 完整覆盖且步长一致以保持亚像素抛物线插值精度
    if tau_max <= tau_min or tau_step <= 0.0:
        taus = np.array([float(tau_min)], dtype=np.float64)
    else:
        n_steps = max(1, int(math.ceil((float(tau_max) - float(tau_min) - 1e-9) / float(tau_step))))
        taus = np.linspace(float(tau_min), float(tau_max), n_steps + 1, dtype=np.float64)
    scores = np.zeros(len(taus), dtype=np.float64)

    stop = float(max_acf_lag - 10)
    pos_all = taus[taus > 0.0]
    if len(pos_all) == 0:
        return taus, scores

    # 动态限制单批行数，保证即使传入极小 tau_min 时临时二维矩阵仍处于数 MB 级
    max_k_possible = max(1, int(math.ceil(stop / float(np.min(pos_all)))))
    chunk_size = max(1, min(512, 262144 // max_k_possible))
    use_fade = math.isfinite(float(end_fade_ms)) and float(end_fade_ms) > 0.0
    fade_w = float(end_fade_ms) if use_fade else 1.0

    for s_idx in range(0, len(taus), chunk_size):
        tc = taus[s_idx : s_idx + chunk_size]
        pos_taus = tc[tc > 0.0]
        if len(pos_taus) == 0:
            continue
        k_max = int(math.ceil((stop - 1e-12) / float(np.min(pos_taus))))
        if k_max < 3:
            continue

        ks = np.arange(1, k_max + 1, dtype=np.float64)[None, :]
        harms = tc[:, None] * ks
        mask = (tc[:, None] > 0.0) & (harms < stop)
        cnt = np.sum(mask, axis=1)
        valid_row = cnt >= 3
        if not np.any(valid_row):
            continue

        safe_h = np.where(mask, harms, 0.0)
        idx_floor = safe_h.astype(np.int64)
        frac = safe_h - idx_floor
        # 线性插值采样（safe_h < stop = max_acf_lag - 10 确保 idx_floor + 1 安全不越界）
        a0 = acf_used[idx_floor]
        sampled = a0 + frac * (acf_used[idx_floor + 1] - a0)

        row_scores = np.zeros(len(tc), dtype=np.float64)
        if use_fade:
            # 末端固定滞后窗 [stop - end_fade_ms, stop) 线性渐隐加权，保证谐波出入边界时分数连续可导
            weights = np.minimum(1.0, (stop - safe_h) / fade_w)
            weights *= mask
            w_sum = np.sum(weights, axis=1)
            sampled *= weights
            num = np.sum(sampled, axis=1)
            ok = valid_row & (w_sum > 1e-9)
            np.divide(num, w_sum, out=row_scores, where=ok)
        else:
            sampled *= mask
            num = np.sum(sampled, axis=1)
            np.divide(num, cnt, out=row_scores, where=valid_row)
        scores[s_idx : s_idx + chunk_size] = row_scores

    return taus, scores


def refine_subpixel_tau(taus: np.ndarray, scores: np.ndarray, best_idx: int) -> float:
    """在峰值点周围使用二次抛物线插值求取亚微秒级精确周期 tau。"""
    if best_idx <= 0 or best_idx >= len(scores) - 1 or len(taus) < 2:
        return float(taus[best_idx]) if 0 <= best_idx < len(taus) else 0.0

    y0 = float(scores[best_idx])
    ym1 = float(scores[best_idx - 1])
    yp1 = float(scores[best_idx + 1])

    denom = (y0 - ym1) + (y0 - yp1)
    if denom <= 0.0 or not math.isfinite(denom):
        return float(taus[best_idx])

    delta = 0.5 * (yp1 - ym1) / denom
    if abs(delta) > 1.0 or not math.isfinite(delta):
        delta = 0.0

    step = float(taus[1] - taus[0])
    return float(taus[best_idx] + delta * step)


def _select_refined_comb_tau(
    diff_1000hz: np.ndarray,
    acf: np.ndarray | None = None,
    coarse_step: float = 0.05,
    fine_step: float = 0.01,
    odd_tie_ratio: float = 0.97,
) -> tuple[float, float]:
    """
    两阶段粗精梳状投影选峰 + 非 2^x 奇数分频（m=5, 3）纯客观子集平局消歧。
    - 阶段 1：以 coarse_step (默认 0.05ms) 全范围粗扫，消除 200~300 BPM 高速段在 30s 滞后窗下的栅栏漏峰；
    - 阶段 2：在粗峰 ±2% 邻域以 fine_step (默认 0.01ms) 局部加密精扫；
      合法边界外侧各留 1 个严格等步长的守卫采样点供 60.00 / 300.00 BPM 边界峰做 3 点抛物线插值，
      插值结果裁剪回 [V3_FORCE_TAU_MIN_MS, V3_FORCE_TAU_MAX_MS] 防贴边过冲触发误折半/误翻倍；
    - 阶段 3：仅针对无法通过 2^x 折叠修正的奇数分频 m in (5, 3)，当 tau/m * 1.015 >= V3_FORCE_TAU_MIN_MS
      且局部精扫得分 >= odd_tie_ratio * S(tau) 并满足谐波同源一致性 (<=1.5%) 时，判定为等幅节拍的子集均值平局并归还基频。
    返回 (subpixel_tau, best_score)；无有效正能量峰时返回 (0.0, 0.0)。
    """
    # 入口一次性校验包络与 ACF，避免多轮 _bounded_scan 重复全曲扫描
    N = len(diff_1000hz) if diff_1000hz is not None else 0
    if N <= 0:
        return 0.0, 0.0
    diff_max = float(np.max(diff_1000hz))
    diff_min = float(np.min(diff_1000hz))
    if (
        not math.isfinite(diff_max)
        or not math.isfinite(diff_min)
        or diff_max <= 1e-6
        or (diff_max - diff_min) <= 1e-6
    ):
        return 0.0, 0.0

    if acf is None or len(acf) < 1000:
        acf_local = compute_autocorrelation_fft(diff_1000hz, exact=False)
    else:
        acf_local = np.ascontiguousarray(acf, dtype=np.float32)

    if len(acf_local) < 600 or not np.all(np.isfinite(acf_local)):
        return 0.0, 0.0

    def _scan_spectrum(lo_in: float, hi_in: float, step: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        ilo = max(V3_FORCE_TAU_MIN_MS, float(lo_in))
        ihi = min(V3_FORCE_TAU_MAX_MS, float(hi_in))
        if ihi < ilo:
            ilo = ihi
        n_in = max(1, int(math.ceil((ihi - ilo) / step)))
        # 在合法区间外侧各扩 1 个严格等步长守卫点，确保边界端点具备左右邻点插值且步长不被 linspace 拉伸
        if ihi >= V3_FORCE_TAU_MAX_MS - 1e-9 and ilo > V3_FORCE_TAU_MIN_MS + 1e-9:
            pad_hi = V3_FORCE_TAU_MAX_MS + step
            pad_lo = pad_hi - float(n_in + 2) * step
        else:
            pad_lo = ilo - step
            pad_hi = pad_lo + float(n_in + 2) * step

        taus_s, scores_s = compute_comb_projection_spectrum(
            diff_1000hz,
            acf=acf_local,
            tau_min=pad_lo,
            tau_max=pad_hi,
            tau_step=step,
            _prevalidated_acf=acf_local,
        )
        if (
            len(scores_s) == 0
            or not math.isfinite(float(np.max(scores_s)))
            or float(np.max(scores_s)) <= 0.0
        ):
            return np.array([]), np.array([]), np.array([], dtype=np.int64)

        valid_idxs = np.flatnonzero(
            (taus_s >= V3_FORCE_TAU_MIN_MS - 1e-6) & (taus_s <= V3_FORCE_TAU_MAX_MS + 1e-6)
        )
        if len(valid_idxs) == 0:
            return np.array([]), np.array([]), np.array([], dtype=np.int64)
        return taus_s, scores_s, valid_idxs

    def _refine_grid_peak(
        taus_s: np.ndarray,
        scores_s: np.ndarray,
        valid_idxs: np.ndarray,
        step: float,
    ) -> tuple[float, float]:
        if len(valid_idxs) == 0:
            return 0.0, 0.0
        best_i = int(valid_idxs[int(np.argmax(scores_s[valid_idxs]))])
        best_s = float(scores_s[best_i])
        if best_s <= 0.0 or not math.isfinite(best_s):
            return 0.0, 0.0

        # 围绕网格最优点做 23 点 0.1*step 局部微扫（300 BPM 下 149*0.001ms=0.15ms << 1ms 采样间隔），
        # 消除极窄脉冲在 0.01ms 网格偏移下的分值跌落与折线拐点插值偏差
        m_step = step * 0.1
        t_center = float(taus_s[best_i])
        mts, msc = compute_comb_projection_spectrum(
            diff_1000hz,
            acf=acf_local,
            tau_min=t_center - step - m_step,
            tau_max=t_center + step + m_step,
            tau_step=m_step,
            _prevalidated_acf=acf_local,
        )
        m_valid = np.flatnonzero(
            (mts >= V3_FORCE_TAU_MIN_MS - 1e-6) & (mts <= V3_FORCE_TAU_MAX_MS + 1e-6)
        )
        if len(m_valid) > 0:
            m_bi = int(m_valid[int(np.argmax(msc[m_valid]))])
            m_bs = float(msc[m_bi])
            if math.isfinite(m_bs) and m_bs > 0.0:
                best_s = max(best_s, m_bs)
                ref_t = refine_subpixel_tau(mts, msc, m_bi)
            else:
                ref_t = refine_subpixel_tau(taus_s, scores_s, best_i)
        else:
            ref_t = refine_subpixel_tau(taus_s, scores_s, best_i)
        ref_t = min(V3_FORCE_TAU_MAX_MS, max(V3_FORCE_TAU_MIN_MS, ref_t))
        return float(ref_t), best_s

    def _bounded_scan(lo_in: float, hi_in: float, step: float) -> tuple[float, float]:
        taus_s, scores_s, valid_idxs = _scan_spectrum(lo_in, hi_in, step)
        return _refine_grid_peak(taus_s, scores_s, valid_idxs, step)

    coarse_taus, coarse_scores, coarse_valid = _scan_spectrum(
        V3_FORCE_TAU_MIN_MS, V3_FORCE_TAU_MAX_MS, coarse_step
    )
    if len(coarse_valid) == 0:
        return 0.0, 0.0

    # [优化 2026-11-15] 从粗扫谱提取 Top-5 互不重叠（>±2%）且 >= 70% coarse_max 的局部极大值并发精扫，
    # 消除 0.05ms 粗网格量化偏置导致高速主峰被 2x/4x 慢倍频在粗扫阶段反超的误折半问题
    sc_valid = coarse_scores[coarse_valid]
    tau_valid = coarse_taus[coarse_valid]
    coarse_max = float(np.max(sc_valid))
    if coarse_max <= 0.0 or not math.isfinite(coarse_max):
        return 0.0, 0.0

    n_v = len(sc_valid)
    is_peak = np.ones(n_v, dtype=bool)
    if n_v > 1:
        is_peak[1:] &= sc_valid[1:] >= sc_valid[:-1]
        is_peak[:-1] &= sc_valid[:-1] >= sc_valid[1:]
    peak_pos = np.flatnonzero(is_peak & (sc_valid >= 0.70 * coarse_max))
    if len(peak_pos) == 0:
        peak_pos = np.array([int(np.argmax(sc_valid))], dtype=np.int64)
    peak_order = peak_pos[np.argsort(sc_valid[peak_pos])[::-1]]

    cand_centers: list[float] = []
    for p_i in peak_order:
        c_tau = float(tau_valid[int(p_i)])
        if any(abs(c_tau - prev_t) <= 0.02 * max(c_tau, prev_t) for prev_t in cand_centers):
            continue
        cand_centers.append(c_tau)
        if len(cand_centers) >= 5:
            break

    tau_win, score_win = 0.0, 0.0
    for c_tau in cand_centers:
        t_cand, s_cand = _bounded_scan(0.98 * c_tau, 1.02 * c_tau, fine_step)
        if t_cand > 0.0 and s_cand > score_win:
            tau_win, score_win = t_cand, s_cand

    if tau_win <= 0.0 or score_win <= 0.0:
        tau_win, score_win = _refine_grid_peak(coarse_taus, coarse_scores, coarse_valid, coarse_step)
        if tau_win <= 0.0 or score_win <= 0.0:
            return 0.0, 0.0

    # 仅针对无法通过 2^x 折叠修正的奇数倍分谐波 (m=5, 3) 消除纯子集等权均值平局
    # [优化 2026-11-15] 门限对齐 1.5% 同源容差，消除 300.00 BPM 贴边时因 5*tau 微偏导致的盲区
    for div_m in (5, 3):
        cand_sub = tau_win / float(div_m)
        if cand_sub * 1.015 >= V3_FORCE_TAU_MIN_MS:
            tau_sub, score_sub = _bounded_scan(0.98 * cand_sub, 1.02 * cand_sub, fine_step)
            if (
                tau_sub > 0.0
                and abs(tau_sub * float(div_m) - tau_win) <= 0.015 * tau_win
                and score_sub >= odd_tie_ratio * score_win
            ):
                tau_win, score_win = tau_sub, score_sub
                break

    return tau_win, score_win


def compute_segmented_acf(
    diff_1000hz: np.ndarray,
    merged_spans: list[tuple[float, float]] | list[list[float]],
    min_seg_ms: int = 4000,
) -> tuple[np.ndarray | None, int]:
    """
    依据已识别的过渡/错相剔除区间 merged_spans，提取互补的干净稳态时间片，
    对各稳态时间片独立计算 FFT 自相关并无跨缝合成 acf_clean。
    返回 (acf_clean, longest_seg_ms)；若无满足门槛的稳态段则返回 (None, 0)。
    """
    N = len(diff_1000hz) if diff_1000hz is not None else 0
    if N <= 0 or not merged_spans:
        return None, 0

    norm_spans: list[list[float]] = []
    for sp in sorted(merged_spans, key=lambda x: (float(x[0]), float(x[1]))):
        s_val = max(0.0, float(sp[0]))
        e_val = min(float(N), float(sp[1]))
        if s_val >= e_val:
            continue
        if not norm_spans:
            norm_spans.append([s_val, e_val])
        else:
            prev = norm_spans[-1]
            if s_val <= prev[1]:
                prev[1] = max(prev[1], e_val)
            else:
                norm_spans.append([s_val, e_val])

    eff_min_len = max(4, int(min_seg_ms))
    valid_segs: list[np.ndarray] = []
    cursor = 0.0
    for s_val, e_val in norm_spans:
        if s_val > cursor:
            s_idx = max(0, min(N, int(math.ceil(cursor))))
            e_idx = max(0, min(N, int(math.floor(s_val))))
            if e_idx - s_idx >= eff_min_len:
                valid_segs.append(diff_1000hz[s_idx:e_idx])
        cursor = max(cursor, e_val)

    if cursor < float(N):
        s_idx = max(0, min(N, int(math.ceil(cursor))))
        e_idx = N
        if e_idx - s_idx >= eff_min_len:
            valid_segs.append(diff_1000hz[s_idx:e_idx])

    if not valid_segs:
        return None, 0

    longest_seg_ms = max(len(seg) for seg in valid_segs)
    if len(valid_segs) == 1:
        acf_single = compute_autocorrelation_fft(valid_segs[0], exact=False)
        if len(acf_single) <= 1:
            return None, longest_seg_ms
        return acf_single, longest_seg_ms

    seg_acfs = [compute_autocorrelation_fft(seg, exact=False) for seg in valid_segs]
    max_out = max((len(a) for a in seg_acfs), default=0)
    if max_out <= 1:
        return None, longest_seg_ms

    acf_clean = np.zeros(max_out, dtype=np.float32)
    for a in seg_acfs:
        out_i = len(a)
        if out_i <= 1:
            continue
        if out_i < max_out and out_i >= 32:
            taper_len = min(250, max(16, out_i // 10))
            a_mod = a.copy()
            taper = (
                0.5 * (1.0 + np.cos(np.pi * np.linspace(0.0, 1.0, taper_len, dtype=np.float64)))
            ).astype(np.float32)
            a_mod[-taper_len:] *= taper
            acf_clean[:out_i] += a_mod
        else:
            acf_clean[:out_i] += a

    return acf_clean, longest_seg_ms


def compute_best_phase_and_rmse(
    diff_1000hz: np.ndarray,
    tau: float,
    win_radius: int = 35,
    meta: dict | None = None,
    transition_guard: float | int | None = None,
    transition_miss_beats: int = 4,
    transition_resume_beats: int = 3,
    comb_tol: int | None = None,
    allow_phase_relock: bool = False,
    refine_phase_center: bool = False,
) -> tuple[float, float, float]:
    """
    依据最佳周期 tau，在全曲时间轴上寻找最优相位偏移 phi，
    并计算与真实起音峰之间的均方根残差 (RMSE) 及归一化不稳定度。
    若传入 transition_guard (秒或毫秒)，则在扫描到中途变速时执行交界内缩裁剪：
    - 连续未命中 >= transition_miss_beats 拍触发变速判定；
    - 随后连续重新命中 >= transition_resume_beats 拍触发 a tempo 恢复；
    - 将 [T_trans_start - Δt, T_atempo_start + Δt] 内的匹配节拍与理论总拍数扣除。
    :param comb_tol: 时域脉冲梳节拍匹配窗口半宽 (10~50ms 整数，若为 None 沿用 win_radius)
    :param allow_phase_relock: 是否启用回溯式分段局部相位重锁与按段中心化残差 (默认 False 严格保持原行为)
    :param refine_phase_center: 是否启用非负起音分桶、三角平滑核及去偏后窗口重对齐/导出时间戳同步
                               (默认 False 严格保持外部调用方逐位一致)
    返回 (best_phi, rmse_ms, instability)。
    """
    if not isinstance(allow_phase_relock, (bool, np.bool_)):
        raise ValueError(f"allow_phase_relock 必须为布尔类型，当前传入: {allow_phase_relock!r}")
    if not isinstance(refine_phase_center, (bool, np.bool_)):
        raise ValueError(f"refine_phase_center 必须为布尔类型，当前传入: {refine_phase_center!r}")

    if comb_tol is not None:
        if not isinstance(comb_tol, int) or isinstance(comb_tol, bool) or not (10 <= comb_tol <= 50):
            raise ValueError(f"comb_tol 必须为 10 到 50 之间的整数，当前传入: {comb_tol!r}")
        eff_win_r = comb_tol
    else:
        eff_win_r = win_radius

    tg_ms = 0.0
    if transition_guard is not None:
        if isinstance(transition_guard, (bool, np.bool_)):
            raise ValueError(f"transition_guard 不能为布尔类型，当前传入: {transition_guard!r}")
        elif isinstance(transition_guard, float):
            if not math.isfinite(transition_guard) or not (0.0 <= transition_guard <= 3.0001):
                raise ValueError(f"transition_guard (秒) 必须在 0.0 到 3.0 之间，当前传入: {transition_guard!r}")
            tg_ms = float(transition_guard) * 1000.0
        elif isinstance(transition_guard, int):
            if not (0 <= transition_guard <= 3000):
                raise ValueError(f"transition_guard (毫秒) 必须在 0 到 3000 之间，当前传入: {transition_guard!r}")
            tg_ms = float(transition_guard)
        else:
            raise ValueError(f"transition_guard 必须为 None、float (0.0~3.0s) 或 int (0~3000ms)，当前传入: {transition_guard!r}")

    if not isinstance(transition_miss_beats, int) or isinstance(transition_miss_beats, bool) or not (2 <= transition_miss_beats <= 10):
        raise ValueError(f"transition_miss_beats 必须为 2 到 10 之间的整数，当前传入: {transition_miss_beats!r}")
    if not isinstance(transition_resume_beats, int) or isinstance(transition_resume_beats, bool) or not (2 <= transition_resume_beats <= 10):
        raise ValueError(f"transition_resume_beats 必须为 2 到 10 之间的整数，当前传入: {transition_resume_beats!r}")

    if meta is not None:
        meta.pop("_recalc_spans", None)

    N = len(diff_1000hz) if diff_1000hz is not None else 0
    if N <= 0 or not math.isfinite(tau) or tau <= 0.0:
        if meta is not None:
            meta.update({
                "n_matched": 0,
                "total_beats": 0,
                "coverage": 0.0,
                "transition_guard_ms": tg_ms,
                "transition_miss_beats": transition_miss_beats,
                "transition_resume_beats": transition_resume_beats,
                "transition_spans": [],
                "matched_beat_times": [],  # [只读导出] 失败路径无命中拍
            })
        return 0.0, 999.0, 1.0

    # 平坦底噪与全零防御守卫
    diff_max = float(np.max(diff_1000hz)) if N > 0 else 0.0
    diff_min = float(np.min(diff_1000hz)) if N > 0 else 0.0
    if (
        not math.isfinite(diff_max)
        or not math.isfinite(diff_min)
        or diff_max <= 1e-6
        or (diff_max - diff_min) <= 1e-6
    ):
        if meta is not None:
            meta.update({
                "n_matched": 0,
                "total_beats": 0,
                "coverage": 0.0,
                "transition_guard_ms": tg_ms,
                "transition_miss_beats": transition_miss_beats,
                "transition_resume_beats": transition_resume_beats,
                "transition_spans": [],
                "matched_beat_times": [],  # [只读导出] 失败路径无命中拍
            })
        return 0.0, 999.0, 1.0

    L = int(round(tau))
    if L <= 0:
        if meta is not None:
            meta.update({
                "n_matched": 0,
                "total_beats": 0,
                "coverage": 0.0,
                "transition_guard_ms": tg_ms,
                "transition_miss_beats": transition_miss_beats,
                "transition_resume_beats": transition_resume_beats,
                "transition_spans": [],
                "matched_beat_times": [],  # [只读导出] 失败路径无命中拍
            })
        return 0.0, 999.0, 1.0

    # 1. 无量纲全周期等分映射直方图，解决 tau 浮点数分桶越界 (L+1 桶) 与截断畸变
    t_all = np.arange(N, dtype=np.float64)
    phi_norm = (t_all % tau) / tau
    bin_idx = np.clip(np.floor(phi_norm * L).astype(np.int64), 0, L - 1)
    # 仅在 refine_phase_center=True 时启用非负起音分桶与三角平滑核（默认关闭以保持外部调用方逐位一致）
    pos_diff_weights = np.maximum(diff_1000hz, 0.0) if refine_phase_center else None
    hist_weights = pos_diff_weights if refine_phase_center else diff_1000hz
    acc = np.bincount(bin_idx, weights=hist_weights, minlength=L)

    # 循环滑动平滑 (半径 15ms)
    smooth_r = min(15, max(1, L // 10))
    smooth_r = min(smooth_r, max(1, (L - 1) // 2))
    acc_ext = np.concatenate([acc[-smooth_r:], acc, acc[:smooth_r]])
    if refine_phase_center:
        # 对称三角帐篷平滑核（中心严格单峰）+ 循环 3 点抛物线插值，消除平顶矩形核左偏
        tent_offsets = np.abs(np.arange(-smooth_r, smooth_r + 1, dtype=np.float64))
        kernel = 1.0 - tent_offsets / float(smooth_r + 1)
        smoothed = np.convolve(acc_ext, kernel, mode="valid")
        best_bin = int(np.argmax(smoothed[:L]))
        delta_h = 0.0
        if L >= 3:
            yh0 = float(smoothed[best_bin])
            yhm1 = float(smoothed[(best_bin - 1) % L])
            yhp1 = float(smoothed[(best_bin + 1) % L])
            denom_h = (yh0 - yhm1) + (yh0 - yhp1)
            if denom_h > 1e-12 and math.isfinite(denom_h):
                cand_dh = 0.5 * (yhp1 - yhm1) / denom_h
                if abs(cand_dh) <= 1.0 and math.isfinite(cand_dh):
                    delta_h = cand_dh
        best_phi = float((float(best_bin) + delta_h) % float(L)) * (tau / float(L))
    else:
        kernel = np.ones(2 * smooth_r + 1, dtype=np.float32)
        smoothed = np.convolve(acc_ext, kernel, mode="valid")
        best_bin = int(np.argmax(smoothed[:L]))
        best_phi = float(best_bin) * (tau / float(L))

    # 2. 沿全曲展开理论节拍网格: T_k = phi + k * tau
    def _compute_robust_peak_threshold(base_thr: float, active_floor_ratio: float) -> tuple[float, float]:
        # 抗首尾 IIR 差分边界伪峰与单次极端瞬态冲击的分位数门限
        pad_edge = min(25, N // 10)
        interior = diff_1000hz[pad_edge : N - pad_edge] if N > 2 * pad_edge + 4 else diff_1000hz
        interior_max = float(np.max(interior)) if len(interior) > 0 else diff_max
        n_blocks = len(interior) // 1000
        if n_blocks >= 3:
            blk_max = np.max(interior[: n_blocks * 1000].reshape(n_blocks, 1000), axis=1)
            act_blk = blk_max[blk_max > active_floor_ratio * interior_max]
            ref_pk = float(np.percentile(act_blk, 65.0)) if len(act_blk) > 0 else interior_max
        else:
            pos_vals = interior[interior > 0.0]
            ref_pk = float(np.percentile(pos_vals, 99.0)) if len(pos_vals) > 0 else interior_max
        return max(1e-6, 0.005 * interior_max, min(base_thr, 0.05 * ref_pk)), ref_pk

    raw_threshold = 0.05 * diff_max
    if refine_phase_center:
        # [优化 2026-11-15] Force V3 默认直通路径同样启用分位数抗瞬态门限，避免单点毛刺打满不稳定度；
        # refine_phase_center=False 时保持原 0.05*diff_max 以确保 enhanced_v2/v3 零回归
        threshold, _robust_ref_peak = _compute_robust_peak_threshold(raw_threshold, 0.005)
    else:
        threshold = raw_threshold
        _robust_ref_peak = diff_max
    w = min(eff_win_r, int(tau * 0.25))

    def _probe_beat(t_probe: float) -> tuple[bool, float, float]:
        t_start = max(0, int(round(t_probe - w)))
        t_end = min(N, int(round(t_probe + w + 1)))
        if t_end - t_start >= 3:
            seg = diff_1000hz[t_start:t_end]
            max_idx = int(np.argmax(seg))
            # 窗口内部波峰判定：仅接受窗口内部波峰，严格剔除边缘单调爬坡伪峰
            if seg[max_idx] >= threshold and 0 < max_idx < len(seg) - 1:
                y0 = float(seg[max_idx])
                ym1 = float(seg[max_idx - 1])
                yp1 = float(seg[max_idx + 1])
                if y0 >= ym1 and y0 >= yp1 and (y0 > ym1 or y0 > yp1):
                    # 3 点二次抛物线微调插值，消除 1ms 离散量化阶跃
                    denom = (y0 - ym1) + (y0 - yp1)
                    delta = 0.5 * (yp1 - ym1) / denom if denom > 1e-9 else 0.0
                    if abs(delta) > 1.0 or not math.isfinite(delta):
                        delta = 0.0
                    actual_t = float(t_start + max_idx) + delta
                    return True, actual_t, actual_t - t_probe
        return False, t_probe, 0.0

    def _scan_grid_beats(phi_start: float) -> list[tuple[int, float, bool, float, float]]:
        # 遍历全曲节拍网格 (修复循环栅栏 Fencepost 截断: while t_exp < N)
        t_cur = phi_start
        beat_cnt = 0
        scanned: list[tuple[int, float, bool, float, float]] = []
        while t_cur < N:
            is_hit_b, actual_t_b, err_b = _probe_beat(t_cur)
            scanned.append((beat_cnt, t_cur, is_hit_b, actual_t_b, err_b))
            beat_cnt += 1
            t_cur += tau
        return scanned

    beat_data = _scan_grid_beats(best_phi)
    total_beats = len(beat_data)
    raw_errors = [b[4] for b in beat_data if b[2]]
    raw_n_matched = len(raw_errors)
    raw_total_beats = total_beats

    if not allow_phase_relock:
        def _eval_single_phase(
            b_data_in: list[tuple[int, float, bool, float, float]],
        ) -> tuple[list[float], list[float], int, list[list[float]]]:
            r_errors = [b[4] for b in b_data_in if b[2]]
            r_tot = len(b_data_in)
            c_hits: list[float] = []
            if tg_ms <= 0.0:
                return r_errors, [b[1] for b in b_data_in if b[2]], r_tot, []

            excluded_spans: list[tuple[float, float]] = []
            in_steady = False
            consecutive_hits = 0
            consecutive_misses = 0
            last_hit_t: float | None = None
            trans_start_t: float | None = None
            resume_cand_idx: int = 0
            had_initial_gap = False

            M = transition_miss_beats
            K = transition_resume_beats

            for b_idx, b_t_exp, b_is_hit, _, _ in b_data_in:
                if b_is_hit:
                    consecutive_misses = 0
                    if in_steady:
                        last_hit_t = b_t_exp
                    else:
                        if consecutive_hits == 0:
                            resume_cand_idx = b_idx
                        consecutive_hits += 1
                        if consecutive_hits >= K:
                            # 确认恢复稳态 a tempo（不回溯，以达成门限的第 K 拍作为恢复锚点）
                            atempo_start_t = b_t_exp
                            if trans_start_t is not None:
                                excluded_spans.append((trans_start_t - tg_ms, atempo_start_t + tg_ms))
                            elif had_initial_gap or resume_cand_idx >= M:
                                # 歌曲前导部分缺失/非稳态进入稳态
                                excluded_spans.append((0.0, atempo_start_t + tg_ms))
                            in_steady = True
                            last_hit_t = b_t_exp
                else:
                    consecutive_hits = 0
                    if in_steady:
                        consecutive_misses += 1
                        if consecutive_misses >= M:
                            # 确认进入变速过渡段
                            in_steady = False
                            trans_start_t = last_hit_t if last_hit_t is not None else 0.0
                    else:
                        consecutive_misses += 1
                        if consecutive_misses >= M:
                            had_initial_gap = True

            # 遍历结束后若仍处于变速状态，排除至曲终
            if not in_steady:
                if trans_start_t is not None:
                    excluded_spans.append((trans_start_t - tg_ms, float(N)))
                elif had_initial_gap or r_tot > 0:
                    excluded_spans.append((0.0, float(N)))

            # 区间合并 (剪穿了就直接当没了)
            m_spans: list[list[float]] = []
            for s_start, s_end in sorted(excluded_spans):
                s_start = max(0.0, s_start)
                s_end = min(float(N), s_end)
                if s_start >= s_end:
                    continue
                if not m_spans:
                    m_spans.append([s_start, s_end])
                else:
                    prev = m_spans[-1]
                    if s_start <= prev[1]:
                        prev[1] = max(prev[1], s_end)
                    else:
                        m_spans.append([s_start, s_end])

            def _is_in_spans(t: float) -> bool:
                for s_start, s_end in m_spans:
                    if s_start <= t <= s_end:
                        return True
                return False

            c_errors: list[float] = []
            c_total_beats = 0
            for _, b_t_exp, b_is_hit, _, b_err in b_data_in:
                if not _is_in_spans(b_t_exp):
                    c_total_beats += 1
                    if b_is_hit:
                        c_errors.append(b_err)
                        c_hits.append(b_t_exp)

            return c_errors, c_hits, c_total_beats, m_spans

        # [只读导出] 计入 n_matched 的命中拍理论网格时刻（ms），供 UI 生成「未匹配主干网格区段」；不参与任何计算
        clean_errors, counted_hit_times, clean_total_beats, merged_spans = _eval_single_phase(beat_data)
        clean_n_matched = len(clean_errors)

        # 若开启相位中心精修且初值一阶矩偏置超过 2ms，在去偏相位上重对齐探测窗 1 次
        if refine_phase_center and clean_errors and clean_n_matched >= max(3, clean_total_beats // 10):
            init_mean_err = float(np.mean(np.array(clean_errors, dtype=np.float64)))
            if abs(init_mean_err) > 2.0 and math.isfinite(init_mean_err):
                cand_phi = float((best_phi + init_mean_err) % tau)
                if cand_phi >= tau:
                    cand_phi -= tau
                elif cand_phi < 0.0:
                    cand_phi = 0.0
                beat_data_2 = _scan_grid_beats(cand_phi)
                clean_errors_2, counted_hit_times_2, clean_total_beats_2, merged_spans_2 = _eval_single_phase(beat_data_2)
                clean_n_matched_2 = len(clean_errors_2)
                if (
                    clean_errors_2
                    and clean_n_matched_2 >= max(3, clean_total_beats_2 // 10)
                    and clean_n_matched_2 >= int(0.9 * clean_n_matched)
                ):
                    best_phi = cand_phi
                    beat_data = beat_data_2
                    raw_errors = [b[4] for b in beat_data if b[2]]
                    raw_n_matched = len(raw_errors)
                    raw_total_beats = len(beat_data)
                    clean_errors = clean_errors_2
                    counted_hit_times = counted_hit_times_2
                    clean_total_beats = clean_total_beats_2
                    clean_n_matched = clean_n_matched_2
                    merged_spans = merged_spans_2

        clean_coverage = float(clean_n_matched) / float(clean_total_beats) if clean_total_beats > 0 else 0.0
        raw_coverage = float(raw_n_matched) / float(raw_total_beats) if raw_total_beats > 0 else 0.0

        has_enough_hits = bool(clean_errors) and clean_n_matched >= max(3, clean_total_beats // 10)
        if has_enough_hits:
            err_arr = np.array(clean_errors, dtype=np.float64)
            # 一阶矩相位中心化去偏：消除乐器包络衰减带来的系统性相位滞后
            mean_err = float(np.mean(err_arr))
            # [只读导出] 开启相位中心精修时将导出网格时刻同步对齐最终 best_phi（保持长度 == n_matched）
            exported_hit_times = (
                [max(0.0, min(float(N), float(t) + mean_err)) for t in counted_hit_times]
                if refine_phase_center
                else counted_hit_times
            )
        else:
            mean_err = 0.0
            exported_hit_times = counted_hit_times

        if meta is not None:
            meta.update({
                "n_matched": clean_n_matched,
                "total_beats": clean_total_beats,
                "coverage": clean_coverage,
                "transition_guard_ms": tg_ms,
                "transition_miss_beats": transition_miss_beats,
                "transition_resume_beats": transition_resume_beats,
                "transition_spans": [(round(s, 2), round(e, 2)) for s, e in merged_spans],
                "raw_n_matched": raw_n_matched,
                "raw_total_beats": raw_total_beats,
                "raw_coverage": raw_coverage,
                "comb_tol": eff_win_r,
                # [只读导出] len(matched_beat_times) == n_matched
                "matched_beat_times": [round(float(t), 2) for t in exported_hit_times],
                "matched_beat_tau": float(tau),
            })

        if not has_enough_hits:
            # 若几乎没有起音匹配，不稳定度打满
            return best_phi, float(w), 1.0

        best_phi = float((best_phi + mean_err) % tau)
        if best_phi >= tau:
            best_phi -= tau
        elif best_phi < 0.0:
            best_phi = 0.0

        centered_err = err_arr - mean_err
        rmse = float(np.sqrt(np.mean(centered_err ** 2)))
    else:
        # 分段回溯相位重锁路径 (allow_phase_relock=True)
        M = transition_miss_beats
        K = transition_resume_beats

        # 先检查全局单相位网格是否全程稳态无失锁；若无失锁则直接快速短路，保证常规稳态曲目与 3+3+2 切分曲目零扰动
        def _check_steady_lost(
            b_data_in: list[tuple[int, float, bool, float, float]],
            raw_n_in: int,
            raw_tot_in: int,
        ) -> bool:
            sg_in_steady = False
            sg_hits = 0
            sg_misses = 0
            sg_max_misses = 0
            sg_resume_idx = 0
            sg_had_gap = False
            sg_lost = False
            for b_idx, _, b_is_hit, _, _ in b_data_in:
                if b_is_hit:
                    sg_misses = 0
                    if not sg_in_steady:
                        if sg_hits == 0:
                            sg_resume_idx = b_idx
                        sg_hits += 1
                        if sg_hits >= K:
                            if sg_had_gap or sg_resume_idx >= M:
                                sg_lost = True
                            sg_in_steady = True
                else:
                    sg_hits = 0
                    sg_misses += 1
                    if sg_misses > sg_max_misses:
                        sg_max_misses = sg_misses
                    if sg_in_steady:
                        if sg_misses >= M:
                            sg_in_steady = False
                            sg_lost = True
                    else:
                        if sg_misses >= M:
                            sg_had_gap = True
            if not sg_in_steady and (sg_max_misses >= M or raw_n_in < max(3, raw_tot_in // 10)):
                sg_lost = True
            return sg_lost

        # [优化 2026-11-15] 开启相位中心精修时，先于稳态失锁判定执行一次 >2ms 初相居中重探，
        # 避免因宽包络初相偏斜误触发 sg_lost_steady 导致稳态曲目落入分段重锁与二次重算
        recentered_once = False
        if refine_phase_center and raw_errors and raw_n_matched >= max(3, raw_total_beats // 10):
            init_mean_sg = float(np.mean(np.array(raw_errors, dtype=np.float64)))
            if abs(init_mean_sg) > 2.0 and math.isfinite(init_mean_sg):
                cand_phi_sg = float((best_phi + init_mean_sg) % tau)
                if cand_phi_sg >= tau:
                    cand_phi_sg -= tau
                elif cand_phi_sg < 0.0:
                    cand_phi_sg = 0.0
                beat_data_sg2 = _scan_grid_beats(cand_phi_sg)
                raw_errors_sg2 = [b[4] for b in beat_data_sg2 if b[2]]
                raw_n_sg2 = len(raw_errors_sg2)
                raw_tot_sg2 = len(beat_data_sg2)
                sg_lost_pre = _check_steady_lost(beat_data, raw_n_matched, raw_total_beats)
                sg_lost_2 = _check_steady_lost(beat_data_sg2, raw_n_sg2, raw_tot_sg2)
                if (
                    raw_errors_sg2
                    and raw_n_sg2 >= max(3, raw_tot_sg2 // 10)
                    and (
                        (sg_lost_pre and not sg_lost_2)
                        or (raw_n_sg2 >= int(0.9 * raw_n_matched) and (not sg_lost_2 or sg_lost_pre))
                    )
                ):
                    best_phi = cand_phi_sg
                    beat_data = beat_data_sg2
                    raw_errors = raw_errors_sg2
                    raw_n_matched = raw_n_sg2
                    raw_total_beats = raw_tot_sg2
                    sg_lost_steady = sg_lost_2
                else:
                    sg_lost_steady = sg_lost_pre
                recentered_once = True

        if not recentered_once:
            sg_lost_steady = _check_steady_lost(beat_data, raw_n_matched, raw_total_beats)

        if not sg_lost_steady:
            merged_spans = []
            clean_n_matched = raw_n_matched
            clean_total_beats = raw_total_beats
            clean_coverage = float(clean_n_matched) / float(clean_total_beats) if clean_total_beats > 0 else 0.0
            raw_coverage = float(raw_n_matched) / float(raw_total_beats) if raw_total_beats > 0 else 0.0
            has_enough_sg = bool(raw_errors) and clean_n_matched >= max(3, clean_total_beats // 10)
            if has_enough_sg:
                err_arr = np.array(raw_errors, dtype=np.float64)
                mean_err = float(np.mean(err_arr))
                sg_hit_times = [
                    max(0.0, min(float(N), float(b[1]) + mean_err)) if refine_phase_center else float(b[1])
                    for b in beat_data
                    if b[2]
                ]
            else:
                mean_err = 0.0
                sg_hit_times = [float(b[1]) for b in beat_data if b[2]]
            if meta is not None:
                meta.update({
                    "n_matched": clean_n_matched,
                    "total_beats": clean_total_beats,
                    "coverage": clean_coverage,
                    "transition_guard_ms": tg_ms,
                    "transition_miss_beats": transition_miss_beats,
                    "transition_resume_beats": transition_resume_beats,
                    "transition_spans": [],
                    "raw_n_matched": raw_n_matched,
                    "raw_total_beats": raw_total_beats,
                    "raw_coverage": raw_coverage,
                    "comb_tol": eff_win_r,
                    # [只读导出] 全程稳态短路：全部命中拍均计入 n_matched
                    "matched_beat_times": [round(float(t), 2) for t in sg_hit_times],
                    "matched_beat_tau": float(tau),
                })
            if not has_enough_sg:
                return best_phi, float(w), 1.0
            best_phi = float((best_phi + mean_err) % tau)
            if best_phi >= tau:
                best_phi -= tau
            elif best_phi < 0.0:
                best_phi = 0.0
            centered_err = err_arr - mean_err
            rmse = float(np.sqrt(np.mean(centered_err ** 2)))
        else:
            # 复用统一抗首尾 IIR 差分边界伪峰与极端瞬态冲击的分位数门限
            if refine_phase_center:
                relock_threshold = threshold
                ref_peak = _robust_ref_peak
            else:
                relock_threshold, ref_peak = _compute_robust_peak_threshold(raw_threshold, 0.02)

            w_half = max(6, min(w, int(0.125 * tau)))
            w_quarter = min(8, max(4, int(0.05 * tau)))
            w_bar = max(8, min(w, int(0.08 * tau) + 6))
            half_tau = 0.5 * tau
            quarter_tau = 0.25 * tau
            homologous_tol = min(10.0, 0.03 * tau)
            max_servo_step = min(1.5, 0.003 * tau)

            def _probe_relock(t_probe: float, win_r_local: int = w) -> tuple[bool, float, float]:
                t_start = max(0, int(round(t_probe - win_r_local)))
                t_end = min(N, int(round(t_probe + win_r_local + 1)))
                if t_end - t_start >= 3:
                    seg = diff_1000hz[t_start:t_end]
                    max_idx = int(np.argmax(seg))
                    if seg[max_idx] >= relock_threshold and 0 < max_idx < len(seg) - 1:
                        y0 = float(seg[max_idx])
                        ym1 = float(seg[max_idx - 1])
                        yp1 = float(seg[max_idx + 1])
                        if y0 >= ym1 and y0 >= yp1 and (y0 > ym1 or y0 > yp1):
                            denom = (y0 - ym1) + (y0 - yp1)
                            delta = 0.5 * (yp1 - ym1) / denom if denom > 1e-9 else 0.0
                            if abs(delta) > 1.0 or not math.isfinite(delta):
                                delta = 0.0
                            actual_t = float(t_start + max_idx) + delta
                            return True, actual_t, actual_t - t_probe
                return False, t_probe, 0.0

            def _next_grid_t(t_min: float, phi_val: float) -> float:
                phi_mod = float(phi_val % tau)
                if phi_mod < 0.0:
                    phi_mod += tau
                if t_min <= phi_mod + 1e-9:
                    return phi_mod
                k_step = math.ceil((t_min - phi_mod) / tau - 1e-9)
                return phi_mod + float(k_step) * tau

            def _is_homologous_phase(phi_a: float, phi_b: float) -> bool:
                if half_tau <= 1e-9:
                    return True
                diff_h = abs((phi_a - phi_b) % half_tau)
                return min(diff_h, half_tau - diff_h) <= homologous_tol

            def _is_halfbeat_shifted(phi_cand_val: float, ref_phi_val: float) -> bool:
                diff_main = abs((phi_cand_val - ref_phi_val) % tau)
                dist_main = min(diff_main, tau - diff_main)
                return dist_main > 0.25 * tau

            def _gap_has_foreign_rhythm(
                g_start: float,
                g_end: float,
                ref_phi_val: float,
                min_span_ms: float | None = None,
            ) -> bool:
                """
                检验失锁间隙内是否存在持续的异速节拍冲击（防止整数小节异速段或 5:4 / 3:4 复节奏碰巧落回同源相位时误判）。
                在 <=150BPM (tau >= 400ms) 时豁免极窄容差 (<=8ms) 的 0.25*tau 十六分切分同源细分。
                """
                req_span = min_span_ms if min_span_ms is not None else max(3000.0, float(2 * M) * tau)
                if (g_end - g_start) < req_span or half_tau <= 1e-9:
                    return False
                i0 = max(1, int(math.ceil(g_start)))
                i1 = min(N - 1, int(math.floor(g_end)))
                if i1 - i0 < 200:
                    return False
                pk_thr = max(relock_threshold * 1.5, 0.08 * ref_peak)
                refractory = max(80, int(0.20 * tau))
                seg_g = diff_1000hz[i0:i1]
                span_len_g = g_end - g_start
                is_single_bar_probe = (min_span_ms is not None and span_len_g <= 4.5 * tau)
                if is_single_bar_probe:
                    min_prom = 2
                    min_off_cnt = 2
                    min_off_ratio = 0.50
                elif min_span_ms is not None:
                    min_prom = 3
                    min_off_cnt = 3
                    min_off_ratio = 0.35
                else:
                    min_prom = max(M + 2, 6)
                    min_off_cnt = max(M, 4)
                    min_off_ratio = 0.35
                cand_idxs = np.flatnonzero(seg_g >= pk_thr)
                if len(cand_idxs) < min_prom:
                    return False
                peaks_t: list[tuple[int, float]] = []
                for rel_idx in cand_idxs:
                    abs_i = i0 + int(rel_idx)
                    y_c = float(diff_1000hz[abs_i])
                    if y_c >= float(diff_1000hz[abs_i - 1]) and y_c >= float(diff_1000hz[abs_i + 1]):
                        if not peaks_t or (abs_i - peaks_t[-1][0]) >= refractory:
                            peaks_t.append((abs_i, y_c))
                        elif y_c > peaks_t[-1][1]:
                            peaks_t[-1] = (abs_i, y_c)
                n_prom = len(peaks_t)
                if n_prom < min_prom:
                    return False
                allow_quarter = (tau >= 400.0 and quarter_tau > 1e-9)
                q_tol = float(w_quarter)
                off_tol = float(w)
                n_off = 0
                for abs_i, _ in peaks_t:
                    d_h = abs((float(abs_i) - ref_phi_val) % half_tau)
                    if min(d_h, half_tau - d_h) > off_tol:
                        if allow_quarter:
                            d_q = abs((float(abs_i) - ref_phi_val) % quarter_tau)
                            if min(d_q, quarter_tau - d_q) <= q_tol:
                                continue
                        n_off += 1
                return n_off >= min_off_cnt and (float(n_off) / float(n_prom)) >= min_off_ratio

            def _estimate_local_phi(w_start: float, w_end: float, fallback_phi: float) -> float:
                s_i = max(0, int(math.floor(w_start)))
                e_i = min(N, int(math.ceil(w_end)))
                if e_i - s_i < max(L, 2):
                    return fallback_phi
                sub_weights = pos_diff_weights[s_i:e_i] if refine_phase_center else diff_1000hz[s_i:e_i]
                # [优化 2026-11-15] 静默/低于门限窗提前快跳，避免无起音间奏长段反复分配数组与卷积
                sub_max = float(np.max(sub_weights))
                if (sub_max < relock_threshold) if refine_phase_center else (sub_max <= 0.0):
                    return fallback_phi
                t_sub = np.arange(s_i, e_i, dtype=np.float64)
                phi_norm_sub = (t_sub % tau) / tau
                bin_sub = np.clip(np.floor(phi_norm_sub * L).astype(np.int64), 0, L - 1)
                acc_sub = np.bincount(bin_sub, weights=sub_weights, minlength=L)
                if float(np.max(acc_sub)) <= 0.0:
                    return fallback_phi
                acc_ext_sub = np.concatenate([acc_sub[-smooth_r:], acc_sub, acc_sub[:smooth_r]])
                smoothed_sub = np.convolve(acc_ext_sub, kernel, mode="valid")
                best_bin_sub = int(np.argmax(smoothed_sub[:L]))
                if refine_phase_center and L >= 3:
                    ys0 = float(smoothed_sub[best_bin_sub])
                    ysm1 = float(smoothed_sub[(best_bin_sub - 1) % L])
                    ysp1 = float(smoothed_sub[(best_bin_sub + 1) % L])
                    denom_s = (ys0 - ysm1) + (ys0 - ysp1)
                    delta_s = 0.0
                    if denom_s > 1e-12 and math.isfinite(denom_s):
                        cand_ds = 0.5 * (ysp1 - ysm1) / denom_s
                        if abs(cand_ds) <= 1.0 and math.isfinite(cand_ds):
                            delta_s = cand_ds
                    return float((float(best_bin_sub) + delta_s) % float(L)) * (tau / float(L))
                return float(best_bin_sub) * (tau / float(L))

            def _try_lock_k_relock(phi_cand: float, cursor_t: float, min_back_t: float) -> tuple[bool, float, float, float]:
                canon_phi = float(phi_cand % tau)
                if canon_phi < 0.0:
                    canon_phi += tau

                max_spread_1x = max(14.0, min(22.0, 0.60 * float(w)))
                max_spread_ht = max(14.0, min(24.0, 0.65 * float(w)))
                max_spread_off = max(10.0, min(22.0, 0.60 * float(w_half)))
                max_spread_bar = min(12.0, 0.50 * float(w_bar))

                j_start_1x = min(K - 1, max(1, K - 1))
                j_start_ht = min(K - 1, max(1, (K - 1) // 2))

                def _try_1x_at(t_base: float) -> tuple[bool, float, float, float] | None:
                    if t_base + float(K - 1) * tau < float(N):
                        ok_all = True
                        errs_1x: list[float] = []
                        for j in range(K):
                            ok_j, _, err_j = _probe_relock(t_base + float(j) * tau, w)
                            if not ok_j:
                                ok_all = False
                                break
                            errs_1x.append(err_j)
                        if ok_all and (max(errs_1x) - min(errs_1x)) <= max_spread_1x:
                            clean_errs_1x = errs_1x[j_start_1x:]
                            mean_1x = float(np.mean(clean_errs_1x)) if clean_errs_1x else 0.0
                            true_phi = float((canon_phi + mean_1x) % tau)
                            if true_phi < 0.0:
                                true_phi += tau
                            first_t_1x = max(0.0, t_base + mean_1x)
                            true_t0 = max(0.0, t_base + float(j_start_1x) * tau + mean_1x)
                            if true_t0 < float(N):
                                return True, true_t0, true_phi, first_t_1x
                    return None

                def _try_ht_at(t_ht: float) -> tuple[bool, float, float, float] | None:
                    if t_ht + float(2 * (K - 1)) * tau < float(N):
                        ok_ht = True
                        errs_ht: list[float] = []
                        for j in range(K):
                            ok_j, _, err_j = _probe_relock(t_ht + float(2 * j) * tau, w)
                            if not ok_j:
                                ok_ht = False
                                break
                            errs_ht.append(err_j)
                        if ok_ht and (max(errs_ht) - min(errs_ht)) <= max_spread_ht:
                            clean_errs_ht = errs_ht[j_start_ht:]
                            mean_ht = float(np.mean(clean_errs_ht)) if clean_errs_ht else 0.0
                            true_phi = float((canon_phi + mean_ht) % tau)
                            if true_phi < 0.0:
                                true_phi += tau
                            first_t_ht = max(0.0, t_ht + mean_ht)
                            true_t_ht = max(0.0, t_ht + float(2 * j_start_ht) * tau + mean_ht)
                            if true_t_ht < float(N):
                                return True, true_t_ht, true_phi, first_t_ht
                    return None

                def _try_off_at(t_base_off: float) -> tuple[bool, float, float, float] | None:
                    if t_base_off + float(K - 1) * tau < float(N):
                        ok_off = True
                        errs_off: list[float] = []
                        for j in range(K):
                            ok_j, _, err_j = _probe_relock(t_base_off + float(j) * tau, w_half)
                            if not ok_j:
                                ok_off = False
                                break
                            errs_off.append(err_j)
                        if ok_off and (max(errs_off) - min(errs_off)) <= max_spread_off:
                            clean_errs_off = errs_off[j_start_1x:]
                            mean_off = float(np.mean(clean_errs_off)) if clean_errs_off else 0.0
                            true_phi = float((canon_phi + mean_off) % tau)
                            if true_phi < 0.0:
                                true_phi += tau
                            adj_first_off = max(0.0, t_base_off + mean_off)
                            first_main_t0 = (
                                adj_first_off - half_tau
                                if (adj_first_off - half_tau >= min_back_t - 1e-9)
                                else (adj_first_off + half_tau)
                            )
                            adj_t0_off = max(0.0, t_base_off + float(j_start_1x) * tau + mean_off)
                            main_t0 = (
                                adj_t0_off - half_tau
                                if (adj_t0_off - half_tau >= min_back_t - 1e-9)
                                else (adj_t0_off + half_tau)
                            )
                            if main_t0 < float(N):
                                return True, main_t0, true_phi, first_main_t0
                    return None

                # 1) 1*tau 标准正拍连续 K 拍入锁（校验跨拍周期一致性防渐慢/异速斜穿，并返回均值校正后的真实段相位）
                t0 = _next_grid_t(cursor_t, canon_phi)
                res_1x = _try_1x_at(t0)
                if res_1x is not None:
                    return res_1x

                # 2) 2*tau Half-time 半速连续 K 个偶拍入锁（测试偶相与奇相两个起点）
                for parity_offset in (0.0, tau):
                    t_ht = t0 + parity_offset
                    if parity_offset > 0.0:
                        res_1x_ahead = _try_1x_at(t_ht)
                        if res_1x_ahead is not None:
                            return res_1x_ahead
                    res_ht = _try_ht_at(t_ht)
                    if res_ht is not None:
                        return res_ht

                # 3) 0.5*tau 半拍/反拍连续 K 拍入锁（窄窗 w_half 防异速误吸，锁成后折算回主网格真实相位）
                off_phi = float((canon_phi + half_tau) % tau)
                t0_off = _next_grid_t(cursor_t, off_phi)
                res_off = _try_off_at(t0_off)
                if res_off is not None:
                    return res_off

                # 4) 反拍 2*tau 半速入锁（窄窗 w_half 测试反拍奇偶相，折算回主网格真实相位）
                for parity_offset in (0.0, tau):
                    t_ht_off = t0_off + parity_offset
                    if parity_offset > 0.0:
                        res_off_ahead = _try_off_at(t_ht_off)
                        if res_off_ahead is not None:
                            return res_off_ahead
                    if t_ht_off + float(2 * (K - 1)) * tau < float(N):
                        ok_hto = True
                        errs_hto: list[float] = []
                        for j in range(K):
                            ok_j, _, err_j = _probe_relock(t_ht_off + float(2 * j) * tau, w_half)
                            if not ok_j:
                                ok_hto = False
                                break
                            errs_hto.append(err_j)
                        if ok_hto and (max(errs_hto) - min(errs_hto)) <= max_spread_off:
                            clean_errs_hto = errs_hto[j_start_ht:]
                            mean_hto = float(np.mean(clean_errs_hto)) if clean_errs_hto else 0.0
                            true_phi = float((canon_phi + mean_hto) % tau)
                            if true_phi < 0.0:
                                true_phi += tau
                            adj_first_hto = max(0.0, t_ht_off + mean_hto)
                            first_main_hto = (
                                adj_first_hto - half_tau
                                if (adj_first_hto - half_tau >= min_back_t - 1e-9)
                                else (adj_first_hto + half_tau)
                            )
                            adj_t_hto = max(0.0, t_ht_off + float(2 * j_start_ht) * tau + mean_hto)
                            main_t_hto = adj_t_hto - half_tau if (adj_t_hto - half_tau >= min_back_t - 1e-9) else (adj_t_hto + half_tau)
                            if main_t_hto < float(N):
                                return True, main_t_hto, true_phi, first_main_hto

                # 5) 4*tau 每小节一击稀疏入锁（仅限 tau <= 750ms，窄容差 + 小节内部异速冲击防线防 5:4 / 3:4 复节奏误锁）
                if tau <= 750.0:
                    k_bar = max(K, 3)
                    j_start_bar = min(k_bar - 1, max(1, (K - 1) // 4))
                    for bar_idx, bar_offset in enumerate((0.0, tau, 2.0 * tau, 3.0 * tau)):
                        t_bar = t0 + bar_offset
                        if t_bar + float(4 * (k_bar - 1)) * tau < float(N):
                            ok_bar = True
                            errs_bar: list[float] = []
                            for j in range(k_bar):
                                ok_j, _, err_j = _probe_relock(t_bar + float(4 * j) * tau, w_bar)
                                if not ok_j:
                                    ok_bar = False
                                    break
                                errs_bar.append(err_j)
                            if ok_bar and (max(errs_bar) - min(errs_bar)) <= max_spread_bar:
                                fwd_mean_bar = float(np.mean(errs_bar)) if errs_bar else 0.0
                                cand_phi_bar = float((canon_phi + fwd_mean_bar) % tau)
                                if cand_phi_bar < 0.0:
                                    cand_phi_bar += tau
                                span_end_bar = t_bar + float(4 * (k_bar - 1)) * tau
                                if not _gap_has_foreign_rhythm(
                                    t_bar + 10.0,
                                    span_end_bar - 10.0,
                                    cand_phi_bar,
                                    min_span_ms=max(1200.0, 3.0 * tau),
                                ):
                                    for m_1x in range(1, bar_idx + 4):
                                        res_1x_sub = _try_1x_at(t0 + float(m_1x) * tau)
                                        if res_1x_sub is not None:
                                            return res_1x_sub
                                    for m_ht in range(2, bar_idx + 4):
                                        res_ht_sub = _try_ht_at(t0 + float(m_ht) * tau)
                                        if res_ht_sub is not None:
                                            return res_ht_sub
                                    clean_errs_bar = errs_bar[j_start_bar:]
                                    mean_bar = float(np.mean(clean_errs_bar)) if clean_errs_bar else 0.0
                                    true_phi = float((canon_phi + mean_bar) % tau)
                                    if true_phi < 0.0:
                                        true_phi += tau
                                    first_t_bar = max(0.0, t_bar + mean_bar)
                                    true_t_bar = max(0.0, t_bar + float(4 * j_start_bar) * tau + mean_bar)
                                    if true_t_bar < float(N):
                                        return True, true_t_bar, true_phi, first_t_bar

                return False, 0.0, canon_phi, 0.0

            cur_phi = best_phi
            seg_id = 0
            seg_phis: dict[int, float] = {}
            seg_live_hits: dict[int, int] = {}
            seg_t0: float = 0.0
            seg_entry_t: float = 0.0
            cum_servo: float = 0.0
            in_steady = False
            t_exp = 0.0
            last_hit_t: float | None = None
            trans_start_t: float | None = None
            prev_trans_start_t: float | None = None
            entry_last_hit_t: float | None = None
            entry_cum_servo: float = 0.0
            entry_seg_t0: float = 0.0
            entry_cur_phi: float = cur_phi
            search_cursor = 0.0
            min_next_cursor = 0.0
            excluded_spans = []
            recalc_excluded_spans: list[tuple[float, float]] = []
            # 第 6 元素记录无伺服基准网格时刻 t_fixed = t_exp - cum_servo，供分段居中导出使用
            piecewise_beats: list[tuple[int, float, bool, float, float, float]] = []
            entry_spans_len = 0
            entry_recalc_spans_len = 0
            entry_beats_len = 0
            entry_seg_id = 0
            entry_seg_hits = 0

            edge_pad = min(float(w + 2), 0.25 * tau)
            step_ms = max(10.0, 0.5 * tau)

            while True:
                if in_steady:
                    consecutive_misses = 0
                    pending_misses: list[tuple[int, float, bool, float, float, float]] = []
                    transition_triggered = False
                    while t_exp < float(N):
                        is_hit, actual_t, err_track = _probe_relock(t_exp, w)
                        if is_hit:
                            if pending_misses:
                                piecewise_beats.extend(pending_misses)
                                pending_misses.clear()
                            err_fixed = err_track + cum_servo
                            piecewise_beats.append((seg_id, t_exp, True, actual_t, err_fixed, t_exp - cum_servo))
                            seg_live_hits[seg_id] = seg_live_hits.get(seg_id, 0) + 1
                            consecutive_misses = 0
                            last_hit_t = t_exp
                            servo_step = max(-max_servo_step, min(max_servo_step, 0.05 * err_track))
                            cum_servo += servo_step
                            t_exp += tau + servo_step
                        else:
                            pending_misses.append((seg_id, t_exp, False, t_exp, 0.0, t_exp - cum_servo))
                            consecutive_misses += 1
                            if consecutive_misses >= M:
                                eff_steady_phi = float((seg_phis.get(seg_id, cur_phi) + cum_servo) % tau)
                                # a) 断锁前检验同源 0.5*tau 半拍/反拍（及反拍 2*tau）网格是否相干持续命中
                                half_probes = [pm[1] + half_tau for pm in pending_misses if pm[1] + half_tau < float(N)]
                                half_results = [_probe_relock(hp, w_half) for hp in half_probes]
                                half_errs = [hr[2] for hr in half_results if hr[0]]
                                near_entry = (t_exp <= seg_entry_t + float(M + 1) * tau)
                                k_eff_half = max(2, K - 1) if near_entry else K
                                need_half = max(2, min(k_eff_half, len(half_probes))) if len(half_probes) >= 2 else 1
                                half_coherent = (
                                    len(half_errs) >= need_half
                                    and (max(half_errs) - min(half_errs)) <= max(12.0, min(22.0, 0.60 * float(w_half)))
                                )
                                if not half_coherent and len(half_results) >= 4:
                                    hit_parities = [idx_h % 2 for idx_h, hr in enumerate(half_results) if hr[0]]
                                    if (
                                        len(half_errs) >= 2
                                        and len(set(hit_parities)) == 1
                                        and (max(half_errs) - min(half_errs)) <= 10.0
                                        and not _gap_has_foreign_rhythm(
                                            pending_misses[0][1] - 0.5 * tau,
                                            t_exp + 0.5 * tau,
                                            eff_steady_phi,
                                            min_span_ms=max(1200.0, 3.0 * tau),
                                        )
                                    ):
                                        half_coherent = True
                                if len(half_probes) > 0 and half_coherent:
                                    last_h_err = 0.0
                                    for idx_pm, pm_item in enumerate(pending_misses):
                                        if idx_pm < len(half_results) and half_results[idx_pm][0]:
                                            _, act_h, err_h_trk = half_results[idx_pm]
                                            piecewise_beats.append((seg_id, pm_item[1], True, act_h, err_h_trk + cum_servo, pm_item[5]))
                                            seg_live_hits[seg_id] = seg_live_hits.get(seg_id, 0) + 1
                                            last_hit_t = pm_item[1] + half_tau
                                            last_h_err = err_h_trk
                                        else:
                                            piecewise_beats.append(pm_item)
                                    pending_misses.clear()
                                    consecutive_misses = 0
                                    servo_step = max(-max_servo_step, min(max_servo_step, 0.05 * last_h_err))
                                    cum_servo += servo_step
                                    t_exp += tau + servo_step
                                    continue

                                # b) 当 M < 4 时检验 4*tau 每小节一击稀疏同源延续（tau <= 750ms 且小节内无异速冲击）
                                if M < 4 and tau <= 750.0 and last_hit_t is not None:
                                    next_bar_t = last_hit_t + 4.0 * tau
                                    if next_bar_t < float(N):
                                        ok_nb, _, err_nb = _probe_relock(next_bar_t, w_bar)
                                        if (
                                            ok_nb
                                            and abs(err_nb) <= min(12.0, 0.50 * float(w_bar))
                                            and not _gap_has_foreign_rhythm(
                                                last_hit_t + 10.0,
                                                next_bar_t - 10.0,
                                                eff_steady_phi,
                                                min_span_ms=max(1200.0, 3.0 * tau),
                                            )
                                        ):
                                            piecewise_beats.extend(pending_misses)
                                            pending_misses.clear()
                                            consecutive_misses = 0
                                            t_exp += tau
                                            continue

                                # c) <=150BPM (tau >= 400ms) 极窄容差 (<=8ms) 0.25*tau 十六分切分同源延续保护
                                if tau >= 400.0:
                                    q_hits_per_pm: list[tuple[bool, float, float, float]] = []
                                    q_errs: list[float] = []
                                    for pm_item in pending_misses:
                                        best_q: tuple[bool, float, float, float] = (False, 0.0, 0.0, 0.0)
                                        for q_off in (quarter_tau, 3.0 * quarter_tau):
                                            qp = pm_item[1] + q_off
                                            if qp < float(N):
                                                ok_q, act_q, err_q = _probe_relock(qp, w_quarter)
                                                if ok_q and (not best_q[0] or abs(err_q) < abs(best_q[2])):
                                                    best_q = (True, act_q, err_q, q_off)
                                        q_hits_per_pm.append(best_q)
                                        if best_q[0]:
                                            q_errs.append(best_q[2])
                                    if (
                                        len(q_errs) >= need_half
                                        and (max(q_errs) - min(q_errs)) <= float(w_quarter)
                                        and not _gap_has_foreign_rhythm(
                                            pending_misses[0][1] - 0.25 * tau,
                                            t_exp + 0.75 * tau,
                                            eff_steady_phi,
                                            min_span_ms=max(1200.0, 3.0 * tau),
                                        )
                                    ):
                                        last_q_err = 0.0
                                        for idx_pm, pm_item in enumerate(pending_misses):
                                            ok_q, act_q, err_q_trk, q_off_used = q_hits_per_pm[idx_pm]
                                            if ok_q:
                                                piecewise_beats.append((seg_id, pm_item[1], True, act_q, err_q_trk + cum_servo, pm_item[5]))
                                                seg_live_hits[seg_id] = seg_live_hits.get(seg_id, 0) + 1
                                                last_hit_t = pm_item[1] + q_off_used
                                                last_q_err = err_q_trk
                                            else:
                                                piecewise_beats.append(pm_item)
                                        pending_misses.clear()
                                        consecutive_misses = 0
                                        servo_step = max(-max_servo_step, min(max_servo_step, 0.05 * last_q_err))
                                        cum_servo += servo_step
                                        t_exp += tau + servo_step
                                        continue

                                pending_misses.clear()
                                in_steady = False
                                if last_hit_t is not None:
                                    trans_start_t = last_hit_t
                                    search_cursor = max(min_next_cursor, last_hit_t + 0.5 * tau)
                                    min_next_cursor = search_cursor
                                else:
                                    del excluded_spans[entry_spans_len:]
                                    del recalc_excluded_spans[entry_recalc_spans_len:]
                                    del piecewise_beats[entry_beats_len:]
                                    if seg_id > entry_seg_id:
                                        seg_phis.pop(seg_id, None)
                                        seg_live_hits.pop(seg_id, None)
                                        seg_id = entry_seg_id
                                    elif entry_seg_id > 0:
                                        seg_live_hits[entry_seg_id] = entry_seg_hits
                                    trans_start_t = prev_trans_start_t
                                    last_hit_t = entry_last_hit_t
                                    cum_servo = entry_cum_servo
                                    seg_t0 = entry_seg_t0
                                    cur_phi = entry_cur_phi
                                    search_cursor = min_next_cursor
                                transition_triggered = True
                                break
                            t_exp += tau
                    if not transition_triggered:
                        if pending_misses:
                            piecewise_beats.extend(pending_misses)
                            pending_misses.clear()
                        break
                else:
                    min_back_t = (last_hit_t + 0.5 * tau) if last_hit_t is not None else 0.0
                    eff_ref_phi = float((seg_phis[seg_id] + cum_servo) % tau) if seg_id in seg_phis else cur_phi
                    locked = False
                    locked_phi = eff_ref_phi
                    atempo_start_t = 0.0
                    first_lock_t = 0.0
                    while search_cursor + float(K - 1) * tau < float(N):
                        ok_orig, t0_orig, phi_orig, first_t_orig = _try_lock_k_relock(eff_ref_phi, search_cursor, min_back_t)
                        if ok_orig:
                            locked = True
                            locked_phi = phi_orig
                            atempo_start_t = t0_orig
                            first_lock_t = first_t_orig
                            break
                        win_end = min(float(N), search_cursor + float(max(K + 2, 6)) * tau)
                        cand_phi = _estimate_local_phi(search_cursor, win_end, eff_ref_phi)
                        ok_cand, t0_cand, phi_cand_out, first_t_cand = _try_lock_k_relock(cand_phi, search_cursor, min_back_t)
                        if ok_cand:
                            if not _is_homologous_phase(phi_cand_out, eff_ref_phi) and first_t_cand > search_cursor + 1.0:
                                ok_orig_r, t0_orig_r, phi_orig_r, first_t_orig_r = _try_lock_k_relock(eff_ref_phi, first_t_cand, min_back_t)
                                if ok_orig_r and first_t_orig_r <= first_t_cand + tau + 1e-6:
                                    locked = True
                                    locked_phi = phi_orig_r
                                    atempo_start_t = t0_orig_r
                                    first_lock_t = first_t_orig_r
                                    break
                            locked = True
                            locked_phi = phi_cand_out
                            atempo_start_t = t0_cand
                            first_lock_t = first_t_cand
                            break
                        search_cursor += step_ms

                    if locked:
                        prev_trans_start_t = trans_start_t
                        entry_last_hit_t = last_hit_t
                        entry_cum_servo = cum_servo
                        entry_seg_t0 = seg_t0
                        entry_cur_phi = cur_phi
                        entry_spans_len = len(excluded_spans)
                        entry_recalc_spans_len = len(recalc_excluded_spans)
                        entry_beats_len = len(piecewise_beats)
                        entry_seg_id = seg_id
                        entry_seg_hits = seg_live_hits.get(seg_id, 0)
                        min_next_cursor = max(min_next_cursor, search_cursor + step_ms, first_lock_t + 0.5 * tau)

                        if trans_start_t is not None:
                            # 双轨解耦：若恢复相位同源且间隙内无持续异速冲击，视为同一主干安静留白：
                            # - 自相关切段轨道 (recalc_excluded_spans) 不切段，保护整段长基线小数位分辨率；
                            # - 覆盖率轨道 (excluded_spans) 在 tg_ms > 0 时照常按 transition_guard 扣除留白拍数。
                            prev_confirmed = (
                                seg_id == 1
                                or seg_live_hits.get(seg_id, 0) >= max(K + 2, 6)
                                or (first_lock_t - trans_start_t) <= float(2 * M) * tau
                            )
                            if (
                                seg_id in seg_phis
                                and prev_confirmed
                                and _is_homologous_phase(locked_phi, eff_ref_phi)
                                and not _gap_has_foreign_rhythm(
                                    trans_start_t + 0.5 * tau, first_lock_t - 0.5 * tau, eff_ref_phi
                                )
                            ):
                                if _is_halfbeat_shifted(locked_phi, eff_ref_phi):
                                    shifted_t = atempo_start_t - half_tau if (atempo_start_t - half_tau >= min_back_t - 1e-9) else (atempo_start_t + half_tau)
                                    if shifted_t < float(N):
                                        atempo_start_t = shifted_t
                                if tg_ms > 0.0:
                                    excluded_spans.append((trans_start_t - tg_ms, atempo_start_t + tg_ms))
                                gap_t = trans_start_t + tau
                                gap_end_t = min(first_lock_t, atempo_start_t) - 0.5 * tau
                                while gap_t < gap_end_t:
                                    piecewise_beats.append((seg_id, gap_t, False, gap_t, 0.0, gap_t - cum_servo))
                                    gap_t += tau
                                k_resume = round((atempo_start_t - seg_t0) / tau)
                                exact_fixed_t = seg_t0 + float(k_resume) * tau
                                cum_servo = atempo_start_t - exact_fixed_t
                                seg_entry_t = atempo_start_t
                                in_steady = True
                                t_exp = atempo_start_t
                                last_hit_t = None
                                continue

                            if tg_ms > 0.0:
                                s_left = trans_start_t - tg_ms
                                s_right = atempo_start_t + tg_ms
                            else:
                                s_left = trans_start_t + edge_pad
                                s_right = atempo_start_t - edge_pad
                                if s_left >= s_right:
                                    mid = 0.5 * (trans_start_t + atempo_start_t)
                                    s_left, s_right = mid - 0.5, mid + 0.5
                            excluded_spans.append((s_left, s_right))
                            recalc_excluded_spans.append((s_left, s_right))
                        elif atempo_start_t >= (float(M) - 0.5) * tau:
                            s_right = (atempo_start_t + tg_ms) if tg_ms > 0.0 else max(0.0, atempo_start_t - edge_pad)
                            if s_right > 0.0:
                                excluded_spans.append((0.0, s_right))
                                recalc_excluded_spans.append((0.0, s_right))
                        else:
                            pre_t = float(locked_phi % tau)
                            if pre_t < 0.0:
                                pre_t += tau
                            pre_end_t = min(first_lock_t, atempo_start_t) - 0.5 * tau
                            while pre_t < pre_end_t:
                                piecewise_beats.append((seg_id + 1, pre_t, False, pre_t, 0.0, pre_t))
                                pre_t += tau

                        seg_id += 1
                        cur_phi = locked_phi
                        seg_phis[seg_id] = cur_phi
                        seg_t0 = atempo_start_t
                        seg_entry_t = atempo_start_t
                        cum_servo = 0.0
                        in_steady = True
                        t_exp = atempo_start_t
                        last_hit_t = None
                    else:
                        if trans_start_t is not None:
                            s_left = (trans_start_t - tg_ms) if tg_ms > 0.0 else (trans_start_t + edge_pad)
                            excluded_spans.append((s_left, float(N)))
                            recalc_excluded_spans.append((s_left, float(N)))
                        elif raw_total_beats > 0:
                            excluded_spans.append((0.0, float(N)))
                            recalc_excluded_spans.append((0.0, float(N)))
                        break

            # 吸收渐快/渐慢过渡段内部偶然短暂命中的伪稳态碎片，合并为完整连续的过渡区间
            seg_beat_times: dict[int, list[float]] = {}
            seg_hit_counts: dict[int, int] = {}
            for b_seg, b_t_exp, b_is_hit, _, _, _ in piecewise_beats:
                seg_beat_times.setdefault(b_seg, []).append(b_t_exp)
                if b_is_hit:
                    seg_hit_counts[b_seg] = seg_hit_counts.get(b_seg, 0) + 1
            if len(seg_beat_times) >= 2:
                min_trunk_hits = max(K + 3, 8)
                min_trunk_span = max(3000.0, float(M + K) * tau)
                valid_trunks = {
                    s_k
                    for s_k, t_list in seg_beat_times.items()
                    if (max(t_list) - min(t_list)) >= min_trunk_span
                    and (
                        seg_hit_counts.get(s_k, 0) >= min_trunk_hits
                        or (
                            tau <= 750.0
                            and (max(t_list) - min(t_list)) >= 12000.0
                            and seg_hit_counts.get(s_k, 0) >= max(K + 2, 5)
                            and float(seg_hit_counts.get(s_k, 0)) * 4.0 * tau >= 0.85 * (max(t_list) - min(t_list))
                        )
                    )
                }
                if valid_trunks:
                    pad_frag = max(tg_ms, 0.5 * tau + edge_pad)
                    for s_k, t_list in seg_beat_times.items():
                        if s_k not in valid_trunks:
                            frag_span = (min(t_list) - pad_frag, max(t_list) + pad_frag)
                            excluded_spans.append(frag_span)
                            recalc_excluded_spans.append(frag_span)

            def _merge_span_list(raw_list: list[tuple[float, float]]) -> list[list[float]]:
                merged_out: list[list[float]] = []
                for s_start, s_end in sorted(raw_list):
                    s_start = max(0.0, s_start)
                    s_end = min(float(N), s_end)
                    if s_start >= s_end:
                        continue
                    if not merged_out:
                        merged_out.append([s_start, s_end])
                    else:
                        prev = merged_out[-1]
                        if s_start <= prev[1]:
                            prev[1] = max(prev[1], s_end)
                        else:
                            merged_out.append([s_start, s_end])
                return merged_out

            merged_spans = _merge_span_list(excluded_spans)
            merged_recalc_spans = _merge_span_list(recalc_excluded_spans)

            def _is_in_relock_spans(t: float) -> bool:
                for s_start, s_end in merged_spans:
                    if s_start <= t <= s_end:
                        return True
                return False

            seg_errors_map: dict[int, list[float]] = {}
            clean_total_beats = 0
            clean_n_matched = 0
            # [只读导出] 记录计入 n_matched 的命中拍 (seg_id, b_t_exp, b_t_fixed)，不参与任何误差计算
            counted_hits_pw: list[tuple[int, float, float]] = []
            for b_seg, b_t_exp, b_is_hit, _, b_err, b_t_fixed in piecewise_beats:
                if not _is_in_relock_spans(b_t_exp):
                    clean_total_beats += 1
                    if b_is_hit:
                        clean_n_matched += 1
                        seg_errors_map.setdefault(b_seg, []).append(b_err)
                        counted_hits_pw.append((b_seg, b_t_exp, b_t_fixed))

            clean_coverage = float(clean_n_matched) / float(clean_total_beats) if clean_total_beats > 0 else 0.0
            raw_coverage = float(raw_n_matched) / float(raw_total_beats) if raw_total_beats > 0 else 0.0
            has_enough_pw = clean_n_matched > 0 and clean_n_matched >= max(3, clean_total_beats // 10)

            seg_means: dict[int, float] = {}
            centered_list: list[float] = []
            if has_enough_pw:
                # 按稳态片段独立一阶矩中心化，消除跨段相位跳变对残差的虚假污染
                primary_seg = max(seg_errors_map.keys(), key=lambda s_k: len(seg_errors_map[s_k]))
                for s_id, errs in seg_errors_map.items():
                    s_arr = np.array(errs, dtype=np.float64)
                    s_mean = float(np.mean(s_arr))
                    seg_means[s_id] = s_mean
                    centered_list.extend((s_arr - s_mean).tolist())
                    if s_id == primary_seg:
                        base_p = seg_phis.get(s_id, best_phi)
                        best_phi = float((base_p + s_mean) % tau)
                        if best_phi >= tau:
                            best_phi -= tau
                        elif best_phi < 0.0:
                            best_phi = 0.0

                # [优化 2026-11-15] 开启相位中心精修时，按各稳态段无伺服基准网格叠加段均值导出等距居中时刻
                if refine_phase_center:
                    exported_hit_times_pw = [
                        max(0.0, min(float(N), float(b_t_fixed) + seg_means.get(b_seg, 0.0)))
                        for b_seg, _, b_t_fixed in counted_hits_pw
                    ]
                else:
                    exported_hit_times_pw = [b_t_exp for _, b_t_exp, _ in counted_hits_pw]
            else:
                exported_hit_times_pw = [b_t_exp for _, b_t_exp, _ in counted_hits_pw]

            if meta is not None:
                meta.update({
                    "n_matched": clean_n_matched,
                    "total_beats": clean_total_beats,
                    "coverage": clean_coverage,
                    "transition_guard_ms": tg_ms,
                    "transition_miss_beats": transition_miss_beats,
                    "transition_resume_beats": transition_resume_beats,
                    "transition_spans": [(round(s, 2), round(e, 2)) for s, e in merged_spans],
                    "_recalc_spans": [(round(s, 2), round(e, 2)) for s, e in merged_recalc_spans],
                    "raw_n_matched": raw_n_matched,
                    "raw_total_beats": raw_total_beats,
                    "raw_coverage": raw_coverage,
                    "comb_tol": eff_win_r,
                    # [只读导出] len(matched_beat_times) == n_matched
                    "matched_beat_times": [round(float(t), 2) for t in exported_hit_times_pw],
                    "matched_beat_tau": float(tau),
                })

            if not has_enough_pw:
                return best_phi, float(w), 1.0

            centered_err = np.array(centered_list, dtype=np.float64)
            rmse = float(np.sqrt(np.mean(centered_err ** 2)))

    # 归一化不稳定度: RMSE / (tau * 0.25) 映射至 [0.0, 1.0]
    # 对微漂移 (如 15~20ms) 输出 0.05~0.15 稳定度，完全契合客观感知
    if not math.isfinite(rmse):
        instability = 1.0
        rmse = float(w)
    else:
        instability = min(1.0, max(0.0, rmse / (tau * 0.25)))

    return best_phi, rmse, instability


def select_best_force_v3(
    diff_1000hz: np.ndarray,
    acf: np.ndarray | None = None,
    meta: dict | None = None,
    octave_norm: bool = False,
    transition_guard: float | int | None = None,
    transition_miss_beats: int = 4,
    transition_resume_beats: int = 3,
    comb_tol: int | None = None,
    trim_recalc: bool | None = None,
) -> tuple[float, float, float, float]:
    """
    Force Fit (V3) 全局梳状网格强行拟合主入口。

    设计规范说明 (依据 2.txt 设计约束)：
    1. 纯客观能量驱动：拒绝一切人耳主观速度先验 (不做对数正态或中心偏置)，
       由全曲梳状网格投影内积能量绝对值最大直接决定周期。
    2. 客观多八度折叠：周期折叠严格遵循 2^x 规则收敛至 [59.99, 300.01] 闭区间（[60.00, 300.00] 留 ±0.01 BPM 余量）。
    3. octave_norm 参数设计：
       为保持与 Malody-BPM-Detector 顶层 API (select_best_tempo / detect_bpm) 的统一签名兼容，
       保留 octave_norm 形参。Force V3 自身严格遵循 2.txt 中“拒绝主观偏置、仅做客观 2^x [59.99, 300.01] 折叠”
       的核心规范，不施加常规击打速度区间 [110, 220] 的主观八度压缩，以保持全局强行拟合的客观性。
    4. comb_tol: 时域脉冲梳节拍匹配窗口半宽 (10~50ms 整数，若为 None 默认为 35ms)。
    5. trim_recalc: 分段相位重锁与剔除变速二次全范围重算开关：
       - None (默认): 当 transition_guard > 0 时自动联动开启，否则关闭；
       - True / False: 显式强制开启或关闭二次重算。

    返回 (bpm, instability, rmse_ms, rel_instability)。
    """
    if trim_recalc is not None and not isinstance(trim_recalc, (bool, np.bool_)):
        raise ValueError(f"trim_recalc 必须为 None 或布尔类型，当前传入: {trim_recalc!r}")
    if comb_tol is not None:
        if not isinstance(comb_tol, int) or isinstance(comb_tol, bool) or not (10 <= comb_tol <= 50):
            raise ValueError(f"comb_tol 必须为 10 到 50 之间的整数，当前传入: {comb_tol!r}")
    tg_positive = False
    if transition_guard is not None:
        if isinstance(transition_guard, (bool, np.bool_)):
            raise ValueError(f"transition_guard 不能为布尔类型，当前传入: {transition_guard!r}")
        elif isinstance(transition_guard, float):
            if not math.isfinite(transition_guard) or not (0.0 <= transition_guard <= 3.0001):
                raise ValueError(f"transition_guard (秒) 必须在 0.0 到 3.0 之间，当前传入: {transition_guard!r}")
            tg_positive = float(transition_guard) > 0.0
        elif isinstance(transition_guard, int):
            if not (0 <= transition_guard <= 3000):
                raise ValueError(f"transition_guard (毫秒) 必须在 0 到 3000 之间，当前传入: {transition_guard!r}")
            tg_positive = int(transition_guard) > 0
        else:
            raise ValueError(f"transition_guard 必须为 None、float (0.0~3.0s) 或 int (0~3000ms)，当前传入: {transition_guard!r}")
    if not isinstance(transition_miss_beats, int) or isinstance(transition_miss_beats, bool) or not (2 <= transition_miss_beats <= 10):
        raise ValueError(f"transition_miss_beats 必须为 2 到 10 之间的整数，当前传入: {transition_miss_beats!r}")
    if not isinstance(transition_resume_beats, int) or isinstance(transition_resume_beats, bool) or not (2 <= transition_resume_beats <= 10):
        raise ValueError(f"transition_resume_beats 必须为 2 到 10 之间的整数，当前传入: {transition_resume_beats!r}")

    eff_trim_recalc = bool(trim_recalc) if trim_recalc is not None else tg_positive

    if meta is not None:
        # 无论本次是否采纳或开启二次精修，入口统一清理上次复用遗留的 acf_clean 脏字段
        meta.pop("acf_clean", None)
        if not eff_trim_recalc:
            for _stale_k in ("trim_recalc", "bpm_recalc_applied", "pass1_bpm"):
                meta.pop(_stale_k, None)

    def _fail_return() -> tuple[float, float, float, float]:
        if meta is not None:
            meta.update({"stage": "fail", "bpm": 0.0, "instab": 1.0, "n_matched": 0, "coverage": 0.0})
            if eff_trim_recalc:
                meta.update({"trim_recalc": True, "bpm_recalc_applied": False, "pass1_bpm": 0.0})
        return 0.0, 1.0, 999.0, 1.0

    if diff_1000hz is None or len(diff_1000hz) < 1000:
        return _fail_return()

    # 平坦底噪与全零防御守卫：无动态起音时拒绝虚假匹配，直接返回客观不稳定度 1.0
    diff_max = float(np.max(diff_1000hz))
    diff_min = float(np.min(diff_1000hz))
    if (
        not math.isfinite(diff_max)
        or not math.isfinite(diff_min)
        or diff_max <= 1e-6
        or (diff_max - diff_min) <= 1e-6
    ):
        return _fail_return()

    # 1. 全局梳状网格能量投影与两级粗精选峰（含边界守卫插值及非 2^x 奇数分频平局消歧）
    subpixel_tau, best_score = _select_refined_comb_tau(diff_1000hz, acf=acf)
    if subpixel_tau <= 0.0 or not math.isfinite(best_score) or best_score <= 0.0:
        return _fail_return()

    # 2. 计算最佳相位偏移与全曲起音网格 RMSE 不稳定度（开启 Force V3 相位中心精修）
    eff_comb_win = comb_tol if comb_tol is not None else 35
    comb_meta: dict = meta if meta is not None else {}
    best_phi, rmse, instab = compute_best_phase_and_rmse(
        diff_1000hz,
        subpixel_tau,
        win_radius=eff_comb_win,
        meta=comb_meta,
        transition_guard=transition_guard,
        transition_miss_beats=transition_miss_beats,
        transition_resume_beats=transition_resume_beats,
        comb_tol=comb_tol,
        allow_phase_relock=eff_trim_recalc,
        refine_phase_center=True,
    )
    recalc_spans = list(comb_meta.pop("_recalc_spans", []))

    raw_bpm = 60000.0 / subpixel_tau if subpixel_tau > 0.0 else 0.0

    # 3. 严格按照 2^x 规则折叠到 [59.99, 300.01] 闭区间内
    final_bpm, fold_scale = fold_bpm_2x_v3_with_scale(raw_bpm)
    pass1_bpm = final_bpm
    bpm_recalc_applied = False

    # 4. 剔除变速跨度后的单次无条件全范围重搜 + 0.01ms 加密亚像素精修（只要 recalc_spans 非空且最长净段 >= 8s）
    if eff_trim_recalc and recalc_spans:
        p1_cov = float(comb_meta.get("coverage", 0.0))
        p1_instab = instab
        acf_clean, longest_seg_ms = compute_segmented_acf(
            diff_1000hz,
            recalc_spans,
            min_seg_ms=4000,
        )
        if acf_clean is not None and longest_seg_ms >= 8000 and len(acf_clean) >= 1000:
            subpixel_tau_2, score_2 = _select_refined_comb_tau(diff_1000hz, acf=acf_clean)
            if subpixel_tau_2 > 0.0 and math.isfinite(subpixel_tau_2) and score_2 > 0.0:
                raw_bpm_2 = 60000.0 / subpixel_tau_2
                final_bpm_2, fold_scale_2 = fold_bpm_2x_v3_with_scale(raw_bpm_2)
                if final_bpm_2 > 0.0:
                    final_bpm_2 = align_bpm_2x_to_reference(final_bpm_2, pass1_bpm)
                    fold_scale_2 = final_bpm_2 / raw_bpm_2 if raw_bpm_2 > 0.0 else 1.0
                    eval_tau_2 = 60000.0 / final_bpm_2
                    comb_meta_2: dict = {}
                    best_phi_2, rmse_2, instab_2 = compute_best_phase_and_rmse(
                        diff_1000hz,
                        eval_tau_2,
                        win_radius=eff_comb_win,
                        meta=comb_meta_2,
                        transition_guard=transition_guard,
                        transition_miss_beats=transition_miss_beats,
                        transition_resume_beats=transition_resume_beats,
                        comb_tol=comb_tol,
                        allow_phase_relock=True,
                        refine_phase_center=True,
                    )
                    comb_meta_2.pop("_recalc_spans", None)
                    n_matched_2 = int(comb_meta_2.get("n_matched", 0))
                    cov_2 = float(comb_meta_2.get("coverage", 0.0))

                    # 尺度无关防劣化门控：只要覆盖率与相对残差不稳定度无明显恶化，即直接采纳干净稳态段重算值
                    no_degrade = (
                        n_matched_2 >= 3
                        and cov_2 >= max(0.05, min(p1_cov * 0.88, p1_cov - 0.02), p1_cov - 0.08)
                        and (
                            instab_2 <= p1_instab + max(0.04, 0.15 * p1_instab)
                            or cov_2 >= p1_cov + 0.05
                        )
                    )
                    if no_degrade:
                        subpixel_tau = subpixel_tau_2
                        best_score = score_2
                        raw_bpm = raw_bpm_2
                        final_bpm = final_bpm_2
                        fold_scale = fold_scale_2
                        best_phi = best_phi_2
                        rmse = rmse_2
                        instab = instab_2
                        comb_meta.update(comb_meta_2)
                        if meta is not None:
                            # [只读导出] 仅在二次精修采纳时导出前 4000ms 净化自相关供前端简图对齐展示
                            meta["acf_clean"] = np.ascontiguousarray(acf_clean[:4000], dtype=np.float32)
                        bpm_recalc_applied = True

    if meta is not None:
        meta.update({
            "stage": "force_v3_global_comb",
            "best_tau": subpixel_tau,
            "best_phi": best_phi,
            "raw_bpm": raw_bpm,
            "bpm": final_bpm,
            "fold_scale": fold_scale,
            "rmse_ms": rmse,
            "instab": instab,
            "rel_inst": instab,
            "score": best_score,
            "comb_tol": eff_comb_win,
        })
        if eff_trim_recalc:
            meta["trim_recalc"] = True
            meta["bpm_recalc_applied"] = bpm_recalc_applied
            meta["pass1_bpm"] = pass1_bpm

    return final_bpm, instab, rmse, instab


