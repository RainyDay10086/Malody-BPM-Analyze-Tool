"""
超越原版的增强优化模式 (mode="enhanced") 的全部决策逻辑。

本模块从 detector.select_best_tempo 的 exact=False 分支整体抽离而来，目的是：
  1. 物理隔离——detector.py 中已 100% 逐位还原的 exact 路径不受任何影响；
  2. 常量集中——原先散落在 6 段贪心补丁 (A0/A/B/C/D/E) 中的绝对魔法阈值
     全部提为 ENH_CFG 具名常量，便于审计与后续调参；
  3. 去重——严格追踪 (_track_full_song_exact) 与松弛追踪 (_relaxed_track)
     通过统一评估器 _evaluate 调用，消除重复代码；
  4. 可观测——每次选择都会把 coverage(周期解释全曲比例) 等相对指标写入
     可选的 meta 字典，供属性测试与未来"相对化"改造使用。

【重要设计说明 / 与原始方案的一处保守偏差】
原方案设想用 coverage 相对指标直接替换 D 段散板熔断的绝对阈值
(len(acf)<15000 且 n_matched<16)。但基于 36 首 golden 基线的实测发现：
  - 语料中没有任何曲目在 enhanced 下真正触发 D 熔断；
  - 《风帆，远行吧》enhanced 的 rel_inst = 0.1166 / 98.8894 ≈ 0.0011791，
    与熔断门限 0.0012 仅差 0.0000208；
若去掉"短时长"这一守卫、仅靠 coverage+rel_inst 判定，长曲中稀疏但有效的
追踪结果 (如被 B/C 成功救援的《昨日重现》83.759) 会被误熔断，直接违反
方案规定的"零回归"硬约束。因此本实现保留经现网验证的门限值 (提为具名常量)，
并把 coverage 作为元数据暴露，作为后续在更大验证集上再"相对化"的安全基础。

（重要说明：本方案与enhanced v2方案均已归档，不再在前端出现。由enhanced v3取精华去糟粕。留在这里诈尸仅仅是因为作者舍不得删。）
"""

import ctypes
import math
import numpy as np

from .detector import _track_full_song_exact
from .filters import _c_dsp, require_c_dsp

# 阶段 2 亚谐波消歧使用的周期边界 (与 detector.select_best_tempo 一致)
TAU_MIN = 272.72727272727275   # 220.0 BPM
TAU_MAX = 545.4545454545455    # 110.0 BPM

