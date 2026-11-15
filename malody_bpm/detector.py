import ctypes
import math
import numpy as np
from .decoder import decode_audio_to_pcm, normalize_channel_mode
from .envelope import extract_onset_envelope
from .autocorr import compute_autocorrelation_fft, estimate_instability
from .harmonic import resolve_harmonic_quantization
from . import filters as _filters


def _track_full_song_exact(acf: np.ndarray, start_tau: float) -> tuple[float, float, float, float, int]:
    """
    Malody 0x1000c0353 (Windows 0x4749e5) 阶段 3 全曲自相关峰值线性回归追踪：
    - 递推预测下一拍中心 target = lrint(exact_t + cur_tau)
    - 搜索 [target - 10, target + 10] 局部极大值
    - 严格要求 acf[local_max_idx] > 0 且 |local_max_idx - target| <= 8，首次失配立即终止
    - 要求至少匹配 n_matched >= 4 拍
    """
    acf_c = np.ascontiguousarray(acf, dtype=np.float32)
    out_stats = (ctypes.c_double * 4)()
    n_matched = int(_filters.require_c_dsp(_filters._c_dsp).track_full_song_exact_c(
        acf_c.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
        len(acf_c),
        start_tau,
        out_stats
    ))
    return float(out_stats[0]), float(out_stats[1]), float(out_stats[2]), float(out_stats[3]), n_matched


