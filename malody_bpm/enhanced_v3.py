"""
Malody BPM Detector - Enhanced V3 (Enhanced 3.0)
架构融合模式：分窗池化自相关 + 两阶长程 OLS 精修 + 3:2 三连音脉冲梳能量对比度安全消歧 + 未达稳态门槛安全归零 + 零量化连续 OLS 回归。
"""

import math
import ctypes
import numpy as np
from typing import Optional, Dict, Any, Tuple, List, Sequence

from . import filters as _filters
from .autocorr import compute_autocorrelation_fft
from .enhanced_v2 import (
    compute_pooled_acf,
    ENH_V2_CFG,
)
from .force_v3 import (
    V3_TAU_MIN_MS,
    V3_TAU_MAX_MS,
    fold_bpm_2x_v3,
    align_bpm_2x_to_reference,
    compute_best_phase_and_rmse,
    compute_segmented_acf,
)
from .v3_post_refine import refine_post_v3_slope


_V3_STALE_META_KEYS = (
    "stage",
    "best_tau",
    "best_phi",
    "bpm",
    "raw_bpm",
    "instab",
    "steady_instab",
    "fold_scale",
    "sigma_t",
    "rel_inst",
    "n_matched",
    "total_beats",
    "coverage",
    "beat_display",
    "rmse_ms",
    "len_acf",
    "acf_pooled",
    "peaks",
    "candidates",
    "triplet_r_contrast",
    "comb_tol",
    "transition_guard",
    "transition_guard_ms",
    "transition_miss_beats",
    "transition_resume_beats",
    "transition_spans",
    "raw_n_matched",
    "raw_total_beats",
    "raw_coverage",
    "matched_beat_times",  # [只读导出] 逐拍命中时刻（供 UI 未匹配区段使用）
    "matched_beat_tau",
    "score",
    "rescue_candidates",
    "ratio_up",
    "ratio_down",
    "trim_recalc",
    "bpm_recalc_applied",
    "pass1_bpm",
    "sub_trunk_min_sec",
)


# =========================================================================
# 1. IEEE 754 下溢防护 OLS 回归器与 Python 谐波追踪器
# =========================================================================

def compute_ols_regression_safeguarded(
    k_vals: Sequence[int | float],
    t_vals: Sequence[float],
) -> Tuple[float, float, float, float]:
    """
    计算动态斜率线性回归，严格防止 IEEE 754 负微小舍入下溢导致 math.sqrt 崩溃。
    返回 (raw_bpm, raw_instability, sigma_t, rel_instability)。
    """
    n = len(k_vals)
    if n < 3 or len(t_vals) != n:
        return 0.0, 0.0, 999.0, 1.0

    S_kk = float(sum(k * k for k in k_vals))
    S_kt = float(sum(k * t for k, t in zip(k_vals, t_vals)))
    S_tt = float(sum(t * t for t in t_vals))

    if (
        not math.isfinite(S_kk)
        or not math.isfinite(S_kt)
        or not math.isfinite(S_tt)
        or S_kk <= 0.0
        or S_kt <= 0.0
    ):
        return 0.0, 0.0, 999.0, 1.0

    raw_bpm = (60000.0 * S_kk) / S_kt
    if not math.isfinite(raw_bpm) or raw_bpm <= 0.0:
        return 0.0, 0.0, 999.0, 1.0

    # 柯西-施瓦茨中间舍入下溢防护: max(0.0, term)
    term = (((S_kk * S_tt) / S_kt) / S_kt - 1.0) / (float(n) - 1.0) * float(n)
    if not math.isfinite(term) or term < 0.0:
        term = 0.0
    rel_instab = math.sqrt(term)
    raw_instab = rel_instab * raw_bpm

    var_t = (S_tt - (S_kt * S_kt) / S_kk) / (float(n) - 1.0)
    if not math.isfinite(var_t) or var_t < 0.0:
        var_t = 0.0
    sigma_t = math.sqrt(var_t)

    if not math.isfinite(raw_instab) or raw_instab < 0.0:
        raw_instab = 0.0
    if not math.isfinite(sigma_t) or sigma_t < 0.0:
        sigma_t = 0.0
    if not math.isfinite(rel_instab) or rel_instab < 0.0:
        rel_instab = 0.0

    return raw_bpm, raw_instab, sigma_t, rel_instab


def _track_with_safeguarded_ols(
    acf_arr: np.ndarray,
    max_lag: int,
    start_tau: float,
    tol: int = 8,
) -> Tuple[float, float, float, float, int]:
    """
    Python 自相关谐波回归追踪器（用于 n == 3 及 track_tol != 8 分支）：
    采用与原生 C 追踪一致的局部极大值窗口搜索与二次抛物线亚像素插值，
    并统一接入 compute_ols_regression_safeguarded 计算回归统计量。
    返回 (raw_bpm, raw_instab, sigma_t, rel_inst, n_matched)。
    """
    limit = min(int(max_lag), len(acf_arr))
    if limit <= 0 or not math.isfinite(start_tau) or start_tau <= 0.0:
        return 0.0, 0.0, 999.0, 1.0, 0

    win = tol + 2
    cur_tau = float(start_tau)
    target = int(round(cur_tau))
    if target < 0:
        return 0.0, 0.0, 999.0, 1.0, 0

    S_kk = 0.0
    S_kt = 0.0
    k_vals: List[int] = []
    t_vals: List[float] = []
    k = 1

    while target + win < limit:
        base = target - win
        if base < 0:
            base = 0
        local_max_idx = base
        max_val = float(acf_arr[base])
        for idx in range(base + 1, target + win + 1):
            val = float(acf_arr[idx])
            if val > max_val:
                max_val = val
                local_max_idx = idx

        if max_val > 0.0 and abs(local_max_idx - target) <= tol:
            c = local_max_idx
            ym1 = float(acf_arr[c - 1]) if c > 0 else 0.0
            y0_d = float(acf_arr[c])
            yp1 = float(acf_arr[c + 1]) if c + 1 < limit else 0.0
            denom = (y0_d - ym1) + (y0_d - yp1)
            delta = 0.5 * (yp1 - ym1) / denom if denom > 1e-9 else 0.0
            exact_t = float(c) + delta
            if not math.isfinite(exact_t):
                return 0.0, 0.0, 999.0, 1.0, 0
            k_vals.append(k)
            t_vals.append(exact_t)
            kf = float(k)
            S_kk += kf * kf
            S_kt += kf * exact_t
            cur_tau = S_kt / S_kk
            next_t = exact_t + cur_tau
            if not math.isfinite(next_t):
                break
            target = int(round(next_t))
            if target < 0:
                break
        else:
            break
        k += 1

    n_matched = len(k_vals)
    if n_matched < 3:
        return 0.0, 0.0, 999.0, 1.0, n_matched

    raw_bpm, raw_instab, sigma_t, rel_inst = compute_ols_regression_safeguarded(k_vals, t_vals)
    if raw_bpm <= 0.0:
        return 0.0, 0.0, 999.0, 1.0, 0
    return raw_bpm, raw_instab, sigma_t, rel_inst, n_matched


def _track_single_trunk_clean_ols(
    acf_arr: np.ndarray,
    max_lag: int,
    start_tau: float,
    tol: int = 8,
) -> Tuple[float, float, float, float, int, List[int], List[float]]:
    """
    仅用于第二轮干净稳态单主干 acf_clean 的保守抗单阶断链长程 OLS 追踪器：
    1. 前段先执行严格零断链匹配，必须达到 n_strict >= 3 才建立初始安全存档点；
    2. 后续最多允许连续跳过 1 阶偶发弱峰（连续 2 阶失配立即终止），以 k * cur_tau 无累积舍入预测重连位置；
    3. 跨过单阶断口后须满足峰高 >= 严格段中位峰高的 10%，且连续命中至少 3 阶有效谐波才提交新存档点；
    4. 最终延伸结果须满足周期一致性 (±1%) 且 rel_inst 无明显劣化，否则自动回滚至严格零断链存档点。
    返回 (raw_bpm, raw_instab, sigma_t, rel_inst, n_matched, k_vals, t_vals)。
    """
    limit = min(int(max_lag), len(acf_arr))
    if limit <= 0 or not math.isfinite(start_tau) or start_tau <= 0.0:
        return 0.0, 0.0, 999.0, 1.0, 0, [], []

    acf_0 = float(acf_arr[0])
    zero_floor = (5e-6 * acf_0) if (math.isfinite(acf_0) and acf_0 > 0.0) else 0.0

    win = tol + 2
    cur_tau = float(start_tau)
    target = int(round(cur_tau))
    if target < 0:
        return 0.0, 0.0, 999.0, 1.0, 0, [], []

    S_kk = 0.0
    S_kt = 0.0
    k_vals: List[int] = []
    t_vals: List[float] = []
    strict_peaks: List[float] = []
    k = 1
    missed_in_strict = False

    while target + win < limit:
        base = target - win
        if base < 0:
            base = 0
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
                return 0.0, 0.0, 999.0, 1.0, 0, [], []
            k_vals.append(k)
            t_vals.append(exact_t)
            strict_peaks.append(max_val)
            kf = float(k)
            S_kk += kf * kf
            S_kt += kf * exact_t
            cur_tau = S_kt / S_kk
            next_t = exact_t + cur_tau
            if not math.isfinite(next_t):
                break
            target = int(round(next_t))
            if target < 0:
                break
        else:
            missed_in_strict = True
            break
        k += 1

    n_strict = len(k_vals)
    if n_strict < 3:
        return 0.0, 0.0, 999.0, 1.0, n_strict, [], []

    b_strict, i_strict, s_strict, r_strict = compute_ols_regression_safeguarded(k_vals, t_vals)
    if b_strict <= 0.0:
        return 0.0, 0.0, 999.0, 1.0, 0, [], []

    if not missed_in_strict:
        return b_strict, i_strict, s_strict, r_strict, n_strict, k_vals, t_vals

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
        base = target - win
        if base < 0:
            base = 0
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
            S_kk += kf * kf
            S_kt += kf * exact_t
            cur_tau = S_kt / S_kk
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
        b_ext, i_ext, s_ext, r_ext = compute_ols_regression_safeguarded(k_comm, t_comm)
        tau_strict = 60000.0 / b_strict
        tau_ext = (60000.0 / b_ext) if b_ext > 0.0 else 0.0
        skk_strict = float(sum(k * k for k in k_vals[:n_strict]))
        skk_ext = float(sum(k * k for k in k_comm))
        # 对齐 0.25ms 亚像素量化底噪对称保底，避免极小样本偶然超低残差下分子尺度不对称
        se_strict = max(s_strict, 0.25) / math.sqrt(skk_strict) if skk_strict > 0.0 else 999.0
        se_ext = max(s_ext, 0.25) / math.sqrt(skk_ext) if skk_ext > 0.0 else 999.0
        if (
            b_ext > 0.0
            and 0.99 * tau_strict <= tau_ext <= 1.01 * tau_strict
            and s_ext <= max(s_strict * 1.8, s_strict + 0.8)
            and se_ext <= se_strict
            and r_ext <= max(r_strict * 1.05, 0.00025)
        ):
            return b_ext, i_ext, s_ext, r_ext, commit_len, k_comm, t_comm

    k_base = k_vals[:n_strict]
    t_base = t_vals[:n_strict]
    return b_strict, i_strict, s_strict, r_strict, n_strict, k_base, t_base