# =============================================================================
# 增强模式全部可调常量 (集中审计入口)。数值与重构前逐一对齐，保证零回归。
# =============================================================================
ENH_CFG = {
    # --- 候选周期搜索范围 ---
    "tau_slow_max": 800.0,        # 75.0 BPM 周期上限
    # --- 阶段 1 次级多谐波救援门限 ---
    "rescue_min_harmonics": 5,    # 次级救援候选最少谐波匹配数
    "rescue_min_mean_ratio": 0.5, # 次级救援候选均值与首峰比值下限 (mean_h > acf[p_lag] * 0.5)
    # --- 严格 / 松弛追踪最少匹配拍 ---
    "min_beats_strict": 4,        # B 段候选回退最少匹配拍 (对齐 exact)
    "min_beats_relaxed": 8,       # C 段单拍切分跨越最少匹配拍
    "relaxed_consec_miss": 1,     # C 段允许的连续失配 (单拍切分跨越) 次数
    "relaxed_tol": 9,             # C 段 |local_max_idx - target| 容差
    # --- A0 全局三连音 3:2 伪谐波消歧 ---
    "triplet_min_beats": 8,       # 3x 候选最少匹配拍
    "triplet_rel_gain": 1.2,      # 3x 不稳定度上限系数 (i32 <= raw_instab * 1.2)
    "triplet_tau_tol": 10.0,      # |cand_tau - 1.5*best_tau| 命中容差
    # --- A 强弱拍交替亚谐波优选 vs 高速早断锁长周期精修 ---
    "subharm_instab_gate": 0.04,  # 1x 时域残差门限 (raw_instab > 0.04 才考虑切换)
    "subharm_rel_gain": 0.5,      # 2x 需显著更稳 (i2 < 0.5 * raw_instab)
    "subharm_min_beats": 20,      # 2x 长程稳态最少匹配拍
    "early_lock_max_beats": 10,   # 1x 早断锁判定 (n_matched < 10)
    "early_lock_max_sigma": 2.0,  # 1x 早断锁判定 (sigma_t <= 2.0)
    "snap_grid": 0.005,           # 切换亚谐波后的 0.005 网格吸附步长
    "snap_tol": 0.002,            # 网格吸附容差
    # --- D 短时散板 / 无鼓点人声熔断 ---
    "fuse_max_len_acf": 15000,    # 仅对短曲 (len(acf) < 15000 ≈ <30s) 熔断
    "fuse_max_beats": 16,         # 匹配拍过少 (n_matched < 16)
    "fuse_rel_inst": 0.0012,      # 相对不稳定度过高 (rel_inst > 0.0012)
    # --- E 回归后双向倍频归一 ---
    "fold_min_bpm": 75.0,         # 低于此值需向上折叠
    "fold_max_bpm": 220.0,        # 高于此值需向下折叠
    "triple_fold_peak_gain": 1.5, # 三拍子检测: peak3 > peak2 * 1.5
    "triple_cand_tol": 8.0,       # 三拍子检测: cur_tau/3 处存在候选的容差
    "triple_peak_win": 8,         # 三拍子检测峰窗半径
    # --- G 消极假设全曲扫描与主干置信度综合选举 ---
    "groove_win_radius": 22,      # 搜索窗口半径 (自适应覆盖 10~15 ms 自然人手微时差)
    "groove_tol": 18,             # 局部极大值允许偏差门限
    "groove_min_hits": 12,        # 全曲累计最少命中拍数门限 (防散板)
    "groove_min_coverage": 0.15,  # 最低全曲周期解释率 (防散板)
    # --- octave_norm 常规击打区间 ---
    "octave_norm_min": 110.0,
}


def _fold_tau_to_range(tau: float) -> float:
    """把任意周期折叠到 [TAU_MIN, tau_slow_max] 常规拍频周期区间 (C 段前置处理)。"""
    cur_tau = float(tau)
    while cur_tau > ENH_CFG["tau_slow_max"]:
        cur_tau *= 0.5
    while cur_tau < TAU_MIN:
        cur_tau *= 2.0
    return cur_tau


def _track_full_song_with_tol(
    acf: np.ndarray,
    start_tau: float,
    tol: int = 8,
    win: int = 10,
    min_beats: int = 4,
) -> tuple[float, float, float, float, int]:
    """严格对齐 C 语言数学公式的全曲自相关峰值线性回归追踪，支持动态 tol 与 win。"""
    max_lag = len(acf)
    cur_tau = float(start_tau)
    target = round(cur_tau)
    S_kk = 0.0
    S_kt = 0.0
    S_tt = 0.0
    n_matched = 0
    k = 1.0

    while target + win < max_lag and target - win >= 0:
        base = target - win
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
            ym1 = float(acf[c - 1])
            y0 = float(acf[c])
            yp1 = float(acf[c + 1])
            denom = (y0 - ym1) + (y0 - yp1)
            delta = 0.5 * (yp1 - ym1) / denom if abs(denom) > 1e-12 else 0.0
            exact_t = float(c) + delta
            if not math.isfinite(exact_t):
                return 0.0, 0.0, 999.0, 1.0, 0
            S_kk += k * k
            S_kt += k * exact_t
            S_tt += exact_t * exact_t
            cur_tau = S_kt / S_kk
            target = round(exact_t + cur_tau)
            k += 1.0
        else:
            break

    if n_matched < min_beats or S_kt <= 0.0:
        return 0.0, 0.0, 999.0, 1.0, n_matched

    raw_bpm = 60000.0 * S_kk / S_kt
    term = (((S_kk * S_tt) / S_kt) / S_kt - 1.0) / (n_matched - 1.0) * n_matched
    raw_instab = math.sqrt(max(0.0, term)) * raw_bpm
    var_t = (S_tt - (S_kt * S_kt) / S_kk) / (n_matched - 1.0)
    sigma_t = math.sqrt(max(0.0, var_t))
    rel_inst = raw_instab / raw_bpm
    return raw_bpm, raw_instab, sigma_t, rel_inst, n_matched


