"""
Malody BPM Detector - Enhanced V3 独立后置小数位精修子模块 (Post-Selection Slope Refiner)

职责边界（严格与候选初筛、220 BPM 亚谐波、3:2 三连音消歧及多主干 Pass-2 联合 OLS 隔离）：
1. 仅在 Enhanced V3 已锁定最终主干候选，且未触发 Pass-2 重算或 Pass-2 实际采纳为唯一单主干时后置介入；
2. 治理全曲/单净主干长程自相关因单阶偶发弱峰早停导致的长基线浪费（要求斜率标准误严格改善 >=15%，且对已桥接链或 2x/4x 等效长链防反向覆盖）；
3. 零开销残差预检（常速曲直接短路跳过，0 额外 FFT 与 bit 级不变）+ 前后不重叠分段独立 ACF
   同斜率共识联合回归，消除中途短桥段/微错相（3~8ms）混入全局自相关造成的过原点斜率弯曲；
4. 硬护栏死锁修正幅度 <= ±0.25% 且严禁翻转 [59.99, 300.01] 八度归属，任何门控不满足立即回退原解。
"""

import math
import numpy as np
from typing import Optional, Dict, Any, Tuple, List, Sequence

from .autocorr import compute_autocorrelation_fft
from .force_v3 import V3_BPM_MIN, V3_BPM_MAX, fold_bpm_2x_v3


def _ols_safeguarded(
    k_vals: Sequence[int | float],
    t_vals: Sequence[float],
) -> Tuple[float, float, float, float, float]:
    """
    过原点最小二乘回归（带柯西-施瓦茨下溢防护），额外返回 S_kk 供斜率标准误评估。
    返回 (raw_bpm, raw_instab, sigma_t, rel_instab, S_kk)。
    """
    n = len(k_vals)
    if n < 3 or len(t_vals) != n:
        return 0.0, 0.0, 999.0, 1.0, 0.0

    s_kk = float(sum(float(k) * float(k) for k in k_vals))
    s_kt = float(sum(float(k) * float(t) for k, t in zip(k_vals, t_vals)))
    s_tt = float(sum(float(t) * float(t) for t in t_vals))

    if (
        not math.isfinite(s_kk)
        or not math.isfinite(s_kt)
        or not math.isfinite(s_tt)
        or s_kk <= 0.0
        or s_kt <= 0.0
    ):
        return 0.0, 0.0, 999.0, 1.0, 0.0

    raw_bpm = (60000.0 * s_kk) / s_kt
    if not math.isfinite(raw_bpm) or raw_bpm <= 0.0:
        return 0.0, 0.0, 999.0, 1.0, 0.0

    term = (((s_kk * s_tt) / s_kt) / s_kt - 1.0) / (float(n) - 1.0) * float(n)
    if not math.isfinite(term) or term < 0.0:
        term = 0.0
    rel_instab = math.sqrt(term)
    raw_instab = rel_instab * raw_bpm

    var_t = (s_tt - (s_kt * s_kt) / s_kk) / (float(n) - 1.0)
    if not math.isfinite(var_t) or var_t < 0.0:
        var_t = 0.0
    sigma_t = math.sqrt(var_t)

    if not math.isfinite(raw_instab) or raw_instab < 0.0:
        raw_instab = 0.0
    if not math.isfinite(sigma_t) or sigma_t < 0.0:
        sigma_t = 0.0
    if not math.isfinite(rel_instab) or rel_instab < 0.0:
        rel_instab = 0.0

    return raw_bpm, raw_instab, sigma_t, rel_instab, s_kk