def _track_multi_trunk_joint_ols(
    primary_acf: np.ndarray,
    secondary_acfs: Optional[List[np.ndarray]],
    start_tau: float,
    tol: int = 8,
    min_req_main_n: int = 3,
    allow_primary_subharmonic: bool = False,
) -> Tuple[float, float, float, float, int, int, int, float, int]:
    """
    多主干各自独立单段自相关 + 支持 2^x 倍频/分频折算的联合过原点最小二乘 (Joint OLS) 拟合：
    - 最长主干优先按 1.0x 独立求解基准谐波观测序列；在允许主主干稀疏同源回退时，若 1.0x 未成链或不足以覆盖
      池化阶数门槛，则依次回退尝试 (2.0, 4.0) 同源尺度并折算等效阶数 kappa = scale * k；
    - 各次要长主干 (>=15s) 独立算自相关寻峰，不仅支持 1:1 同周期，还支持 2^x (0.25x/0.5x/2x/4x)
      编曲疏密同源主干通过等效谐波阶数 kappa = scale * k 折算至同一基准斜率回归；
    - 仅吸纳折算周期一致、时域残差 sigma_t 无发散且高斯-马尔可夫斜率标准误严格改善的主干，否则退回单最长主干。
    返回 (raw_bpm, raw_instab, sigma_t, rel_inst, primary_eff_n_matched, joint_n_matched, joint_acf_denom, joint_skk, accepted_trunks)。
    """
    if primary_acf is None or len(primary_acf) <= 0 or not math.isfinite(start_tau) or start_tau <= 0.0:
        return 0.0, 0.0, 999.0, 1.0, 0, 0, 0, 0.0, 0

    def _has_weak_odd_harmonics(
        acf_seq: np.ndarray,
        k_seq: Sequence[int],
        t_seq: Sequence[float],
    ) -> bool:
        """软性触发器：当主主干 1.0x 奇阶谐波显著偏弱 (<5% P80) 时，允许 (2.0, 4.0) 参与标准误竞选。"""
        if len(k_seq) < 4 or len(acf_seq) <= 0:
            return False
        limit_idx = len(acf_seq) - 1
        all_amps: List[float] = []
        odd_amps: List[float] = []
        for kv, tv in zip(k_seq, t_seq):
            idx_c = min(limit_idx, max(0, int(round(tv))))
            amp_v = float(acf_seq[idx_c])
            all_amps.append(amp_v)
            if int(kv) % 2 == 1:
                odd_amps.append(amp_v)
        if not odd_amps or len(all_amps) < 4:
            return False
        ref_p80 = float(np.percentile(all_amps, 80.0))
        return ref_p80 > 0.0 and float(np.median(odd_amps)) < 0.05 * ref_p80

    b_main, i_main, s_main, r_main, n_main, k_main, t_main = _track_single_trunk_clean_ols(
        primary_acf, len(primary_acf), start_tau, tol=tol
    )
    eff_n_main = n_main
    prim_scale_used = 1.0
    req_main = max(3, int(min_req_main_n))
    main_weak_odd = _has_weak_odd_harmonics(primary_acf, k_main, t_main) if n_main >= 4 else False

    if allow_primary_subharmonic and (
        n_main < req_main or b_main <= 0.0 or main_weak_odd or s_main > 1.2
    ):
        if n_main >= req_main and b_main > 0.0:
            skk_1x = float(sum(k * k for k in k_main))
            best_prim_se = (0.90 * max(s_main, 0.15) / math.sqrt(skk_1x)) if skk_1x > 0.0 else float("inf")
        else:
            best_prim_se = float("inf")

        for prim_scale in (2.0, 4.0):
            if prim_scale == 4.0 and start_tau > 750.0:
                continue
            prim_tau = prim_scale * start_tau
            max_prim_tau = 3000.0 if prim_scale == 4.0 else 2500.0
            if not (150.0 <= prim_tau <= max_prim_tau):
                continue
            b_p, _, s_p, r_p, n_p, k_p, t_p = _track_single_trunk_clean_ols(
                primary_acf, len(primary_acf), prim_tau, tol=tol
            )
            eff_n_p = int(round(prim_scale * float(n_p)))
            if (
                n_p >= 3
                and eff_n_p >= req_main
                and b_p > 0.0
                and k_p
                and s_p <= 2.2
                and r_p <= 0.0010
            ):
                b_p_eq = b_p * prim_scale
                tau_p_eq = 60000.0 / b_p_eq
                skk_p = float(sum(k * k for k in k_p))
                if 0.98 * start_tau <= tau_p_eq <= 1.02 * start_tau and skk_p > 0.0:
                    se_p = max(s_p, 0.15) / (prim_scale * math.sqrt(skk_p))
                    if prim_scale == 2.0:
                        se_p *= 0.95
                    if se_p < best_prim_se:
                        kap_p = [prim_scale * float(k) for k in k_p]
                        b_eq, i_eq, s_eq, r_eq = compute_ols_regression_safeguarded(kap_p, t_p)
                        if b_eq > 0.0:
                            best_prim_se = se_p
                            b_main, i_main, s_main, r_main = b_eq, i_eq, s_eq, r_eq
                            n_main = n_p
                            eff_n_main = eff_n_p
                            k_main = k_p
                            t_main = t_p
                            prim_scale_used = prim_scale

    if n_main < 3 or b_main <= 0.0:
        return 0.0, 0.0, 999.0, 1.0, eff_n_main, eff_n_main, len(primary_acf), 0.0, 0

    joint_k: List[float] = [prim_scale_used * float(k) for k in k_main]
    cur_skk = float(sum(kv * kv for kv in joint_k))
    accepted_trunks = 1

    if not secondary_acfs:
        return (
            b_main,
            i_main,
            s_main,
            r_main,
            eff_n_main,
            eff_n_main,
            len(primary_acf),
            cur_skk,
            accepted_trunks,
        )

    joint_t: List[float] = list(t_main)
    cur_b, cur_i, cur_s, cur_r = b_main, i_main, s_main, r_main
    tau_main = 60000.0 / b_main
    joint_denom = len(primary_acf)
    eff_joint_beats = eff_n_main

    for sub_acf in secondary_acfs:
        if sub_acf is None or len(sub_acf) <= 32:
            continue

        chosen_sub: Optional[Tuple[float, float, float, int, List[int], List[float]]] = None
        best_sub_se = float("inf")

        # 统一在同源尺度族 (1.0, 2.0, 0.5, 4.0, 0.25) 中择优：
        # 既防止纯 Half-time 2x 次主干在奇数滞后噪声包上被劣质 1.0x 误抢占，又允许干净加密拍自然升级为 0.5x
        for cand_scale in (1.0, 2.0, 0.5, 4.0, 0.25):
            if cand_scale == 0.25 and start_tau < 400.0:
                continue
            if cand_scale == 4.0 and start_tau > 750.0:
                continue
            cand_tau = cand_scale * start_tau
            max_cand_tau = 3000.0 if cand_scale == 4.0 else 2500.0
            if cand_scale != 1.0 and not (100.0 <= cand_tau <= max_cand_tau):
                continue
            if cand_scale in (2.0, 0.5, 4.0) and cand_tau < 150.0:
                continue

            b_c, _, s_c, r_c, n_c, k_c, t_c = _track_single_trunk_clean_ols(
                sub_acf, len(sub_acf), cand_tau, tol=tol
            )
            min_req_n = 3 if cand_scale >= 4.0 else (6 if cand_scale < 1.0 else 4)
            if n_c < min_req_n or b_c <= 0.0 or not k_c or s_c > 2.2 or r_c > 0.0010:
                continue
            if cand_scale == 0.25 and s_c > 0.65:
                continue

            b_c_eq = b_c * cand_scale
            if abs(b_c_eq - b_main) > 0.005 * b_main:
                continue
            tau_c_eq = 60000.0 / b_c_eq
            max_kap_c = cand_scale * float(max(k_c))
            if abs(tau_c_eq - tau_main) * max_kap_c > float(tol):
                continue

            skk_c = float(sum(k * k for k in k_c))
            if skk_c <= 0.0:
                continue
            se_c = max(s_c, 0.15) / (cand_scale * math.sqrt(skk_c))
            if cand_scale == 1.0:
                se_c *= 0.90
            if se_c < best_sub_se:
                best_sub_se = se_c
                chosen_sub = (cand_scale, b_c, s_c, n_c, k_c, t_c)

        if chosen_sub is None:
            continue

        chosen_scale, b_sub, s_sub, n_sub, k_sub, t_sub = chosen_sub
        kappa_sub = [chosen_scale * float(k) for k in k_sub]

        cand_k = joint_k + kappa_sub
        cand_t = joint_t + t_sub
        cand_skk = cur_skk + float(sum(kv * kv for kv in kappa_sub))
        b_try, i_try, s_try, r_try = compute_ols_regression_safeguarded(cand_k, cand_t)
        cur_se = max(cur_s, 0.55) / math.sqrt(cur_skk) if cur_skk > 0.0 else 999.0
        try_se = max(s_try, 0.55) / math.sqrt(cand_skk) if cand_skk > 0.0 else 999.0
        if (
            b_try > 0.0
            and s_try <= min(1.8, max(cur_s, s_sub) * 1.25 + 0.25)
            and try_se <= cur_se
            and r_try <= max(r_main * 1.50, 0.00035)
        ):
            joint_k = cand_k
            joint_t = cand_t
            cur_skk = cand_skk
            cur_b, cur_i, cur_s, cur_r = b_try, i_try, s_try, r_try
            joint_denom += len(sub_acf)
            eff_joint_beats += max(1, int(round(chosen_scale * float(n_sub))))
            accepted_trunks += 1

    return (
        cur_b,
        cur_i,
        cur_s,
        cur_r,
        eff_n_main,
        eff_joint_beats,
        joint_denom,
        cur_skk,
        accepted_trunks,
    )