def _relaxed_track_with_tol(
    acf: np.ndarray,
    start_tau: float,
    tol: int = 9,
    win: int = 11,
    consec_miss_limit: int = 1,
    min_beats: int = 8,
) -> tuple[float, float, float, float, int]:
    """严格对齐 C 语言数学公式的切分松弛回归追踪，支持动态 tol 与 win。"""
    max_lag = len(acf)
    cur_tau = float(start_tau)
    target = round(cur_tau)
    S_kk = 0.0
    S_kt = 0.0
    S_tt = 0.0
    n_matched = 0
    consec_miss = 0
    k = 1.0

    while target + win < max_lag and target - win >= 0:
        base = target - win
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
            consec_miss = 0
            ym1 = float(acf[c - 1])
            y0 = float(acf[c])
            yp1 = float(acf[c + 1])
            denom = (y0 - ym1) + (y0 - yp1)
            delta = 0.5 * (yp1 - ym1) / denom if abs(denom) > 1e-12 else 0.0
            exact_t = float(c) + delta
            if not math.isfinite(exact_t):
                return 0.0, 0.0, 999.0, 1.0, 0
            S_kk += k * k
            S_kt += k * exact_t
            S_tt += exact_t * exact_t
            cur_tau = S_kt / S_kk
            target = round(exact_t + cur_tau)
        else:
            consec_miss += 1
            if consec_miss > consec_miss_limit:
                break
            target = round(float(target) + cur_tau)
        k += 1.0

    if n_matched < min_beats or S_kt <= 0.0:
        return 0.0, 0.0, 999.0, 1.0, n_matched

    raw_bpm = 60000.0 * S_kk / S_kt
    term = (((S_kk * S_tt) / S_kt) / S_kt - 1.0) / (n_matched - 1.0) * n_matched
    raw_instab = math.sqrt(max(0.0, term)) * raw_bpm
    var_t = (S_tt - (S_kt * S_kt) / S_kk) / (n_matched - 1.0)
    sigma_t = math.sqrt(max(0.0, var_t))
    rel_inst = raw_instab / raw_bpm
    return raw_bpm, raw_instab, sigma_t, rel_inst, n_matched


def _track_exact_sanitized(
    acf: np.ndarray,
    start_tau: float,
    groove_tol: int | None = None
) -> tuple[float, float, float, float, int]:
    """增强模式专用严格追踪封装：未指定 groove_tol 时 100% 走 C 动态库基线；指定时应用参数化容差。"""
    if groove_tol is None:
        b, ins, sig, rel, n = _track_full_song_exact(acf, start_tau)
    else:
        b, ins, sig, rel, n = _track_full_song_with_tol(
            acf, start_tau,
            tol=int(groove_tol),
            win=int(groove_tol) + 2,
            min_beats=int(ENH_CFG["min_beats_strict"])
        )
    if b > 0.0:
        if not math.isfinite(ins) or ins < 0.0:
            ins = 0.0
        if not math.isfinite(sig) or sig < 0.0:
            sig = 0.0
        if not math.isfinite(rel) or rel < 0.0:
            rel = 0.0
    return b, ins, sig, rel, n


def _relaxed_track(
    acf: np.ndarray,
    start_tau: float,
    groove_tol: int | None = None
):
    """
    C 段"允许最多 relaxed_consec_miss 次单拍切分跨越"的松弛全曲回归追踪。
    针对《围兜狸猫，跳跳跳》等强切分曲目，在严格追踪熔断后提供救援。
    返回 (raw_bpm, raw_instab, sigma_t, rel_inst, n_matched)；失败返回 (0,0,999,1,n_m)。
    """
    if groove_tol is None:
        consec_miss_limit = int(ENH_CFG["relaxed_consec_miss"])
        tol = int(ENH_CFG["relaxed_tol"])
        min_beats = int(ENH_CFG["min_beats_relaxed"])

        acf_c = np.ascontiguousarray(acf, dtype=np.float32)
        out_stats = (ctypes.c_double * 4)()
        n_m = int(require_c_dsp(_c_dsp).track_full_song_relaxed_c(
            acf_c.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
            len(acf_c),
            float(start_tau),
            consec_miss_limit,
            tol,
            min_beats,
            out_stats,
        ))
        return float(out_stats[0]), float(out_stats[1]), float(out_stats[2]), float(out_stats[3]), n_m
    else:
        return _relaxed_track_with_tol(
            acf, start_tau,
            tol=int(groove_tol),
            win=int(groove_tol) + 2,
            consec_miss_limit=int(ENH_CFG["relaxed_consec_miss"]),
            min_beats=int(ENH_CFG["min_beats_relaxed"])
        )