def select_best_tempo(
    acf: np.ndarray,
    octave_norm: bool = False,
    exact: bool = True,
    meta: dict | None = None,
    mode: str = "exact",
    groove_tol: int | None = None,
    diff_1000hz: np.ndarray | None = None,
    pcm_duration_ms: float | None = None,
    transition_guard: float | int | None = None,
    transition_miss_beats: int = 4,
    transition_resume_beats: int = 3,
    track_tol: int = 8,
    comb_tol: int | None = None,
    acf_decay_ratio: float = 0.7,
    cand_update_ratio: float = 1.25,
    trim_recalc: bool | None = None,
    sub_trunk_min_sec: int = 15,
    halve_contrast_ratio: float = 1.10,
) -> tuple[float, float, float, float]:
    """
    基于 Malody 0x1000bfa7c (Windows 0x4741f0) 的三阶段时延搜索与全局回归测速算法：
    - exact=True:  100% 还原 Malody 原版汇编逻辑 (阶段 1 动态斜率更新 + 阶段 2 单向 220 BPM 亚谐波消歧 + 阶段 3 严格单次失配回归)
    - exact=False: 超越原版的增强优化模式：
                   1. 小节级稳态追踪 + 双向 2^n 倍频归一：保留长周期小节锚点的高精度低方差回归斜率，回归后再将 <75 BPM 或 >220 BPM 按 2^n 倍率折叠至常规拍频区间 [75, 220] BPM
                   2. 强弱拍交替亚谐波优选：当高频细分拍 (150~220 BPM) 呈现强弱拍交替抖动 (inst > 0.04) 且 2 倍周期亚谐波全曲回归显著更稳时 (如《使一颗心免于哀伤》79.995 BPM)，自动优选基频周期；若属于高速曲目单拍早断锁而 2 倍长周期更稳 (如《blsl》200 BPM)，则借用 2 倍周期的高精度斜率同时保持原八度
                   3. 多候选容错与切分跨越：当首选候选因前奏切分音导致 n_matched < 4 熔断时 (如《围兜狸猫，跳跳跳》)，自动回退检验次优高分候选及单拍切分跨越
                   4. 短时散板/无鼓点人声熔断：对短时高残差散板片段 (如《昨日重现》20s 散板前奏) 自动熔断为 0.0
    :param track_tol: 自相关谐波回归容差 (5~25ms 整数，默认 8ms)
    :param comb_tol: 时域脉冲梳节拍匹配窗口半宽 (10~50ms 整数，若为 None 默认 35ms)
    :param trim_recalc: 仅在 enhanced_v3 / force_v3 生效，分段相位重锁与剔除变速二次重算开关 (默认 None 时随 transition_guard > 0 自动开启；True/False 显式强制开/关)
    :param sub_trunk_min_sec: 仅在 enhanced_v3 生效，副主干进入二次多主干联合 OLS 的最小有效时长 (8~15 秒整数，默认 15 秒)
    :param halve_contrast_ratio: 仅在 enhanced_v3 生效，首次定位于 240~300.01+ BPM (199.99~250ms) 时折半的强弱拍对比度门限 (1.0~2.0，默认 1.10；1.0=总是折半，2.0=几乎不折半)
    返回 (raw_bpm, raw_instability, sigma_t, rel_instability)。
    """
    if trim_recalc is not None and not isinstance(trim_recalc, (bool, np.bool_)):
        raise ValueError(f"trim_recalc 必须为 None 或布尔类型，当前传入: {trim_recalc!r}")

    norm_trim_recalc = None if trim_recalc is None else bool(trim_recalc)

    if meta is not None:
        meta.setdefault("suspected_triple_meter", False)

    if mode in ("force_v3", "force_fit", "v3"):
        from .force_v3 import select_best_force_v3
        return select_best_force_v3(
            diff_1000hz=diff_1000hz if diff_1000hz is not None else np.array([]),
            acf=acf,
            meta=meta,
            octave_norm=octave_norm,
            transition_guard=transition_guard,
            transition_miss_beats=transition_miss_beats,
            transition_resume_beats=transition_resume_beats,
            comb_tol=comb_tol,
            trim_recalc=norm_trim_recalc,
        )

    if mode in ("enhanced_v3", "enhanced3", "v3_enhanced", "enh_v3"):
        from .enhanced_v3 import select_best_enhanced_v3
        return select_best_enhanced_v3(
            acf=acf,
            diff_1000hz=diff_1000hz,
            octave_norm=octave_norm,
            meta=meta,
            pcm_duration_ms=pcm_duration_ms,
            transition_guard=transition_guard,
            transition_miss_beats=transition_miss_beats,
            transition_resume_beats=transition_resume_beats,
            track_tol=track_tol,
            comb_tol=comb_tol,
            acf_decay_ratio=acf_decay_ratio,
            cand_update_ratio=cand_update_ratio,
            trim_recalc=norm_trim_recalc,
            sub_trunk_min_sec=sub_trunk_min_sec,
            halve_contrast_ratio=halve_contrast_ratio,
        )

    if not exact and mode in ("enhanced_v2", "enhanced2", "v2"):
        from .enhanced_v2 import select_best_enhanced_v2
        return select_best_enhanced_v2(
            acf=acf,
            octave_norm=octave_norm,
            meta=meta,
            diff_1000hz=diff_1000hz,
            pcm_duration_ms=pcm_duration_ms,
            transition_guard=transition_guard,
            transition_miss_beats=transition_miss_beats,
            transition_resume_beats=transition_resume_beats,
            track_tol=track_tol,
            comb_tol=comb_tol,
        )

    max_lag_cands = min(len(acf), 4000)
    if max_lag_cands <= 32:
        return 0.0, 0.0, 999.0, 1.0

    acf_c = np.ascontiguousarray(acf, dtype=np.float32)
    peaks_buf = (ctypes.c_int * max_lag_cands)()
    n_peaks = int(_filters.require_c_dsp(_filters._c_dsp).find_acf_local_peaks_c(
        acf_c.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
        max_lag_cands,
        peaks_buf
    ))
    peaks = [int(peaks_buf[i]) for i in range(n_peaks)]

    if not peaks:
        return 0.0, 0.0, 999.0, 1.0

    # 2. 阶段 1: 候选周期多谐波动态跟踪与 1.25x 门限评估 (严格对齐 Malody 0x1000bfd8b - 0x1000bff56)
    candidates = []
    rescue_candidates = []
    best_cand_idx = 0
    best_score = np.float32(0.0)
    if not exact:
        from .enhanced import ENH_CFG
        rescue_min_h = int(ENH_CFG["rescue_min_harmonics"])
        rescue_min_r = np.float32(ENH_CFG["rescue_min_mean_ratio"])

    for rbx, p_lag in enumerate(peaks):
        if p_lag >= max_lag_cands:
            break
        if len(candidates) > 0 and acf[p_lag] <= 0.0:
            continue

        tau = float(p_lag)
        target = int(round(tau))
        if target >= max_lag_cands - 10:
            continue

        sum_h = np.float32(0.0)
        n_matched = 0
        S_kt = 0.0
        S_kk = 0.0
        k = 1.0
        p_idx = rbx

        while True:
            while p_idx < len(peaks) and peaks[p_idx] < target - 10:
                p_idx += 1
            if p_idx < len(peaks) and peaks[p_idx] <= target + 10:
                c = peaks[p_idx]
                n_matched += 1
                y0 = acf[c]
                sum_h = np.float32(sum_h + y0)
                ym1 = float(acf[c - 1])
                y0_d = float(y0)
                yp1 = float(acf[c + 1])
                denom = (y0_d - ym1) + (y0_d - yp1)
                # Malody 0x1000bfec9 无条件 divsd：denom=0 时得到 inf/NaN，
                # 不能像旧实现那样把 delta 填成 0，否则会产生一个原版没有的候选。
                with np.errstate(divide="ignore", invalid="ignore"):
                    delta = float(np.float64(0.5 * (yp1 - ym1)) / np.float64(denom))
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
                # lrint(inf/NaN) 在 x86 上得到 INT64_MIN；下一轮会把 p_idx 推到末尾
                # 并以 target <= max_lag/2 丢弃该候选。这里用同一哨兵等价跳过。
                target = -(1 << 63)
            if target >= max_lag_cands - 10:
                break
            k += 1.0
            if not matched:
                break

        if n_matched == 0 or target <= max_lag_cands // 2:
            continue

        mean_h = np.float32(sum_h / np.float32(n_matched))
        if mean_h <= best_score:
            if not exact and n_matched >= rescue_min_h and mean_h > np.float32(acf[p_lag] * rescue_min_r):
                rescue_candidates.append((S_kt / S_kk, float(mean_h)))
            continue
        if mean_h <= np.float32(acf[p_lag] * np.float32(0.699999988079071)):
            if not exact and n_matched >= rescue_min_h and mean_h > np.float32(acf[p_lag] * rescue_min_r):
                rescue_candidates.append((S_kt / S_kk, float(mean_h)))
            continue

        # 二进制候选结构仅含 {tau, mean_h} 两个 double；p_lag 不被后续任何逻辑使用，故不携带
        candidates.append((S_kt / S_kk, float(mean_h)))
        if mean_h > np.float32(best_score * np.float32(1.25)):
            best_cand_idx = len(candidates) - 1
        best_score = mean_h

    if meta is not None:
        meta["peaks"] = [{"lag": int(p), "val": float(acf[p])} for p in peaks]
        meta["candidates"] = [
            {"tau": float(t), "bpm": round(60000.0 / float(t), 3), "score": float(s)}
            for t, s in candidates
        ]
        meta["rescue_candidates"] = [
            {"tau": float(t), "bpm": round(60000.0 / float(t), 3), "score": float(s)}
            for t, s in rescue_candidates
        ]

    if best_score <= 0.0 or not candidates:
        if not exact and rescue_candidates:
            from .enhanced import select_best_enhanced
            return select_best_enhanced(
                acf,
                [],
                0.0,
                0,
                octave_norm=octave_norm,
                meta=meta,
                rescue_candidates=rescue_candidates,
            )
        if meta is not None:
            meta.update({"stage": "fail", "bpm": 0.0, "n_matched": 0, "coverage": 0.0})
        return 0.0, 0.0, 999.0, 1.0

    best_tau = candidates[best_cand_idx][0]

    # 3. 阶段 2: 220 BPM 亚谐波倍频消歧 (严格对齐 Malody 0x1000c0106 - 0x1000c0224)
    TAU_MIN = 272.72727272727275  # 220.0 BPM
    TAU_MAX = 545.4545454545455   # 110.0 BPM

    if best_tau < TAU_MIN:
        while best_cand_idx < len(candidates) - 1:
            if candidates[best_cand_idx + 1][0] >= TAU_MAX:
                break
            found = False
            for j in range(best_cand_idx + 1, len(candidates)):
                cand_tau = candidates[j][0]
                if cand_tau >= TAU_MAX:
                    break
                if abs(math.remainder(cand_tau, best_tau)) <= 10.0:
                    best_cand_idx = j
                    best_tau = cand_tau
                    found = True
                    break
            if not found or best_tau >= TAU_MIN:
                break

    # 3.5 原版 AutoTiming 结果里的两个谐波倍率字段 (out+0x18 / out+0x1c,
    #     Malody 0x1000c0239-0x1000c034c)。它们不参与 BPM 计算，只作为诊断输出。
    if exact and meta is not None:
        ratio_up = 1
        if best_cand_idx < len(candidates) - 1:
            for j in range(best_cand_idx + 1, len(candidates)):
                cand_tau = candidates[j][0]
                if cand_tau >= TAU_MAX:
                    break
                if abs(math.remainder(cand_tau, best_tau)) <= 10.0:
                    ratio_up = int(round(cand_tau / best_tau))
                    if ratio_up >= 3:
                        break
        ratio_down = 1
        if best_cand_idx > 0:
            cur_tau = candidates[best_cand_idx][0]
            for j in range(best_cand_idx - 1, -1, -1):
                lower_tau = candidates[j][0]
                if abs(math.remainder(cur_tau, lower_tau)) <= 10.0:
                    ratio_down = int(round(cur_tau / lower_tau))
                    if ratio_down > 2:
                        break
        meta["ratio_up"] = ratio_up
        meta["ratio_down"] = ratio_down

    # 4. 阶段 3: 全曲自相关峰值线性回归追踪
    if exact:
        raw_bpm, raw_instab, sigma_t, rel_inst, n_matched = _track_full_song_exact(acf, best_tau)
        if raw_bpm <= 0.0:
            if meta is not None:
                meta.update({"stage": "fail", "best_tau": best_tau, "bpm": 0.0, "n_matched": n_matched, "coverage": 0.0})
            return 0.0, 0.0, 999.0, 1.0
        pre_fold_bpm = raw_bpm
        if octave_norm:
            while raw_bpm < 110.0:
                raw_bpm *= 2.0
                raw_instab *= 2.0
            while raw_bpm > 220.0:
                raw_bpm /= 2.0
                raw_instab /= 2.0
        if meta is not None:
            tracked_tau = 60000.0 / pre_fold_bpm
            meta.update({
                "stage": "exact",
                "best_tau": tracked_tau,
                "bpm": raw_bpm,
                "raw_bpm": pre_fold_bpm,
                "instab": raw_instab,
                "fold_scale": raw_bpm / pre_fold_bpm,
                "sigma_t": sigma_t,
                "rel_inst": rel_inst,
                "n_matched": n_matched,
                "coverage": float(n_matched) * tracked_tau / float(len(acf)) if len(acf) > 0 else 0.0,
                "len_acf": len(acf),
            })
        return raw_bpm, raw_instab, sigma_t, rel_inst

    # =========================================================================
    # 以下为增强优化模式 (exact=False)：全部决策逻辑已整体迁移至 malody_bpm/enhanced.py
    # (A0 三连音 3:2 消歧 / A 亚谐波优选与高速早断锁精修 / B 候选回退 /
    #  C 单拍切分跨越 / D 短时散板熔断 / E 回归后倍频归一)。
    # 迁移后 exact 路径 (上方) 物理不受影响，魔法阈值全部提为 enhanced.ENH_CFG 具名常量。
    # =========================================================================
    from .enhanced import select_best_enhanced

    return select_best_enhanced(
        acf,
        candidates,
        best_tau,
        best_cand_idx,
        octave_norm=octave_norm,
        meta=meta,
        rescue_candidates=rescue_candidates,
        peaks=peaks,
        groove_tol=groove_tol,
    )


