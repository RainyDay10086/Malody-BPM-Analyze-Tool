#!/usr/bin/env python3
"""
Malody BPM 测算工作台 —— UI 层独立变速区域识别子模块

设计原则：
1. 独立位于 UI 同级目录，对 malody_bpm/ 后端核心库保持 100% 零改动，不影响主干 BPM、不稳定度与匹配拍数统计；
2. 针对增强模式（enhanced_v3）与强制线性回归模式（force_v3）共享同一套 2^x 倍频无关的复谐波相量锁定判据：
   将主干 BPM 折叠至标准节拍周期 base_tau ∈ [250, 500) ms，在一维前缀和上以 O(N) 复杂度计算滑动窗对
   k ∈ {0.5, 1, 2, 4} 同源谐波的复相量相干度 |Z_k|，从物理上解决高密度十六分音符/双踩鼓点在固定容差窗内
   随机撞峰导致的“中途真明显变速漏报”问题；
3. 首尾纯延音/余音过滤：触碰曲首或曲末边界的失锁段必须具备显著且前后半段相位自洽的非 2^x 异速周期能量
   （如异速前奏/尾奏）才予保留，自动滤除曲末和弦余音衰减、收尾滚奏乱打与曲首无节拍底噪渐入；
4. 曲中长段失锁保留与保守交界策略：曲中真实异速段与长段失去主干节拍的自由散板/无鼓点间奏一并提示；
   区间边界直接采用滑动窗中心点自然内缩（交界处保守内缩约 1~2 秒），宁少报边缘一两秒也不向外扩边误伤稳态段。
"""

from __future__ import annotations

import math
import numpy as np

from malody_bpm.autocorr import compute_autocorrelation_fft

WINDOW_MS: int = 4500
HOP_MS: int = 250
DEFAULT_MIN_SPAN_MS: float = 3500.0
HEAD_BOUNDARY_GUARD_MS: float = 3000.0
TAIL_BOUNDARY_GUARD_MS: float = 4500.0
MAX_MERGE_GAP_MS: float = 8000.0
FOREIGN_COMB_MIN_SCORE: float = 0.11