def _coverage(n_matched: int, tau: float, len_acf: int) -> float:
    """周期解释全曲的比例 (量纲无关的可观测指标，供审计/属性测试)。"""
    if len_acf <= 0 or tau <= 0.0:
        return 0.0
    return float(n_matched) * float(tau) / float(len_acf)


def _stage_a0_triplet(acf, candidates, best_cand_idx, best_tau, result, groove_tol: int | None = None):
    """
    A0: 全局三连音 3:2 伪谐波消歧。
    原版阶段 2 在 420 BPM 三连音下贪婪停在 2x 谐波 210 BPM，忽略了同在常规区间内
    且峰高均值更高、更稳定的 3x 真实正拍 140 BPM (如《沸き上がる闘志》)。
    返回 (best_cand_idx, best_tau, result)，result=(bpm,instab,sigma,rel,n)。
    """
    raw_bpm, raw_instab, sigma_t, rel_inst, n_matched = result
    if raw_bpm > 0.0:
        for j in range(best_cand_idx + 1, len(candidates)):
            cand_tau = candidates[j][0]
            if cand_tau <= ENH_CFG["tau_slow_max"] and \
                    abs(cand_tau - 1.5 * best_tau) <= ENH_CFG["triplet_tau_tol"]:
                b32, i32, s32, r32, n32 = _track_exact_sanitized(acf, cand_tau, groove_tol=groove_tol)
                if b32 > 0.0 and n32 >= ENH_CFG["triplet_min_beats"] and \
                        i32 <= raw_instab * ENH_CFG["triplet_rel_gain"]:
                    best_cand_idx = j
                    best_tau = cand_tau
                    result = (b32, i32, s32, r32, n32)
                break
    return best_cand_idx, best_tau, result


def _stage_a_subharmonic(acf, best_tau, result, groove_tol: int | None = None):
    """
    A: 强弱拍交替亚谐波优选 vs 高速曲目早断锁长周期精修。
    - 1x 长程跟踪但时域残差大 (如《知更鸟》8 分音符 160 BPM 强弱交替) → 切至 4 分基频；
    - 1x 因局部加花过早失配但 2x 能长程稳态跟踪 (如《blsl》200 BPM) → 借 2x 斜率保原八度。
    返回更新后的 result。
    """
    raw_bpm, raw_instab, sigma_t, rel_inst, n_matched = result
    if raw_bpm > 0.0 and raw_instab > ENH_CFG["subharm_instab_gate"] and \
            best_tau * 2.0 <= ENH_CFG["tau_slow_max"]:
        b2, i2, s2, r2, n2 = _track_exact_sanitized(acf, best_tau * 2.0, groove_tol=groove_tol)
        if b2 > 0.0 and n2 >= ENH_CFG["subharm_min_beats"] and \
                i2 < ENH_CFG["subharm_rel_gain"] * raw_instab:
            if n_matched < ENH_CFG["early_lock_max_beats"] and sigma_t <= ENH_CFG["early_lock_max_sigma"]:
                # 高速早断锁: 保留原八度，借用 2x 周期的高精度回归斜率
                result = (b2 * 2.0, i2 * 2.0, s2, r2, n2)
            else:
                # 1x 本身不稳: 切换至 2x 亚谐波基频
                if abs(math.remainder(b2, ENH_CFG["snap_grid"])) <= ENH_CFG["snap_tol"]:
                    b2 = round(b2 * 200.0) / 200.0
                result = (b2, i2, s2, r2, n2)
    return result