def _track_with_single_gap_guard(
    acf_arr: np.ndarray,
    max_lag: int,
    start_tau: float,
    tol: int = 8,
) -> Tuple[
    float,
    float,
    float,
    float,
    float,
    List[int],
    List[float],
    int,
    bool,
    bool,
]:
    """
    先执行严格零断链谐波追踪，记录是否因中途失配而提前终止 (stopped_early)；
    若提前终止且严格段已满 3 阶，再尝试跨越单阶偶发弱峰（连续 2 阶失配立即终止，
    跨断口后需连续命中 >= 3 阶且峰高 >= 严格段中位峰高的 10%，且最终标准误严格不劣于严格段）。
    返回 (bpm, instab, sigma_t, rel_inst, s_kk, k_out, t_out, n_strict, stopped_early, did_bridge)。
    """
    limit = min(int(max_lag), len(acf_arr))
    if limit <= 0 or not math.isfinite(start_tau) or start_tau <= 0.0:
        return 0.0, 0.0, 999.0, 1.0, 0.0, [], [], 0, False, False

    acf_0 = float(acf_arr[0])
    zero_floor = (5e-6 * acf_0) if (math.isfinite(acf_0) and acf_0 > 0.0) else 0.0

    win = int(tol) + 2
    cur_tau = float(start_tau)
    target = int(round(cur_tau))
    if target < 0:
        return 0.0, 0.0, 999.0, 1.0, 0.0, [], [], 0, False, False

    s_kk = 0.0
    s_kt = 0.0
    k_vals: List[int] = []
    t_vals: List[float] = []
    strict_peaks: List[float] = []
    k = 1
    stopped_early = False

    while target + win < limit:
        base = max(0, target - win)
        local_max_idx = base
        max_val = float(acf_arr[base])
        for idx in range(base + 1, target + win + 1):
            val = float(acf_arr[idx])
            if val > max_val:
                max_val = val
                local_max_idx = idx

        if max_val > zero_floor and abs(local_max_idx - target) <= tol:
            c = local_max_idx
            ym1 = float(acf_arr[c - 1]) if c > 0 else 0.0
            y0_d = float(acf_arr[c])
            yp1 = float(acf_arr[c + 1]) if c + 1 < limit else 0.0
            denom = (y0_d - ym1) + (y0_d - yp1)
            delta = 0.5 * (yp1 - ym1) / denom if denom > 1e-9 else 0.0
            if not math.isfinite(delta) or abs(delta) > 1.0:
                delta = 0.0
            exact_t = float(c) + delta
            if not math.isfinite(exact_t):
                return 0.0, 0.0, 999.0, 1.0, 0.0, [], [], 0, False, False
            k_vals.append(k)
            t_vals.append(exact_t)
            strict_peaks.append(max_val)
            kf = float(k)
            s_kk += kf * kf
            s_kt += kf * exact_t
            cur_tau = s_kt / s_kk
            next_t = exact_t + cur_tau
            if not math.isfinite(next_t):
                break
            target = int(round(next_t))
            if target < 0:
                break
        else:
            stopped_early = True
            break
        k += 1

    n_strict = len(k_vals)
    if n_strict < 3:
        return 0.0, 0.0, 999.0, 1.0, 0.0, [], [], n_strict, stopped_early, False

    b_strict, i_strict, s_strict, r_strict, skk_strict = _ols_safeguarded(k_vals, t_vals)
    if b_strict <= 0.0:
        return 0.0, 0.0, 999.0, 1.0, 0.0, [], [], 0, stopped_early, False

    if not stopped_early:
        return (
            b_strict,
            i_strict,
            s_strict,
            r_strict,
            skk_strict,
            k_vals,
            t_vals,
            n_strict,
            False,
            False,
        )

    ref_peak = float(np.median(strict_peaks)) if strict_peaks else 0.0
    min_reconnect_amp = max(1e-9, zero_floor, 0.10 * ref_peak)
    commit_len = n_strict
    consec_miss = 1
    in_uncommitted_bridge = True
    post_gap_hits = 0

    k += 1
    next_t = float(k) * cur_tau
    target = int(round(next_t)) if math.isfinite(next_t) else -1

    while target >= 0 and target + win < limit:
        base = max(0, target - win)
        local_max_idx = base
        max_val = float(acf_arr[base])
        for idx in range(base + 1, target + win + 1):
            val = float(acf_arr[idx])
            if val > max_val:
                max_val = val
                local_max_idx = idx

        amp_ok = (max_val >= min_reconnect_amp) if in_uncommitted_bridge else (max_val > zero_floor)
        is_hit = False
        exact_t = 0.0
        if amp_ok and max_val > zero_floor and abs(local_max_idx - target) <= tol:
            c = local_max_idx
            ym1 = float(acf_arr[c - 1]) if c > 0 else 0.0
            y0_d = float(acf_arr[c])
            yp1 = float(acf_arr[c + 1]) if c + 1 < limit else 0.0
            denom = (y0_d - ym1) + (y0_d - yp1)
            delta = 0.5 * (yp1 - ym1) / denom if denom > 1e-9 else 0.0
            if not math.isfinite(delta) or abs(delta) > 1.0:
                delta = 0.0
            cand_t = float(c) + delta
            if math.isfinite(cand_t) and abs(cand_t - float(k) * cur_tau) <= float(tol) + 1.5:
                is_hit = True
                exact_t = cand_t

        if is_hit:
            k_vals.append(k)
            t_vals.append(exact_t)
            kf = float(k)
            s_kk += kf * kf
            s_kt += kf * exact_t
            cur_tau = s_kt / s_kk
            consec_miss = 0
            if in_uncommitted_bridge:
                post_gap_hits += 1
                if post_gap_hits >= 3:
                    commit_len = len(k_vals)
                    in_uncommitted_bridge = False
                    post_gap_hits = 0
            else:
                commit_len = len(k_vals)

            next_t = exact_t + cur_tau
            target = int(round(next_t)) if math.isfinite(next_t) else -1
            k += 1
        else:
            if consec_miss >= 1 or in_uncommitted_bridge:
                break
            consec_miss = 1
            in_uncommitted_bridge = True
            post_gap_hits = 0
            k += 1
            next_t = float(k) * cur_tau
            target = int(round(next_t)) if math.isfinite(next_t) else -1

    if commit_len > n_strict:
        k_comm = k_vals[:commit_len]
        t_comm = t_vals[:commit_len]
        b_ext, i_ext, s_ext, r_ext, skk_ext = _ols_safeguarded(k_comm, t_comm)
        tau_strict = 60000.0 / b_strict
        tau_ext = (60000.0 / b_ext) if b_ext > 0.0 else 0.0
        se_strict = max(s_strict, 0.20) / math.sqrt(skk_strict) if skk_strict > 0.0 else 999.0
        se_ext = max(s_ext, 0.20) / math.sqrt(skk_ext) if skk_ext > 0.0 else 999.0
        if (
            b_ext > 0.0
            and 0.99 * tau_strict <= tau_ext <= 1.01 * tau_strict
            and s_ext <= max(s_strict * 1.8, s_strict + 0.8)
            and se_ext <= se_strict
            and r_ext <= max(r_strict * 1.05, 0.00025)
        ):
            return (
                b_ext,
                i_ext,
                s_ext,
                r_ext,
                skk_ext,
                k_comm,
                t_comm,
                n_strict,
                True,
                True,
            )

    k_base = k_vals[:n_strict]
    t_base = t_vals[:n_strict]
    return (
        b_strict,
        i_strict,
        s_strict,
        r_strict,
        skk_strict,
        k_base,
        t_base,
        n_strict,
        True,
        False,
    )