# =========================================================================
# 2. 1ms 直方图分箱时域脉冲梳能量对比度计算器 (3:2 消歧专用)
# =========================================================================

def compute_harmonic_comb_contrast_ratio(
    diff_1000hz: np.ndarray,
    tau_base: float,
    tau_hyp: float,
    comb_tol: Optional[int] = 35,
) -> float:
    """
    计算基于 1ms 直方图分箱的时域脉冲梳能量对比度 R = E_comb(tau_hyp) / max(E_comb(tau_base), 1e-4)。
    基于 diff_1000hz 全局邻点执行局部真波峰判定与 val >= 0.05 * diff_max 底噪过滤（未命中窗口计 0.0 以维持预期总拍数分母），
    最佳相位直方图与能量评分统一基于该真波峰序列 (peak_only)，
    仅当 (e_hyp - e_base) <= 1e-4 时返回 1.0。
    """
    if comb_tol is not None:
        if not isinstance(comb_tol, int) or isinstance(comb_tol, bool) or not (10 <= comb_tol <= 50):
            raise ValueError(f"comb_tol 必须为 10 到 50 之间的整数，当前传入: {comb_tol!r}")
        eff_comb_tol = comb_tol
    else:
        eff_comb_tol = 35

    N = len(diff_1000hz) if diff_1000hz is not None else 0
    if (
        N <= 0
        or isinstance(tau_base, (bool, np.bool_))
        or isinstance(tau_hyp, (bool, np.bool_))
        or not isinstance(tau_base, (int, float, np.integer, np.floating))
        or not isinstance(tau_hyp, (int, float, np.integer, np.floating))
        or not math.isfinite(float(tau_base))
        or not math.isfinite(float(tau_hyp))
        or float(tau_base) <= 0.0
        or float(tau_hyp) <= 0.0
    ):
        return 1.0

    tau_base_f = float(tau_base)
    tau_hyp_f = float(tau_hyp)

    diff_arr = np.asarray(diff_1000hz, dtype=np.float64)
    if not np.all(np.isfinite(diff_arr)):
        return 1.0

    diff_max = float(np.max(diff_arr))
    diff_min = float(np.min(diff_arr))
    if diff_max <= 1e-6 or (diff_max - diff_min) <= 1e-6:
        return 1.0

    threshold = 0.05 * diff_max
    is_peak = np.zeros(N, dtype=bool)
    if N == 1:
        is_peak[0] = diff_arr[0] >= threshold
    else:
        is_peak[0] = (diff_arr[0] >= threshold) and (diff_arr[0] > diff_arr[1])
        is_peak[N - 1] = (diff_arr[N - 1] >= threshold) and (diff_arr[N - 1] > diff_arr[N - 2])
        if N >= 3:
            mid = diff_arr[1 : N - 1]
            left = diff_arr[0 : N - 2]
            right = diff_arr[2:N]
            is_peak[1 : N - 1] = (
                (mid >= threshold)
                & (mid >= left)
                & (mid >= right)
                & ((mid > left) | (mid > right))
            )
    peak_only = np.where(is_peak, diff_arr, 0.0)

    def _eval_comb_energy_1ms(tau: float) -> float:
        L = int(round(tau))
        if L <= 0:
            return 0.0

        t_all = np.arange(N, dtype=np.float64)
        phi_norm = (t_all % tau) / tau
        bin_idx = np.clip(np.floor(phi_norm * L).astype(np.int64), 0, L - 1)
        # [修正 2026-11-13] 相位直方图改用 peak_only 加权（原为 diff_arr）：
        # 下方能量评分只统计 peak_only 真波峰值，若相位按 diff_arr 原始能量面积选取，
        # 两者度量不一致——例如反拍型曲目中底鼓衰减长、面积大但峰值低于强反拍镲片，
        # 基线梳会被锁到弱拍相位，而 1.5 倍假设梳交替命中强/弱两类拍点反而“胜出”，
        # 导致 3:2 三连音误判（合成用例 157.18 BPM 被误判为 104.79 BPM，R=1.157）。
        # 统一度量后该误判消失，真三连音用例 R 基本不变（1.2496 -> 1.2493）。
        # 退化情形：peak_only 全零时两假设能量均为 0，下方 (e_hyp - e_base) <= 1e-4 安全返回 1.0。
        acc = np.bincount(bin_idx, weights=peak_only, minlength=L)

        if L == 1:
            best_bin = 0
        else:
            smooth_r = min(15, max(1, L // 10))
            smooth_r = min(smooth_r, max(1, (L - 1) // 2))
            acc_ext = np.concatenate([acc[-smooth_r:], acc, acc[:smooth_r]])
            kernel = np.ones(2 * smooth_r + 1, dtype=np.float64)
            smoothed = np.convolve(acc_ext, kernel, mode="valid")
            best_bin = int(np.argmax(smoothed[:L]))
        best_phi = float(best_bin) * (tau / float(L))

        w = min(eff_comb_tol, int(tau * 0.25))
        t_exp = best_phi
        peak_vals: List[float] = []
        while t_exp < N:
            t_start = max(0, int(round(t_exp - w)))
            t_end = min(N, int(round(t_exp + w + 1)))
            if t_end > t_start:
                peak_vals.append(float(np.max(peak_only[t_start:t_end])))
            else:
                peak_vals.append(0.0)
            t_exp += tau
        return float(np.mean(peak_vals)) if peak_vals else 0.0

    e_base = _eval_comb_energy_1ms(tau_base_f)
    e_hyp = _eval_comb_energy_1ms(tau_hyp_f)

    if (e_hyp - e_base) <= 1e-4:
        return 1.0
    return float(e_hyp / max(e_base, 1e-4))


# =========================================================================
# 3. 主驱动引擎: select_best_enhanced_v3
# =========================================================================

def _extract_clean_diff_segments(
    diff_1000hz: np.ndarray,
    recalc_spans: List[Tuple[float, float]] | List[List[float]],
    min_seg_ms: int = 4000,
) -> List[np.ndarray]:
    """根据真变速区间 recalc_spans 切分并返回长度 >= min_seg_ms 的各段连续干净包络切片。"""
    N = len(diff_1000hz) if diff_1000hz is not None else 0
    if N <= 0:
        return []

    norm_spans: List[List[float]] = []
    for sp in sorted(recalc_spans, key=lambda x: (float(x[0]), float(x[1]))):
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
    valid_segs: List[np.ndarray] = []
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

    return valid_segs


def _compute_segmented_pooled_acf(
    diff_1000hz: np.ndarray,
    recalc_spans: List[Tuple[float, float]] | List[List[float]],
    win_len: int = 15000,
    hop_len: int = 7500,
    max_lag: int = 8000,
    min_seg_ms: int = 15000,
) -> Optional[np.ndarray]:
    """
    在剔除真变速区间 recalc_spans 后的干净稳态长片段 (>= win_len) 内，
    严格按标准满窗无跨缝提取分窗池化自相关并等权平均 (长度 max_lag)，杜绝短碎片低重叠尾部噪声。
    """
    eff_min_seg = max(int(win_len), int(min_seg_ms))
    valid_segs = _extract_clean_diff_segments(diff_1000hz, recalc_spans, min_seg_ms=eff_min_seg)
    if not valid_segs:
        return None

    c_dsp = _filters.require_c_dsp(_filters._c_dsp)
    acf_sum = np.zeros(max_lag, dtype=np.float64)
    total_weight = 0.0
    win_buf = np.empty(max_lag, dtype=np.float32)

    for seg in valid_segs:
        seg_len = len(seg)
        if seg_len < win_len:
            continue
        use_seg = np.ascontiguousarray(seg, dtype=np.float32)
        starts = list(range(0, seg_len - win_len + 1, hop_len))
        if (seg_len - win_len) % hop_len >= hop_len // 2:
            tail_st = seg_len - win_len
            if not starts or starts[-1] != tail_st:
                starts.append(tail_st)
        if not starts:
            starts = [0]
        for st in starts:
            w = np.ascontiguousarray(use_seg[st : st + win_len], dtype=np.float32)
            c_dsp.autocorr_fft_exact_c(
                w.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                win_len,
                max_lag,
                win_buf.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
            )
            acf_sum += win_buf.astype(np.float64)
            total_weight += 1.0

    if total_weight <= 0.0:
        return None
    return np.ascontiguousarray(acf_sum / total_weight, dtype=np.float32)


def select_best_enhanced_v3(
    acf: Optional[np.ndarray] = None,
    diff_1000hz: Optional[np.ndarray] = None,
    octave_norm: bool = False,
    meta: Optional[Dict[str, Any]] = None,
    pcm_duration_ms: Optional[float] = None,
    transition_guard: Optional[float | int] = None,
    transition_miss_beats: int = 4,
    transition_resume_beats: int = 3,
    track_tol: int = 8,
    comb_tol: Optional[int] = None,
    acf_decay_ratio: float = 0.7,
    cand_update_ratio: float = 1.25,
    trim_recalc: Optional[bool] = None,
    sub_trunk_min_sec: int = 15,
    halve_contrast_ratio: float = 1.10,
) -> Tuple[float, float, float, float]:
    """
    Enhanced V3 核心决策引擎：
    1. 分窗无偏池化自相关 (Pooled ACF 8000ms)；
    2. 局部极大值候选池生成与加权初筛，及分段门控亚谐波前瞻（<199.99ms 即 >~300.015 BPM 无条件跳倍周期；199.99~250ms 仅当
       时域脉冲梳强弱拍对比度 R = E(2τ)/E(τ) >= halve_contrast_ratio 时折半；220~240 BPM 原样保留）；
       - halve_contrast_ratio: 折半合理性判定门限 (1.0~2.0，默认 1.10)。差分包络近似对数压缩，
         合成实测 R≈1.10 对应反拍比正拍弱约 6dB（幅度 50%），1.15≈7.5dB，1.33≈18dB；
         1.0 = 有倍周期候选即折半（240~300.01 段等同原逻辑），2.0 = 几乎从不折半；
    3. 两阶长程 OLS 精修（8000ms 池化自相关 -> 全曲长程自相关）与严格 n_matched >= 3 稳态门槛；
    4. 基于两阶精修周期与时域脉冲梳能量对比度的 3:2 三连音客观安全消歧 (R_contrast >= 1.15，无 diff_1000hz 时仅在基线成链下执行带拍数比防线的纯 ACF 检验)；
    5. 对称 2^x 八度折叠 [59.99, 300.01]（为 [60.00, 300.00] 保留 ±0.01 BPM 余量，忽略 octave_norm）与零量化高精度连续浮点输出。
    6. trim_recalc 分段相位重锁与剔除变速二次全范围重算：
       - None (默认): 当 transition_guard > 0 时自动联动开启，否则关闭；
       - True / False: 显式强制开启或关闭二次重算；
       - sub_trunk_min_sec: 副主干进入二次多主干联合 OLS 的最小有效时长 (8~15 秒整数，默认 15 秒)。
    """
    # 参数类型与范围防御
    if trim_recalc is not None and not isinstance(trim_recalc, (bool, np.bool_)):
        raise ValueError(f"trim_recalc 必须为 None 或布尔类型，当前传入: {trim_recalc!r}")

    if not isinstance(sub_trunk_min_sec, int) or isinstance(sub_trunk_min_sec, bool) or not (8 <= sub_trunk_min_sec <= 15):
        raise ValueError(f"sub_trunk_min_sec 必须为 8 到 15 之间的整数，当前传入: {sub_trunk_min_sec!r}")

    if pcm_duration_ms is not None:
        if (
            isinstance(pcm_duration_ms, (bool, np.bool_))
            or not isinstance(pcm_duration_ms, (int, float, np.integer, np.floating))
            or not math.isfinite(float(pcm_duration_ms))
        ):
            raise ValueError(f"pcm_duration_ms 必须为有限数值，当前传入: {pcm_duration_ms!r}")
        if not (45000.0 <= float(pcm_duration_ms) <= 600000.0):
            raise ValueError(f"PCM 时长 {pcm_duration_ms}ms 超出允许范围 [45s, 600s]")

    if not isinstance(track_tol, int) or isinstance(track_tol, bool) or not (5 <= track_tol <= 25):
        raise ValueError(f"track_tol 必须为 5 到 25 之间的整数，当前传入: {track_tol!r}")

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

    if (
        isinstance(acf_decay_ratio, (bool, np.bool_))
        or not isinstance(acf_decay_ratio, (int, float, np.integer, np.floating))
        or not math.isfinite(float(acf_decay_ratio))
        or not (0.1 <= float(acf_decay_ratio) <= 1.0)
    ):
        raise ValueError(f"acf_decay_ratio 必须为 0.1 到 1.0 之间的浮点数，当前传入: {acf_decay_ratio!r}")
    acf_decay_ratio = float(acf_decay_ratio)

    if (
        isinstance(cand_update_ratio, (bool, np.bool_))
        or not isinstance(cand_update_ratio, (int, float, np.integer, np.floating))
        or not math.isfinite(float(cand_update_ratio))
        or not (1.0 <= float(cand_update_ratio) <= 2.0)
    ):
        raise ValueError(f"cand_update_ratio 必须为 1.0 到 2.0 之间的浮点数，当前传入: {cand_update_ratio!r}")
    cand_update_ratio = float(cand_update_ratio)

    # [新增 2026-11-14] 240~300 BPM 折半合理性判定门限 (强弱拍时域脉冲梳对比度 R)
    if (
        isinstance(halve_contrast_ratio, (bool, np.bool_))
        or not isinstance(halve_contrast_ratio, (int, float, np.integer, np.floating))
        or not math.isfinite(float(halve_contrast_ratio))
        or not (1.0 <= float(halve_contrast_ratio) <= 2.0)
    ):
        raise ValueError(f"halve_contrast_ratio 必须为 1.0 到 2.0 之间的浮点数，当前传入: {halve_contrast_ratio!r}")
    halve_contrast_ratio = float(halve_contrast_ratio)

    eff_trim_recalc = bool(trim_recalc) if trim_recalc is not None else tg_positive
    eff_comb_win = comb_tol if comb_tol is not None else 35

    if meta is not None:
        for _stale_k in _V3_STALE_META_KEYS:
            meta.pop(_stale_k, None)
        meta["suspected_triple_meter"] = False
        meta["track_tol"] = track_tol
        if comb_tol is not None:
            meta["comb_tol"] = comb_tol
        if transition_guard is not None:
            meta["transition_guard"] = transition_guard
        meta["acf_decay_ratio"] = acf_decay_ratio
        meta["cand_update_ratio"] = cand_update_ratio
        meta["halve_contrast_ratio"] = halve_contrast_ratio
        meta["sub_trunk_min_sec"] = sub_trunk_min_sec

    def _populate_fail_meta(
        best_tau_val: float,
        n_m_val: int,
        pooled_arr: np.ndarray,
        p_list: List[int],
        cand_list: List[Tuple[float, float]],
    ) -> None:
        if meta is not None:
            meta.update({
                "stage": "fail",
                "best_tau": best_tau_val,
                "bpm": 0.0,
                "raw_bpm": 0.0,
                "instab": 1.0,
                "steady_instab": 0.0,
                "fold_scale": 1.0,
                "sigma_t": 999.0,
                "rel_inst": 1.0,
                "n_matched": n_m_val,
                "coverage": 0.0,
                "len_acf": len(pooled_arr),
                "acf_pooled": pooled_arr,
                "peaks": [{"lag": int(p), "val": float(pooled_arr[p])} for p in p_list],
                "candidates": [
                    {"tau": float(t), "bpm": round(60000.0 / float(t), 3), "score": float(s)}
                    for t, s in cand_list
                ],
                "suspected_triple_meter": False,
            })
            if eff_trim_recalc:
                meta.update({
                    "trim_recalc": True,
                    "bpm_recalc_applied": False,
                    "pass1_bpm": 0.0,
                })

    # 1. 计算池化自相关序列 (8000ms)
    win_len = int(ENH_V2_CFG["win_len_ms"])
    hop_len = int(ENH_V2_CFG["hop_len_ms"])
    max_lag = int(ENH_V2_CFG["max_lag_ms"])

    if diff_1000hz is not None and len(diff_1000hz) > 0:
        acf_pooled = compute_pooled_acf(diff_1000hz, win_len=win_len, hop_len=hop_len, max_lag=max_lag)
        if (acf is None or len(acf) <= max_lag) and len(diff_1000hz) > max_lag:
            acf = compute_autocorrelation_fft(diff_1000hz, exact=False)
    elif acf is not None and len(acf) > 0:
        if len(acf) >= max_lag:
            acf_pooled = np.ascontiguousarray(acf[:max_lag], dtype=np.float32)
        else:
            acf_pooled = np.pad(np.ascontiguousarray(acf, dtype=np.float32), (0, max_lag - len(acf)))
    else:
        _populate_fail_meta(0.0, 0, np.zeros(0, dtype=np.float32), [], [])
        return 0.0, 0.0, 999.0, 1.0

    c_dsp = _filters.require_c_dsp(_filters._c_dsp)

    def _run_track_exact(
        target_acf: np.ndarray,
        target_len: int,
        target_tau: float,
    ) -> Tuple[float, float, float, float, int]:
        eff_len = min(int(target_len), len(target_acf))
        if eff_len <= 0 or not math.isfinite(target_tau) or target_tau <= 0.0:
            return 0.0, 0.0, 999.0, 1.0, 0
        if track_tol == 8 and round(target_tau) >= 10:
            target_acf_c = np.ascontiguousarray(target_acf[:eff_len], dtype=np.float32)
            stats = (ctypes.c_double * 4)()
            n = int(c_dsp.track_full_song_exact_c(
                target_acf_c.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                eff_len,
                float(target_tau),
                stats,
            ))
            if n >= 4:
                b = float(stats[0])
                inst = float(stats[1])
                sig = float(stats[2])
                rel = float(stats[3])
                if not math.isfinite(b) or b <= 0.0:
                    return 0.0, 0.0, 999.0, 1.0, 0
                if not math.isfinite(inst) or inst < 0.0:
                    inst = 0.0
                if not math.isfinite(sig) or sig < 0.0:
                    sig = 0.0
                if not math.isfinite(rel) or rel < 0.0:
                    rel = 0.0
                return b, inst, sig, rel, n
            elif n == 3:
                return _track_with_safeguarded_ols(target_acf_c, eff_len, target_tau, tol=8)
            else:
                return 0.0, 0.0, 999.0, 1.0, n
        else:
            return _track_with_safeguarded_ols(target_acf, eff_len, target_tau, tol=track_tol)

    def _solve_v3_core(
        cur_pooled: np.ndarray,
        cur_full_acf: Optional[np.ndarray],
        cur_diff: Optional[np.ndarray],
        secondary_full_acfs: Optional[List[np.ndarray]] = None,
        clean_recalc_mode: bool = False,
    ) -> Dict[str, Any]:
        """
        在给定的 (池化自相关, 长程自相关, 时域差分包络) 上执行完整的候选提取、
        加权初筛、分段门控亚谐波前瞻（>300 BPM 无条件 / 240~300 BPM 强弱拍对比度 R>=halve_contrast_ratio 才折半 / 220~240 BPM 保留）、
        超低速打捞、两阶 OLS 回归与 3:2 三连音消歧。
        当 clean_recalc_mode=True 时，仅在第二轮干净长程阶段启用抗单阶断链与多主干联合 OLS。
        """
        res: Dict[str, Any] = {
            "stage": "fail",
            "sel_tau": 0.0,
            "raw_bpm": 0.0,
            "raw_instab": 0.0,
            "sigma_t": 999.0,
            "rel_inst": 1.0,
            "n_m": 0,
            "used_full_acf": False,
            "clean_acf_denom": len(cur_full_acf) if cur_full_acf is not None else 0,
            "accepted_trunks": 0,
            "peaks_list": [],
            "cands": [],
            "suspected_triple": False,
            "triplet_r_contrast": None,
        }

        cur_max_lag_cands = min(len(cur_pooled), int(ENH_V2_CFG["max_lag_cands_ms"]))
        if cur_max_lag_cands <= 32:
            return res

        cur_pooled_c = np.ascontiguousarray(cur_pooled, dtype=np.float32)
        p_buf = (ctypes.c_int * cur_max_lag_cands)()
        n_pk = int(c_dsp.find_acf_local_peaks_c(
            cur_pooled_c.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
            cur_max_lag_cands,
            p_buf,
        ))
        pk_list = [int(p_buf[i]) for i in range(n_pk)]
        res["peaks_list"] = pk_list

        cand_list: List[Tuple[float, float]] = []
        best_c_idx = 0
        best_c_score = np.float32(0.0)

        for rbx, p_lag in enumerate(pk_list):
            if p_lag >= cur_max_lag_cands:
                break
            if len(cand_list) > 0 and cur_pooled_c[p_lag] <= 0.0:
                continue

            tau = float(p_lag)
            target = int(round(tau))
            win_cand = track_tol + 2
            if target >= cur_max_lag_cands - win_cand:
                continue

            sum_h = np.float32(0.0)
            n_matched = 0
            S_kt = 0.0
            S_kk = 0.0
            k = 1.0
            p_idx = rbx

            while True:
                while p_idx < len(pk_list) and pk_list[p_idx] < target - win_cand:
                    p_idx += 1
                if p_idx < len(pk_list) and pk_list[p_idx] <= target + win_cand:
                    c = pk_list[p_idx]
                    n_matched += 1
                    y0 = cur_pooled_c[c]
                    sum_h = np.float32(sum_h + y0)
                    ym1 = float(cur_pooled_c[c - 1])
                    y0_d = float(y0)
                    yp1 = float(cur_pooled_c[c + 1])
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
                if target >= cur_max_lag_cands - win_cand:
                    break
                k += 1.0
                if not matched:
                    break

            if n_matched == 0 or target <= cur_max_lag_cands // 2:
                continue

            mean_h = np.float32(sum_h / np.float32(n_matched))
            if mean_h <= best_c_score:
                continue
            if mean_h <= np.float32(cur_pooled_c[p_lag] * np.float32(acf_decay_ratio)):
                continue

            cand_list.append((S_kt / S_kk, float(mean_h)))
            if mean_h > np.float32(best_c_score * np.float32(cand_update_ratio)):
                best_c_idx = len(cand_list) - 1
            best_c_score = mean_h

        res["cands"] = cand_list
        if not cand_list or best_c_score <= 0.0:
            return res

        def _track_two_stage_local(
            seed_tau: float,
        ) -> Tuple[float, float, float, float, int, float, bool, int, int]:
            b_p, i_p, s_p, r_p, n_p = _run_track_exact(cur_pooled_c, len(cur_pooled_c), seed_tau)
            if n_p < 3 or b_p <= 0.0:
                return 0.0, 0.0, 999.0, 1.0, n_p, r_p, False, len(cur_pooled_c), 0
            b_fin, i_fin, s_fin, r_fin, n_fin = b_p, i_p, s_p, r_p, n_p
            used_f = False
            eff_denom = len(cur_pooled_c)
            eff_acc_trunks = 0
            if cur_full_acf is not None and len(cur_full_acf) > len(cur_pooled_c):
                if clean_recalc_mode:
                    (
                        b_f,
                        i_f,
                        s_f,
                        r_f,
                        n_f_main,
                        n_f_joint,
                        denom_f,
                        _skk_tf,
                        acc_trunks_tf,
                    ) = _track_multi_trunk_joint_ols(
                        cur_full_acf, secondary_full_acfs, seed_tau, tol=track_tol
                    )
                    if n_f_main >= n_p and b_f > 0.0:
                        b_fin, i_fin, s_fin, r_fin, n_fin = b_f, i_f, s_f, r_f, n_f_joint
                        used_f = True
                        eff_denom = denom_f
                        eff_acc_trunks = acc_trunks_tf
                else:
                    b_f, i_f, s_f, r_f, n_f = _run_track_exact(cur_full_acf, len(cur_full_acf), seed_tau)
                    if n_f >= n_p and b_f > 0.0:
                        b_fin, i_fin, s_fin, r_fin, n_fin = b_f, i_f, s_f, r_f, n_f
                        used_f = True
                        eff_denom = len(cur_full_acf)
                        eff_acc_trunks = 1
            return b_fin, i_fin, s_fin, r_fin, n_fin, r_p, used_f, eff_denom, eff_acc_trunks

        s_tau = cand_list[best_c_idx][0]

        # [修改 2026-11-14] 「220 BPM 亚谐波前瞻」→「分段门控亚谐波前瞻」
        # 问题：原逻辑只要当前周期 < 272.7ms (>220 BPM) 就无条件沿整数倍候选跳向更长周期，
        #   而真实曲目的长周期候选几乎总是存在（小节级重复使其谐波均值更高），
        #   导致 220~300 BPM 几乎全部被折半，与 [59.99, 300.01] 折叠输出区间矛盾。
        # 修复：候选匹配规则与 TAU_MAX 上界保持原样，仅按「当前周期所处区段」决定本步是否允许跳转：
        #   - tau <  199.99ms       (>~300.015 BPM)：沿用原逻辑，无条件跳转；
        #   - 199.99 <= tau < 250ms (240~300.01+ BPM)：跳向 >=2 倍周期前先做「折半合理性」判定——
        #       时域脉冲梳强弱拍对比度 R = E_comb(2τ) / E_comb(τ) ≈ 2S/(S+W)（15s 分窗中位数）>= halve_contrast_ratio（默认 1.10）
        #       （S/W 为差分包络正/反拍峰值均值；包络近似对数压缩，1.10 约对应音频上反拍弱 6dB）才跳转；
        #       无 diff 包络时不跳转（门限为 1.0 时除外，此时等同原逻辑无条件跳转）；
        #   - 250 <= tau < 272.7ms  (220~240 BPM) ：不再跳转，原样保留。
        #   （在 199.99~250ms 区段内，受 545ms 上界约束，唯一可达的倍数跳转即 2 倍周期。）
        # 区段外（首选周期 >= 272.7ms；或 <199.99ms 且每一步跳转落点均 <199.99ms 或 >= 272.7ms）的行为与原逻辑逐位一致。
        TAU_MAX = 545.4545454545455   # 110.0 BPM（原前瞻候选上界，保持不变）
        TAU_FREE = V3_TAU_MIN_MS      # 199.99ms (~300.015 BPM)：短于此周期 -> 无条件跳转（预留 300.00 BPM 余量）
        TAU_KEEP = 250.0              # 240.0 BPM：不短于此周期 -> 不再跳转（原阈值 272.7ms / 220 BPM）
        has_cur_diff = cur_diff is not None and len(cur_diff) > 0

        def _halving_reasonable(base_tau: float) -> bool:
            """199.99~250ms (240~300.01+ BPM) 区段折半合理性判定：强弱拍交替明显 (R >= halve_contrast_ratio) 才允许跳向 2 倍周期。"""
            if halve_contrast_ratio <= 1.0:
                return True  # R 恒 >= 1.0，门限 1.0 即无条件折半（等同原逻辑），省去计算
            if not has_cur_diff:
                return False
            # 先用两阶长程 OLS 精修基准周期，减小候选粗周期在时域梳上的累积漂移；精修失败或偏离过大时回退原周期
            b_ref = _track_two_stage_local(base_tau)[0]
            tau_ref = base_tau
            if b_ref > 0.0 and math.isfinite(b_ref):
                t_r = 60000.0 / b_ref
                if 0.95 * base_tau <= t_r <= 1.05 * base_tau:
                    tau_ref = t_r
            # 分窗（与池化 ACF 同为 15s 窗长 / 7.5s 步进）各自独立选相后逐窗计算 R，取中位数：
            # 全曲单一相位网格遇到曲中相位跳变时，2τ 梳会在后半段错锁到反拍，R 被拉回 ~1.0
            # （合成用例：125 BPM + 弱八分、中途相位跳变，全曲 R=1.000 而分窗中位数 R=1.177）。
            # 近静音窗（含 Pass-2 被置零的变速跨度）不参与统计；无有效窗时回退全曲单次计算。
            diff_arr = np.asarray(cur_diff, dtype=np.float64)
            g_max = float(np.max(diff_arr))
            win_rs: List[float] = []
            for w0 in range(0, len(diff_arr) - win_len + 1, hop_len):
                seg = diff_arr[w0:w0 + win_len]
                if float(np.max(seg)) < 0.1 * g_max:
                    continue
                win_rs.append(compute_harmonic_comb_contrast_ratio(
                    seg, tau_base=tau_ref, tau_hyp=2.0 * tau_ref, comb_tol=eff_comb_win,
                ))
            if win_rs:
                r_halve = float(np.median(win_rs))
            else:
                r_halve = compute_harmonic_comb_contrast_ratio(
                    diff_arr, tau_base=tau_ref, tau_hyp=2.0 * tau_ref, comb_tol=eff_comb_win,
                )
            return r_halve >= halve_contrast_ratio

        if s_tau < TAU_KEEP:
            while best_c_idx < len(cand_list) - 1:
                if cand_list[best_c_idx + 1][0] >= TAU_MAX:
                    break
                found = False
                for j in range(best_c_idx + 1, len(cand_list)):
                    cand_tau = cand_list[j][0]
                    if cand_tau >= TAU_MAX:
                        break
                    if abs(math.remainder(cand_tau, s_tau)) <= 10.0:
                        # 240~300 BPM 区段的倍周期跳转需通过折半合理性判定，否则停止前瞻（found 保持 False）
                        if (
                            s_tau >= TAU_FREE
                            and round(cand_tau / s_tau) >= 2
                            and not _halving_reasonable(s_tau)
                        ):
                            break
                        best_c_idx = j
                        s_tau = cand_tau
                        found = True
                        break
                if not found or s_tau >= TAU_KEEP:
                    break

        r_bpm, r_instab, sig_t, r_inst, n_match = _run_track_exact(cur_pooled_c, len(cur_pooled_c), s_tau)
        used_full = False
        clean_acf_denom = len(cur_full_acf) if cur_full_acf is not None else 0
        accepted_trunks = 0

        # 超低速 (s_tau > 1000.2ms) 候选未成链时，回溯 V3 周期区间 [199.99, 1000.2]ms 内的谐波/基频候选
        if n_match < 3 and s_tau > V3_TAU_MAX_MS:
            for factor in (8, 4, 2):
                sub_target = s_tau / float(factor)
                if V3_TAU_MIN_MS <= sub_target <= V3_TAU_MAX_MS:
                    best_sub_idx = None
                    min_diff = 10.0
                    for idx in range(best_c_idx):
                        cand_tau = cand_list[idx][0]
                        diff = abs(cand_tau - sub_target)
                        if diff < min_diff:
                            min_diff = diff
                            best_sub_idx = idx

                    cand_tau_to_try = cand_list[best_sub_idx][0] if best_sub_idx is not None else None
                    if cand_tau_to_try is None:
                        p_min_diff = 10.0
                        for p_lag in pk_list:
                            diff = abs(float(p_lag) - sub_target)
                            if diff < p_min_diff:
                                p_min_diff = diff
                                cand_tau_to_try = float(p_lag)

                    if cand_tau_to_try is not None:
                        try_bpm, try_inst, try_sig, try_rel, try_n = _run_track_exact(
                            cur_pooled_c, len(cur_pooled_c), cand_tau_to_try
                        )
                        if try_n >= 3 and try_bpm > 0.0:
                            if best_sub_idx is not None:
                                best_c_idx = best_sub_idx
                            s_tau = cand_tau_to_try
                            r_bpm, r_instab, sig_t, r_inst, n_match = try_bpm, try_inst, try_sig, try_rel, try_n
                            break

        base_ok = (n_match >= 3 and r_bpm > 0.0)
        pooled_base_rel = r_inst
        if base_ok and cur_full_acf is not None and len(cur_full_acf) > len(cur_pooled_c):
            if clean_recalc_mode:
                (
                    r_bpm_f,
                    r_instab_f,
                    sig_t_f,
                    r_inst_f,
                    n_full_main,
                    n_full_joint,
                    denom_f,
                    _skk_f,
                    acc_trunks_f,
                ) = _track_multi_trunk_joint_ols(
                    cur_full_acf,
                    secondary_full_acfs,
                    s_tau,
                    tol=track_tol,
                    min_req_main_n=n_match,
                    allow_primary_subharmonic=True,
                )
                if n_full_main >= n_match and r_bpm_f > 0.0:
                    r_bpm, r_instab, sig_t, r_inst, n_match = (
                        r_bpm_f, r_instab_f, sig_t_f, r_inst_f, n_full_joint
                    )
                    used_full = True
                    clean_acf_denom = denom_f
                    accepted_trunks = acc_trunks_f
            else:
                r_bpm_f, r_instab_f, sig_t_f, r_inst_f, n_full = _run_track_exact(
                    cur_full_acf, len(cur_full_acf), s_tau
                )
                if n_full >= n_match and r_bpm_f > 0.0:
                    r_bpm, r_instab, sig_t, r_inst, n_match = (
                        r_bpm_f, r_instab_f, sig_t_f, r_inst_f, n_full
                    )
                    used_full = True
                    accepted_trunks = 1

        tau_base_eval = (60000.0 / r_bpm) if base_ok else s_tau
        if tau_base_eval > 0.0:
            tau_trip_est = tau_base_eval * 1.5
            if V3_TAU_MIN_MS <= tau_trip_est <= V3_TAU_MAX_MS:
                cand_trip_tau = None
                min_trip_diff = 10.0001
                for ct, _ in cand_list:
                    d_trip = abs(ct - tau_trip_est)
                    if d_trip <= 10.0 and d_trip < min_trip_diff:
                        min_trip_diff = d_trip
                        cand_trip_tau = ct

                r32_p_limit = max(pooled_base_rel * 1.2, 0.005) if base_ok else 0.015
                r32_fin_limit = max(r_inst * 1.2, 0.005) if base_ok else 0.015
                has_diff = (cur_diff is not None and len(cur_diff) > 0)

                if has_diff:
                    tau_trip_seed = cand_trip_tau if cand_trip_tau is not None else tau_trip_est
                    (
                        b32,
                        i32,
                        s32,
                        r32,
                        n32,
                        r32_p,
                        trip_used_full,
                        trip_denom,
                        trip_acc_trunks,
                    ) = _track_two_stage_local(tau_trip_seed)
                    if (
                        n32 >= 3
                        and b32 > 0.0
                        and r32_p <= r32_p_limit
                        and r32 <= r32_fin_limit
                    ):
                        tau_trip_refined = 60000.0 / b32
                        r_contrast = compute_harmonic_comb_contrast_ratio(
                            cur_diff,
                            tau_base=tau_base_eval,
                            tau_hyp=tau_trip_refined,
                            comb_tol=eff_comb_win,
                        )
                        if r_contrast >= 1.15:
                            s_tau = tau_trip_seed
                            r_bpm, r_instab, sig_t, r_inst, n_match = b32, i32, s32, r32, n32
                            used_full = trip_used_full
                            if trip_used_full:
                                clean_acf_denom = trip_denom
                                accepted_trunks = trip_acc_trunks
                            else:
                                accepted_trunks = 0
                            res["triplet_r_contrast"] = r_contrast
                            res["suspected_triple"] = True
                elif base_ok and cand_trip_tau is not None:
                    (
                        b32,
                        i32,
                        s32,
                        r32,
                        n32,
                        r32_p,
                        trip_used_full,
                        trip_denom,
                        trip_acc_trunks,
                    ) = _track_two_stage_local(cand_trip_tau)
                    if (
                        n32 >= max(3, int(n_match * 0.5))
                        and b32 > 0.0
                        and r32_p <= r32_p_limit
                        and r32 <= r32_fin_limit
                    ):
                        s_tau = cand_trip_tau
                        r_bpm, r_instab, sig_t, r_inst, n_match = b32, i32, s32, r32, n32
                        used_full = trip_used_full
                        if trip_used_full:
                            clean_acf_denom = trip_denom
                            accepted_trunks = trip_acc_trunks
                        else:
                            accepted_trunks = 0
                        res["suspected_triple"] = True

        res.update({
            "sel_tau": s_tau,
            "raw_bpm": r_bpm,
            "raw_instab": r_instab,
            "sigma_t": sig_t,
            "rel_inst": r_inst,
            "n_m": n_match,
            "used_full_acf": used_full,
            "clean_acf_denom": clean_acf_denom,
            "accepted_trunks": accepted_trunks,
        })
        if n_match >= 3 and r_bpm > 0.0:
            res["stage"] = "v3_tier1"
        return res

    # 2~5. 执行第一轮全曲池化/长程两阶 OLS 回归与 3:2 消歧
    p1_res = _solve_v3_core(acf_pooled, acf, diff_1000hz)
    chosen_stage = p1_res["stage"]
    sel_tau = p1_res["sel_tau"]
    raw_bpm = p1_res["raw_bpm"]
    raw_instab = p1_res["raw_instab"]
    sigma_t = p1_res["sigma_t"]
    rel_inst = p1_res["rel_inst"]
    n_m = p1_res["n_m"]
    used_full_acf = p1_res["used_full_acf"]
    peaks_list = p1_res["peaks_list"]
    cands = p1_res["cands"]
    suspected_triple = p1_res["suspected_triple"]
    triplet_r_contrast = p1_res["triplet_r_contrast"]

    # 若未达 n_matched >= 3 稳态门槛，严格安全退出（不执行全局脉冲梳强行拟合）
    if chosen_stage == "fail" or raw_bpm <= 0.0 or n_m < 3:
        _populate_fail_meta(sel_tau, n_m, acf_pooled, peaks_list, cands)
        return 0.0, 0.0, 999.0, 1.0

    if meta is not None:
        meta["suspected_triple_meter"] = suspected_triple
        if triplet_r_contrast is not None:
            meta["triplet_r_contrast"] = triplet_r_contrast

    # 6. 对称 2^x 八度折叠 [59.99, 300.01]（忽略 octave_norm）与连续高精度输出
    if not math.isfinite(raw_instab) or raw_instab < 0.0:
        raw_instab = 0.0
    if not math.isfinite(sigma_t) or sigma_t < 0.0:
        sigma_t = 0.0
    if not math.isfinite(rel_inst) or rel_inst < 0.0:
        rel_inst = 0.0

    final_bpm = fold_bpm_2x_v3(raw_bpm)
    pass1_bpm = final_bpm
    bpm_recalc_applied = False

    fold_scale = final_bpm / raw_bpm if raw_bpm > 0.0 else 1.0
    final_instab = raw_instab * fold_scale
    steady_instab = final_bpm * rel_inst
    tracked_tau = 60000.0 / raw_bpm

    # 全曲时间轴时域脉冲梳覆盖度评估（对齐 V2 comb_rmse = 0.0、全曲 acf 覆盖率分母与 transition_guard 全裁剪归零）
    cov_denom = len(acf) if (used_full_acf and acf is not None and len(acf) > 0) else len(acf_pooled)
    comb_n_matched = n_m
    comb_total_beats = 0
    comb_cov = float(n_m) * tracked_tau / float(cov_denom) if cov_denom > 0 else 0.0
    comb_rmse = 0.0
    comb_phi = 0.0
    comb_instab = 1.0
    comb_meta: Dict[str, Any] = {}
    p2_trunk_count = 0
    p2_single_trunk_acf: Optional[np.ndarray] = None
    p2_single_trunk_diff: Optional[np.ndarray] = None

    if diff_1000hz is not None and len(diff_1000hz) > 0 and final_bpm > 0.0:
        target_tau = 60000.0 / final_bpm
        comb_phi, comb_rmse, comb_instab = compute_best_phase_and_rmse(
            diff_1000hz,
            target_tau,
            win_radius=eff_comb_win,
            meta=comb_meta,
            transition_guard=transition_guard,
            transition_miss_beats=transition_miss_beats,
            transition_resume_beats=transition_resume_beats,
            comb_tol=comb_tol,
            allow_phase_relock=eff_trim_recalc,
        )
        recalc_spans = list(comb_meta.pop("_recalc_spans", []))
        if comb_meta.get("total_beats", 0) > 0 or comb_meta.get("raw_total_beats", 0) > 0:
            comb_n_matched = int(comb_meta.get("n_matched", 0))
            comb_total_beats = int(comb_meta.get("total_beats", 0))
            comb_cov = float(comb_meta.get("coverage", 0.0))

        # 7. 剔除变速跨度后的单次无条件全范围重搜 + 多主干独立自相关联合 OLS 精修（只要 recalc_spans 非空且最长净段 >= 16.5s）
        if eff_trim_recalc and recalc_spans:
            p1_n_acf = n_m
            p1_acf_cov = (float(p1_n_acf) * tracked_tau / float(cov_denom)) if cov_denom > 0 else 0.0
            p1_cov = comb_cov
            p1_comb_instab = comb_instab

            # 副主干进入二次多主干联合 OLS 的最小有效时长 (8~15s 整数，默认 15s 与满窗池化对齐)
            min_sub_trunk_ms = min(win_len, int(sub_trunk_min_sec) * 1000)
            clean_trunks = _extract_clean_diff_segments(
                diff_1000hz,
                recalc_spans,
                min_seg_ms=min_sub_trunk_ms,
            )
            longest_seg_ms = max((len(seg) for seg in clean_trunks), default=0)
            if clean_trunks and longest_seg_ms >= 16500:
                ranked_trunks = sorted(
                    clean_trunks,
                    key=lambda seg: (
                        len(seg),
                        float(np.sum(np.maximum(seg, 0.0).astype(np.float64) ** 2)),
                    ),
                    reverse=True,
                )
                acf_primary = compute_autocorrelation_fft(ranked_trunks[0], exact=False)
                secondary_acfs = [
                    a
                    for a in (compute_autocorrelation_fft(seg, exact=False) for seg in ranked_trunks[1:])
                    if len(a) > 32
                ]
                if len(acf_primary) > max_lag:
                    acf_pooled_clean = _compute_segmented_pooled_acf(
                        diff_1000hz,
                        recalc_spans,
                        win_len=win_len,
                        hop_len=hop_len,
                        max_lag=max_lag,
                        min_seg_ms=win_len,
                    )
                    diff_clean_masked = np.array(diff_1000hz, copy=True)
                    n_diff = len(diff_clean_masked)
                    for sp in recalc_spans:
                        i0 = max(0, min(n_diff, int(math.floor(float(sp[0])))))
                        i1 = max(0, min(n_diff, int(math.ceil(float(sp[1])))))
                        if i1 > i0:
                            diff_clean_masked[i0:i1] = 0.0

                    p2_cand_payload: Optional[Dict[str, Any]] = None
                    fb_pooled_ref = acf_pooled
                    fb_peaks_ref = peaks_list
                    fb_cands_ref = cands

                    # 优先在无跨缝满窗池化 + 独立多主干自相关上执行全范围候选重搜、抗单阶断链联合 OLS 与 3:2 消歧
                    if acf_pooled_clean is not None and len(acf_pooled_clean) > 32:
                        p2_res = _solve_v3_core(
                            acf_pooled_clean,
                            acf_primary,
                            diff_clean_masked,
                            secondary_full_acfs=secondary_acfs,
                            clean_recalc_mode=True,
                        )
                        fb_pooled_ref = acf_pooled_clean
                        fb_peaks_ref = p2_res["peaks_list"]
                        fb_cands_ref = p2_res["cands"]
                        if p2_res["stage"] == "v3_tier1" and p2_res["raw_bpm"] > 0.0 and p2_res["n_m"] >= 3:
                            r_bpm_2 = float(p2_res["raw_bpm"])
                            r_inst_2 = float(p2_res["raw_instab"]) if math.isfinite(p2_res["raw_instab"]) and p2_res["raw_instab"] >= 0.0 else 0.0
                            sig_2 = float(p2_res["sigma_t"]) if math.isfinite(p2_res["sigma_t"]) and p2_res["sigma_t"] >= 0.0 else 0.0
                            rel_2 = float(p2_res["rel_inst"]) if math.isfinite(p2_res["rel_inst"]) and p2_res["rel_inst"] >= 0.0 else 0.0
                            fin_bpm_2 = fold_bpm_2x_v3(r_bpm_2)
                            if fin_bpm_2 > 0.0:
                                fin_bpm_2 = align_bpm_2x_to_reference(fin_bpm_2, pass1_bpm)
                                cov_denom_2 = (
                                    int(p2_res["clean_acf_denom"])
                                    if (p2_res["used_full_acf"] and int(p2_res["clean_acf_denom"]) > 0)
                                    else len(acf_pooled_clean)
                                )
                                eff_n_acf_2 = int(p2_res["n_m"])
                                if raw_bpm > 0.0 and r_bpm_2 > 0.0:
                                    raw_ratio_12 = raw_bpm / r_bpm_2
                                    near_pow2 = 2.0 ** round(math.log2(raw_ratio_12))
                                    if abs(raw_ratio_12 - near_pow2) <= 0.05 * near_pow2:
                                        eff_n_acf_2 = max(1, int(round(float(p2_res["n_m"]) * near_pow2)))
                                p2_cand_payload = {
                                    "sel_tau": float(p2_res["sel_tau"]),
                                    "raw_bpm": r_bpm_2,
                                    "raw_instab": r_inst_2,
                                    "sigma_t": sig_2,
                                    "rel_inst": rel_2,
                                    "n_m": int(p2_res["n_m"]),
                                    "used_full_acf": bool(p2_res["used_full_acf"]),
                                    "accepted_trunks": int(p2_res["accepted_trunks"]),
                                    "eff_n_acf": eff_n_acf_2,
                                    "p2_acf_cov": (
                                        (float(p2_res["n_m"]) * (60000.0 / r_bpm_2) / float(cov_denom_2))
                                        if cov_denom_2 > 0
                                        else 0.0
                                    ),
                                    "final_bpm": fin_bpm_2,
                                    "suspected_triple": bool(p2_res["suspected_triple"]),
                                    "triplet_r_contrast": p2_res["triplet_r_contrast"],
                                    "acf_pooled": acf_pooled_clean,
                                    "peaks_list": p2_res["peaks_list"],
                                    "cands": p2_res["cands"],
                                }

                    # 种子兜底：若满窗池化因严格衰减门限未成链，回退用第一轮种子在干净主干上直接联合 OLS 精修
                    if p2_cand_payload is None:
                        seed_candidates: List[Tuple[float, float]] = [(tracked_tau, 1.0)]
                        cur_seed = tracked_tau * 0.5
                        while cur_seed >= target_tau * 0.98:
                            if all(abs(cur_seed - prev_s) > 1.0 for prev_s, _ in seed_candidates):
                                seed_candidates.append((cur_seed, 1.0))
                            cur_seed *= 0.5
                        if all(abs(target_tau - prev_s) > 1.0 for prev_s, _ in seed_candidates):
                            seed_candidates.append((target_tau, 1.0))
                        for sub_mul in (2.0, 4.0):
                            if sub_mul == 4.0 and target_tau > 750.0:
                                continue
                            max_sub_tau = 3000.0 if sub_mul == 4.0 else 2050.0
                            sub_kx_tau = sub_mul * target_tau
                            if sub_kx_tau <= max_sub_tau and all(abs(sub_kx_tau - prev_s) > 1.0 for prev_s, _ in seed_candidates):
                                seed_candidates.append((sub_kx_tau, sub_mul))

                        best_fb_se = float("inf")
                        for seed_try, oct_mul in seed_candidates:
                            (
                                b_2,
                                i_2,
                                s_2,
                                r_2,
                                n_main_2,
                                n_joint_2,
                                denom_fb_2,
                                skk_fb_2,
                                acc_trunks_2,
                            ) = _track_multi_trunk_joint_ols(
                                acf_primary, secondary_acfs, seed_try, tol=track_tol
                            )
                            if (
                                n_main_2 >= 3
                                and b_2 > 0.0
                                and math.isfinite(b_2)
                                and skk_fb_2 > 0.0
                                and math.isfinite(skk_fb_2)
                            ):
                                tau_2 = 60000.0 / b_2
                                if 0.95 * seed_try <= tau_2 <= 1.05 * seed_try:
                                    i_2 = i_2 if (math.isfinite(i_2) and i_2 >= 0.0) else 0.0
                                    s_2 = s_2 if (math.isfinite(s_2) and s_2 >= 0.0) else 0.0
                                    r_2 = r_2 if (math.isfinite(r_2) and r_2 >= 0.0) else 0.0
                                    fb_rel_se = max(s_2, 0.15) / (tau_2 * math.sqrt(skk_fb_2))
                                    raw_bpm_fb = b_2 * oct_mul
                                    fin_bpm_fb = fold_bpm_2x_v3(raw_bpm_fb)
                                    if fin_bpm_fb > 0.0:
                                        fin_bpm_fb = align_bpm_2x_to_reference(fin_bpm_fb, pass1_bpm)
                                    if fin_bpm_fb > 0.0 and fb_rel_se < best_fb_se:
                                        best_fb_se = fb_rel_se
                                        eff_n_m_fb = max(n_joint_2, int(round(float(n_joint_2) * oct_mul)))
                                        eff_n_acf_fb = max(1, int(round(float(n_joint_2) * oct_mul)))
                                        if raw_bpm > 0.0 and b_2 > 0.0:
                                            raw_ratio_fb = raw_bpm / b_2
                                            near_pow2_fb = 2.0 ** round(math.log2(raw_ratio_fb))
                                            if abs(raw_ratio_fb - near_pow2_fb) <= 0.05 * near_pow2_fb:
                                                eff_n_acf_fb = max(1, int(round(float(n_joint_2) * near_pow2_fb)))
                                        p2_cand_payload = {
                                            "sel_tau": seed_try,
                                            "raw_bpm": raw_bpm_fb,
                                            "raw_instab": i_2 * oct_mul,
                                            "sigma_t": s_2,
                                            "rel_inst": r_2,
                                            "n_m": eff_n_m_fb,
                                            "used_full_acf": True,
                                            "accepted_trunks": int(acc_trunks_2),
                                            "eff_n_acf": eff_n_acf_fb,
                                            "p2_acf_cov": (
                                                (float(n_joint_2) * tau_2 / float(denom_fb_2))
                                                if denom_fb_2 > 0
                                                else 0.0
                                            ),
                                            "final_bpm": fin_bpm_fb,
                                            "suspected_triple": suspected_triple,
                                            "triplet_r_contrast": triplet_r_contrast,
                                            "acf_pooled": fb_pooled_ref,
                                            "peaks_list": fb_peaks_ref,
                                            "cands": fb_cands_ref,
                                        }

                if p2_cand_payload is not None:
                    final_bpm_2 = p2_cand_payload["final_bpm"]
                    comb_meta_2: Dict[str, Any] = {}
                    target_tau_2 = 60000.0 / final_bpm_2
                    comb_phi_2, comb_rmse_2, comb_instab_2 = compute_best_phase_and_rmse(
                        diff_1000hz,
                        target_tau_2,
                        win_radius=eff_comb_win,
                        meta=comb_meta_2,
                        transition_guard=transition_guard,
                        transition_miss_beats=transition_miss_beats,
                        transition_resume_beats=transition_resume_beats,
                        comb_tol=comb_tol,
                        allow_phase_relock=True,
                    )
                    comb_meta_2.pop("_recalc_spans", None)
                    comb_n_matched_2 = int(comb_meta_2.get("n_matched", 0))
                    cov_2 = float(comb_meta_2.get("coverage", 0.0))

                    # 尺度无关防劣化门控：只要覆盖率与相对残差不稳定度无明显恶化，即直接采纳干净稳态段重算结果
                    no_degrade = (
                        comb_n_matched_2 >= 3
                        and cov_2 >= max(0.05, min(p1_cov * 0.88, p1_cov - 0.02), p1_cov - 0.08)
                        and (
                            comb_instab_2 <= p1_comb_instab + max(0.04, 0.15 * p1_comb_instab)
                            or cov_2 >= p1_cov + 0.05
                        )
                    )
                    if (
                        no_degrade
                        and 0.95 * pass1_bpm <= final_bpm_2 <= 1.05 * pass1_bpm
                        and p2_cand_payload["p2_acf_cov"] < 0.35 * p1_acf_cov
                        and p2_cand_payload["eff_n_acf"] < p1_n_acf
                        and comb_instab_2 > p1_comb_instab + 0.005
                    ):
                        no_degrade = False

                    if no_degrade:
                        sel_tau = p2_cand_payload["sel_tau"]
                        raw_bpm = p2_cand_payload["raw_bpm"]
                        raw_instab = p2_cand_payload["raw_instab"]
                        sigma_t = p2_cand_payload["sigma_t"]
                        rel_inst = p2_cand_payload["rel_inst"]
                        n_m = p2_cand_payload["n_m"]
                        used_full_acf = bool(p2_cand_payload["used_full_acf"])
                        final_bpm = final_bpm_2
                        fold_scale = final_bpm / raw_bpm if raw_bpm > 0.0 else 1.0
                        final_instab = raw_instab * fold_scale
                        steady_instab = final_bpm * rel_inst
                        tracked_tau = 60000.0 / raw_bpm
                        acf_pooled = p2_cand_payload["acf_pooled"]
                        peaks_list = p2_cand_payload["peaks_list"]
                        cands = p2_cand_payload["cands"]
                        bpm_recalc_applied = True
                        acc_trunks_p2 = int(p2_cand_payload.get("accepted_trunks", 0))
                        p2_trunk_count = (
                            acc_trunks_p2
                            if (used_full_acf and acc_trunks_p2 >= 1)
                            else max(1, sum(1 for seg in ranked_trunks if len(seg) >= win_len))
                        )
                        if p2_trunk_count == 1:
                            p2_single_trunk_acf = acf_primary
                            p2_single_trunk_diff = ranked_trunks[0]
                        else:
                            p2_single_trunk_acf = None
                            p2_single_trunk_diff = None

                        if meta is not None:
                            meta["suspected_triple_meter"] = p2_cand_payload["suspected_triple"]
                            if p2_cand_payload["triplet_r_contrast"] is not None:
                                meta["triplet_r_contrast"] = p2_cand_payload["triplet_r_contrast"]
                            else:
                                meta.pop("triplet_r_contrast", None)

                        comb_phi = comb_phi_2
                        comb_rmse = comb_rmse_2
                        comb_instab = comb_instab_2
                        comb_meta = comb_meta_2
                        if comb_meta.get("total_beats", 0) > 0 or comb_meta.get("raw_total_beats", 0) > 0:
                            comb_n_matched = int(comb_meta.get("n_matched", 0))
                            comb_total_beats = int(comb_meta.get("total_beats", 0))
                            comb_cov = float(comb_meta.get("coverage", 0.0))

    # 8. 独立后置小数位精修子模块（未触发 Pass-2，或单主干 Pass-2 仅裁除头/尾时在该唯一净主干上串联零开销预检；多主干 Pass-2 已分段联合 OLS 则跳过）
    post_acf: Optional[np.ndarray] = None
    post_diff: Optional[np.ndarray] = None
    if not bpm_recalc_applied and acf is not None:
        post_acf = acf
        post_diff = diff_1000hz
    elif bpm_recalc_applied and p2_trunk_count == 1 and p2_single_trunk_acf is not None:
        post_acf = p2_single_trunk_acf
        post_diff = p2_single_trunk_diff

    if post_acf is not None:
        post_ref = refine_post_v3_slope(
            acf=post_acf,
            diff_1000hz=post_diff,
            acf_pooled_len=len(acf_pooled),
            sel_tau=sel_tau,
            raw_bpm=raw_bpm,
            raw_instab=raw_instab,
            sigma_t=sigma_t,
            rel_inst=rel_inst,
            n_m=n_m,
            used_full_acf=used_full_acf,
            final_bpm=final_bpm,
            track_tol=track_tol,
        )
        if post_ref is not None:
            ref_final_bpm = float(post_ref["final_bpm"])
            ref_raw_bpm = float(post_ref["raw_bpm"])
            ref_raw_instab = float(post_ref["raw_instab"])
            ref_sigma_t = float(post_ref["sigma_t"])
            ref_rel_inst = float(post_ref["rel_inst"])
            ref_n_m = int(post_ref["n_m"])
            if diff_1000hz is not None and len(diff_1000hz) > 0 and ref_final_bpm > 0.0:
                comb_meta_ref: Dict[str, Any] = {}
                comb_phi_ref, comb_rmse_ref, comb_instab_ref = compute_best_phase_and_rmse(
                    diff_1000hz,
                    60000.0 / ref_final_bpm,
                    win_radius=eff_comb_win,
                    meta=comb_meta_ref,
                    transition_guard=transition_guard,
                    transition_miss_beats=transition_miss_beats,
                    transition_resume_beats=transition_resume_beats,
                    comb_tol=comb_tol,
                    allow_phase_relock=eff_trim_recalc,
                )
                comb_meta_ref.pop("_recalc_spans", None)
                n_matched_ref = int(comb_meta_ref.get("n_matched", 0))
                cov_ref = float(comb_meta_ref.get("coverage", 0.0))
                # [修正 2026-11-13] 「分段独立 ACF 消偏」(disjoint_acf_deskew) 豁免梳不稳定度不劣化门控：
                # 该路径仅在全曲谐波残差出现系统性阶跃/截距（曲中相位跳变）时触发，而梳不稳定度基于全曲单一网格，
                # 面对相位跳变时略带偏斜的 BPM 可用线性漂移“追”台阶、残差反而更小，结构性偏向未修正的偏斜解，
                # 导致消偏正确结果被自身否决（合成用例 160 BPM 跳变 70ms：159.9819 -> 应为 160.0000，113 ppm 被拒）。
                # 消偏自身已有严格护栏（>=2 个互不重叠 >=15s 独立分段、各段斜率极差 <=0.08%、修正幅度 <=±0.25%、
                # 禁止翻转八度），此处仅保留命中拍数与覆盖率门控；「单阶断口桥接」(single_gap_bridge) 仍走原门控。
                inst_gate_ok = comb_instab_ref <= comb_instab + max(0.02, 0.10 * comb_instab)
                if post_ref.get("reason") == "disjoint_acf_deskew":
                    inst_gate_ok = True
                if (
                    n_matched_ref >= 3
                    and cov_ref >= max(0.05, comb_cov - 0.03)
                    and inst_gate_ok
                ):
                    raw_bpm = ref_raw_bpm
                    raw_instab = ref_raw_instab
                    sigma_t = ref_sigma_t
                    rel_inst = ref_rel_inst
                    n_m = ref_n_m
                    final_bpm = ref_final_bpm
                    fold_scale = final_bpm / raw_bpm if raw_bpm > 0.0 else 1.0
                    final_instab = raw_instab * fold_scale
                    steady_instab = final_bpm * rel_inst
                    tracked_tau = 60000.0 / raw_bpm
                    bpm_recalc_applied = True
                    comb_phi = comb_phi_ref
                    comb_rmse = comb_rmse_ref
                    comb_instab = comb_instab_ref
                    comb_meta = comb_meta_ref
                    if comb_meta.get("total_beats", 0) > 0 or comb_meta.get("raw_total_beats", 0) > 0:
                        comb_n_matched = n_matched_ref
                        comb_total_beats = int(comb_meta.get("total_beats", 0))
                        comb_cov = cov_ref
            elif diff_1000hz is None and ref_final_bpm > 0.0 and ref_raw_bpm > 0.0:
                raw_bpm = ref_raw_bpm
                raw_instab = ref_raw_instab
                sigma_t = ref_sigma_t
                rel_inst = ref_rel_inst
                n_m = ref_n_m
                final_bpm = ref_final_bpm
                fold_scale = final_bpm / raw_bpm if raw_bpm > 0.0 else 1.0
                final_instab = raw_instab * fold_scale
                steady_instab = final_bpm * rel_inst
                tracked_tau = 60000.0 / raw_bpm
                bpm_recalc_applied = True
                comb_n_matched = n_m
                comb_cov = float(n_m) * tracked_tau / float(len(acf)) if len(acf) > 0 else 0.0

    if not math.isfinite(comb_cov):
        comb_cov = 0.0
    comb_cov = min(1.0, max(0.0, comb_cov))

    beat_display = (
        f"{comb_n_matched} / {comb_total_beats} 拍 ({comb_cov * 100.0:.1f}%)"
        if comb_total_beats > 0
        else f"{comb_n_matched} 拍 ({comb_cov * 100.0:.1f}%)"
    )

    if meta is not None:
        meta.update({
            "stage": chosen_stage,
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
            "coverage": comb_cov,
            "beat_display": beat_display,
            "rmse_ms": comb_rmse,
            "len_acf": len(acf_pooled),
            "acf_pooled": acf_pooled,
            "peaks": [{"lag": int(p), "val": float(acf_pooled[p])} for p in peaks_list],
            "candidates": [
                {"tau": float(t), "bpm": round(60000.0 / float(t), 3), "score": float(s)}
                for t, s in cands
            ],
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
            # [只读导出] 逐拍命中时刻：仅当与最终 n_matched 严格一致时透传（防止 n_m 回退路径导致错配）
            mbt = comb_meta.get("matched_beat_times")
            if isinstance(mbt, list) and len(mbt) == int(comb_n_matched) and "matched_beat_tau" in comb_meta:
                meta["matched_beat_times"] = mbt
                meta["matched_beat_tau"] = float(comb_meta["matched_beat_tau"])
        if eff_trim_recalc:
            meta["trim_recalc"] = True
            meta["bpm_recalc_applied"] = bpm_recalc_applied
            meta["pass1_bpm"] = pass1_bpm

    return final_bpm, final_instab, sigma_t, rel_inst