def _stage_b_fallback(acf, candidates, best_tau, groove_tol: int | None = None):
    """
    B: 首选候选因前奏切分/局部失配熔断 (n_matched < 4) 时，
    按峰高均值降序回退检验其余高分候选。成功返回 result，否则返回 None。
    """
    for cand_tau, _ in sorted(candidates, key=lambda x: x[1], reverse=True):
        if abs(cand_tau - best_tau) < 1e-6:
            continue
        b_alt, i_alt, s_alt, r_alt, n_alt = _track_exact_sanitized(acf, cand_tau, groove_tol=groove_tol)
        if b_alt > 0.0 and n_alt >= ENH_CFG["min_beats_strict"]:
            return (b_alt, i_alt, s_alt, r_alt, n_alt)
    return None


def _stage_c_syncopation(acf, candidates, groove_tol: int | None = None):
    """
    C: 所有候选严格单次失配均 < 4 拍时，允许最多 relaxed_consec_miss 次单拍切分跨越
    (针对《围兜狸猫，跳跳跳》等强切分曲目)。成功返回 result，否则返回 None。
    """
    for cand_tau, _ in sorted(candidates, key=lambda x: x[1], reverse=True):
        cur_tau = _fold_tau_to_range(cand_tau)
        res = _relaxed_track(acf, cur_tau, groove_tol=groove_tol)
        if res[0] > 0.0:
            return res
    return None



def _scan_full_song_harmonics(
    acf: np.ndarray,
    tau: float,
    win_radius: int = 22,
    tol: int = 18,
    min_hits: int = 12,
    min_coverage: float = 0.15,
):
    """
    对指定周期 tau 遍历 100% 全曲自相关滞后区间，绝不因中途失配提前 break 截断。
    使用 C 原生动态库扩展加速。
    """
    acf_c = np.ascontiguousarray(acf, dtype=np.float32)
    out_stats = (ctypes.c_double * 6)()
    n_hits = int(require_c_dsp(_c_dsp).scan_full_song_harmonics_c(
        acf_c.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
        len(acf_c),
        float(tau),
        int(win_radius),
        int(tol),
        int(min_hits),
        float(min_coverage),
        out_stats
    ))
    return (
        float(out_stats[0]), float(out_stats[1]), float(out_stats[2]),
        float(out_stats[3]), n_hits, float(out_stats[4]), float(out_stats[5])
    )


def _stage_a0_triplet_comb(acf, candidates, best_cand_idx, best_tau, result, win_r=22, tol=18):
    """
    Enhanced 2.0 专用 A0: 全局三连音 3:2 伪谐波梳状扫描消歧。
    使用 _scan_full_song_harmonics 替代易断链的 _track_full_song_exact，
    在全曲滞后区间消极假设评估 3x 周期，避免因前奏/中途变速导致三连音消歧误判。
    """
    raw_bpm, raw_instab, sigma_t, rel_inst, n_matched = result
    if raw_bpm > 0.0:
        for j in range(best_cand_idx + 1, len(candidates)):
            cand_tau = candidates[j][0]
            if cand_tau <= ENH_CFG["tau_slow_max"] and \
                    abs(cand_tau - 1.5 * best_tau) <= ENH_CFG["triplet_tau_tol"]:
                res = _scan_full_song_harmonics(
                    acf, cand_tau, win_radius=win_r, tol=tol,
                    min_hits=int(ENH_CFG["triplet_min_beats"]), min_coverage=0.15
                )
                b32, i32, s32, r32, n32 = res[0], res[1], res[2], res[3], res[4]
                if b32 > 0.0 and n32 >= ENH_CFG["triplet_min_beats"] and \
                        i32 <= raw_instab * ENH_CFG["triplet_rel_gain"]:
                    best_cand_idx = j
                    best_tau = cand_tau
                    result = (b32, i32, s32, r32, n32)
                break
    return best_cand_idx, best_tau, result