def _has_systematic_residual_drift(
    k_vals: Sequence[int | float],
    t_vals: Sequence[float],
    tau_est: float,
) -> bool:
    """
    零开销谐波残差预检（不执行任何额外 FFT）：
    检验长程自相关谐波序列是否存在因中途跨段微错相（3~8ms）混入而引发的系统性阶跃、凸曲率或非零截距。
    常规稳态曲目残差平坦（通常 <0.3ms），在此处直接短路返回 False。
    """
    n = len(k_vals)
    if n < 12 or len(t_vals) != n or not math.isfinite(tau_est) or tau_est <= 0.0:
        return False

    k_arr = np.asarray(k_vals, dtype=np.float64)
    t_arr = np.asarray(t_vals, dtype=np.float64)
    errs = t_arr - k_arr * float(tau_est)
    max_abs_e = float(np.max(np.abs(errs)))

    # 1. 前段低滞后同相基准斜率对尾段的外推偏移量
    m_head = max(6, min(30, n // 4))
    m_tail = max(4, n // 5)
    k_head = k_arr[:m_head]
    t_head = t_arr[:m_head]
    skk_h = float(np.dot(k_head, k_head))
    tail_drift = 0.0
    if skk_h > 0.0:
        tau_head = float(np.dot(k_head, t_head)) / skk_h
        tail_drift = abs(float(np.mean(t_arr[-m_tail:] - k_arr[-m_tail:] * tau_head)))

    # 2. 带截距线性拟合 t = a + b * k 的全局截距偏离度
    k_mean = float(np.mean(k_arr))
    t_mean = float(np.mean(t_arr))
    k_cent = k_arr - k_mean
    denom_var = float(np.dot(k_cent, k_cent))
    abs_intercept = 0.0
    if denom_var > 1e-9:
        slope_int = float(np.dot(k_cent, t_arr - t_mean)) / denom_var
        abs_intercept = abs(t_mean - slope_int * k_mean)

    return bool(max_abs_e >= 0.90 or tail_drift >= 1.50 or abs_intercept >= 1.20)


def _solve_disjoint_partition_ols(
    diff_1000hz: np.ndarray,
    n_parts: int,
    seed_tau: float,
    ref_raw_bpm: float,
    tol: int,
    max_rel_adjust: float,
) -> Optional[Dict[str, Any]]:
    """
    将时域包络切分为 n_parts 个严格不重叠的连续长宏段（每段 >= 15s），
    各自独立计算自相关并追踪谐波链，彻底物理隔绝跨桥段互相关项：
    - 要求至少 2 个独立宏段成链且内部平坦 (sigma_t <= 0.85ms)；
    - 要求各入围宏段独立斜率高度一致 (极差 <= 0.08%)，严格排除真人渐快/渐慢曲目；
    - 通过共识检验后执行多段过原点联合 OLS。
    """
    n_total = len(diff_1000hz)
    if n_parts < 2 or n_total < n_parts * 15000:
        return None

    clean_chunks: List[Tuple[float, float, List[float], List[float]]] = []
    for idx in range(n_parts):
        s_i = (idx * n_total) // n_parts
        e_i = ((idx + 1) * n_total) // n_parts
        if e_i - s_i < 15000:
            continue
        sub_diff = diff_1000hz[s_i:e_i]
        acf_sub = compute_autocorrelation_fft(sub_diff, exact=False)
        if len(acf_sub) <= 32:
            continue

        b_c, _, s_c, _, _, k_c, t_c, _, _, _ = _track_with_single_gap_guard(
            acf_sub, len(acf_sub), seed_tau, tol=tol
        )
        if (
            len(k_c) >= 8
            and b_c > 0.0
            and s_c <= 0.85
            and abs(b_c - ref_raw_bpm) <= max_rel_adjust * ref_raw_bpm
        ):
            clean_chunks.append((b_c, s_c, [float(kv) for kv in k_c], t_c))
            continue

        # 兼容某一段纯 Half-time (2x 周期) 稀疏编曲：折算等效阶数 kappa = 2 * k
        if seed_tau <= 1250.0:
            b_2, _, s_2, _, _, k_2, t_2, _, _, _ = _track_with_single_gap_guard(
                acf_sub, len(acf_sub), 2.0 * seed_tau, tol=tol
            )
            b_2_eq = b_2 * 2.0
            if (
                len(k_2) >= 5
                and b_2_eq > 0.0
                and s_2 <= 0.85
                and abs(b_2_eq - ref_raw_bpm) <= max_rel_adjust * ref_raw_bpm
            ):
                clean_chunks.append((b_2_eq, s_2, [2.0 * float(kv) for kv in k_2], t_2))

    if len(clean_chunks) < 2:
        return None

    bpms = [c[0] for c in clean_chunks]
    mean_bpm = float(np.mean(bpms))
    if mean_bpm <= 0.0 or (max(bpms) - min(bpms)) > 0.0008 * mean_bpm:
        return None

    joint_k: List[float] = []
    joint_t: List[float] = []
    for _, _, k_c, t_c in clean_chunks:
        joint_k.extend(k_c)
        joint_t.extend(t_c)

    b_j, i_j, s_j, r_j, skk_j = _ols_safeguarded(joint_k, joint_t)
    if b_j <= 0.0 or s_j > 0.85 or skk_j <= 0.0:
        return None
    if abs(b_j - ref_raw_bpm) > max_rel_adjust * ref_raw_bpm:
        return None

    se_j = max(s_j, 0.08) / math.sqrt(skk_j)
    return {
        "raw_bpm": b_j,
        "raw_instab": i_j,
        "sigma_t": s_j,
        "rel_inst": r_j,
        "n_m": int(round(len(joint_k))),
        "s_kk": skk_j,
        "se": se_j,
    }


def _try_clean_prefix_de_skew(
    k_vals: Sequence[int],
    t_vals: Sequence[float],
    ref_raw_bpm: float,
    full_sigma: float,
    max_rel_adjust: float,
) -> Optional[Dict[str, Any]]:
    """
    纯 ACF 无时域包络输入 (diff_1000hz is None) 时的保守兜底消偏：
    仅当前段前缀 (>= 15 阶) 极度平坦 (sigma <= 0.45ms)，且尾段带截距局部斜率与前缀斜率
    高度一致 (<= 0.08%) 但存在 >= 2.5ms 的常数平移阶跃时，回退采纳纯净同相前缀。
    """
    n = len(k_vals)
    if n < 30:
        return None

    k_arr = np.asarray(k_vals, dtype=np.float64)
    t_arr = np.asarray(t_vals, dtype=np.float64)

    best_pref_len = 0
    for m in range(15, (2 * n) // 3 + 1):
        _, _, s_m, _, skk_m = _ols_safeguarded(k_vals[:m], t_vals[:m])
        if skk_m <= 0.0 or s_m > 0.45:
            break
        tau_m = float(np.dot(k_arr[:m], t_arr[:m])) / skk_m
        recent_mean = abs(float(np.mean(t_arr[m - 4 : m] - k_arr[m - 4 : m] * tau_m)))
        if recent_mean > 0.75:
            break
        best_pref_len = m

    if best_pref_len < 15:
        return None

    m_core = max(15, (best_pref_len * 2) // 3)
    _, _, s_core, _, skk_core = _ols_safeguarded(k_vals[:m_core], t_vals[:m_core])
    if skk_core > 0.0:
        tau_core = float(np.dot(k_arr[:m_core], t_arr[:m_core])) / skk_core
        knee_tol = max(0.20, 2.5 * s_core)
        while (
            best_pref_len > m_core
            and abs(float(t_arr[best_pref_len - 1] - k_arr[best_pref_len - 1] * tau_core)) > knee_tol
        ):
            best_pref_len -= 1

    b_p, i_p, s_p, r_p, skk_p = _ols_safeguarded(k_vals[:best_pref_len], t_vals[:best_pref_len])
    if b_p <= 0.0 or skk_p <= 0.0 or s_p >= full_sigma * 0.60:
        return None
    if abs(b_p - ref_raw_bpm) > max_rel_adjust * ref_raw_bpm:
        return None

    m_tail = max(10, n // 4)
    if n - m_tail <= best_pref_len:
        return None
    k_t = k_arr[-m_tail:]
    t_t = t_arr[-m_tail:]
    kc = k_t - float(np.mean(k_t))
    denom = float(np.dot(kc, kc))
    if denom <= 1e-9:
        return None
    tau_tail_local = float(np.dot(kc, t_t - float(np.mean(t_t)))) / denom
    tau_pref = 60000.0 / b_p
    if abs(tau_tail_local - tau_pref) > 0.0008 * tau_pref:
        return None

    tail_step = abs(float(np.mean(t_t - k_t * tau_pref)))
    if tail_step < 2.50:
        return None

    return {
        "raw_bpm": b_p,
        "raw_instab": i_p,
        "sigma_t": s_p,
        "rel_inst": r_p,
        "n_m": best_pref_len,
    }


def refine_post_v3_slope(
    acf: Optional[np.ndarray],
    diff_1000hz: Optional[np.ndarray],
    acf_pooled_len: int,
    sel_tau: float,
    raw_bpm: float,
    raw_instab: float,
    sigma_t: float,
    rel_inst: float,
    n_m: int,
    used_full_acf: bool,
    final_bpm: float,
    track_tol: int = 8,
    max_rel_adjust: float = 0.0025,
) -> Optional[Dict[str, Any]]:
    """
    Enhanced V3 选定主干后的独立小数位精修入口。
    若无需修正或未通过严苛防劣化门控，返回 None（调用方保持原值 bit 级不变）。
    """
    if (
        acf is None
        or len(acf) <= max(32, int(acf_pooled_len))
        or not math.isfinite(raw_bpm)
        or raw_bpm <= 0.0
        or not math.isfinite(final_bpm)
        or final_bpm <= 0.0
        or not math.isfinite(float(n_m))
        or n_m < 3
        or not math.isfinite(float(sigma_t))
        or float(sigma_t) < 0.0
        or not math.isfinite(float(rel_inst))
        or float(rel_inst) < 0.0
    ):
        return None

    base_tau = 60000.0 / float(raw_bpm)
    if math.isfinite(sel_tau) and sel_tau > 0.0 and abs(float(sel_tau) - base_tau) <= 0.05 * base_tau:
        seed_tau = float(sel_tau)
    else:
        seed_tau = base_tau

    orig_fold_scale = float(final_bpm) / float(raw_bpm)

    def _to_guarded_final_bpm(cand_raw_bpm: float) -> Optional[float]:
        if not math.isfinite(cand_raw_bpm) or cand_raw_bpm <= 0.0:
            return None
        if abs(cand_raw_bpm - raw_bpm) > max_rel_adjust * raw_bpm:
            return None
        scaled = cand_raw_bpm * orig_fold_scale
        if V3_BPM_MIN <= scaled <= V3_BPM_MAX:
            cand_fin = scaled
        else:
            cand_fin = fold_bpm_2x_v3(cand_raw_bpm)
        if not (V3_BPM_MIN <= cand_fin <= V3_BPM_MAX):
            return None
        if abs(cand_fin - final_bpm) > max_rel_adjust * final_bpm:
            return None
        return float(cand_fin)

    # 1. 在全曲长程自相关上执行带单阶断口守卫的追踪
    (
        b_w,
        i_w,
        s_w,
        r_w,
        skk_w,
        k_w,
        t_w,
        n_strict,
        stopped_early,
        did_bridge,
    ) = _track_with_single_gap_guard(acf, len(acf), seed_tau, tol=track_tol)

    if len(k_w) < 3 or b_w <= 0.0 or skk_w <= 0.0:
        return None

    # 评估 Stage A（抗单阶偶发断口长程延伸）是否有效成立
    stage_a_cand: Optional[Dict[str, Any]] = None
    eff_base_n = max(n_strict, int(n_m)) if used_full_acf else int(n_m)
    req_stage_a_len = (eff_base_n + 3) if used_full_acf else max(n_strict + 3, eff_base_n)
    if stopped_early and did_bridge and len(k_w) >= req_stage_a_len:
        skk_base = float(eff_base_n) * float(eff_base_n + 1) * float(2 * eff_base_n + 1) / 6.0
        se_base = max(float(sigma_t), 0.20) / math.sqrt(skk_base) if skk_base > 0.0 else 999.0
        se_w = max(s_w, 0.20) / math.sqrt(skk_w)
        fin_w = _to_guarded_final_bpm(b_w)
        if (
            fin_w is not None
            and se_w <= 0.85 * se_base
            and s_w <= min(1.8, max(float(sigma_t) * 1.6, float(sigma_t) + 0.6))
            and r_w <= max(float(rel_inst) * 1.05, 0.00025)
        ):
            stage_a_cand = {
                "raw_bpm": b_w,
                "raw_instab": i_w,
                "sigma_t": s_w,
                "rel_inst": r_w,
                "n_m": len(k_w),
                "final_bpm": fin_w,
                "reason": "single_gap_bridge",
            }

    # 2. 零开销残差漂移预检：若无系统性阶跃/曲率，直接短路返回（常速曲 0 额外 FFT、原解 bit 级不变）
    tau_w = 60000.0 / b_w
    if not _has_systematic_residual_drift(k_w, t_w, tau_w):
        return stage_a_cand

    # 3. 命中残差漂移预检：执行前后不重叠分段独立 ACF 共识联合 OLS 消偏
    best_de_skew: Optional[Dict[str, Any]] = None
    if diff_1000hz is not None and len(diff_1000hz) >= 30000:
        part_candidates: List[Dict[str, Any]] = []
        for n_parts in (2, 3, 4):
            p_res = _solve_disjoint_partition_ols(
                diff_1000hz,
                n_parts=n_parts,
                seed_tau=seed_tau,
                ref_raw_bpm=raw_bpm,
                tol=track_tol,
                max_rel_adjust=max_rel_adjust,
            )
            if p_res is not None:
                part_candidates.append(p_res)

        if part_candidates:
            best_part = min(part_candidates, key=lambda d: float(d["se"]))
            fin_ds = _to_guarded_final_bpm(best_part["raw_bpm"])
            if stage_a_cand is not None:
                ref_sigma = s_w
            elif used_full_acf:
                ref_sigma = min(s_w, float(sigma_t)) if len(k_w) < int(n_m) else s_w
            else:
                ref_sigma = float(sigma_t)
            if (
                fin_ds is not None
                and best_part["sigma_t"] <= min(0.85, ref_sigma * 0.75)
                and abs(best_part["raw_bpm"] - b_w) >= 0.00002 * b_w
            ):
                best_de_skew = {
                    "raw_bpm": best_part["raw_bpm"],
                    "raw_instab": best_part["raw_instab"],
                    "sigma_t": best_part["sigma_t"],
                    "rel_inst": best_part["rel_inst"],
                    "n_m": best_part["n_m"],
                    "final_bpm": fin_ds,
                    "reason": "disjoint_acf_deskew",
                }
    elif diff_1000hz is None:
        pref_res = _try_clean_prefix_de_skew(
            k_w,
            t_w,
            ref_raw_bpm=raw_bpm,
            full_sigma=s_w,
            max_rel_adjust=max_rel_adjust,
        )
        if pref_res is not None:
            fin_pf = _to_guarded_final_bpm(pref_res["raw_bpm"])
            if fin_pf is not None and abs(pref_res["raw_bpm"] - b_w) >= 0.00002 * b_w:
                best_de_skew = {
                    "raw_bpm": pref_res["raw_bpm"],
                    "raw_instab": pref_res["raw_instab"],
                    "sigma_t": pref_res["sigma_t"],
                    "rel_inst": pref_res["rel_inst"],
                    "n_m": pref_res["n_m"],
                    "final_bpm": fin_pf,
                    "reason": "clean_prefix_deskew",
                }

    if best_de_skew is not None:
        return best_de_skew

    return stage_a_cand