def detect_ui_tempo_change_spans(
    diff_1000hz: np.ndarray | None,
    bpm: float,
    min_span_ms: float = DEFAULT_MIN_SPAN_MS,
) -> list[list[float]]:
    """
    基于已提取的 1000 Hz 起音差分包络与主干 BPM，识别用于 UI 卡片展示的变速/长失锁区间（裁剪后时间轴，单位 ms）。

    返回格式: [[start_ms, end_ms], ...]，按时间升序且互不重叠。
    """
    if diff_1000hz is None:
        return []
    N = int(len(diff_1000hz))
    if N < WINDOW_MS + 500 or not math.isfinite(bpm) or bpm <= 0.0:
        return []

    # 1. 将参考周期折叠至标准八度 [250, 500) ms（对应 120~240 BPM），确保 2^x 倍频结果完全一致
    base_tau = 60000.0 / float(bpm)
    while base_tau < 250.0:
        base_tau *= 2.0
    while base_tau >= 500.0:
        base_tau *= 0.5

    raw_arr = np.asarray(diff_1000hz, dtype=np.float64)
    x = np.where(np.isfinite(raw_arr) & (raw_arr > 0.0), raw_arr, 0.0)
    max_x = float(np.max(x)) if len(x) > 0 else 0.0
    if max_x <= 1e-6:
        return []

    # 2. 对单次极端瞬态爆音（如孤立重击镲片）做分位数软限幅，避免压低整窗归一化相干度
    pos_nz = x[x > 0.01 * max_x]
    cap_val = float(np.percentile(pos_nz, 98.0)) * 2.5 if len(pos_nz) > 20 else max_x
    x_cap = np.minimum(x, max(1e-6, cap_val))

    # 3. 构造权重与同源谐波 k ∈ {0.5, 1.0, 2.0, 4.0} 的一维前缀和
    t_arr = np.arange(N, dtype=np.float64)
    cs_w = np.concatenate(([0.0], np.cumsum(x_cap)))

    harmonics = (0.5, 1.0, 2.0, 4.0)
    weights_h = {0.5: 0.90, 1.0: 1.00, 2.0: 0.96, 4.0: 0.82}
    cs_re: dict[float, np.ndarray] = {}
    cs_im: dict[float, np.ndarray] = {}
    for k in harmonics:
        ang = (2.0 * math.pi * k / base_tau) * t_arr
        cs_re[k] = np.concatenate(([0.0], np.cumsum(x_cap * np.cos(ang))))
        cs_im[k] = np.concatenate(([0.0], np.cumsum(x_cap * np.sin(ang))))

    starts = np.arange(0, N - WINDOW_MS + 1, HOP_MS, dtype=np.int64)
    if len(starts) == 0:
        return []
    if starts[-1] < N - WINDOW_MS:
        starts = np.append(starts, N - WINDOW_MS)
    ends = starts + WINDOW_MS
    centers = 0.5 * (starts.astype(np.float64) + ends.astype(np.float64))

    w_sum = cs_w[ends] - cs_w[starts]
    max_w_sum = float(np.max(w_sum))
    if max_w_sum <= 1e-9:
        return []
    active_w = w_sum[w_sum > 0.02 * max_w_sum]
    ref_w_sum = float(np.percentile(active_w, 60.0)) if len(active_w) > 0 else max_w_sum
    denom = np.maximum(w_sum, 0.08 * ref_w_sum)

    lock_score = np.zeros(len(starts), dtype=np.float64)
    primary_lock_score = np.zeros(len(starts), dtype=np.float64)
    for k in harmonics:
        zr = (cs_re[k][ends] - cs_re[k][starts]) / denom
        zi = (cs_im[k][ends] - cs_im[k][starts]) / denom
        mag = np.hypot(zr, zi) * weights_h[k]
        lock_score = np.maximum(lock_score, mag)
        if k <= 2.0:
            primary_lock_score = np.maximum(primary_lock_score, mag)

    # 自适应失锁门限与主拍稳态确认门限
    p65_lock = float(np.percentile(lock_score, 65.0))
    p65_prim = float(np.percentile(primary_lock_score, 65.0))
    unlock_thr = min(0.155, max(0.105, 0.42 * p65_lock))
    steady_prim_thr = max(unlock_thr + 0.05, 0.72 * p65_prim)

    raw_mask = lock_score < unlock_thr
    if not np.any(raw_mask):
        return []

    # 桥接失锁段内部短暂的鼓花过门波动（若间隔 <= 8s 且未在主拍谐波 k<=2 上重建稳态锁定则视为同一连续失锁段）
    bridged = raw_mask.copy()
    max_bridge_hops = int(round(MAX_MERGE_GAP_MS / float(HOP_MS)))
    idx_true = np.flatnonzero(bridged)
    if len(idx_true) >= 2:
        for i in range(len(idx_true) - 1):
            a, b = int(idx_true[i]), int(idx_true[i + 1])
            gap_hops = b - a - 1
            if 1 <= gap_hops <= max_bridge_hops:
                gap_all = lock_score[a + 1 : b]
                gap_prim = primary_lock_score[a + 1 : b]
                if gap_hops <= 8 and float(np.max(gap_all)) < unlock_thr + 0.06:
                    bridged[a + 1 : b] = True
                elif float(np.mean(gap_prim)) < steady_prim_thr:
                    bridged[a + 1 : b] = True

    cand_clusters: list[tuple[int, int]] = []
    in_c = False
    c0 = 0
    for i, flag in enumerate(bridged):
        if flag and not in_c:
            in_c = True
            c0 = i
        elif not flag and in_c:
            in_c = False
            cand_clusters.append((c0, i - 1))
    if in_c:
        cand_clusters.append((c0, len(bridged) - 1))

    def _interval_lock(t0_ms: int, t1_ms: int, max_harmonic: float = 4.0) -> float:
        a = max(0, min(N, t0_ms))
        b = max(0, min(N, t1_ms))
        if b - a < 400:
            return 0.0
        ws = cs_w[b] - cs_w[a]
        dn = max(ws, 0.08 * ref_w_sum * (float(b - a) / float(WINDOW_MS)))
        best_m = 0.0
        for k in harmonics:
            if k > max_harmonic:
                continue
            zr = (cs_re[k][b] - cs_re[k][a]) / dn
            zi = (cs_im[k][b] - cs_im[k][a]) / dn
            best_m = max(best_m, math.hypot(zr, zi) * weights_h[k])
        return best_m

    def _foreign_tempo_strength(seg_arr: np.ndarray) -> float:
        """
        复核候选片段是否具备明确的非 2^x 同源周期性节拍：
        用于区分「真实异速前奏/尾奏」与「曲末和弦余音衰减 / 收尾自由滚奏 / 曲首底噪渐入」。
        """
        L_s = len(seg_arr)
        if L_s < 2400:
            return 0.0
        pk_thr = max(0.03 * max_x, 0.18 * (ref_w_sum / float(WINDOW_MS)) * 6.0)
        mid_v = seg_arr[1:-1]
        cand_p = np.flatnonzero((mid_v >= pk_thr) & (mid_v >= seg_arr[:-2]) & (mid_v >= seg_arr[2:])) + 1
        if len(cand_p) < 5:
            return 0.0

        pks = 1
        last_i = int(cand_p[0])
        for ci in cand_p[1:]:
            ci_int = int(ci)
            if ci_int - last_i >= 140:
                pks += 1
                last_i = ci_int
                if pks >= 5:
                    break
        if pks < 5:
            return 0.0

        acf = compute_autocorrelation_fft(seg_arr.astype(np.float32), exact=False)
        acf0 = max(1e-12, float(acf[0]))
        L_a = len(acf)
        upper_tau = min(1001, L_a // 3)
        if upper_tau <= 183:
            return 0.0
        taus = np.arange(180, upper_tau, 3, dtype=np.float64)
        fmask = np.ones(len(taus), dtype=bool)
        for kk in (-2, -1, 0, 1, 2, 3):
            tk = base_tau * (2.0 ** kk)
            fmask &= (np.abs(taus - tk) > 0.045 * tk)
        ftaus = taus[fmask]
        if len(ftaus) == 0:
            return 0.0

        lp = np.vstack([np.round(m * ftaus).astype(np.int64) for m in range(1, 5)])
        lm = np.vstack([np.round((m - 0.5) * ftaus).astype(np.int64) for m in range(1, 5)])
        valid = lp < L_a
        lpc = np.clip(lp, 0, L_a - 1)
        lmc = np.clip(lm, 0, L_a - 1)
        dmat = np.where(valid, (acf[lpc] - acf[lmc]) / acf0, 0.0)
        scs = np.sum(dmat, axis=0) / np.maximum(1, np.sum(valid, axis=0))
        bi = int(np.argmax(scs))
        best_sc = float(scs[bi])
        best_t = float(ftaus[bi])

        # 前后半段异速相位一致性校验：过滤曲末无规律滚奏乱打造成的偶然伪峰
        if best_sc >= 0.09 and best_t > 0.0:
            mid = L_s // 2
            ta = np.arange(L_s, dtype=np.float64)
            z_h1 = np.sum(seg_arr[:mid] * np.exp(-1j * 2.0 * math.pi * ta[:mid] / best_t)) / (np.sum(seg_arr[:mid]) + 1e-9)
            z_h2 = np.sum(seg_arr[mid:] * np.exp(-1j * 2.0 * math.pi * ta[mid:] / best_t)) / (np.sum(seg_arr[mid:]) + 1e-9)
            cos_diff = float(np.real(z_h1 * np.conj(z_h2))) / max(1e-9, abs(z_h1) * abs(z_h2))
            if min(abs(z_h1), abs(z_h2)) < 0.10 or cos_diff < 0.35:
                best_sc *= 0.45
        return best_sc

    accepted: list[list[float]] = []
    n_wins = len(starts)

    for idx0, idx1 in cand_clusters:
        # 保守边界：直接使用滑动窗中心点（天然向变速段内部内缩约 1~2s，避免向外扩边误伤两侧稳态段）
        s_ms = float(centers[idx0])
        e_ms = float(centers[idx1])

        # 若首窗/末窗本身已失锁，则检查最外侧 2.5s 边缘块是否同样失锁以决定是否贴合端点
        if idx0 == 0 and _interval_lock(0, min(N, 2500)) < unlock_thr * 1.05:
            s_ms = 0.0
        if idx1 == n_wins - 1 and _interval_lock(max(0, N - 2500), N) < unlock_thr * 1.05:
            e_ms = float(N)

        if (e_ms - s_ms) < min_span_ms:
            continue

        touches_head = (s_ms <= HEAD_BOUNDARY_GUARD_MS)
        touches_tail = (e_ms >= float(N) - TAIL_BOUNDARY_GUARD_MS)

        if touches_head or touches_tail:
            # 用包含完整窗口覆盖范围的切片检验是否存在稳定异速节拍
            eval_0 = 0 if touches_head else int(starts[idx0])
            eval_1 = N if touches_tail else int(ends[idx1])
            f_sc = _foreign_tempo_strength(x_cap[eval_0:eval_1])
            if f_sc < FOREIGN_COMB_MIN_SCORE:
                continue
            if touches_head:
                s_ms = 0.0

        if not accepted:
            accepted.append([s_ms, e_ms])
        else:
            gap_ms = s_ms - accepted[-1][1]
            if gap_ms <= MAX_MERGE_GAP_MS:
                g0, g1 = int(round(accepted[-1][1])), int(round(s_ms))
                if gap_ms <= 2500.0 or _interval_lock(g0, g1, max_harmonic=2.0) < steady_prim_thr:
                    accepted[-1][1] = max(accepted[-1][1], e_ms)
                    continue
            accepted.append([s_ms, e_ms])

    return [[round(a, 2), round(b, 2)] for a, b in accepted]