def _stage_a_subharmonic_comb(acf, best_tau, result, win_r=22, tol=18):
    """
    Enhanced 2.0 专用 A: 强弱拍交替亚谐波优选 vs 高速曲目早断锁长周期精修 (梳状扫描版)。
    使用 _scan_full_song_harmonics 替代 _track_full_song_exact，保证全曲抗断链。
    """
    raw_bpm, raw_instab, sigma_t, rel_inst, n_matched = result
    if raw_bpm > 0.0 and raw_instab > ENH_CFG["subharm_instab_gate"] and \
            best_tau * 2.0 <= ENH_CFG["tau_slow_max"]:
        res2 = _scan_full_song_harmonics(
            acf, best_tau * 2.0, win_radius=win_r, tol=tol,
            min_hits=int(ENH_CFG["subharm_min_beats"]), min_coverage=0.15
        )
        b2, i2, s2, r2, n2 = res2[0], res2[1], res2[2], res2[3], res2[4]
        if b2 > 0.0 and n2 >= ENH_CFG["subharm_min_beats"] and \
                i2 < ENH_CFG["subharm_rel_gain"] * raw_instab:
            if n_matched < ENH_CFG["early_lock_max_beats"] and sigma_t <= ENH_CFG["early_lock_max_sigma"]:
                # 高速早断锁: 保留原八度，借用 2x 周期的高精度回归斜率
                result = (b2 * 2.0, i2 * 2.0, s2, r2, n2)
            else:
                # 1x 本身不稳: 切换至 2x 亚谐波基频
                if abs(math.remainder(b2, ENH_CFG["snap_grid"])) <= ENH_CFG["snap_tol"]:
                    b2 = round(b2 * 200.0) / 200.0
                result = (b2, i2, s2, r2, n2)
    return result


def _stage_g_full_song_vote(acf, candidates, rescue_candidates=None, peaks=None, groove_tol: int | None = None):
    """
    G: 消极假设全曲无中断扫描与主干置信度综合选举 (Stage G)。
    当曲目中途存在加花、变奏、rit.、accel.、Rubato、局部静音或人手微时差导致常规递推断链时，
    遍历候选池与前 10 大高能主峰，对每个周期在全曲 100% 滞后区间进行全局网格扫描，
    记录 Hits 与 Misses (绝不中途 break 截断)，由全曲覆盖率与稳态最强的 Dominant BPM 胜出。
    """
    if groove_tol is not None:
        tol = int(groove_tol)
        win_r = int(groove_tol) + 4
    else:
        win_r = int(ENH_CFG.get("groove_win_radius", 22))
        tol = int(ENH_CFG.get("groove_tol", 18))
    min_hits = int(ENH_CFG.get("groove_min_hits", 12))
    min_cov = float(ENH_CFG.get("groove_min_coverage", 0.15))

    cands_to_test = set()
    all_cands = (candidates or []) + (rescue_candidates or [])
    for c_tau, _ in all_cands:
        cur = float(c_tau)
        while cur > ENH_CFG["tau_slow_max"]: cur *= 0.5
        while cur < TAU_MIN: cur *= 2.0
        cands_to_test.add(round(cur, 3))
        if c_tau > ENH_CFG["tau_slow_max"]:
            cur3 = float(c_tau) / 3.0
            while cur3 > ENH_CFG["tau_slow_max"]: cur3 *= 0.5
            while cur3 < TAU_MIN: cur3 *= 2.0
            cands_to_test.add(round(cur3, 3))

    if peaks:
        sorted_p = sorted(
            peaks,
            key=lambda x: x["val"] if isinstance(x, dict) else acf[x],
            reverse=True
        )[:10]
        for p in sorted_p:
            p_lag = float(p["lag"] if isinstance(p, dict) else p)
            cur = p_lag
            while cur > ENH_CFG["tau_slow_max"]: cur *= 0.5
            while cur < TAU_MIN: cur *= 2.0
            cands_to_test.add(round(cur, 3))
            if p_lag > ENH_CFG["tau_slow_max"]:
                cur3 = p_lag / 3.0
                while cur3 > ENH_CFG["tau_slow_max"]: cur3 *= 0.5
                while cur3 < TAU_MIN: cur3 *= 2.0
                cands_to_test.add(round(cur3, 3))

    best_res = None
    best_score = -1.0

    for tau in sorted(cands_to_test):
        res = _scan_full_song_harmonics(
            acf, tau, win_radius=win_r, tol=tol, min_hits=min_hits, min_coverage=min_cov
        )
        if res[0] > 0.0 and res[6] > best_score:
            best_score = res[6]
            best_res = (res[0], res[1], res[2], res[3], res[4])

    return best_res