def detect_bpm(
    audio_path: str,
    target_sr: int | None = None,
    octave_norm: bool = False,
    mode: str = "exact",
    groove_tol: int | None = None,
    meta: dict | None = None,
    transition_guard: float | int | None = None,
    transition_miss_beats: int = 4,
    transition_resume_beats: int = 3,
    track_tol: int = 8,
    comb_tol: int | None = None,
    acf_decay_ratio: float = 0.7,
    cand_update_ratio: float = 1.25,
    allow_any_duration: bool = False,
    trim_recalc: bool | None = None,
    sub_trunk_min_sec: int = 15,
    channel: str = "auto",
    halve_contrast_ratio: float = 1.10,
) -> tuple[float, float]:
    """
    Malody BPM 核心测算流水线入口：
    :param audio_path: 音频文件路径
    :param target_sr: 可选强制目标采样率 (默认自动按 2^n 倍数最佳匹配 48000 / 44100 / 32000)
    :param octave_norm: 是否将测算结果强制归一化至常规击打速度区间 [110, 220] BPM
    :param mode: 运行模式：
                 - "exact" (默认): 100% 原版还原模式，严格复现 Malody 0x1000bec2e (Windows 0x4741f0) 的每一处指令细节
                 - "enhanced":     超越原版的增强优化模式 1.0 (浮点直解、双精度滤波、双向倍频消歧、切分抗扰与散板熔断)
                 - "enhanced_v2" (或 "enhanced2", "v2"): 增强优化模式 2.0 (分窗池化自相关 Pooled ACF + exact 原生高精度回归)
                 - "enhanced_v3" (或 "enhanced3", "v3_enhanced", "enh_v3"): 增强 3.0 融合模式 (分窗池化自相关 + 两阶长程 OLS 精修 + 3:2 三连音脉冲梳安全消歧 + 未达稳态门槛安全归零)
                 - "force_v3" (或 "force_fit", "v3"): Force Fit 模式 3.0 (全局梳状网格能量投影强行拟合)
    :param groove_tol: 仅在 enhanced (V1) 模式生效，手动指定的有效节拍允许偏差门限 (毫秒正整数，范围 5~60)
    :param meta: 可选诊断字典；exact 模式会写入 ratio_up / ratio_down 等原版结果字段
    :param transition_guard: 仅在 enhanced_v2 / enhanced_v3 / force_v3 模式生效，过渡内缩时间，支持秒 (float, 0.0~3.0) 或毫秒 (int, 0~3000)，None/0 为关闭
    :param transition_miss_beats: 仅在 enhanced_v2 / enhanced_v3 / force_v3 模式生效，连续未命中判定为变速的拍数门限 (2~10，默认 4)
    :param transition_resume_beats: 仅在 enhanced_v2 / enhanced_v3 / force_v3 模式生效，连续命中判定为 a tempo 恢复的拍数门限 (2~10，默认 3)
    :param track_tol: 仅在 enhanced_v2 / enhanced_v3 模式生效，自相关谐波回归容差 (5~25ms 整数，默认 8ms)
    :param comb_tol: 仅在 enhanced_v2 / enhanced_v3 / force_v3 模式生效，时域脉冲梳节拍匹配窗口半宽 (10~50ms 整数，若为 None 默认 35ms)
    :param acf_decay_ratio: 仅在 enhanced_v3 模式生效，初筛谐波链均值相对于首峰的衰减门限 (0.1~1.0，默认 0.7)
    :param cand_update_ratio: 仅在 enhanced_v3 模式生效，初筛候选更替倍率门限 (1.0~2.0，默认 1.25)
    :param allow_any_duration: 是否解除 [45s, 600s] 音频时长限制 (默认 False)
    :param trim_recalc: 仅在 enhanced_v3 / force_v3 模式生效，分段相位重锁与剔除变速二次重算开关 (默认 None 时随 transition_guard > 0 自动开启；True/False 显式强制开/关)
    :param sub_trunk_min_sec: 仅在 enhanced_v3 模式生效，副主干进入二次多主干联合 OLS 的最小有效时长 (8~15 秒整数，默认 15 秒)
    :param channel: 仅在非 exact 模式生效，声道分析策略 (大小写不敏感；exact 模式下仅校验合法性并静默忽略)：
                    - "auto" (默认): 多声道 4 频段平方能量域融合（抗反相抵消并保留 LFE）
                    - "left" (或 "l"): 只分析左声道 (1ch 回退唯一声道；>2ch 优先 FL 否则 c0)
                    - "right" (或 "r"): 只分析右声道 (1ch 回退唯一声道；>2ch 优先 FR 否则 c1)
                    - "downmix": 旧版 FFmpeg -ac 1 线性波形混音
    :param halve_contrast_ratio: 仅在 enhanced_v3 模式生效，首次定位于 240~300.01+ BPM (199.99~250ms) 时折半合理性判定的强弱拍时域脉冲梳对比度门限
                    (1.0~2.0，默认 1.10≈反拍比正拍弱 6dB；1.0=有倍周期候选即折半，2.0=几乎从不折半；220~240 BPM 恒不折半)
    :return: (bpm, instability)
    """
    norm_channel = normalize_channel_mode(channel)
    if trim_recalc is not None and not isinstance(trim_recalc, (bool, np.bool_)):
        raise ValueError(f"trim_recalc 必须为 None 或布尔类型，当前传入: {trim_recalc!r}")

    is_exact = (mode == "exact")
    is_v2 = mode in ("enhanced_v2", "enhanced2", "v2")
    is_v3 = mode in ("enhanced_v3", "enhanced3", "v3_enhanced", "enh_v3")
    is_force = mode in ("force_v3", "force_fit", "v3")

    if meta is not None:
        meta.setdefault("suspected_triple_meter", False)
        if not is_exact:
            meta["channel"] = norm_channel

    if mode == "enhanced" and groove_tol is not None:
        if not isinstance(groove_tol, int) or isinstance(groove_tol, bool) or not (5 <= groove_tol <= 60):
            raise ValueError(f"groove_tol 必须为 5 到 60 之间的正整数，当前传入: {groove_tol!r}")

    if is_v2 or is_v3:
        if not isinstance(track_tol, int) or isinstance(track_tol, bool) or not (5 <= track_tol <= 25):
            raise ValueError(f"track_tol 必须为 5 到 25 之间的整数，当前传入: {track_tol!r}")

    if is_v2 or is_v3 or is_force:
        if comb_tol is not None:
            if not isinstance(comb_tol, int) or isinstance(comb_tol, bool) or not (10 <= comb_tol <= 50):
                raise ValueError(f"comb_tol 必须为 10 到 50 之间的整数，当前传入: {comb_tol!r}")

        if transition_guard is not None:
            if isinstance(transition_guard, (bool, np.bool_)):
                raise ValueError(f"transition_guard 不能为布尔类型，当前传入: {transition_guard!r}")
            elif isinstance(transition_guard, float):
                if not math.isfinite(transition_guard) or not (0.0 <= transition_guard <= 3.0001):
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

    if is_v3:
        if not isinstance(sub_trunk_min_sec, int) or isinstance(sub_trunk_min_sec, bool) or not (8 <= sub_trunk_min_sec <= 15):
            raise ValueError(f"sub_trunk_min_sec 必须为 8 到 15 之间的整数，当前传入: {sub_trunk_min_sec!r}")
        if (
            isinstance(acf_decay_ratio, (bool, np.bool_))
            or not isinstance(acf_decay_ratio, (int, float, np.integer, np.floating))
            or not math.isfinite(float(acf_decay_ratio))
            or not (0.1 <= float(acf_decay_ratio) <= 1.0)
        ):
            raise ValueError(f"acf_decay_ratio 必须为 0.1 到 1.0 之间的浮点数，当前传入: {acf_decay_ratio!r}")
        if (
            isinstance(cand_update_ratio, (bool, np.bool_))
            or not isinstance(cand_update_ratio, (int, float, np.integer, np.floating))
            or not math.isfinite(float(cand_update_ratio))
            or not (1.0 <= float(cand_update_ratio) <= 2.0)
        ):
            raise ValueError(f"cand_update_ratio 必须为 1.0 到 2.0 之间的浮点数，当前传入: {cand_update_ratio!r}")
        # [新增 2026-11-14] 240~300 BPM 折半合理性判定门限校验（解码前提前报错）
        if (
            isinstance(halve_contrast_ratio, (bool, np.bool_))
            or not isinstance(halve_contrast_ratio, (int, float, np.integer, np.floating))
            or not math.isfinite(float(halve_contrast_ratio))
            or not (1.0 <= float(halve_contrast_ratio) <= 2.0)
        ):
            raise ValueError(f"halve_contrast_ratio 必须为 1.0 到 2.0 之间的浮点数，当前传入: {halve_contrast_ratio!r}")

    _filters.require_c_dsp(_filters._c_dsp)

    # 1. 音频解码与声道处理 (默认含全模式 [45s, 600s] 实际 PCM 时长校验；allow_any_duration=True 时解除)
    pcm, sr = decode_audio_to_pcm(
        audio_path,
        target_sr=target_sr,
        exact=is_exact,
        allow_any_duration=allow_any_duration,
        channel="downmix" if is_exact else norm_channel,
    )
    pcm_duration_ms = float(len(pcm)) * 1000.0 / float(sr)

    # 2. 提取 4 频段包络与 1000 Hz 零相位错位差分包络
    diff_1000hz, env_1000hz = extract_onset_envelope(pcm, sr, exact=is_exact)

    # 3. 维纳-辛钦 FFT 全局自相关 (保留前 n // 2 有效滞后窗)
    acf = compute_autocorrelation_fft(diff_1000hz, exact=is_exact)

    # 4. 三阶段周期搜索与全局回归拟合
    raw_bpm, raw_instab, sigma_t, rel_inst = select_best_tempo(
        acf,
        octave_norm=octave_norm,
        exact=is_exact,
        mode=mode,
        groove_tol=groove_tol if mode == "enhanced" else None,
        meta=meta,
        diff_1000hz=diff_1000hz if not is_exact else None,
        pcm_duration_ms=None if allow_any_duration else (pcm_duration_ms if (is_v2 or is_v3) else None),
        transition_guard=transition_guard if (is_v2 or is_v3 or is_force) else None,
        transition_miss_beats=transition_miss_beats if (is_v2 or is_v3 or is_force) else 4,
        transition_resume_beats=transition_resume_beats if (is_v2 or is_v3 or is_force) else 3,
        track_tol=track_tol if (is_v2 or is_v3) else 8,
        comb_tol=comb_tol if (is_v2 or is_v3 or is_force) else None,
        acf_decay_ratio=acf_decay_ratio if is_v3 else 0.7,
        cand_update_ratio=cand_update_ratio if is_v3 else 1.25,
        trim_recalc=(None if trim_recalc is None else bool(trim_recalc)) if (is_v3 or is_force) else False,
        sub_trunk_min_sec=sub_trunk_min_sec if is_v3 else 15,
        halve_contrast_ratio=halve_contrast_ratio if is_v3 else 1.10,
    )

    if raw_bpm <= 0.0:
        return 0.0, 0.0 if is_exact else 1.0

    if is_force:
        if meta is not None:
            meta["bpm"] = raw_bpm
        return raw_bpm, float(raw_instab)

    # 5. 7 级阶梯 IEEE remainder 谐波量化吸附 (严格对齐 Malody 0x1000c07ce / Windows 0x474d30)
    # 注：Enhanced V2 / V3 模式直接输出高精度物理连续回归值，完全关闭阶梯量化吸附以消除跨网格误吸附
    if is_v2 or is_v3:
        final_bpm = raw_bpm
        is_quantized = False
    else:
        final_bpm, is_quantized = resolve_harmonic_quantization(
            raw_bpm,
            tolerance=raw_instab,
            sigma_t=sigma_t,
            rel_instability=rel_inst
        )

    clamp_scale = 1.0
    if (is_v2 or is_v3) and final_bpm > 0.0:
        pre_clamp = final_bpm
        # V3 放宽至 [59.99, 300.01] BPM 闭区间，V2 保持 [60.0, 300.0] 不变
        if is_v3:
            from .force_v3 import V3_BPM_MIN, V3_BPM_MAX
            lo_bpm, hi_bpm = V3_BPM_MIN, V3_BPM_MAX
        else:
            lo_bpm, hi_bpm = 60.0, 300.0
        while final_bpm < lo_bpm:
            final_bpm *= 2.0
        while final_bpm > hi_bpm:
            final_bpm *= 0.5
        clamp_scale = final_bpm / pre_clamp
        if meta is not None:
            meta["bpm"] = final_bpm

    # 6. 计算最终不稳定度 (对齐 Malody 原版 UI "%.2f"；增强模式下按折叠后八度同步量纲 raw_bpm * rel_inst)
    effective_raw_instab = raw_instab if is_exact else (raw_bpm * rel_inst)
    if is_v2 or is_v3:
        if meta is not None:
            effective_raw_instab = float(meta.get("steady_instab", effective_raw_instab))
        effective_raw_instab *= clamp_scale
    instability = estimate_instability(
        env_1000hz,
        final_bpm,
        is_quantized=is_quantized,
        raw_instability=effective_raw_instab
    )

    return final_bpm, instability