def _stage_d_rubato_fuse(result, len_acf) -> bool:
    """
    D: 短时散板 / 无鼓点人声片段伪周期熔断 (如《昨日重现》20s 散板前奏)。
    仅对"短曲 + 匹配拍过少 + 相对不稳定度过高"三者同时成立时熔断。
    保留 len(acf) 短时长守卫以避免误伤长曲中稀疏但有效的救援结果 (见模块 docstring)。
    """
    _, _, _, rel_inst, n_matched = result
    return (len_acf < ENH_CFG["fuse_max_len_acf"]
            and n_matched < ENH_CFG["fuse_max_beats"]
            and rel_inst > ENH_CFG["fuse_rel_inst"])


def _stage_e_octave_fold(raw_bpm, acf, candidates, octave_norm) -> float:
    """
    E: 回归后双向倍频归一。自动识别三拍子/六拍子 (3x 小节亚谐波) 与
    二四/四四拍 (2^n 亚谐波)，将 <75 BPM 折叠至常规区间；>220 BPM 无条件折半；
    octave_norm 时进一步把 <110 BPM 上折至常规击打区间。
    """
    fold_min = ENH_CFG["fold_min_bpm"]
    fold_max = ENH_CFG["fold_max_bpm"]
    win = ENH_CFG["triple_peak_win"]

    while raw_bpm < fold_min:
        cur_tau = 60000.0 / raw_bpm
        tau2 = int(round(cur_tau / 2.0))
        tau3 = int(round(cur_tau / 3.0))
        is_triple = False
        if raw_bpm * 3.0 <= fold_max and tau3 - win >= 0 and tau2 + win < len(acf):
            peak2 = float(np.max(acf[tau2 - win : tau2 + win + 1]))
            peak3 = float(np.max(acf[tau3 - win : tau3 + win + 1]))
            has_cand3 = any(
                abs(c_tau - cur_tau / 3.0) <= ENH_CFG["triple_cand_tol"]
                for c_tau, _ in candidates
            )
            if peak3 > 0.0 and peak3 > peak2 * ENH_CFG["triple_fold_peak_gain"] and has_cand3:
                is_triple = True
        if is_triple:
            raw_bpm *= 3.0
        else:
            raw_bpm *= 2.0

    if octave_norm:
        while raw_bpm < ENH_CFG["octave_norm_min"]:
            raw_bpm *= 2.0
    while raw_bpm > fold_max:
        raw_bpm /= 2.0
    return raw_bpm


def select_best_enhanced(
    acf: np.ndarray,
    candidates: list,
    best_tau: float,
    best_cand_idx: int,
    octave_norm: bool = False,
    meta: dict | None = None,
    rescue_candidates: list | None = None,
    peaks: list | None = None,
    groove_tol: int | None = None,
) -> tuple[float, float, float, float]:
    """
    增强优化模式主入口 (从 detector.select_best_tempo 的 exact=False 分支迁入)。
    入参 best_tau / best_cand_idx / candidates 均为阶段 1+2 (与 exact 共享) 的产物。
    返回与 detector 一致的 (raw_bpm, raw_instab, sigma_t, rel_inst)。

    决策顺序 (与重构前完全一致，仅重构为独立可调用的 stage 函数):
      base 严格追踪 → A0 三连音 3:2 → A 亚谐波/早断锁 → B 候选回退 →
      C 单拍切分跨越 → (可选) B/C 次级多谐波救援 → G 消极假设全曲扫描与主干选举 → D 散板熔断 → E 倍频归一。
    """
    if groove_tol is not None:
        if not isinstance(groove_tol, int) or isinstance(groove_tol, bool) or not (5 <= groove_tol <= 60):
            raise ValueError(f"groove_tol 必须为 5 到 60 之间的正整数，当前传入: {groove_tol!r}")
        if meta is not None:
            meta["groove_tol"] = int(groove_tol)

    len_acf = len(acf)
    stage = "base"

    # base: 首选周期全曲回归 (按 groove_tol 参数化容差，None 时 100% 走 C 原生基线)
    result = _track_exact_sanitized(acf, best_tau, groove_tol=groove_tol)

    # A0 / A: base 成功时的三连音与亚谐波优选
    prev = result
    best_cand_idx, best_tau, result = _stage_a0_triplet(
        acf, candidates, best_cand_idx, best_tau, result, groove_tol=groove_tol
    )
    if result is not prev:
        stage = "a0"
    prev = result
    result = _stage_a_subharmonic(acf, best_tau, result, groove_tol=groove_tol)
    if result is not prev:
        stage = "a"

    # B / C: base (及 A0/A) 失败时的救援
    if result[0] <= 0.0 and len(candidates) > 1:
        alt = _stage_b_fallback(acf, candidates, best_tau, groove_tol=groove_tol)
        if alt is not None:
            result = alt
            stage = "b"
    if result[0] <= 0.0:
        sync = _stage_c_syncopation(acf, candidates, groove_tol=groove_tol)
        if sync is not None:
            result = sync
            stage = "c"

    # B_rescue / C_rescue: 主候选池全部失败时，尝试阶段 1 因首峰尖峰门限被拒的多谐波次级候选
    if result[0] <= 0.0 and rescue_candidates:
        alt_r = _stage_b_fallback(acf, rescue_candidates, -1.0, groove_tol=groove_tol)
        if alt_r is not None:
            result = alt_r
            stage = "b_rescue"
    if result[0] <= 0.0 and rescue_candidates:
        sync_r = _stage_c_syncopation(acf, rescue_candidates, groove_tol=groove_tol)
        if sync_r is not None:
            result = sync_r
            stage = "c_rescue"

    # G: 消极假设全曲无中断扫描与主干置信度综合选举 (Stage G)
    if result[0] <= 0.0:
        vote_g = _stage_g_full_song_vote(acf, candidates, rescue_candidates, peaks, groove_tol=groove_tol)
        if vote_g is not None:
            result = vote_g
            stage = "g"

    raw_bpm, raw_instab, sigma_t, rel_inst, n_matched = result
    if raw_bpm <= 0.0:
        if meta is not None:
            meta.update({"stage": "fail", "bpm": 0.0, "n_matched": n_matched, "coverage": 0.0})
        return 0.0, 0.0, 999.0, 1.0

    # 同步实际胜出周期的 best_tau (确保 Stage A/B/C 切换周期后 coverage 计算与真实胜出周期一致)
    best_tau = 60000.0 / raw_bpm

    # D: 短时散板熔断
    if _stage_d_rubato_fuse(result, len_acf):
        if meta is not None:
            meta.update({"stage": "d_fuse", "bpm": 0.0, "n_matched": n_matched,
                         "coverage": _coverage(n_matched, best_tau, len_acf)})
        return 0.0, 0.0, 999.0, 1.0

    # E: 倍频归一
    final_bpm = _stage_e_octave_fold(raw_bpm, acf, candidates, octave_norm)
    fold_scale = final_bpm / raw_bpm

    if meta is not None:
        meta.update({
            "stage": stage,
            "best_tau": best_tau,
            "bpm": final_bpm,
            "raw_bpm": raw_bpm,
            "instab": raw_instab,
            "final_instab": final_bpm * rel_inst,
            "fold_scale": fold_scale,
            "sigma_t": sigma_t,
            "rel_inst": rel_inst,
            "n_matched": n_matched,
            "coverage": _coverage(n_matched, best_tau, len_acf),
            "len_acf": len_acf,
        })

    return final_bpm, raw_instab, sigma_t, rel_inst


# =============================================================================
# 增强优化模式 2.0 (enhanced_v2) 逻辑已整体物理迁移至独立子模块 malody_bpm/enhanced_v2.py
# =============================================================================
from .enhanced_v2 import select_best_enhanced_v2



