#!/usr/bin/env python3
"""
Malody BPM & 不稳定度测算引擎 —— 本地 HTTP Web GUI (基于 Flask)

本模块提供 Flask 应用工厂 (create_app)、测算路由与纯浏览器启动入口 (main)。
直接运行本文件将启动本地多线程 HTTP 服务并在默认浏览器中打开工作台页面（按 Ctrl+C 停止服务）。
"""

import argparse
import atexit
import json
import math
import multiprocessing
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser

# 将项目根目录加入模块搜索路径，并在 macOS 下补齐常见 Homebrew 二进制路径（供 ffmpeg/ffprobe 探测）
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

if sys.platform == "darwin":
    _extra_bin_dirs = ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin"]
    _cur_path_parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    for _d in _extra_bin_dirs:
        if _d not in _cur_path_parts and os.path.isdir(_d):
            _cur_path_parts.append(_d)
    os.environ["PATH"] = os.pathsep.join(_cur_path_parts)

try:
    from flask import Flask, jsonify, render_template_string, request
    from werkzeug.serving import make_server
except ImportError as exc:
    raise ImportError("本地 Web GUI 需要 Flask 依赖库支持，请先执行安装: pip install flask") from exc

from malody_bpm.autocorr import compute_autocorrelation_fft, estimate_instability
from malody_bpm.decoder import (
    _resolve_ff_binary,
    _subproc_extra_kwargs,
    decode_audio_to_pcm,
    normalize_channel_mode,
)
from malody_bpm.detector import select_best_tempo
from malody_bpm.envelope import extract_onset_envelope
from malody_bpm.filters import _c_dsp, _detect_process_arch, require_c_dsp
from malody_bpm.force_v3 import V3_BPM_MAX, V3_BPM_MIN
from malody_bpm.harmonic import resolve_harmonic_quantization
from web_gui_settings import (
    inject_settings_ui,
    parse_analyze_form_settings,
    parse_form_bool,
    parse_form_float,
    parse_form_int,
)
from web_gui_silence import (
    DEFAULT_SHORT_BURST_MAX_SEC,
    DEFAULT_SHORT_BURST_MIN_GAP_SEC,
    DEFAULT_SILENCE_THRESHOLD_DB,
    SilenceTrimInfo,
    check_raw_safety_cap,
    shift_spans_to_raw_timeline,
    trim_head_tail_silence,
    validate_short_burst_max_sec,
    validate_short_burst_min_gap_sec,
    validate_silence_threshold_db,
    validate_trimmed_pcm_frames,
)
from web_gui_tempo_regions import detect_ui_tempo_change_spans
from web_gui_unmatched import build_unmatched_grid_info

STAGE_LABELS = {
    "exact": "原版阶段 3 严格单次失配回归",
    "base": "首选周期严格全曲回归 (Base)",
    "a0": "Stage A0: 全局 3:2 三连音伪谐波消歧",
    "a": "Stage A: 强弱拍亚谐波优选 / 高速早断锁长周期精修",
    "b": "Stage B: 次优高分候选回退救援",
    "c": "Stage C: 单拍切分跨越松弛追踪救援",
    "b_rescue": "Stage B-Rescue: 阶段 1 多谐波次级候选严格救援",
    "c_rescue": "Stage C-Rescue: 阶段 1 多谐波次级候选松弛救援",
    "g": "Stage G: 消极假设全曲扫描与主干置信度选举",
    "g_v2": "Stage G-v2: 全局网格消极假设主干选举",
    "g_v2_a0": "Stage G-v2 + A0: 全局网格选举 + 3:2 三连音消歧",
    "g_v2_a": "Stage G-v2 + A: 全局网格选举 + 强弱拍亚谐波精修",
    "g_v2_track": "Stage G-v2: 逐拍匀速段追踪",
    "g_v2_track_fast": "Stage G-v2: 长曲旧窗口门控快速回退",
    "g_v2_track_fallback": "Stage G-v2: 逐拍追踪 + 旧窗口门控回退",
    "d_fuse": "Stage D: 匀速段不足 50% 自动熔断",
    "v2_pooled": "Stage V2: 分窗池化自相关 + 原生高精度回归",
    "v3_tier1": "Stage V3: 分窗池化自相关 + 两阶长程 OLS 精修",
    "force_v3_global_comb": "Stage V3: 全局梳状网格能量投影 (Force Fit)",
    "fail": "无法自动计算 (未找到满足门限的稳定周期)",
}


def _build_silence_pill(trim_info: SilenceTrimInfo) -> dict:
    """构建主界面进度卡片底部的头尾静音预处理结果胶囊描述。"""
    if trim_info.show_badge:
        label = "已去静音·含短振幅" if trim_info.short_burst_trimmed else "已去静音"
        return {
            "text": f"{label} (头 {trim_info.head_trim_sec:.1f}s / 尾 {trim_info.tail_trim_sec:.1f}s)",
            "cls": "highlight",
        }
    return {"text": "无头尾静音", "cls": ""}


def _emit_progress(
    progress_cb,
    text: str | None = None,
    *,
    phase: str | None = None,
    dwell: bool = False,
    pill: dict | None = None,
) -> None:
    """向流式进度回调发送阶段事件（对仅接收纯字符串的传统回调保持向后兼容）。"""
    if progress_cb is None:
        return
    if getattr(progress_cb, "_accepts_event_dict", False):
        evt: dict = {}
        if text is not None:
            evt["text"] = text
        if phase is not None:
            evt["phase"] = phase
        if dwell:
            evt["dwell"] = True
        if pill is not None:
            evt["pill"] = pill
        if evt:
            progress_cb(evt)
    elif text is not None:
        progress_cb(text)


_CONTAINER_LABELS = {
    "ogg": "OGG",
    "mp3": "MP3",
    "flac": "FLAC",
    "wav": "WAV",
    "mov": "M4A/MP4",
    "aac": "AAC",
}


def _probe_audio_meta(file_path: str) -> dict:
    """使用 ffprobe 轻量提取音频原始封装、编码、原始采样率、声道数与位深（仅用于未经过预检缓存时的兜底）。"""
    try:
        cmd = [
            _resolve_ff_binary("ffprobe"),
            "-v",
            "error",
            "-show_entries",
            "format=format_name:stream=codec_type,codec_name,sample_rate,channels,bits_per_raw_sample,bits_per_sample",
            "-of",
            "json",
            file_path,
        ]
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            **_subproc_extra_kwargs(),
        )
        if res.returncode != 0 or not res.stdout.strip():
            return {}
        data = json.loads(res.stdout)
        if not isinstance(data, dict):
            return {}
        fmt_name = str((data.get("format") or {}).get("format_name") or "").strip().lower()
        container_label = ""
        for tok in (t.strip() for t in fmt_name.split(",") if t.strip()):
            if tok in _CONTAINER_LABELS:
                container_label = _CONTAINER_LABELS[tok]
                break
        if not container_label and fmt_name:
            container_label = fmt_name.split(",")[0].strip().upper()

        streams = data.get("streams") or []
        audio_streams = [
            s for s in streams
            if isinstance(s, dict) and str(s.get("codec_type") or "").strip().lower() == "audio"
        ]
        if not audio_streams:
            return {"container_label": container_label} if container_label else {}

        st0 = audio_streams[0]
        codec_name = str(st0.get("codec_name") or "").strip().lower()
        try:
            raw_sr = int(st0.get("sample_rate") or 0)
        except (TypeError, ValueError):
            raw_sr = 0
        try:
            channels = int(st0.get("channels") or 0)
        except (TypeError, ValueError):
            channels = 0
        raw_bits = st0.get("bits_per_raw_sample") or st0.get("bits_per_sample") or 0
        try:
            bits = int(raw_bits)
        except (TypeError, ValueError):
            bits = 0
        ch_label = (
            "单声道" if channels == 1
            else ("双声道" if channels == 2 else (f"{channels} 声道" if channels > 0 else ""))
        )
        return {
            "container_label": container_label,
            "codec_name": codec_name,
            "raw_sr": raw_sr,
            "channels": channels,
            "channel_label": ch_label,
            "bits": bits,
        }
    except Exception:
        return {}


def analyze_single_mode(
    audio_path: str,
    mode: str = "exact",
    octave_norm: bool = False,
    target_sr: int | None = None,
    trim_silence: bool = False,
    allow_any_duration: bool = False,
    acf_decay_ratio: float = 0.7,
    cand_update_ratio: float = 1.25,
    halve_contrast_ratio: float = 1.10,  # [新增 2026-11-14] 仅 enhanced_v3 生效：240~300 BPM 折半对比度门限
    track_tol: int = 8,
    comb_tol: int | None = None,
    transition_miss_beats: int = 4,
    transition_resume_beats: int = 3,
    transition_guard: float | None = None,
    trim_recalc: bool = False,
    sub_trunk_min_sec: int = 15,
    silence_threshold_db: float = DEFAULT_SILENCE_THRESHOLD_DB,
    trim_short_bursts: bool = False,
    short_burst_max_sec: float = DEFAULT_SHORT_BURST_MAX_SEC,
    short_burst_min_gap_sec: float = DEFAULT_SHORT_BURST_MIN_GAP_SEC,
    channel: str = "auto",
    _predecoded: tuple | None = None,
    _raw_sr: int | None = None,
    progress_cb=None,
) -> dict:
    """
    执行单模式 (exact / enhanced_v3 / force_v3 等) 完整测算，并收集用于 GUI 展示的 ACF 曲线、局部峰、候选池与阶段诊断元数据。
    """
    is_exact = (mode == "exact")
    is_v2 = mode in ("enhanced_v2", "enhanced2", "v2")
    is_v3 = mode in ("enhanced_v3", "enhanced3", "v3_enhanced", "enh_v3")
    is_force = mode in ("force_v3", "force_fit", "v3")
    mode_label = (
        "Malody 模式" if is_exact
        else ("增强模式" if is_v3 else ("强制线性回归模式" if is_force else mode))
    )

    require_c_dsp(_c_dsp)
    t0 = time.perf_counter()
    base_elapsed = 0.0
    norm_channel = normalize_channel_mode(channel)
    eff_channel = "downmix" if is_exact else norm_channel
    thr_db = validate_silence_threshold_db(silence_threshold_db)
    burst_max_sec = validate_short_burst_max_sec(short_burst_max_sec)
    burst_min_gap_sec = validate_short_burst_min_gap_sec(short_burst_min_gap_sec)

    if _predecoded is not None:
        if len(_predecoded) >= 6:
            pcm, sr, diff_1000hz, env_1000hz, acf, trim_info = _predecoded[:6]
            if len(_predecoded) >= 7:
                base_elapsed = float(_predecoded[6])
        else:
            pcm, sr, diff_1000hz, env_1000hz, acf = _predecoded
            pre_frames = int(len(pcm))
            pre_dur = (float(pre_frames) / float(sr)) if sr > 0 else 0.0
            trim_info = SilenceTrimInfo(
                enabled=bool(trim_silence),
                threshold_db=thr_db,
                did_trim=False,
                raw_frames=pre_frames,
                trimmed_frames=pre_frames,
                raw_duration_sec=pre_dur,
                trimmed_duration_sec=pre_dur,
                head_trim_ms=0.0,
                tail_trim_ms=0.0,
                trim_short_bursts=bool(trim_silence and trim_short_bursts),
                short_burst_max_sec=burst_max_sec,
                short_burst_min_gap_sec=burst_min_gap_sec,
                short_burst_trimmed=False,
            )
    else:
        if trim_silence:
            check_raw_safety_cap(audio_path, allow_any_duration=allow_any_duration)
        _emit_progress(progress_cb, f"解码音频数据（{mode_label}）…", phase="音频解码与预处理")
        pcm_raw, sr = decode_audio_to_pcm(
            audio_path,
            target_sr=target_sr,
            exact=is_exact,
            allow_any_duration=(allow_any_duration or bool(trim_silence)),
            channel=eff_channel,
        )
        if trim_silence:
            _emit_progress(progress_cb, "去除音频头尾静音…", phase="音频解码与预处理", dwell=True)
        pcm, trim_info = trim_head_tail_silence(
            pcm_raw,
            sr,
            enabled=trim_silence,
            threshold_db=thr_db,
            trim_short_bursts=trim_short_bursts,
            short_burst_max_sec=burst_max_sec,
            short_burst_min_gap_sec=burst_min_gap_sec,
        )
        if trim_silence:
            _emit_progress(progress_cb, pill=_build_silence_pill(trim_info))
            validate_trimmed_pcm_frames(
                len(pcm),
                sr,
                allow_any_duration=allow_any_duration,
                did_trim=trim_info.did_trim,
            )
        _emit_progress(progress_cb, f"提取起音包络与自相关（{mode_label}）…", phase="核心算法测算")
        diff_1000hz, env_1000hz = extract_onset_envelope(pcm, sr, exact=is_exact)
        acf = compute_autocorrelation_fft(diff_1000hz, exact=is_exact)

    if isinstance(_raw_sr, int) and _raw_sr > 0:
        resolved_raw_sr = int(_raw_sr)
    else:
        probed_sr = _probe_audio_meta(audio_path).get("raw_sr")
        resolved_raw_sr = int(probed_sr) if isinstance(probed_sr, int) and probed_sr > 0 else int(sr)

    raw_exact_duration_sec = trim_info.raw_duration_sec
    eff_exact_duration_sec = trim_info.trimmed_duration_sec
    duration_sec = round(raw_exact_duration_sec, 2)
    trimmed_duration_sec = round(eff_exact_duration_sec, 2)
    raw_pcm_duration_ms = raw_exact_duration_sec * 1000.0
    eff_pcm_duration_ms = eff_exact_duration_sec * 1000.0

    _emit_progress(progress_cb, f"计算 BPM 与不稳定度（{mode_label}）…", phase="核心算法测算")
    meta: dict = {}
    raw_bpm, raw_instab, sigma_t, rel_inst = select_best_tempo(
        acf,
        octave_norm=octave_norm,
        exact=is_exact,
        meta=meta,
        mode=mode,
        diff_1000hz=diff_1000hz if not is_exact else None,
        pcm_duration_ms=None if allow_any_duration else (eff_pcm_duration_ms if (is_v2 or is_v3) else None),
        transition_guard=transition_guard if (is_v2 or is_v3 or is_force) else None,
        transition_miss_beats=transition_miss_beats if (is_v2 or is_v3 or is_force) else 4,
        transition_resume_beats=transition_resume_beats if (is_v2 or is_v3 or is_force) else 3,
        track_tol=track_tol if (is_v2 or is_v3) else 8,
        comb_tol=comb_tol if (is_v2 or is_v3 or is_force) else None,
        acf_decay_ratio=acf_decay_ratio if is_v3 else 0.7,
        cand_update_ratio=cand_update_ratio if is_v3 else 1.25,
        # [新增 2026-11-14] 透传 240~300 BPM 折半对比度门限（仅 enhanced_v3 生效）
        halve_contrast_ratio=halve_contrast_ratio if is_v3 else 1.10,
        trim_recalc=bool(trim_recalc) if (is_v3 or is_force) else False,
        sub_trunk_min_sec=sub_trunk_min_sec if is_v3 else 15,
    )

    # 若强制模式采纳了二次精修，优先展示净化后的 acf_clean 曲线；否则沿用池化或原始全局 ACF
    acf_clean_vis = meta.get("acf_clean")
    cur_acf = acf_clean_vis if acf_clean_vis is not None else meta.get("acf_pooled", acf)
    max_vis_lag = min(len(cur_acf), 4000)
    acf_curve = [round(float(x), 6) for x in cur_acf[:max_vis_lag]]

    eff_comb_tol = int(meta.get("comb_tol", comb_tol if comb_tol is not None else 35))
    tg_val = float(transition_guard) if transition_guard is not None else 0.0
    trimming_active = bool((not is_exact) and (tg_val > 1e-9 or bool(trim_recalc)))

    if raw_bpm <= 0.0:
        elapsed = round(base_elapsed + (time.perf_counter() - t0), 3)
        stage_code = meta.get("stage", "fail")
        fail_instab = 0.0 if is_exact else 1.0
        return {
            "mode": mode,
            "channel": eff_channel,
            "status": "无法自动计算",
            "ok": False,
            "bpm": "0.000",
            "bpm_num": 0.0,
            "instability": f"{fail_instab:.3f}",
            "malody_ui_instability": f"{fail_instab:.2f}",
            "is_quantized": False,
            "suspected_triple_meter": False,
            "trim_recalc": bool(meta.get("trim_recalc", False)),
            "sub_trunk_min_sec": int(meta.get("sub_trunk_min_sec", sub_trunk_min_sec)),
            "bpm_recalc_applied": False,
            "pass1_bpm": "0.000",
            "raw_bpm": "0.000",
            "stage": stage_code,
            "stage_label": STAGE_LABELS.get(stage_code, stage_code),
            "fold_scale": 1.0,
            "best_tau": round(float(meta.get("best_tau", 0.0)), 3),
            "final_tau": 0.0,
            "n_matched": int(meta.get("n_matched", 0)),
            "total_beats": int(meta.get("total_beats", 0)),
            "coverage": round(float(meta.get("coverage", 0.0)) * 100.0, 1),
            "matched_duration_sec": 0.0,
            "duration_ratio_pct": 0.0,
            "eff_comb_tol": eff_comb_tol,
            "trimming_active": trimming_active,
            "transition_spans": [],
            "exact_early_break": False,
            "exact_break_lag_ms": 0.0,
            "trim_silence": bool(trim_info.enabled),
            "silence_threshold_db": round(float(trim_info.threshold_db), 1),
            "silence_trimmed": bool(trim_info.show_badge),
            "trim_short_bursts": bool(trim_info.trim_short_bursts),
            "short_burst_max_sec": round(float(trim_info.short_burst_max_sec), 2),
            "short_burst_min_gap_sec": round(float(trim_info.short_burst_min_gap_sec), 2),
            "short_burst_trimmed": bool(trim_info.short_burst_trimmed and trim_info.show_badge),
            "head_trim_sec": trim_info.head_trim_sec,
            "tail_trim_sec": trim_info.tail_trim_sec,
            "trimmed_duration_sec": trimmed_duration_sec,
            "rmse_ms": round(float(meta.get("rmse_ms", 0.0)), 2),
            "beat_display": meta.get("beat_display", ""),
            "sample_rate": int(sr),
            "raw_sample_rate": resolved_raw_sr,
            "duration_sec": duration_sec,
            "elapsed_seconds": elapsed,
            "acf_curve": acf_curve,
            "peaks": meta.get("peaks", []),
            "candidates": meta.get("candidates", []),
            "rescue_candidates": meta.get("rescue_candidates", []),
            "ratio_up": int(meta.get("ratio_up", 1)),
            "ratio_down": int(meta.get("ratio_down", 1)),
        }

    if is_force:
        final_bpm = float(raw_bpm)
        is_quantized = False
        instability = float(raw_instab)
        if meta is not None:
            meta["bpm"] = final_bpm
    else:
        if is_v2 or is_v3:
            final_bpm = raw_bpm
            is_quantized = False
        else:
            final_bpm, is_quantized = resolve_harmonic_quantization(
                raw_bpm,
                tolerance=raw_instab,
                sigma_t=sigma_t,
                rel_instability=rel_inst,
            )

        clamp_scale = 1.0
        if (is_v2 or is_v3) and final_bpm > 0.0:
            pre_clamp = final_bpm
            # V3 放宽至 [59.99, 300.01] BPM 闭区间，归档的 V2 保持 [60.0, 300.0] 不变
            lo_bpm, hi_bpm = (V3_BPM_MIN, V3_BPM_MAX) if is_v3 else (60.0, 300.0)
            while final_bpm < lo_bpm:
                final_bpm *= 2.0
            while final_bpm > hi_bpm:
                final_bpm *= 0.5
            clamp_scale = final_bpm / pre_clamp
            if meta is not None:
                meta["bpm"] = final_bpm

        effective_raw_instab = raw_instab if is_exact else (raw_bpm * rel_inst)
        if is_v2 or is_v3:
            if meta is not None:
                effective_raw_instab = float(meta.get("steady_instab", effective_raw_instab))
            effective_raw_instab *= clamp_scale
        instability = estimate_instability(
            env_1000hz,
            final_bpm,
            is_quantized=is_quantized,
            raw_instability=effective_raw_instab,
        )

    elapsed = round(base_elapsed + (time.perf_counter() - t0), 3)

    stage_code = meta.get("stage", "exact" if is_exact else "base")
    fold_scale = float(meta.get("fold_scale", 1.0))
    pre_fold_raw_bpm = float(meta.get("raw_bpm", raw_bpm))
    # 增强模式卡片与 ACF 图谱的胜出周期 τ 统一对齐为精修后的基底周期 (60000 / raw_bpm)，保证与最终 BPM 严格自洽
    best_tau = (
        (60000.0 / pre_fold_raw_bpm)
        if (is_v3 and pre_fold_raw_bpm > 0.0)
        else float(meta.get("best_tau", 60000.0 / pre_fold_raw_bpm if pre_fold_raw_bpm > 0 else 0.0))
    )
    pass1_bpm_val = float(meta.get("pass1_bpm", 0.0))

    n_matched_val = int(meta.get("n_matched", 0))
    matched_beat_tau_val = meta.get("matched_beat_tau")
    if (
        is_force
        and matched_beat_tau_val is not None
        and math.isfinite(float(matched_beat_tau_val))
        and float(matched_beat_tau_val) > 0.0
    ):
        # 强制模式优先按实际产出 n_matched 的追踪周期折算匹配秒数（兼容二次精修 2^x 八度对齐）
        beat_ref_bpm = 60000.0 / float(matched_beat_tau_val)
    elif is_force and pre_fold_raw_bpm > 0.0:
        beat_ref_bpm = pre_fold_raw_bpm
    else:
        beat_ref_bpm = float(final_bpm)
    raw_matched_sec = (60.0 * float(n_matched_val) / beat_ref_bpm) if beat_ref_bpm > 0.0 else 0.0
    matched_sec = min(eff_exact_duration_sec, max(0.0, raw_matched_sec)) if eff_exact_duration_sec > 0.0 else 0.0
    duration_ratio_pct = (
        min(100.0, max(0.0, (matched_sec / eff_exact_duration_sec) * 100.0))
        if eff_exact_duration_sec > 0.0 else 0.0
    )

    if (is_v3 or is_force) and trimming_active and diff_1000hz is not None and final_bpm > 0.0:
        raw_spans = detect_ui_tempo_change_spans(diff_1000hz, float(final_bpm))
    else:
        raw_spans = meta.get("transition_spans", []) if not is_exact else []
    if trim_info.did_trim and trim_info.head_trim_ms > 0.0:
        transition_spans_out = shift_spans_to_raw_timeline(
            raw_spans,
            head_trim_ms=trim_info.head_trim_ms,
            raw_duration_ms=raw_pcm_duration_ms,
        )
    else:
        transition_spans_out = [
            [round(float(s), 2), round(float(e), 2)]
            for s, e in raw_spans
        ]

    # 未匹配主干网格区段（增强 / 强制模式始终计算，不受变速裁剪开关影响）：
    # 复用后端计算匹配占比的同一套逐拍命中序列，保证「匹配% + 未匹配合计% = 100%」
    unmatched_grid: dict | None = None
    if is_v3 or is_force:
        unmatched_grid = build_unmatched_grid_info(
            meta.get("matched_beat_times"),
            meta.get("matched_beat_tau"),
            eff_pcm_duration_ms,
            round(duration_ratio_pct, 1),  # 与卡片显示的占比值保持同一精度
        )
        if trim_info.did_trim and trim_info.head_trim_ms > 0.0 and unmatched_grid["spans"]:
            unmatched_grid["spans"] = shift_spans_to_raw_timeline(
                unmatched_grid["spans"],
                head_trim_ms=trim_info.head_trim_ms,
                raw_duration_ms=raw_pcm_duration_ms,
            )

    exact_early_break = False
    exact_break_lag_ms = 0.0
    if is_exact and n_matched_val > 0 and best_tau > 0.0:
        len_acf_val = int(meta.get("len_acf", len(acf)))
        approx_c = int(round(float(n_matched_val) * best_tau))
        lo_idx = max(1, approx_c - 10)
        hi_idx = min(len_acf_val - 2, approx_c + 10)
        exact_last_t = float(n_matched_val) * best_tau
        if hi_idx >= lo_idx:
            c_idx = max(range(lo_idx, hi_idx + 1), key=lambda idx: float(acf[idx]))
            if float(acf[c_idx]) > 0.0:
                ym1 = float(acf[c_idx - 1])
                y0_d = float(acf[c_idx])
                yp1 = float(acf[c_idx + 1])
                denom = (y0_d - ym1) + (y0_d - yp1)
                if abs(denom) > 1e-12:
                    delta = 0.5 * (yp1 - ym1) / denom
                    if -1.0 <= delta <= 1.0:
                        exact_last_t = float(c_idx) + delta
        next_target = int(round(exact_last_t + best_tau))
        if next_target + 10 < len_acf_val:
            exact_early_break = True
            exact_break_lag_ms = round(float(next_target), 2)

    return {
        "mode": mode,
        "channel": eff_channel,
        "status": "ok",
        "ok": True,
        "bpm": f"{final_bpm:.3f}",
        "bpm_num": round(float(final_bpm), 6),
        "instability": f"{instability:.3f}",
        "malody_ui_instability": f"{instability:.2f}",
        "is_quantized": bool(is_quantized),
        "suspected_triple_meter": bool(meta.get("suspected_triple_meter", False)),
        "trim_recalc": bool(meta.get("trim_recalc", False)),
        "sub_trunk_min_sec": int(meta.get("sub_trunk_min_sec", sub_trunk_min_sec)),
        "bpm_recalc_applied": bool(meta.get("bpm_recalc_applied", False)),
        "pass1_bpm": f"{pass1_bpm_val:.3f}" if pass1_bpm_val > 0 else "",
        "raw_bpm": f"{pre_fold_raw_bpm:.3f}",
        "folded_raw_bpm": f"{raw_bpm:.3f}",
        "stage": stage_code,
        "stage_label": STAGE_LABELS.get(stage_code, stage_code),
        "fold_scale": round(fold_scale, 4),
        "best_tau": round(best_tau, 3),
        "final_tau": round(60000.0 / float(final_bpm), 3) if final_bpm > 0 else 0.0,
        "n_matched": n_matched_val,
        "total_beats": int(meta.get("total_beats", 0)),
        "coverage": round(float(meta.get("coverage", 0.0)) * 100.0, 1),
        "matched_duration_sec": round(matched_sec, 1),
        "duration_ratio_pct": round(duration_ratio_pct, 1),
        "eff_comb_tol": eff_comb_tol,
        "trimming_active": trimming_active,
        "transition_spans": transition_spans_out,
        "unmatched_grid": unmatched_grid,  # 未匹配主干网格区段（原始音频时间轴；Malody 模式为 None）
        "exact_early_break": bool(exact_early_break),
        "exact_break_lag_ms": exact_break_lag_ms,
        "trim_silence": bool(trim_info.enabled),
        "silence_threshold_db": round(float(trim_info.threshold_db), 1),
        "silence_trimmed": bool(trim_info.show_badge),
        "trim_short_bursts": bool(trim_info.trim_short_bursts),
        "short_burst_max_sec": round(float(trim_info.short_burst_max_sec), 2),
        "short_burst_min_gap_sec": round(float(trim_info.short_burst_min_gap_sec), 2),
        "short_burst_trimmed": bool(trim_info.short_burst_trimmed and trim_info.show_badge),
        "head_trim_sec": trim_info.head_trim_sec,
        "tail_trim_sec": trim_info.tail_trim_sec,
        "trimmed_duration_sec": trimmed_duration_sec,
        "rmse_ms": round(float(meta.get("rmse_ms", 0.0)), 2),
        "beat_display": meta.get("beat_display", ""),
        "sample_rate": int(sr),
        "raw_sample_rate": resolved_raw_sr,
        "duration_sec": duration_sec,
        "elapsed_seconds": elapsed,
        "acf_curve": acf_curve,
        "peaks": meta.get("peaks", []),
        "candidates": meta.get("candidates", []),
        "rescue_candidates": meta.get("rescue_candidates", []),
        "ratio_up": int(meta.get("ratio_up", 1)),
        "ratio_down": int(meta.get("ratio_down", 1)),
    }


def analyze_audio(
    audio_path: str,
    mode: str = "all",
    octave_norm: bool = False,
    target_sr: int | None = None,
    trim_silence: bool = False,
    allow_any_duration: bool = False,
    acf_decay_ratio: float = 0.7,
    cand_update_ratio: float = 1.25,
    halve_contrast_ratio: float = 1.10,  # [新增 2026-11-14] 240~300 BPM 折半对比度门限
    track_tol: int = 8,
    comb_tol: int = 35,
    apply_comb_tol_to_force: bool = False,
    transition_miss_beats: int = 4,
    transition_resume_beats: int = 3,
    transition_guard: float | None = 1.0,
    trim_recalc: bool = False,
    sub_trunk_min_sec: int = 15,
    silence_threshold_db: float = DEFAULT_SILENCE_THRESHOLD_DB,
    trim_short_bursts: bool = False,
    short_burst_max_sec: float = DEFAULT_SHORT_BURST_MAX_SEC,
    short_burst_min_gap_sec: float = DEFAULT_SHORT_BURST_MIN_GAP_SEC,
    channel: str = "auto",
    _audio_meta: dict | None = None,
    progress_cb=None,
) -> dict:
    """根据所选模式 ('exact' | 'enhanced_v3' | 'force_v3' | 'all' 等) 执行分析并返回汇总结果字典。"""
    norm_channel = normalize_channel_mode(channel)
    thr_db = validate_silence_threshold_db(silence_threshold_db)
    burst_max_sec = validate_short_burst_max_sec(short_burst_max_sec)
    burst_min_gap_sec = validate_short_burst_min_gap_sec(short_burst_min_gap_sec)
    results = {}

    # 在「全部」模式下统一前置完成标准与高精度音频解码、头尾静音裁剪及有效时长校验，再单向进入包络/自相关与 BPM 测算
    shared_exact: tuple | None = None
    shared_non_exact: tuple | None = None
    if mode == "all":
        require_c_dsp(_c_dsp)
        if trim_silence:
            check_raw_safety_cap(audio_path, allow_any_duration=allow_any_duration)
        _emit_progress(progress_cb, "解码音频数据…", phase="音频解码与预处理")
        t_e0 = time.perf_counter()
        pcm_e_raw, sr_e = decode_audio_to_pcm(
            audio_path,
            target_sr=target_sr,
            exact=True,
            allow_any_duration=(allow_any_duration or bool(trim_silence)),
        )
        prep_elapsed_e = time.perf_counter() - t_e0

        pcm_f_raw, sr_f = decode_audio_to_pcm(
            audio_path,
            target_sr=target_sr,
            exact=False,
            allow_any_duration=(allow_any_duration or bool(trim_silence)),
            channel=norm_channel,
        )

        if trim_silence:
            _emit_progress(progress_cb, "去除音频头尾静音…", phase="音频解码与预处理", dwell=True)

        t_e1 = time.perf_counter()
        pcm_e, trim_info_e = trim_head_tail_silence(
            pcm_e_raw,
            sr_e,
            enabled=trim_silence,
            threshold_db=thr_db,
            trim_short_bursts=trim_short_bursts,
            short_burst_max_sec=burst_max_sec,
            short_burst_min_gap_sec=burst_min_gap_sec,
        )
        prep_elapsed_e += time.perf_counter() - t_e1
        del pcm_e_raw

        pcm_f, trim_info_f = trim_head_tail_silence(
            pcm_f_raw,
            sr_f,
            enabled=trim_silence,
            threshold_db=thr_db,
            trim_short_bursts=trim_short_bursts,
            short_burst_max_sec=burst_max_sec,
            short_burst_min_gap_sec=burst_min_gap_sec,
        )
        del pcm_f_raw

        if trim_silence:
            _emit_progress(progress_cb, pill=_build_silence_pill(trim_info_f))
            validate_trimmed_pcm_frames(
                len(pcm_e),
                sr_e,
                allow_any_duration=allow_any_duration,
                did_trim=trim_info_e.did_trim,
            )
            validate_trimmed_pcm_frames(
                len(pcm_f),
                sr_f,
                allow_any_duration=allow_any_duration,
                did_trim=trim_info_f.did_trim,
            )

        _emit_progress(progress_cb, "提取起音包络与自相关图谱…", phase="核心算法测算")
        t_e2 = time.perf_counter()
        diff_e, env_e = extract_onset_envelope(pcm_e, sr_e, exact=True)
        acf_e = compute_autocorrelation_fft(diff_e, exact=True)
        prep_elapsed_e += time.perf_counter() - t_e2
        shared_exact = (pcm_e, sr_e, diff_e, env_e, acf_e, trim_info_e, prep_elapsed_e)

        diff_f, env_f = extract_onset_envelope(pcm_f, sr_f, exact=False)
        acf_f = compute_autocorrelation_fft(diff_f, exact=False)
        shared_non_exact = (pcm_f, sr_f, diff_f, env_f, acf_f, trim_info_f)

    audio_meta = (
        dict(_audio_meta)
        if isinstance(_audio_meta, dict) and _audio_meta
        else _probe_audio_meta(audio_path)
    )
    raw_sr_val = audio_meta.get("raw_sr")
    probed_raw_sr = int(raw_sr_val) if isinstance(raw_sr_val, int) and raw_sr_val > 0 else None

    if mode in ("exact", "both", "all"):
        results["exact"] = analyze_single_mode(
            audio_path,
            mode="exact",
            octave_norm=octave_norm,
            target_sr=target_sr,
            trim_silence=trim_silence,
            allow_any_duration=allow_any_duration,
            silence_threshold_db=thr_db,
            trim_short_bursts=trim_short_bursts,
            short_burst_max_sec=burst_max_sec,
            short_burst_min_gap_sec=burst_min_gap_sec,
            _predecoded=shared_exact,
            _raw_sr=probed_raw_sr,
            progress_cb=progress_cb,
        )

    if mode in ("enhanced_v3", "all"):
        results["enhanced_v3"] = analyze_single_mode(
            audio_path,
            mode="enhanced_v3",
            octave_norm=octave_norm,
            target_sr=target_sr,
            trim_silence=trim_silence,
            allow_any_duration=allow_any_duration,
            acf_decay_ratio=acf_decay_ratio,
            cand_update_ratio=cand_update_ratio,
            halve_contrast_ratio=halve_contrast_ratio,
            track_tol=track_tol,
            comb_tol=comb_tol,
            transition_miss_beats=transition_miss_beats,
            transition_resume_beats=transition_resume_beats,
            transition_guard=transition_guard,
            trim_recalc=trim_recalc,
            sub_trunk_min_sec=sub_trunk_min_sec,
            silence_threshold_db=thr_db,
            trim_short_bursts=trim_short_bursts,
            short_burst_max_sec=burst_max_sec,
            short_burst_min_gap_sec=burst_min_gap_sec,
            channel=norm_channel,
            _predecoded=shared_non_exact,
            _raw_sr=probed_raw_sr,
            progress_cb=progress_cb,
        )

    if mode in ("force_v3", "all"):
        force_comb_tol = comb_tol if apply_comb_tol_to_force else None
        results["force_v3"] = analyze_single_mode(
            audio_path,
            mode="force_v3",
            octave_norm=octave_norm,
            target_sr=target_sr,
            trim_silence=trim_silence,
            allow_any_duration=allow_any_duration,
            comb_tol=force_comb_tol,
            transition_miss_beats=transition_miss_beats,
            transition_resume_beats=transition_resume_beats,
            transition_guard=transition_guard,
            trim_recalc=trim_recalc,
            silence_threshold_db=thr_db,
            trim_short_bursts=trim_short_bursts,
            short_burst_max_sec=burst_max_sec,
            short_burst_min_gap_sec=burst_min_gap_sec,
            channel=norm_channel,
            _predecoded=shared_non_exact,
            _raw_sr=probed_raw_sr,
            progress_cb=progress_cb,
        )

    # 保留对旧版单模式显式调用的向后兼容
    if mode in ("enhanced", "both"):
        results["enhanced"] = analyze_single_mode(
            audio_path,
            mode="enhanced",
            octave_norm=octave_norm,
            target_sr=target_sr,
            trim_silence=trim_silence,
            allow_any_duration=allow_any_duration,
            silence_threshold_db=thr_db,
            trim_short_bursts=trim_short_bursts,
            short_burst_max_sec=burst_max_sec,
            short_burst_min_gap_sec=burst_min_gap_sec,
            channel=norm_channel,
            _raw_sr=probed_raw_sr,
            progress_cb=progress_cb,
        )
    if mode in ("enhanced_v2", "both"):
        results["enhanced_v2"] = analyze_single_mode(
            audio_path,
            mode="enhanced_v2",
            octave_norm=octave_norm,
            target_sr=target_sr,
            trim_silence=trim_silence,
            allow_any_duration=allow_any_duration,
            track_tol=track_tol,
            comb_tol=comb_tol,
            transition_miss_beats=transition_miss_beats,
            transition_resume_beats=transition_resume_beats,
            transition_guard=transition_guard,
            silence_threshold_db=thr_db,
            trim_short_bursts=trim_short_bursts,
            short_burst_max_sec=burst_max_sec,
            short_burst_min_gap_sec=burst_min_gap_sec,
            channel=norm_channel,
            _raw_sr=probed_raw_sr,
            progress_cb=progress_cb,
        )

    return {
        "mode": mode,
        "channel": norm_channel,
        "octave_norm": octave_norm,
        "target_sr": target_sr,
        "trim_silence": bool(trim_silence),
        "silence_threshold_db": round(float(thr_db), 1),
        "trim_short_bursts": bool(trim_silence and trim_short_bursts),
        "short_burst_max_sec": round(float(burst_max_sec), 2),
        "short_burst_min_gap_sec": round(float(burst_min_gap_sec), 2),
        "allow_any_duration": bool(allow_any_duration),
        "trim_recalc": bool(trim_recalc),
        "sub_trunk_min_sec": int(sub_trunk_min_sec),
        "audio_meta": audio_meta,
        "results": results,
    }


_BASE_INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>音频 BPM 深度测算工具</title>
<style>
  :root {
    color-scheme: dark;
    --header-h: 64px;
    --bg: #000000;
    --panel: #141415; /* 主界面卡片面板，提升深色对比与视觉层级 */
    --panel-alt: #1c1c1f; /* 主界面次级悬停/交互块背景 */
    --border: #242424;
    --text: #FFFFFF;
    --muted: #8b949e;
    --accent: #58a6ff;
    --accent-glow: rgba(88, 166, 255, 0.18);
    --green: #3fb950;
    --amber: #d29922;
    --purple: #bc8cff;
    --red: #f85149;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  button, input, select, textarea { font-family: inherit; }
  body {
    font-family: "Helvetica Neue", Arial, "PingFang SC", "Microsoft YaHei", sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    display: flex;
    flex-direction: column;
  }
  header {
    position: sticky;
    top: 0;
    z-index: 90;
    min-height: var(--header-h);
    background: rgba(20, 20, 22, 0.88); /* 顶部固定导航条微透深空灰背景 */
    backdrop-filter: blur(14px);
    -webkit-backdrop-filter: blur(14px);
    border-bottom: 1px solid rgba(255, 255, 255, 0.38); /* 顶部标题栏底部分界线，统一柔和半透白边框 */
    box-shadow: 0 1px 0 rgba(255, 255, 255, 0.03), 0 6px 20px rgba(0, 0, 0, 0.45);
    padding: 11px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 12px 18px;
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 13px;
    min-width: 0;
  }
  .brand-emblem {
    width: 38px;
    height: 38px;
    border-radius: 10px;
    background: linear-gradient(180deg, #181818 0%, #0e0e0e 100%);
    border: 1px solid #282828;
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.06), 0 2px 8px rgba(0, 0, 0, 0.45);
    display: inline-flex;
    align-items: center;
    justify-content: center;
    color: var(--accent);
    flex-shrink: 0;
  }
  .brand-emblem svg {
    width: 21px;
    height: 21px;
    filter: drop-shadow(0 0 6px rgba(88, 166, 255, 0.28));
  }
  .brand-text {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }
  .brand h1 {
    font-size: 17px;
    font-weight: 700;
    color: var(--text);
    letter-spacing: 0.3px;
    line-height: 1.24;
  }
  .brand-sub {
    font-size: 11.5px;
    color: var(--muted);
    letter-spacing: 0.2px;
    line-height: 1.3;
  }
  .controls {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    /* 顶栏按钮右对齐 + 逐个折行（修改日志 2026-11-11）：
       - flex: 1 1 0 让按钮区占据标题右侧剩余宽度，而不是按"全部按钮一行"的宽度参与折行，
         因此窗口变窄时只有放不下的按钮才被挤到下一行；
       - 默认 min-width:auto = 最宽单个按钮的宽度，连一个按钮都放不下时整组才掉到标题下方；
       - justify-content: flex-end 保证每一行（含折行）都贴右。 */
    flex: 1 1 0;
    justify-content: flex-end;
  }
  .controls > * {
    /* 按钮自身不压缩、文字不断行，保证"最宽按钮"这一阈值稳定 */
    flex-shrink: 0;
    white-space: nowrap;
  }
  .seg-group {
    display: inline-flex;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 2px;
  }
  .seg-btn {
    background: transparent;
    border: none;
    color: var(--muted);
    padding: 6px 12px;
    font-size: 13px;
    border-radius: 6px;
    cursor: pointer;
    transition: all 0.15s;
  }
  .seg-btn.active {
    background: var(--accent);
    color: #000000;
    font-weight: 600;
  }
  .ctrl-item {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 13px;
    color: var(--text);
  }
  select, .btn {
    background: var(--panel-alt);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 13px;
    cursor: pointer;
  }
  .btn-primary {
    background: var(--accent);
    color: #000000;
    border-color: var(--accent);
    font-weight: 600;
  }
  .btn-primary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .main-layout {
    display: grid;
    grid-template-columns: 310px 1fr;
    gap: 18px;
    padding: 18px 24px;
    flex: 1;
  }
  .sidebar {
    display: flex;
    flex-direction: column;
    gap: 14px;
    min-width: 0;
    position: sticky;
    top: calc(var(--header-h, 64px) + 18px);
    align-self: start;
    height: calc(100vh - var(--header-h, 64px) - 36px);
    min-height: 392px;
  }
  @media (max-width: 960px) {
    .main-layout { grid-template-columns: 1fr; }
    .sidebar {
      position: static;
      height: auto;
      min-height: 0;
      max-height: 55vh;
    }
  }
  .dropzone {
    background: var(--panel);
    border: 1px solid rgba(255, 255, 255, 0.38); /* 柔和半透白过渡边框 */
    border-radius: 12px;
    padding: 24px 16px;
    text-align: center;
    cursor: pointer;
    user-select: none;
    transition: all 0.2s;
  }
  .dropzone:hover {
    border-color: var(--accent);
    background: var(--accent-glow);
  }
  .dropzone-icon {
    font-size: 28px;
    margin-bottom: 8px;
  }
  .dropzone-title {
    font-size: 14px;
    font-weight: 600;
    margin-bottom: 4px;
  }
  .dropzone-sub {
    font-size: 12px;
    color: var(--muted);
  }
  .file-list-card {
    background: var(--panel);
    border: 1px solid rgba(255, 255, 255, 0.38); /* 柔和半透白过渡边框 */
    border-radius: 12px;
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    min-height: 180px;
  }
  .card-header {
    padding: 10px 14px;
    border-bottom: 1px solid var(--border);
    font-size: 13px;
    font-weight: 600;
    display: flex;
    justify-content: space-between;
    align-items: center;
    color: var(--muted);
  }
  .file-list {
    list-style: none;
    overflow-y: auto;
    flex: 1;
    min-height: 0;
  }
  .file-item {
    padding: 10px 10px 10px 11px;
    border-left: 3px solid transparent;
    border-bottom: 1px solid rgba(36, 36, 36, 0.65);
    cursor: pointer;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
    transition: background 0.12s;
  }
  .file-item:hover { background: var(--panel-alt); }
  .file-item.active {
    background: rgba(88, 166, 255, 0.14);
    border-left-color: var(--accent);
  }
  .file-name-wrap {
    flex: 1;
    min-width: 0;
    font-size: 13px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .file-item.active .file-name-wrap {
    text-overflow: clip;
  }
  .file-name-text {
    display: inline;
  }
  .file-item.active .file-name-text {
    display: inline-block;
    will-change: transform;
  }
  .file-item-right {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    flex-shrink: 0;
  }
  .file-badge {
    font-size: 12px;
    padding: 2px 7px;
    border-radius: 4px;
    background: var(--panel-alt);
    color: var(--green);
    flex-shrink: 0;
  }
  .file-badge.pending { color: var(--amber); }
  .file-badge.err { color: var(--red); }
  .file-remove-btn {
    width: 20px;
    height: 20px;
    border-radius: 4px;
    border: none;
    background: transparent;
    color: var(--muted);
    opacity: 0.4;
    font-size: 12px;
    line-height: 1;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    transition: opacity 0.12s, color 0.12s, background 0.12s;
  }
  .file-item:hover .file-remove-btn,
  .file-item.active .file-remove-btn {
    opacity: 0.82;
  }
  .file-remove-btn:hover {
    opacity: 1 !important;
    color: var(--red);
    background: rgba(248, 81, 73, 0.15);
  }
  .content-area {
    display: flex;
    flex-direction: column;
    gap: 16px;
    min-width: 0;
  }
  .cards-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(290px, 1fr));
    gap: 16px;
  }
  .res-card {
    background: var(--panel);
    border: 1px solid rgba(255, 255, 255, 0.38); /* 柔和半透白过渡边框 */
    border-radius: 12px;
    padding: 18px;
    display: flex;
    flex-direction: column;
    gap: 14px;
    min-width: 0;
    transition: border-color 0.15s ease, box-shadow 0.15s ease;
  }
  .res-card.card-selectable {
    cursor: pointer;
  }
  .res-card.card-selectable:hover {
    border-color: #FFFFFF;
    box-shadow: 0 0 12px rgba(255, 255, 255, 0.22); /* 悬浮时增加纯白外发光微晕提升交互层次 */
  }
  .res-card.card-selectable:focus-visible {
    outline: none;
    border-color: rgba(88, 166, 255, 0.75);
    box-shadow: 0 0 0 2px rgba(88, 166, 255, 0.28);
  }
  .res-card.enhanced-card {
    border-color: rgba(88, 166, 255, 0.48);
    box-shadow: 0 0 16px rgba(88, 166, 255, 0.07);
  }
  .res-card.force-card {
    border-color: rgba(188, 140, 255, 0.45);
    box-shadow: 0 0 16px rgba(188, 140, 255, 0.07);
  }
  /* 「全部」模式下强制线性回归模式卡片默认折叠（2026-11 新增）：
     正文仍完整渲染但以 visibility 隐藏（保持卡片尺寸不变、内部按钮不可聚焦/点击），提示层绝对定位居中覆盖；
     点按超链接后移除 is-collapsed 即展开为普通卡片（不记忆，每次重绘重新折叠）。 */
  .force-collapse-wrap {
    position: relative;
  }
  .force-collapse-body {
    display: flex;
    flex-direction: column;
    gap: 14px; /* 与 .res-card 子元素间距一致，保证折叠包裹前后版式相同 */
  }
  .force-collapse-wrap.is-collapsed > .force-collapse-body {
    visibility: hidden;
  }
  .force-collapse-overlay {
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    text-align: center;
    padding: 0 12px;
    font-size: 13px;
    line-height: 1.7;
    color: var(--muted);
  }
  .force-collapse-wrap:not(.is-collapsed) > .force-collapse-overlay {
    display: none;
  }
  .force-reveal-link {
    color: var(--accent);
    text-decoration: underline;
    text-underline-offset: 2px;
    cursor: pointer;
    white-space: nowrap;
  }
  .force-reveal-link:hover {
    filter: brightness(1.2);
  }
  .force-reveal-link:focus-visible {
    outline: 1px solid rgba(88, 166, 255, 0.75);
    outline-offset: 2px;
    border-radius: 2px;
  }
  .res-card.card-selectable.acf-selected {
    box-shadow: 0 0 0 1px rgba(88, 166, 255, 0.55), 0 6px 20px rgba(0, 0, 0, 0.28);
  }
  .acf-viewing-tag {
    font-size: 11px;
    font-weight: 600;
    color: var(--accent);
    background: rgba(88, 166, 255, 0.14);
    border: 1px solid rgba(88, 166, 255, 0.36);
    border-radius: 4px;
    padding: 1px 6px;
    display: none;
  }
  .res-card.card-selectable.acf-selected .acf-viewing-tag {
    display: inline-block;
  }
  .res-top {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
  }
  .mode-tag {
    font-size: 12px;
    font-weight: 600;
    padding: 3px 9px;
    border-radius: 5px;
    background: var(--panel-alt);
    color: var(--muted);
  }
  .mode-tag.enh {
    background: rgba(88, 166, 255, 0.18);
    color: var(--accent);
  }
  .mode-tag.force {
    background: rgba(188, 140, 255, 0.22);
    color: var(--purple);
  }
  .bpm-hero {
    display: flex;
    align-items: baseline;
    gap: 12px;
    flex-wrap: wrap;
  }
  .bpm-val {
    font-size: 40px;
    letter-spacing: -0.5px;
    color: var(--text);
  }
  .bpm-val.fail {
    font-size: 24px;
    color: var(--red);
  }
  .triple-alt-box {
    background: rgba(210, 153, 34, 0.1);
    border: 1px solid rgba(210, 153, 34, 0.4);
    border-radius: 8px;
    padding: 9px 11px;
    display: flex;
    flex-direction: column;
    gap: 7px;
  }
  .triple-alt-title {
    font-size: 11.5px;
    font-weight: 600;
    color: var(--amber);
  }
  .triple-alt-row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }
  .triple-alt-item {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 4px 8px;
    font-size: 12px;
  }
  .triple-alt-item .alt-k {
    color: var(--muted);
    font-size: 11px;
  }
  .triple-alt-item .alt-v {
    color: var(--text);
    font-weight: 600;
  }
  .copy-btn {
    background: var(--panel-alt);
    border: 1px solid var(--border);
    color: var(--muted);
    font-size: 12px;
    padding: 4px 8px;
    border-radius: 5px;
    cursor: pointer;
    transition: opacity 0.12s, color 0.12s, border-color 0.12s;
  }
  .copy-btn:hover:not(:disabled) { color: var(--text); border-color: var(--accent); }
  .copy-btn:disabled {
    opacity: 0.38;
    cursor: not-allowed;
  }
  .metrics-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 10px 14px;
    background: var(--bg);
    padding: 12px;
    border-radius: 8px;
    border: 1px solid var(--border);
  }
  .metric-box {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .metric-box.metric-span-full {
    grid-column: 1 / -1;
    border-top: 1px solid rgba(36, 36, 36, 0.85);
    padding-top: 9px;
    margin-top: 1px;
  }
  .metric-label {
    font-size: 11px;
    color: var(--muted);
  }
  .metric-val {
    font-size: 13px;
    font-weight: 600;
  }
  .metric-sub-status {
    margin-top: 3px;
    font-size: 11.5px;
    line-height: 1.5;
    color: var(--muted);
    word-break: break-word;
  }
  .metric-sub-status.is-warn {
    color: var(--amber);
  }
  .metric-head-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
  }
  .metric-help-link {
    font-size: 11px;
    color: var(--accent);
    cursor: pointer;
    user-select: none;
    line-height: 1.2;
    transition: color 0.12s ease;
  }
  .metric-help-link:hover,
  .metric-help-link:focus-visible {
    color: #79c0ff;
    text-decoration: underline;
    text-underline-offset: 2px;
    outline: none;
  }
  .metric-popover {
    position: fixed;
    z-index: 1200;
    display: none;
    pointer-events: none;
    width: max-content;
    max-width: min(350px, calc(100vw - 24px));
    background: rgba(16, 16, 16, 0.98);
    border: 1px solid var(--border);
    border-radius: 9px;
    padding: 10px 13px;
    box-shadow: 0 10px 28px rgba(0, 0, 0, 0.52);
  }
  .metric-popover.is-enh {
    border-color: rgba(88, 166, 255, 0.45);
  }
  .metric-popover.is-force {
    border-color: rgba(188, 140, 255, 0.45);
  }
  .metric-popover-title {
    font-size: 12px;
    font-weight: 600;
    color: var(--text);
    margin-bottom: 5px;
  }
  .metric-popover-body {
    font-size: 12px;
    line-height: 1.58;
    color: #FFFFFF;
    font-weight: 400;
    white-space: normal;
  }
  .diag-row {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    font-size: 12px;
  }
  .pill {
    padding: 3px 8px;
    border-radius: 4px;
    background: var(--panel-alt);
    border: 1px solid var(--border);
    color: var(--muted);
  }
  .pill.highlight {
    color: var(--green);
    border-color: rgba(63, 185, 80, 0.4);
  }
  .pill.fold {
    color: var(--purple);
    border-color: rgba(188, 140, 255, 0.4);
  }
  .pill.amber {
    color: var(--amber);
    border-color: rgba(210, 153, 34, 0.45);
  }
  .audio-info-bar {
    background: var(--panel);
    border: 1px solid rgba(255, 255, 255, 0.38); /* 柔和半透白过渡边框 */
    border-radius: 12px;
    padding: 11px 16px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px 16px;
    flex-wrap: wrap;
  }
  .audio-info-title-wrap {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 150px;
    flex: 1 1 200px;
    overflow: hidden;
  }
  .audio-info-title {
    font-size: 13.5px;
    font-weight: 600;
    color: var(--text);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .audio-info-pills {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 6px;
    font-size: 12px;
  }
  .pill.sr-resample-pill {
    font-size: 12px;
    color: var(--muted);
    cursor: help;
    line-height: inherit;
    transition: color 0.12s ease, border-color 0.12s ease;
  }
  .pill.sr-resample-pill:hover {
    color: var(--text);
    border-color: rgba(88, 166, 255, 0.48);
    text-decoration: none;
  }
  .pill.amber.sr-resample-pill {
    color: var(--amber);
  }
  .pill.amber.sr-resample-pill:hover {
    color: var(--amber);
    border-color: rgba(210, 153, 34, 0.78);
  }
  .sr-resample-text {
    border-bottom: 1px dashed rgba(139, 148, 158, 0.72);
    padding-bottom: 0.5px;
  }
  .pill.amber .sr-resample-text {
    border-bottom-color: rgba(210, 153, 34, 0.75);
  }
  .pill.sr-resample-pill:hover .sr-resample-text {
    border-bottom-color: currentColor;
  }
  .chart-card {
    background: var(--panel);
    border: 1px solid rgba(255, 255, 255, 0.38); /* 柔和半透白过渡边框 */
    border-radius: 12px;
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .chart-toolbar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 10px;
  }
  .legend {
    display: flex;
    gap: 14px;
    flex-wrap: wrap;
    font-size: 12px;
    color: var(--muted);
  }
  .legend-item {
    display: flex;
    align-items: center;
    gap: 5px;
  }
  .dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    display: inline-block;
  }
  .canvas-wrap {
    position: relative;
    width: 100%;
    height: 310px;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
  }
  canvas {
    width: 100%;
    height: 100%;
    display: block;
    cursor: crosshair;
  }
  .tooltip {
    position: absolute;
    pointer-events: none;
    background: rgba(16, 16, 16, 0.95);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 7px 10px;
    font-size: 12px;
    color: var(--text);
    display: none;
    z-index: 10;
    white-space: nowrap;
  }
  .empty-state {
    background: var(--panel);
    border: 1px solid rgba(255, 255, 255, 0.38); /* 柔和半透白过渡边框 */
    border-radius: 12px;
    padding: 38px 24px;
    text-align: center;
    color: var(--muted);
  }
  .header-combo-btn {
    height: 36px;
    padding: 0 13px 0 12px;
    border-radius: 999px;
    background: rgba(88, 166, 255, 0.18);
    border: 1px solid rgba(88, 166, 255, 0.52);
    color: var(--text);
    display: inline-flex;
    align-items: center;
    gap: 9px;
    cursor: pointer;
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.08), 0 0 12px rgba(88, 166, 255, 0.12);
    transition: background 0.16s ease, border-color 0.16s ease, box-shadow 0.16s ease;
  }
  .header-combo-btn:hover:not(:disabled) {
    background: rgba(88, 166, 255, 0.26);
    border-color: rgba(121, 192, 255, 0.78);
    box-shadow: 0 0 0 3px rgba(88, 166, 255, 0.16), 0 0 16px rgba(88, 166, 255, 0.20);
  }
  .header-combo-btn:active:not(:disabled) {
    background: rgba(88, 166, 255, 0.32);
  }
  .header-combo-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
  .header-btn-icon {
    width: 14px;
    height: 14px;
    flex-shrink: 0;
  }
  .btn-danger {
    background: var(--red);
    color: #FFFFFF;
    border-color: var(--red);
    font-weight: 600;
  }
  .btn-danger:hover {
    filter: brightness(1.08);
  }

  /* 启动非阻塞居中 HUD 缩放提示悬浮窗 */
  .startup-zoom-hud {
    position: fixed;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    z-index: 1500;
    pointer-events: none;
    user-select: none;
    display: inline-flex;
    align-items: center;
    gap: 10px;
    padding: 13px 20px;
    border-radius: 14px;
    background: rgba(18, 18, 20, 0.92);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    border: 1px solid rgba(88, 166, 255, 0.45);
    box-shadow: 0 14px 36px rgba(0, 0, 0, 0.65), 0 0 18px rgba(88, 166, 255, 0.14);
    color: var(--text);
    font-size: 14.5px;
    line-height: 1.4;
    white-space: nowrap;
    max-width: calc(100vw - 32px);
    opacity: 1;
    transition: opacity 0.22s ease, transform 0.22s ease;
  }
  .startup-zoom-hud.is-hidden {
    opacity: 0;
    transform: translate(-50%, calc(-50% - 6px));
  }
  .startup-zoom-badge {
    font-size: 12.5px;
    font-weight: 600;
    color: #79c0ff;
    background: rgba(88, 166, 255, 0.18);
    border: 1px solid rgba(88, 166, 255, 0.42);
    border-radius: 6px;
    padding: 2px 8px;
    line-height: 1.3;
    flex-shrink: 0;
  }

  /* Android Material You 深色居中模态弹窗与分组卡片岛样式 */
  .modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.82);
    backdrop-filter: blur(6px);
    display: none;
    align-items: center;
    justify-content: center;
    z-index: 1000;
    padding: 16px;
  }
  .modal-backdrop.open {
    display: flex;
  }
  .modal-backdrop.confirm-layer {
    z-index: 1100;
  }
  .modal-dialog {
    background: #161618; /* 二级模态弹窗外壳统一深空灰调 */
    border: 2px solid #FFFFFF; /* 二级页面/模态弹窗外围纯白高对比过渡边框 */
    border-radius: 26px;
    width: min(760px, 100%);
    max-height: 88vh;
    display: flex;
    flex-direction: column;
    box-shadow: 0 24px 64px rgba(0, 0, 0, 0.65);
    overflow: hidden;
  }
  .modal-dialog.confirm-dialog {
    background: #1a1a1d; /* 确认对话框背景 */
    width: min(400px, 100%);
    border-radius: 24px;
  }
  .modal-header {
    padding: 18px 22px 14px 24px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .modal-dialog.confirm-dialog .modal-header {
    border-bottom: none;
    padding: 22px 24px 6px 24px;
  }
  .modal-header h2 {
    font-size: 18px;
    font-weight: 600;
    letter-spacing: 0.2px;
    color: #FFFFFF;
  }
  .modal-close-btn {
    background: transparent;
    border: none;
    color: #9aa4b2;
    font-size: 17px;
    cursor: pointer;
    width: 34px;
    height: 34px;
    border-radius: 50%;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    line-height: 1;
    transition: background 0.15s ease, color 0.15s ease;
  }
  .modal-close-btn:hover {
    color: #FFFFFF;
    background: rgba(255, 255, 255, 0.08);
  }
  .modal-body {
    padding: 18px 20px 24px 20px;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 22px;
  }
  .modal-dialog.confirm-dialog .modal-body {
    padding: 8px 24px 20px 24px;
  }
  .modal-body::-webkit-scrollbar {
    width: 6px;
  }
  .modal-body::-webkit-scrollbar-track {
    background: transparent;
  }
  .modal-body::-webkit-scrollbar-thumb {
    background: rgba(255, 255, 255, 0.14);
    border-radius: 999px;
  }
  .modal-footer {
    padding: 14px 22px;
    border-top: 1px solid rgba(255, 255, 255, 0.06);
    background: #121214; /* 弹窗底栏过渡深色背景 */
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  .modal-dialog.confirm-dialog .modal-footer {
    border-top: none;
    background: transparent;
    padding: 0 24px 20px 24px;
  }
  .modal-footer-right {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .m3-btn {
    border-radius: 999px;
    padding: 8px 18px;
    font-size: 13.5px;
    font-weight: 500;
    cursor: pointer;
    border: 1px solid transparent;
    transition: background 0.15s ease, color 0.15s ease, filter 0.15s ease;
  }
  .m3-btn-text {
    background: transparent;
    color: #9aa4b2;
  }
  .m3-btn-text:hover {
    background: rgba(255, 255, 255, 0.06);
    color: #FFFFFF;
  }
  .m3-btn-tonal {
    background: rgba(255, 255, 255, 0.07);
    color: #FFFFFF;
  }
  .m3-btn-tonal:hover {
    background: rgba(255, 255, 255, 0.12);
  }
  .m3-btn-filled {
    background: #8ab4f8;
    color: #000000;
    font-weight: 600;
  }
  .m3-btn-filled:hover {
    background: #a8c7fa;
  }
  .m3-btn-danger {
    background: #f28b82;
    color: #3c0806;
    font-weight: 600;
  }
  .m3-btn-danger:hover {
    filter: brightness(1.06);
  }

  /* __SETTINGS_CSS_ANCHOR__ */
</style>
</head>
<body>

<header>
  <div class="brand">
    <span class="brand-emblem" aria-hidden="true">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">
        <path d="M2.5 12h3.2l2.1-4.8 3.4 9.6 3.2-11.2 2.7 8.4 1.9-2h2.5"/>
      </svg>
    </span>
    <div class="brand-text">
      <h1>音频 BPM 深度测算工具</h1>
      <div class="brand-sub">基于 Malody 测速内核衍生增强 · 集成 Offset 包络定位</div>
    </div>
  </div>
  <div class="controls">
    <!-- __SETTINGS_HEADER_BTN_ANCHOR__ -->
  </div>
</header>

<div class="main-layout">
  <aside class="sidebar">
    <div class="dropzone" id="dropzone" role="button" tabindex="0">
      <div class="dropzone-icon">🎵</div>
      <div class="dropzone-title">点按此处选择音频文件</div>
      <div class="dropzone-sub">支持 .ogg / .flac / .mp3 / .wav / .m4a (可多选批量)</div>
      <input type="file" id="fileInput" multiple accept="audio/*,.ogg,.flac,.mp3,.wav,.m4a,.opus,.aac" style="display:none">
    </div>
    <div class="file-list-card">
      <div class="card-header">
        <span>测算队列 (<span id="queueCount">0</span>)</span>
        <button class="copy-btn" id="clearQueueBtn" style="padding:2px 6px;" disabled>清空</button>
      </div>
      <ul class="file-list" id="fileList"></ul>
    </div>
  </aside>

  <main class="content-area" id="mainContent">
    <div class="empty-state" id="emptyState">
      <p style="font-size:15px; margin-bottom:6px;">尚未导入音频文件</p>
      <p style="font-size:13px;">请从左侧点选曲目，即可查看各模式 BPM 测算结果及自相关峰值图谱</p>
    </div>

    <div id="detailView" style="display:none; flex-direction:column; gap:16px;">
      <div class="audio-info-bar" id="audioInfoBar" style="display:none;"></div>
      <div class="cards-grid" id="cardsGrid"></div>

      <div class="chart-card" id="chartCard">
        <div class="chart-toolbar">
          <div style="display:flex; align-items:center; flex-wrap:wrap; gap:8px 12px;">
            <strong style="font-size:14px;">自相关曲线 (ACF) 与候选峰值简图</strong>
            <div class="seg-group" id="acfSourceGroup">
              <button class="seg-btn" data-src="exact">Malody模式 ACF</button>
              <button class="seg-btn active" data-src="enhanced_v3">增强模式 ACF</button>
              <button class="seg-btn" data-src="force_v3">强制线性回归 ACF</button>
            </div>
          </div>
          <div style="display:flex; align-items:center; flex-wrap:wrap; gap:8px 12px;">
            <div class="legend">
              <span class="legend-item"><span class="dot" style="background:#58a6ff;"></span>ACF 曲线</span>
              <span class="legend-item"><span class="dot" style="background:#8b949e;"></span>局部峰值 (15ms窗)</span>
              <span class="legend-item"><span class="dot" style="background:#d29922;"></span>阶段1入选候选</span>
              <span class="legend-item"><span class="dot" style="background:#3fb950;"></span>胜出周期及谐波梳 (kτ)</span>
              <span class="legend-item"><span class="dot" style="background:#bc8cff;"></span>折叠后最终周期</span>
            </div>
            <div class="seg-group" id="zoomGroup">
              <button class="seg-btn" data-maxlag="1000">1s</button>
              <button class="seg-btn" data-maxlag="2000">2s</button>
              <button class="seg-btn active" data-maxlag="4000">4s</button>
            </div>
          </div>
        </div>
        <div class="canvas-wrap">
          <canvas id="acfCanvas"></canvas>
          <div class="tooltip" id="chartTooltip"></div>
        </div>
      </div>
    </div>
  </main>
</div>

<!-- __SETTINGS_MODALS_ANCHOR__ -->

<!-- 节拍匹配与占总时长比例 · 0延迟轻量计算说明浮窗 -->
<div class="metric-popover" id="metricNotePopover" role="tooltip" aria-hidden="true">
  <div class="metric-popover-title" id="metricNotePopoverTitle"></div>
  <div class="metric-popover-body" id="metricNotePopoverBody"></div>
</div>

<!-- 启动非阻塞居中 HUD 缩放提示悬浮窗 -->
<div class="startup-zoom-hud" id="startupZoomHud" role="status" aria-live="polite">
  <span class="startup-zoom-badge">提示</span>
  <span>关闭浏览器缩放功能可获得最佳体验。</span>
</div>

<script>
// __SETTINGS_CONFIG_JS_ANCHOR__

const state = {
  settings: createDefaultSettings(),
  draftSettings: createDefaultSettings(),
  items: [],       // [{ file, name, status, data, error }]
  activeIndex: -1,
  acfSource: "enhanced_v3",
  maxLag: 4000,
};

const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("fileInput");
const fileListEl = document.getElementById("fileList");
const queueCountEl = document.getElementById("queueCount");
const clearQueueBtn = document.getElementById("clearQueueBtn");
const emptyState = document.getElementById("emptyState");
const detailView = document.getElementById("detailView");
const audioInfoBar = document.getElementById("audioInfoBar");
const cardsGrid = document.getElementById("cardsGrid");
const chartCard = document.getElementById("chartCard");
const acfCanvas = document.getElementById("acfCanvas");
const chartTooltip = document.getElementById("chartTooltip");

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// __SETTINGS_CONTROLLER_JS_ANCHOR__

clearQueueBtn.addEventListener("click", () => {
  if (state.items.length === 0) return;
  state.items = [];
  state.activeIndex = -1;
  renderFileList(true);
  renderActiveDetail();
});

// 左栏选中项长文件名平滑滚字（1.5s 起始停顿 → 平滑左滚到头 → 1.5s 停顿 → 重置回开头循环）
const MARQUEE_PAUSE_MS = 1500;
const MARQUEE_SPEED_PX_PER_SEC = 38;
const fileRowMap = new WeakMap();
const marqueeState = {
  rafId: null,
  activeItem: null,
  wrapEl: null,
  textEl: null,
  overflowPx: 0,
  phase: "idle", // "start_pause" | "scrolling" | "end_pause"
  phaseStartTs: 0,
};

function stopMarquee() {
  if (marqueeState.rafId !== null) {
    cancelAnimationFrame(marqueeState.rafId);
    marqueeState.rafId = null;
  }
  if (marqueeState.textEl) {
    marqueeState.textEl.style.transform = "";
  }
  marqueeState.activeItem = null;
  marqueeState.wrapEl = null;
  marqueeState.textEl = null;
  marqueeState.overflowPx = 0;
  marqueeState.phase = "idle";
  marqueeState.phaseStartTs = 0;
}

function measureRowOverflow(wrapEl, textEl) {
  if (!wrapEl || !textEl) return 0;
  return textEl.getBoundingClientRect().width - wrapEl.clientWidth;
}

function stepMarquee(now) {
  if (!marqueeState.activeItem || !marqueeState.textEl || !marqueeState.wrapEl) {
    stopMarquee();
    return;
  }
  const elapsed = now - marqueeState.phaseStartTs;
  if (marqueeState.phase === "start_pause") {
    marqueeState.textEl.style.transform = "translateX(0px)";
    if (elapsed >= MARQUEE_PAUSE_MS) {
      const latestOverflow = measureRowOverflow(marqueeState.wrapEl, marqueeState.textEl);
      if (latestOverflow <= 1.5) {
        stopMarquee();
        return;
      }
      marqueeState.overflowPx = latestOverflow;
      marqueeState.phase = "scrolling";
      marqueeState.phaseStartTs = now;
    }
  } else if (marqueeState.phase === "scrolling") {
    const dist = (elapsed / 1000) * MARQUEE_SPEED_PX_PER_SEC;
    if (dist >= marqueeState.overflowPx) {
      marqueeState.textEl.style.transform = `translateX(${-marqueeState.overflowPx}px)`;
      marqueeState.phase = "end_pause";
      marqueeState.phaseStartTs = now;
    } else {
      marqueeState.textEl.style.transform = `translateX(${-dist}px)`;
    }
  } else if (marqueeState.phase === "end_pause") {
    marqueeState.textEl.style.transform = `translateX(${-marqueeState.overflowPx}px)`;
    if (elapsed >= MARQUEE_PAUSE_MS) {
      marqueeState.textEl.style.transform = "translateX(0px)";
      marqueeState.phase = "start_pause";
      marqueeState.phaseStartTs = now;
    }
  }
  marqueeState.rafId = requestAnimationFrame(stepMarquee);
}

function syncActiveMarquee(forceRestart = false) {
  const activeItem = (state.activeIndex >= 0 && state.activeIndex < state.items.length)
    ? state.items[state.activeIndex]
    : null;
  if (!activeItem) {
    stopMarquee();
    return;
  }
  const li = fileRowMap.get(activeItem);
  if (!li) {
    stopMarquee();
    return;
  }
  const wrapEl = li.querySelector(".file-name-wrap");
  const textEl = li.querySelector(".file-name-text");
  const overflowPx = measureRowOverflow(wrapEl, textEl);

  if (overflowPx <= 1.5) {
    stopMarquee();
    return;
  }

  if (!forceRestart && marqueeState.activeItem === activeItem && marqueeState.rafId !== null) {
    marqueeState.wrapEl = wrapEl;
    marqueeState.textEl = textEl;
    marqueeState.overflowPx = overflowPx;
    if (marqueeState.phase === "end_pause") {
      marqueeState.textEl.style.transform = `translateX(${-overflowPx}px)`;
    }
    return;
  }

  stopMarquee();
  marqueeState.activeItem = activeItem;
  marqueeState.wrapEl = wrapEl;
  marqueeState.textEl = textEl;
  marqueeState.overflowPx = overflowPx;
  marqueeState.phase = "start_pause";
  marqueeState.phaseStartTs = performance.now();
  textEl.style.transform = "translateX(0px)";
  marqueeState.rafId = requestAnimationFrame(stepMarquee);
}

// 全局键盘快捷键（Esc 逐层关闭弹窗，↑/↓ 切换队列选中项）
window.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (activeNoteLinkEl) {
      hideMetricNotePopover();
      return;
    }
    handleSettingsEscapeKey(e);
    return;
  }

  if (e.key === "ArrowUp" || e.key === "ArrowDown") {
    if (isSettingsModalOpen()) {
      return;
    }
    const tag = e.target && e.target.tagName ? e.target.tagName.toUpperCase() : "";
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (e.target && e.target.isContentEditable)) {
      return;
    }
    if (state.items.length === 0) return;

    e.preventDefault();
    const dir = e.key === "ArrowUp" ? -1 : 1;
    const nextIdx = state.activeIndex < 0
      ? 0
      : Math.max(0, Math.min(state.items.length - 1, state.activeIndex + dir));
    if (nextIdx === state.activeIndex) return;

    state.activeIndex = nextIdx;
    renderFileList(true);
    const activeLi = fileRowMap.get(state.items[nextIdx]);
    if (activeLi && activeLi.scrollIntoView) {
      activeLi.scrollIntoView({ block: "nearest" });
    }
    renderActiveDetail();
  }
});

// ACF 图表数据源与结果卡片双向联动切换
function syncCardAcfSelection() {
  cardsGrid.querySelectorAll(".res-card[data-card-mode]").forEach(card => {
    const isCur = card.dataset.cardMode === state.acfSource;
    card.classList.toggle("acf-selected", isCur);
    if (card.classList.contains("card-selectable")) {
      card.setAttribute("aria-pressed", isCur ? "true" : "false");
    }
  });
}

function syncAcfSourceButtons() {
  const activeItem = (state.activeIndex >= 0 && state.activeIndex < state.items.length)
    ? state.items[state.activeIndex]
    : null;
  const results = (activeItem && activeItem.data && activeItem.data.results) ? activeItem.data.results : null;

  document.querySelectorAll("#acfSourceGroup .seg-btn").forEach(b => {
    const src = b.dataset.src;
    if (results) {
      b.style.display = results[src] ? "" : "none";
    } else {
      b.style.display = "";
    }
    b.classList.toggle("active", src === state.acfSource);
  });
}

document.querySelectorAll("#acfSourceGroup .seg-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    state.acfSource = btn.dataset.src;
    syncCardAcfSelection();
    syncAcfSourceButtons();
    drawAcfChart();
  });
});

const metricNotePopover = document.getElementById("metricNotePopover");
const metricNotePopoverTitle = document.getElementById("metricNotePopoverTitle");
const metricNotePopoverBody = document.getElementById("metricNotePopoverBody");
let activeNoteLinkEl = null;
let isMetricNotePinned = false;

function restoreCardNativeTitle(linkEl) {
  if (!linkEl) return;
  const card = linkEl.closest(".res-card");
  if (card && card.dataset.savedTitle !== undefined) {
    card.setAttribute("title", card.dataset.savedTitle);
    delete card.dataset.savedTitle;
  }
}

function hideMetricNotePopover() {
  if (activeNoteLinkEl) {
    restoreCardNativeTitle(activeNoteLinkEl);
    activeNoteLinkEl = null;
  }
  isMetricNotePinned = false;
  if (metricNotePopover) {
    metricNotePopover.style.display = "none";
    metricNotePopover.setAttribute("aria-hidden", "true");
  }
}

function showMetricNotePopover(linkEl, pin = false) {
  if (!linkEl || !metricNotePopover) return;
  if (activeNoteLinkEl && activeNoteLinkEl !== linkEl) {
    restoreCardNativeTitle(activeNoteLinkEl);
  }
  activeNoteLinkEl = linkEl;
  if (pin) {
    isMetricNotePinned = true;
  }

  const card = linkEl.closest(".res-card");
  if (card && card.hasAttribute("title")) {
    card.dataset.savedTitle = card.getAttribute("title");
    card.removeAttribute("title");
  }

  const mode = linkEl.dataset.noteMode || "";
  metricNotePopover.classList.toggle("is-enh", mode === "enhanced_v3");
  metricNotePopover.classList.toggle("is-force", mode === "force_v3");
  metricNotePopoverTitle.textContent = linkEl.dataset.noteTitle || "计算说明";
  metricNotePopoverBody.textContent = linkEl.dataset.noteBody || "";
  metricNotePopover.style.visibility = "hidden";
  metricNotePopover.style.display = "block";
  metricNotePopover.setAttribute("aria-hidden", "false");

  const linkRect = linkEl.getBoundingClientRect();
  const popRect = metricNotePopover.getBoundingClientRect();
  const vw = window.innerWidth;
  const vh = window.innerHeight;

  let left = linkRect.right - popRect.width;
  left = Math.max(12, Math.min(vw - popRect.width - 12, left));

  let top = linkRect.top - popRect.height - 8;
  if (top < 12) {
    top = Math.min(vh - popRect.height - 12, linkRect.bottom + 8);
  }

  metricNotePopover.style.left = `${Math.round(left)}px`;
  metricNotePopover.style.top = `${Math.round(top)}px`;
  metricNotePopover.style.visibility = "visible";
}

detailView.addEventListener("pointerover", (e) => {
  const link = e.target.closest(".metric-help-link");
  if (!link || link === activeNoteLinkEl) return;
  showMetricNotePopover(link, false);
});

detailView.addEventListener("pointerout", (e) => {
  if (!activeNoteLinkEl || isMetricNotePinned) return;
  const link = e.target.closest(".metric-help-link");
  if (!link) return;
  if (e.relatedTarget && link.contains(e.relatedTarget)) return;
  hideMetricNotePopover();
});

// 支持点击（或触屏轻触）打开/固定/关闭说明浮窗
detailView.addEventListener("click", (e) => {
  const link = e.target.closest(".metric-help-link");
  if (!link) return;
  e.stopPropagation();
  if (activeNoteLinkEl === link && isMetricNotePinned) {
    hideMetricNotePopover();
  } else {
    showMetricNotePopover(link, true);
  }
});

// 支持键盘聚焦在说明小链接上按 Enter 或 Space 切换展开/关闭
detailView.addEventListener("keydown", (e) => {
  const link = e.target.closest(".metric-help-link");
  if (!link) return;
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    e.stopPropagation();
    if (activeNoteLinkEl === link && isMetricNotePinned) {
      hideMetricNotePopover();
    } else {
      showMetricNotePopover(link, true);
    }
  }
});

// 点击浮窗及链接外部区域自动收起
document.addEventListener("pointerdown", (e) => {
  if (!activeNoteLinkEl) return;
  if (e.target.closest(".metric-help-link, #metricNotePopover")) return;
  hideMetricNotePopover();
});

window.addEventListener("scroll", hideMetricNotePopover, true);
window.addEventListener("resize", hideMetricNotePopover);
window.addEventListener("blur", hideMetricNotePopover);

// 展开被折叠的强制线性回归模式卡片（仅移除 DOM 上的折叠类，不写入任何状态，故重绘后会重新折叠）
function revealCollapsedForceCard(linkEl) {
  const wrap = linkEl.closest(".force-collapse-wrap");
  if (!wrap) return;
  wrap.classList.remove("is-collapsed");
  // 提示层随之隐藏，焦点交还给卡片本身（若可聚焦），避免键盘焦点丢失
  const card = wrap.closest(".res-card");
  if (card && card.hasAttribute("tabindex")) card.focus({ preventScroll: true });
}

cardsGrid.addEventListener("click", (e) => {
  // 点按「仍要查看？请点按此处。」：只展开，不切换 ACF 来源
  const revealLink = e.target.closest(".force-reveal-link");
  if (revealLink) {
    revealCollapsedForceCard(revealLink);
    return;
  }
  if (e.target.closest("button, .metric-help-link")) return;
  const sel = window.getSelection ? window.getSelection().toString() : "";
  if (sel && sel.length > 0) return;
  const card = e.target.closest(".res-card[data-card-mode]");
  if (!card || !card.classList.contains("card-selectable")) return;
  const targetMode = card.dataset.cardMode;
  if (!targetMode || targetMode === state.acfSource) return;
  state.acfSource = targetMode;
  syncCardAcfSelection();
  syncAcfSourceButtons();
  drawAcfChart();
});

// 支持键盘聚焦在可选结果卡片上按 Enter 或 Space 切换对应图谱
cardsGrid.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" && e.key !== " ") return;
  // 键盘聚焦在展开超链接上时：Enter / Space 只展开，不切换 ACF 来源
  const revealLink = e.target.closest(".force-reveal-link");
  if (revealLink) {
    e.preventDefault();
    revealCollapsedForceCard(revealLink);
    return;
  }
  if (e.target.closest("button, .metric-help-link")) return;
  const card = e.target.closest(".res-card[data-card-mode]");
  if (!card || !card.classList.contains("card-selectable")) return;
  e.preventDefault();
  const targetMode = card.dataset.cardMode;
  if (!targetMode || targetMode === state.acfSource) return;
  state.acfSource = targetMode;
  syncCardAcfSelection();
  syncAcfSourceButtons();
  drawAcfChart();
});

document.querySelectorAll("#zoomGroup .seg-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#zoomGroup .seg-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    state.maxLag = parseInt(btn.dataset.maxlag, 10);
    drawAcfChart();
  });
});

// 点选上传（禁用拖拽导入，并全局拦截浏览器默认拖放打开音频页行为）
fileInput.addEventListener("click", (e) => e.stopPropagation());
dropzone.addEventListener("click", () => fileInput.click());
dropzone.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    fileInput.click();
  }
});
window.addEventListener("dragover", (e) => {
  if (e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files")) {
    e.preventDefault();
    e.dataTransfer.dropEffect = "none";
  }
});
window.addEventListener("drop", (e) => {
  if (e.dataTransfer && ((e.dataTransfer.files && e.dataTransfer.files.length > 0) || Array.from(e.dataTransfer.types || []).includes("Files"))) {
    e.preventDefault();
  }
});
fileInput.addEventListener("change", (e) => {
  if (e.target.files && e.target.files.length > 0) {
    handleNewFiles(Array.from(e.target.files));
    fileInput.value = "";
  }
});

let isQueueDraining = false;

function handleNewFiles(files) {
  if (!files || files.length === 0) return;
  const firstNewIdx = state.items.length;
  for (const f of files) {
    state.items.push({ file: f, name: f.name, status: "pending", data: null, error: null });
  }
  state.activeIndex = firstNewIdx;
  renderFileList(true);
  const activeLi = fileRowMap.get(state.items[firstNewIdx]);
  if (activeLi && activeLi.scrollIntoView) {
    activeLi.scrollIntoView({ block: "nearest" });
  }
  renderActiveDetail();
  drainAnalysisQueue();
}

async function drainAnalysisQueue() {
  if (isQueueDraining) return;
  isQueueDraining = true;
  try {
    while (true) {
      const nextItem = state.items.find(it => it.status === "pending");
      if (!nextItem) break;
      await analyzeSingleItem(nextItem);
    }
  } finally {
    isQueueDraining = false;
  }
}

async function analyzeSingleItem(item) {
  if (!item || !state.items.includes(item)) return;
  item.status = "running";
  renderFileList(false);
  if (state.items[state.activeIndex] === item) renderActiveDetail();

  const formData = new FormData();
  formData.append("file", item.file);
  appendSettingsToAnalyzeFormData(formData, state.settings);

  try {
    const resp = await fetch("/api/analyze", { method: "POST", body: formData });
    const json = await resp.json();
    // 若测算期间该曲目已被单删或清空，丢弃过期响应
    if (!state.items.includes(item)) return;
    if (!resp.ok || json.error) {
      throw new Error(json.error || `HTTP ${resp.status}`);
    }
    item.status = "done";
    item.data = json;
    item.error = null;
  } catch (err) {
    if (!state.items.includes(item)) return;
    item.status = "error";
    item.error = err.message || String(err);
  }
  renderFileList(false);
  if (state.items[state.activeIndex] === item) {
    renderActiveDetail();
  }
}

function removeQueueItem(item) {
  const idx = state.items.indexOf(item);
  if (idx === -1) return;
  const wasActive = (idx === state.activeIndex);
  state.items.splice(idx, 1);

  if (state.items.length === 0) {
    state.activeIndex = -1;
  } else if (wasActive) {
    state.activeIndex = Math.min(idx, state.items.length - 1);
  } else if (idx < state.activeIndex) {
    state.activeIndex -= 1;
  }

  renderFileList(wasActive);
  if (wasActive || state.items.length === 0) {
    renderActiveDetail();
  }
}

function pickPreferredBadgeResult(results) {
  if (!results) return null;
  const order = ["enhanced_v3", "exact", "force_v3"];
  for (const k of order) {
    if (results[k] && results[k].ok) return results[k];
  }
  return results.enhanced_v3 || results.exact || results.force_v3 || null;
}

function createFileRowElement(item) {
  const li = document.createElement("li");
  li.className = "file-item";

  const nameWrap = document.createElement("span");
  nameWrap.className = "file-name-wrap";
  nameWrap.title = item.name;

  const nameText = document.createElement("span");
  nameText.className = "file-name-text";
  nameText.textContent = item.name;
  nameWrap.appendChild(nameText);

  const rightBox = document.createElement("div");
  rightBox.className = "file-item-right";

  const badge = document.createElement("span");
  badge.className = "file-badge";

  const removeBtn = document.createElement("button");
  removeBtn.type = "button";
  removeBtn.className = "file-remove-btn";
  removeBtn.title = "移除此曲目";
  removeBtn.setAttribute("aria-label", "移除此曲目");
  removeBtn.textContent = "✕";
  removeBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    removeQueueItem(item);
  });

  rightBox.appendChild(badge);
  rightBox.appendChild(removeBtn);

  li.appendChild(nameWrap);
  li.appendChild(rightBox);

  li.addEventListener("click", () => {
    const curIdx = state.items.indexOf(item);
    if (curIdx === -1 || curIdx === state.activeIndex) return;
    state.activeIndex = curIdx;
    renderFileList(true);
    renderActiveDetail();
  });

  return li;
}

function updateFileRowElement(li, item, isActive) {
  const wasActive = li.classList.contains("active");
  li.classList.toggle("active", isActive);
  if (wasActive && !isActive) {
    const textEl = li.querySelector(".file-name-text");
    if (textEl) textEl.style.transform = "";
  }

  const badge = li.querySelector(".file-badge");
  badge.className = "file-badge";
  if (item.status === "pending" || item.status === "running") {
    badge.classList.add("pending");
    badge.textContent = item.status === "running" ? "测算中..." : "排队中";
  } else if (item.status === "error") {
    badge.classList.add("err");
    badge.textContent = "错误";
  } else if (item.data && item.data.results) {
    const r = pickPreferredBadgeResult(item.data.results);
    if (r && r.ok) {
      badge.textContent = `${r.bpm} BPM`;
    } else {
      badge.classList.add("err");
      badge.textContent = "无法计算";
    }
  }
}

function renderFileList(forceRestartMarquee = false) {
  queueCountEl.textContent = state.items.length;
  clearQueueBtn.disabled = (state.items.length === 0);

  const desiredRows = state.items.map((item, idx) => {
    let li = fileRowMap.get(item);
    if (!li) {
      li = createFileRowElement(item);
      fileRowMap.set(item, li);
    }
    updateFileRowElement(li, item, idx === state.activeIndex);
    return li;
  });

  // 增量同步子节点列表，避免销毁重建正在滚字的激活节点
  let cursor = fileListEl.firstChild;
  for (const row of desiredRows) {
    if (row === cursor) {
      cursor = cursor.nextSibling;
    } else {
      fileListEl.insertBefore(row, cursor);
    }
  }
  while (cursor) {
    const next = cursor.nextSibling;
    fileListEl.removeChild(cursor);
    cursor = next;
  }

  syncActiveMarquee(forceRestartMarquee);
}

function copyText(txt, btnEl, ev) {
  if (ev) ev.stopPropagation();
  const showOk = () => {
    if (!btnEl.dataset.origText) {
      btnEl.dataset.origText = btnEl.textContent;
    }
    if (btnEl._copyTimer) {
      clearTimeout(btnEl._copyTimer);
    }
    btnEl.textContent = "已复制 ✓";
    btnEl._copyTimer = setTimeout(() => {
      btnEl.textContent = btnEl.dataset.origText;
      btnEl._copyTimer = null;
    }, 1200);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(txt).then(showOk).catch(() => fallbackCopy(txt, showOk));
  } else {
    fallbackCopy(txt, showOk);
  }
}

function fallbackCopy(txt, cb) {
  const ta = document.createElement("textarea");
  ta.value = txt;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); cb(); } catch (_) {}
  document.body.removeChild(ta);
}

function formatFloorMinSec(ms) {
  const val = Number(ms);
  if (!Number.isFinite(val) || val <= 0) return "0:00";
  const totalSec = Math.floor(val / 1000);
  const mins = Math.floor(totalSec / 60);
  const secs = totalSec % 60;
  return `${mins}:${String(secs).padStart(2, "0")}`;
}

function formatDurationPillText(sec) {
  const val = Number(sec);
  if (!Number.isFinite(val) || val <= 0) return "";
  const tenth = Math.round(val * 10) / 10;
  const totalSec = Math.floor(tenth + 1e-9);
  const mins = Math.floor(totalSec / 60);
  const secs = totalSec % 60;
  return `${mins}:${String(secs).padStart(2, "0")} (${tenth.toFixed(1)}s)`;
}

function isPow2RatioInt(n) {
  return Number.isInteger(n) && n >= 2 && (n & (n - 1)) === 0;
}

function buildSampleRatePillHtml(rawSr, procSr) {
  const r = Math.round(Number(rawSr) || 0);
  const p = Math.round(Number(procSr) || 0);
  if (p <= 0) return "";
  if (r <= 0 || r === p) {
    const amberCls = p >= 96000 ? " amber" : "";
    return `<span class="pill${amberCls}">${escapeHtml(p)} Hz</span>`;
  }

  const amberCls = r >= 96000 ? " amber" : "";
  let noteTitle = "采样率折算说明";
  let noteBody = "";
  if (r > p && r % p === 0 && isPow2RatioInt(r / p)) {
    const factor = r / p;
    noteBody = `原始音频采样率为 ${r} Hz，核心测算引擎仅原生支持 32000 / 44100 / 48000 Hz 三档标准采样率。解码阶段已按整数倍（÷${factor}）降采样折算至 ${p} Hz 后参与包络与 BPM 测算，无非整数插值失真。`;
  } else if (p > r && p % r === 0 && isPow2RatioInt(p / r)) {
    const factor = p / r;
    noteBody = `原始音频采样率为 ${r} Hz，核心测算引擎仅原生支持 32000 / 44100 / 48000 Hz 三档标准采样率。解码阶段已按整数倍（×${factor}）升采样折算至 ${p} Hz 后参与包络与 BPM 测算。`;
  } else if (r === 32000 || r === 44100 || r === 48000) {
    noteTitle = "采样率重采样说明";
    noteBody = `原始音频采样率为 ${r} Hz，本次已按指定目标采样率重采样至 ${p} Hz 后参与包络与 BPM 测算。`;
  } else {
    noteTitle = "采样率重采样说明";
    noteBody = `原始音频采样率为 ${r} Hz，不在核心测算引擎原生支持的 32000 / 44100 / 48000 Hz 整数倍频范围内。解码阶段已重采样至最接近的支持档位 ${p} Hz 后参与包络与 BPM 测算。`;
  }

  return `<span class="pill metric-help-link sr-resample-pill${amberCls}" tabindex="0" role="button" aria-haspopup="dialog" data-note-mode="" data-note-title="${escapeHtml(noteTitle)}" data-note-body="${escapeHtml(noteBody)}"><span class="sr-resample-text">${escapeHtml(r)}→${escapeHtml(p)} Hz</span></span>`;
}

function buildAudioInfoBarHtml(item) {
  if (!item || !item.data) return "";
  const results = item.data.results || {};
  const primaryRes = (
    results.enhanced_v3
    || results.exact
    || results.force_v3
    || results.enhanced_v2
    || results.enhanced
    || null
  );
  const rawMeta = item.data.audio_meta;
  const meta = (
    (rawMeta && typeof rawMeta === "object" && Object.keys(rawMeta).length > 0 ? rawMeta : null)
    || item.precheckMeta
    || {}
  );
  const displayName = item.name || item.data.filename || "未命名音频";

  const pills = [];
  const fmtStr = [meta.container_label, meta.codec_name].filter(Boolean).join(" · ");
  if (fmtStr) {
    pills.push(`<span class="pill">${escapeHtml(fmtStr)}</span>`);
  }

  const ch = Number(meta.channels || 0);
  const chLabel = (
    meta.channel_label
    || (ch === 1 ? "单声道" : (ch === 2 ? "双声道" : (ch > 2 ? `${ch} 声道` : "")))
  );
  if (chLabel) {
    pills.push(`<span class="pill${ch > 2 ? ' amber' : ''}">${escapeHtml(chLabel)}</span>`);
  }

  const bits = Number(meta.bits || 0);
  if (bits > 0) {
    pills.push(`<span class="pill">${escapeHtml(bits)}-bit</span>`);
  }

  const procSr = Number((primaryRes && primaryRes.sample_rate) || 0);
  const rawSr = Number((primaryRes && primaryRes.raw_sample_rate) || meta.raw_sr || procSr || 0);
  if (procSr > 0) {
    pills.push(buildSampleRatePillHtml(rawSr, procSr));
  }

  const durText = primaryRes ? formatDurationPillText(primaryRes.duration_sec) : "";
  if (durText) {
    pills.push(`<span class="pill">${escapeHtml(durText)}</span>`);
  }

  const silenceRes = (
    [results.enhanced_v3, results.force_v3, results.exact, primaryRes]
      .find(r => r && r.silence_trimmed)
    || primaryRes
  );
  if (silenceRes && silenceRes.silence_trimmed) {
    const silenceLabel = silenceRes.short_burst_trimmed ? "已去静音·含短振幅" : "已去静音";
    const headStr = Number(silenceRes.head_trim_sec ?? 0).toFixed(1);
    const tailStr = Number(silenceRes.tail_trim_sec ?? 0).toFixed(1);
    pills.push(`<span class="pill highlight">${silenceLabel} (头 ${headStr}s / 尾 ${tailStr}s)</span>`);
  }

  const usedChannel = String(item.data.channel || "auto").toLowerCase();
  const chStrategyLabel = (item.data.mode !== "exact") ? CHANNEL_PILL_LABELS[usedChannel] : "";
  if (chStrategyLabel) {
    pills.push(`<span class="pill highlight">${escapeHtml(chStrategyLabel)}</span>`);
  }

  return `
    <div class="audio-info-title-wrap">
      <span class="audio-info-title" title="${escapeHtml(displayName)}">${escapeHtml(displayName)}</span>
    </div>
    <div class="audio-info-pills">
      ${pills.join("")}
    </div>
  `;
}

function buildResultCardHtml(title, tagClass, res, isSelectable = false, collapsed = false) {
  if (!res) return "";
  const isOk = res.ok;
  let quantBadge = "";
  if (isOk) {
    if (res.mode === "enhanced_v3") {
      quantBadge = `<span class="pill">连续线性回归 (无7阶量化吸附)</span>`;
    } else if (res.mode === "force_v3") {
      quantBadge = `<span class="pill">全局梳状网格强制拟合</span>`;
    } else {
      quantBadge = res.is_quantized
        ? `<span class="pill highlight">谐波网格吸附 (原始 ${escapeHtml(res.folded_raw_bpm || res.raw_bpm)})</span>`
        : `<span class="pill">原始线性回归 (未触发吸附)</span>`;
    }
  }

  const foldBadge = (isOk && Math.abs(res.fold_scale - 1.0) > 1e-4)
    ? `<span class="pill fold">倍频折叠 ×${escapeHtml(res.fold_scale)} (基底 ${escapeHtml(res.raw_bpm)} BPM)</span>`
    : "";

  const recalcBadge = (isOk && res.bpm_recalc_applied && res.pass1_bpm)
    ? `<span class="pill highlight">二次精修 (初测 ${escapeHtml(res.pass1_bpm)} BPM)</span>`
    : "";

  const showTripleAlt = (
    isOk
    && res.mode === "enhanced_v3"
    && !!res.suspected_triple_meter
    && !!state.settings.optimizeTripleMeter
  );

  let tripleAltHtml = "";
  let tripleBadge = "";
  if (showTripleAlt) {
    const bpmMul = (res.bpm_num * 1.5).toFixed(3);
    const bpmDiv = (res.bpm_num / 1.5).toFixed(3);
    tripleBadge = `<span class="pill amber">疑似三拍子 / 3:2 消歧触发</span>`;
    tripleAltHtml = `
      <div class="triple-alt-box">
        <span class="triple-alt-title">疑似三拍子 / 3:2 三连音消歧备选 BPM（原始未折叠）</span>
        <div class="triple-alt-row">
          <span class="triple-alt-item">
            <span class="alt-k">×1.5</span>
            <span class="alt-v">${bpmMul}</span>
            <button class="copy-btn" style="padding:2px 6px;font-size:11px;" onclick="copyText('${bpmMul}', this, event)">复制</button>
          </span>
          <span class="triple-alt-item">
            <span class="alt-k">÷1.5</span>
            <span class="alt-v">${bpmDiv}</span>
            <button class="copy-btn" style="padding:2px 6px;font-size:11px;" onclick="copyText('${bpmDiv}', this, event)">复制</button>
          </span>
        </div>
      </div>
    `;
  }

  const cardCls = [
    res.mode === "enhanced_v3" ? "enhanced-card" : (res.mode === "force_v3" ? "force-card" : ""),
    isSelectable ? "card-selectable" : "",
  ].filter(Boolean).join(" ");

  const isSilenceTrimmed = !!res.silence_trimmed;
  const ratioMetricTitle = isSilenceTrimmed ? "节拍匹配与占有效时长比例" : "节拍匹配与占总时长比例";
  const ratioBaseLabel = isSilenceTrimmed ? "占有效时长" : "占总时长";
  let beatSummaryText = "-";
  if (isOk) {
    const matchedSecStr = Number(res.matched_duration_sec ?? 0).toFixed(1);
    const ratioPctStr = Number(res.duration_ratio_pct ?? 0).toFixed(1);
    beatSummaryText = `匹配 ${res.n_matched} 拍（约 ${matchedSecStr} 秒），${ratioBaseLabel} ${ratioPctStr}%`;
  } else if (res.mode === "exact" && res.n_matched > 0) {
    beatSummaryText = `仅连续匹配 ${res.n_matched} 拍（未达 4 拍起算门槛）`;
  }

  const effTol = res.eff_comb_tol ?? 35;
  const trimOn = res.trimming_active !== false;
  const recalcDone = !!(res.bpm_recalc_applied && res.pass1_bpm);

  const overlapNoteTitle = `${title} · 为什么节拍匹配占比与变速占比相加可能超过 100%？`;
  const overlapNoteBody = `上方「节拍匹配占比」来自后端追踪器在允许容差窗（±${effTol} ms）内的峰值命中统计：在鼓点或十六分音符较密的变速段落中，现有追踪可能放行部分偶然落入网格容差窗内的瞬态起拍，使匹配占比偏高；下方「变速区域」则由 UI 基于局部复谐波相位锁定度独立识别，因此两者折算占比相加可能超过 100%。由于主干 BPM 由长基线自相关多阶线性回归主导（配合分段精修），放行此类局部变速起拍一般不会影响主干 BPM 的计算精度。`;

  let trackSubHtml = "-";
  let trackSubWarn = false;
  if (isOk) {
    if (res.mode === "exact") {
      if (res.exact_early_break) {
        const breakLagStr = formatFloorMinSec(res.exact_break_lag_ms);
        trackSubHtml = escapeHtml(`于自相关开始（一般为稳速开始位置，非音频第0秒） ${breakLagStr} 处提前跳出主干追踪（受变速、切分或留白影响）`);
        trackSubWarn = true;
      } else {
        trackSubHtml = escapeHtml("自相关追踪已跑满窗口上限");
      }
    } else if (res.mode === "enhanced_v3" || res.mode === "force_v3") {
      if (!trimOn) {
        trackSubHtml = escapeHtml("变速区域：未开启检测");
      } else if (Array.isArray(res.transition_spans) && res.transition_spans.length > 0) {
        const spanParts = res.transition_spans.map(pair => {
          const sStr = formatFloorMinSec(pair[0]);
          const eStr = formatFloorMinSec(pair[1]);
          return `${sStr}-${eStr}`;
        });
        const baseSpanText = `变速区域：${spanParts.join("、")}（仅供参考。可能存在漏算、错算或多算的情况。需要配合手动二次确认。）`;
        trackSubHtml = `${escapeHtml(baseSpanText)}<span class="metric-help-link" tabindex="0" role="button" aria-haspopup="dialog" style="margin-left:6px; white-space:nowrap;" data-note-mode="${escapeHtml(res.mode)}" data-note-title="${escapeHtml(overlapNoteTitle)}" data-note-body="${escapeHtml(overlapNoteBody)}">为何与上方占比相加可超100%？</span>`;
        trackSubWarn = true;
      } else {
        trackSubHtml = escapeHtml("变速区域：无（或未检测出）");
      }
    }
  }

  // 未匹配主干网格区段（2026-11 新增）：与上方「节拍匹配占比」同源的逐拍命中序列生成，
  // 显示的匹配% + 未匹配合计% 严格等于 100%；增强 / 强制模式始终显示，Malody 模式不显示。
  let unmatchedHtml = "";
  const ug = res.unmatched_grid;
  if (isOk && (res.mode === "enhanced_v3" || res.mode === "force_v3") && ug && typeof ug === "object") {
    const fmtPct = v => Number(v ?? 0).toFixed(1);
    const ugTotal = Number(ug.total_pct ?? 0);
    let ugText;
    if (ugTotal <= 0) {
      ugText = "未匹配主干网格：无（全程匹配）";
    } else if (!ug.available) {
      ugText = `未匹配主干网格：合计 ${fmtPct(ugTotal)}%（逐拍序列不可用，无法列出区段）`;
    } else if (Array.isArray(ug.spans) && ug.spans.length > 0) {
      const ugParts = ug.spans.map(p => `${formatFloorMinSec(p[0])}-${formatFloorMinSec(p[1])}`);
      ugText = `未匹配主干网格：${ugParts.join("、")}；合计 ${fmtPct(ugTotal)}%（列出区段 ${fmtPct(ug.listed_pct)}%，零散丢拍 ${fmtPct(ug.scattered_pct)}%）`;
    } else {
      ugText = `未匹配主干网格：无连续 ≥3 秒区段；合计 ${fmtPct(ugTotal)}%（均为零散丢拍）`;
    }
    const ugNoteTitle = `${title} · 未匹配主干网格区段说明`;
    const ugNoteBody = `本行与上方「节拍匹配占比」完全同源：后端按最终 BPM 铺设节拍网格，每个落在允许偏差（±${effTol} ms）内且计入统计的命中拍占据一个节拍时长；有效时长中未被命中拍覆盖的部分即为「未匹配」，因此「匹配占比 + 未匹配合计」恒等于 100%。连续未匹配（中间夹杂短于 1 秒的零星命中也视为连续）满 3 秒的列为区段，其余计入零散丢拍。未匹配的常见成因包括：真实变速、无鼓点留白或间奏、弱起音/铺底乐器、切分或反拍、真人演奏速度漂移，以及开启变速裁剪后被剔除的过渡段。与下方「变速区域」的区别：「变速区域」由 UI 基于局部复谐波相位锁定度独立识别，只提示明显偏离主干速度的段落；本行则如实列出所有未对上主干网格的时间，二者可互相印证，但并非一一对应。`;
    const ugWarn = ugTotal >= 10.0;
    unmatchedHtml = `<span class="metric-sub-status${ugWarn ? ' is-warn' : ''}">${escapeHtml(ugText)}<span class="metric-help-link" tabindex="0" role="button" aria-haspopup="dialog" style="margin-left:6px; white-space:nowrap;" data-note-mode="${escapeHtml(res.mode)}" data-note-title="${escapeHtml(ugNoteTitle)}" data-note-body="${escapeHtml(ugNoteBody)}">这是什么？</span></span>`;
  }

  const overlapNoteTail = "此外，在高密度变速区域内，现有追踪容差窗可能放行偶然撞网的局部起拍，因此本项匹配占比与下方 UI 变速区域占比相加可能超过 100%，但主干 BPM 由长基线自相关多阶回归主导，一般不受此类局部起拍放行影响。";

  let beatNoteBody = "";
  if (res.mode === "exact") {
    beatNoteBody = "原版不在音频时间轴上逐拍比对鼓点，而是在自相关（ACF）曲线上从第 1 拍延迟起按整数倍递推寻峰。由于原版为抑制尾部高方差伪峰，将有效 ACF 窗口截断为音频总长的一半（超 699 秒曲目还会先截断输入），此占比的理论上限最高仅为 50%；同时追踪要求峰值偏差 ≤ ±8 ms，一旦中途遇到单次弱拍或切分音失配便立即终止循环、不再往后统计，因此实际占比往往更低。";
  } else if (res.mode === "enhanced_v3") {
    if (trimOn) {
      const subTrunkSec = res.sub_trunk_min_sec ?? state.settings.subTrunkMinSec ?? 15;
      const recalcTail = recalcDone
        ? `本次已触发二次精修：在剔除变速段或局部失真污染后，基于满 16.5 秒主净段（及达标的满 ${subTrunkSec} 秒副净段）独立自相关与后置去偏回归执行 2^x 同源精修，将初测 ${res.pass1_bpm} BPM 净化为当前值并锁定初测主八度。`
        : (res.trim_recalc
          ? `当存在有效变速或局部失真污染时，会基于满 16.5 秒主净段（及达标的满 ${subTrunkSec} 秒副净段）独立自相关与后置去偏回归自动执行 2^x 同源二次精修并锁定初测主八度（全程常速或未达精修条件时自动保留初测稳健解）。`
          : "");
      beatNoteBody = `按最终 BPM 在全曲真实起音包络上铺设节拍网格，统计落在允许偏差（±${effTol} ms）内的真实起音波峰拍数并折算时长（理论上限 100%）。已按当前设置自动剔除前奏/尾奏无鼓点留白与中途真变速过渡段（及交界缓冲），同时对半速 Half-time、≥80 BPM 每小节一击、≤150 BPM 十六分切分等 2^x 同源疏密变化予以保留不断锁。${recalcTail}${overlapNoteTail}`;
    } else {
      beatNoteBody = `按最终 BPM 在全曲真实起音包络上铺设节拍网格，统计全曲从头到尾落在允许偏差（±${effTol} ms）内的真实起音波峰拍数并折算时长（当前已关闭变速裁剪与分段重锁，理论上限 100%）。`;
    }
  } else if (res.mode === "force_v3") {
    if (trimOn) {
      const forceRecalcTail = recalcDone
        ? `本次已基于满 8 秒净稳定段完成二次重算（初测 ${res.pass1_bpm} BPM，2^x 同源锁定初测主八度）。`
        : (res.trim_recalc
          ? "当剔除变速段后的最长净稳定段满 8 秒时，会自动重算主干 BPM 并在 2^x 同源时锁定初测主八度。"
          : "");
      beatNoteBody = `以全局梳状网格强行拟合的主干节奏在全曲真实起音包络上逐拍探测（允许偏差 ±${effTol} ms），自动保留 0.25x~4x 同源编曲疏密段落并剔除前奏/尾奏留白与中途真变速段后折算有效稳态时长（理论上限 100%）；若真人演奏曲目大段速度漂移脱网，有效占比会相应偏低。${forceRecalcTail}${overlapNoteTail}`;
    } else {
      beatNoteBody = `以全局梳状网格强行拟合的主干节奏在全曲真实起音包络上逐拍探测（允许偏差 ±${effTol} ms），直接统计全曲落在网格容差内的总拍数并折算时长（当前已关闭变速裁剪与分段重锁，理论上限 100%）。`;
    }
  }

  const selectableAttrs = isSelectable
    ? ` tabindex="0" role="button" aria-pressed="${res.mode === state.acfSource ? 'true' : 'false'}" title="点按或按回车切换下方 ACF 简图至此模式"`
    : "";

  // 卡片正文（BPM、指标、诊断胶囊）；折叠时整体包入 .force-collapse-wrap，未折叠时原样输出
  const bodyHtml = `
      <div class="bpm-hero">
        <span class="bpm-val ${isOk ? '' : 'fail'}">${isOk ? escapeHtml(res.bpm) : '无法自动计算'}</span>
        ${isOk ? `<span style="font-size:14px; color:var(--muted);">BPM</span>
                  <button class="copy-btn" onclick="copyText('${escapeHtml(res.bpm)}', this, event)">复制 BPM</button>` : ''}
      </div>
      ${tripleAltHtml}
      <div class="metrics-grid">
        <div class="metric-box">
          <span class="metric-label">不稳定度</span>
          <span class="metric-val">${isOk ? escapeHtml(res.instability) : '-'}</span>
        </div>
        <div class="metric-box">
          <span class="metric-label">胜出周期 τ</span>
          <span class="metric-val">${res.best_tau > 0 ? escapeHtml(res.best_tau) + ' ms' : '-'}</span>
        </div>
        <div class="metric-box metric-span-full">
          <div class="metric-head-row">
            <span class="metric-label">${ratioMetricTitle}</span>
            ${beatNoteBody ? `<span class="metric-help-link" tabindex="0" role="button" aria-haspopup="dialog" data-note-mode="${escapeHtml(res.mode)}" data-note-title="${escapeHtml(title + ' · 计算口径说明')}" data-note-body="${escapeHtml(beatNoteBody)}">查看计算说明</span>` : ""}
          </div>
          <span class="metric-val">${escapeHtml(beatSummaryText)}</span>
          ${unmatchedHtml}
          <span class="metric-sub-status${trackSubWarn ? ' is-warn' : ''}">${trackSubHtml}</span>
        </div>
      </div>
      <div class="diag-row">
        <span class="pill">${escapeHtml(res.stage_label)}</span>
        ${quantBadge}
        ${foldBadge}
        ${recalcBadge}
        ${tripleBadge}
      </div>`;

  // 「全部」模式下强制线性回归模式默认折叠：保留标题栏，正文隐藏（尺寸不变）并居中覆盖提示与展开超链接
  const wrappedBodyHtml = collapsed
    ? `
      <div class="force-collapse-wrap is-collapsed">
        <div class="force-collapse-body">${bodyHtml}
        </div>
        <div class="force-collapse-overlay">
          <div>此模式计算结果只建议在前两个模式均计算失败的情况下参考。<span class="force-reveal-link" tabindex="0" role="button" title="展开显示强制线性回归模式的计算结果">仍要查看？请点按此处。</span></div>
        </div>
      </div>`
    : bodyHtml;

  return `
    <div class="res-card ${cardCls}" data-card-mode="${escapeHtml(res.mode)}"${selectableAttrs}>
      <div class="res-top">
        <span style="display:inline-flex; align-items:center; gap:6px;">
          <span class="mode-tag ${tagClass}">${escapeHtml(title)}</span>
          <span class="acf-viewing-tag">当前图谱</span>
        </span>
        <span style="font-size:12px; color:var(--muted);">耗时 ${escapeHtml(res.elapsed_seconds)}s</span>
      </div>${wrappedBodyHtml}
    </div>
  `;
}

function renderActiveDetail() {
  hideMetricNotePopover();
  if (state.activeIndex < 0 || state.activeIndex >= state.items.length) {
    emptyState.style.display = "block";
    detailView.style.display = "none";
    chartTooltip.style.display = "none";
    return;
  }
  const item = state.items[state.activeIndex];
  emptyState.style.display = "none";
  detailView.style.display = "flex";

  if (item.status === "pending" || item.status === "running") {
    if (audioInfoBar) audioInfoBar.style.display = "none";
    chartCard.style.display = "none";
    chartTooltip.style.display = "none";
    const statusVerb = item.status === "running" ? "正在测算" : "排队等待测算";
    cardsGrid.innerHTML = `<div class="res-card"><p style="color:var(--muted);">${statusVerb} ${escapeHtml(item.name)} ...</p></div>`;
    return;
  }
  if (item.status === "error") {
    if (audioInfoBar) audioInfoBar.style.display = "none";
    chartCard.style.display = "none";
    chartTooltip.style.display = "none";
    cardsGrid.innerHTML = `<div class="res-card"><p style="color:var(--red);">测算失败 (${escapeHtml(item.name)}): ${escapeHtml(item.error)}</p></div>`;
    return;
  }

  if (audioInfoBar) {
    audioInfoBar.innerHTML = buildAudioInfoBarHtml(item);
    audioInfoBar.style.display = "flex";
  }
  chartCard.style.display = "flex";
  const results = item.data.results || {};
  const modeCount = ["exact", "enhanced_v3", "force_v3"].filter(k => !!results[k]).length;
  const isSelectable = modeCount > 1;

  // 若当前选中的 acfSource 在单模式下不存在，自动切换到存在的模式
  if (!results[state.acfSource]) {
    if (results.enhanced_v3) state.acfSource = "enhanced_v3";
    else if (results.exact) state.acfSource = "exact";
    else if (results.force_v3) state.acfSource = "force_v3";
  }

  let html = "";
  // 按设置文档顺序排列：Malody模式 → 增强模式 → 强制线性回归模式
  if (results.exact) {
    html += buildResultCardHtml("Malody模式", "", results.exact, isSelectable);
  }
  if (results.enhanced_v3) {
    html += buildResultCardHtml("增强模式", "enh", results.enhanced_v3, isSelectable);
  }
  if (results.force_v3) {
    // 「全部」模式下，只要 Malody 或增强模式至少一个计算成功（ok=true），强制线性回归模式卡片默认折叠；
    // 两者均「无法自动计算」时直接展开。展开状态不记忆，每次重绘重新折叠。
    const collapseForce = (
      item.data.mode === "all"
      && !!((results.exact && results.exact.ok) || (results.enhanced_v3 && results.enhanced_v3.ok))
    );
    html += buildResultCardHtml("强制线性回归模式", "force", results.force_v3, isSelectable, collapseForce);
  }
  cardsGrid.innerHTML = html;

  syncCardAcfSelection();
  syncAcfSourceButtons();
  drawAcfChart();
}

function getActiveModeResult() {
  if (state.activeIndex < 0 || state.activeIndex >= state.items.length) return null;
  const item = state.items[state.activeIndex];
  if (!item || !item.data || !item.data.results) return null;
  return item.data.results[state.acfSource] || item.data.results.enhanced_v3 || item.data.results.exact || item.data.results.force_v3;
}

function formatAcfAxisVal(val) {
  if (!Number.isFinite(val) || Math.abs(val) < 1e-12) return "0";
  const abs = Math.abs(val);
  const sign = val < 0 ? "-" : "";
  if (abs >= 1e6) return sign + (abs / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
  if (abs >= 1e3) return sign + (abs / 1e3).toFixed(1).replace(/\.0$/, "") + "k";
  if (abs >= 100) return String(Math.round(val));
  if (abs >= 10) return val.toFixed(1).replace(/\.0$/, "");
  if (abs >= 1) return val.toFixed(2).replace(/(\.\d*?[1-9])0+$|\.0+$/, "$1");
  if (abs >= 0.01) return val.toFixed(3).replace(/(\.\d*?[1-9])0+$|\.0+$/, "$1");
  return val.toExponential(1);
}

function pickAcfYStep(posMax) {
  const rawStep = posMax / 2.4;
  const exp = Math.floor(Math.log10(rawStep));
  const base = Math.pow(10, exp);
  const norm = rawStep / base;
  if (norm <= 1.5) return 1 * base;
  if (norm <= 3.6) return 2 * base;
  if (norm <= 7.5) return 5 * base;
  return 10 * base;
}

function drawAcfChart(hoverLag = null) {
  const res = getActiveModeResult();
  const rect = acfCanvas.parentElement.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  acfCanvas.width = rect.width * dpr;
  acfCanvas.height = rect.height * dpr;
  const ctx = acfCanvas.getContext("2d");
  ctx.scale(dpr, dpr);

  const W = rect.width;
  const H = rect.height;
  ctx.clearRect(0, 0, W, H);

  if (!res || !res.acf_curve || res.acf_curve.length === 0) return;

  const acf = res.acf_curve;
  const maxLag = Math.min(state.maxLag, acf.length);
  const padL = 52, padR = 20, padT = 24, padB = 28;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  if (plotW <= 0 || plotH <= 0) return;

  let minV = 0, rawMaxV = 1e-6;
  for (let i = 16; i < maxLag; i++) {
    if (acf[i] > rawMaxV) rawMaxV = acf[i];
    if (acf[i] < minV) minV = acf[i];
  }
  minV = Math.min(minV, -rawMaxV * 0.12);
  const maxV = rawMaxV * 1.12;

  const xOf = (lag) => padL + (lag / maxLag) * plotW;
  const yOf = (val) => padT + (1 - (val - minV) / (maxV - minV)) * plotH;

  ctx.font = '11px "Helvetica Neue", Arial, "PingFang SC", "Microsoft YaHei", sans-serif';

  // Y 轴辅助网格线、零线与左栏刻度
  const zeroY = yOf(0);
  const yTicks = [0];
  if (rawMaxV > 1e-5) {
    const yStep = pickAcfYStep(rawMaxV);
    for (let v = yStep; v <= maxV * 0.995; v += yStep) {
      yTicks.push(v);
    }
    for (let v = -yStep; v >= minV * 0.995; v -= yStep) {
      yTicks.push(v);
    }
  }

  ctx.save();
  ctx.textAlign = "right";
  ctx.fillStyle = "#8b949e";
  ctx.lineWidth = 1;
  for (const v of yTicks) {
    const ty = yOf(v);
    if (ty < padT - 1 || ty > H - padB + 1) continue;
    const isZero = Math.abs(v) < 1e-12;
    if (!isZero && Math.abs(ty - zeroY) < 14) continue;
    ctx.strokeStyle = isZero ? "rgba(48, 48, 48, 0.78)" : "rgba(48, 48, 48, 0.36)";
    ctx.beginPath();
    ctx.moveTo(padL, ty);
    ctx.lineTo(W - padR, ty);
    ctx.stroke();
    if (ty >= padT + 4 && ty <= H - padB - 4) {
      ctx.fillText(formatAcfAxisVal(v), padL - 6, ty + 3.5);
    }
  }
  ctx.restore();

  // X 轴网格与防越界刻度
  ctx.fillStyle = "#8b949e";
  const stepLag = maxLag <= 1000 ? 200 : (maxLag <= 2000 ? 400 : 500);
  for (let lag = 0; lag <= maxLag; lag += stepLag) {
    const x = xOf(lag);
    ctx.strokeStyle = "rgba(48, 48, 48, 0.42)";
    ctx.beginPath();
    ctx.moveTo(x, padT);
    ctx.lineTo(x, H - padB);
    ctx.stroke();
    const xLabel = `${lag}ms`;
    const xLabelW = ctx.measureText(xLabel).width;
    const xTextPos = Math.min(W - 4 - xLabelW, Math.max(padL, x - xLabelW / 2));
    ctx.fillText(xLabel, xTextPos, H - 8);
  }

  // 1. 绘制胜出周期 best_tau 及其整数倍谐波梳状线 (k * best_tau)
  if (res.best_tau > 0) {
    ctx.save();
    for (let k = 1; k * res.best_tau <= maxLag; k++) {
      const hLag = k * res.best_tau;
      const hx = xOf(hLag);
      ctx.strokeStyle = k === 1 ? "rgba(63, 185, 80, 0.9)" : "rgba(63, 185, 80, 0.35)";
      ctx.lineWidth = k === 1 ? 2 : 1;
      ctx.setLineDash(k === 1 ? [] : [4, 4]);
      ctx.beginPath();
      ctx.moveTo(hx, padT);
      ctx.lineTo(hx, H - padB);
      ctx.stroke();
      if (k === 1) {
        ctx.fillStyle = "#3fb950";
        const tauLabel = `τ=${res.best_tau}ms`;
        const tauLabelW = ctx.measureText(tauLabel).width;
        const tauTextX = Math.max(padL + 2, Math.min(hx + 4, W - 6 - tauLabelW));
        ctx.fillText(tauLabel, tauTextX, padT + 12);
      }
    }
    ctx.restore();
  }

  // 2. 若发生倍频折叠，绘制最终 BPM 对应的周期线 final_tau
  if (res.final_tau > 0 && Math.abs(res.final_tau - res.best_tau) > 1.0 && res.final_tau <= maxLag) {
    const fx = xOf(res.final_tau);
    ctx.save();
    ctx.strokeStyle = "rgba(188, 140, 255, 0.9)";
    ctx.lineWidth = 2;
    ctx.setLineDash([6, 3]);
    ctx.beginPath();
    ctx.moveTo(fx, padT);
    ctx.lineTo(fx, H - padB);
    ctx.stroke();
    ctx.fillStyle = "#bc8cff";
    const finalLabel = `终值τ=${res.final_tau}ms (${res.bpm} BPM)`;
    const finalLabelW = ctx.measureText(finalLabel).width;
    const finalTextX = Math.max(padL + 2, Math.min(fx + 4, W - 6 - finalLabelW));
    ctx.fillText(finalLabel, finalTextX, padT + 26);
    ctx.restore();
  }

  // 3. 绘制 ACF 曲线
  ctx.strokeStyle = "#58a6ff";
  ctx.lineWidth = 1.4;
  ctx.beginPath();
  for (let lag = 0; lag < maxLag; lag++) {
    const x = xOf(lag);
    const y = yOf(acf[lag]);
    if (lag === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.stroke();

  // 4. 绘制 15ms 局部极大值峰 (peaks)
  ctx.fillStyle = "rgba(139, 148, 158, 0.6)";
  for (const p of (res.peaks || [])) {
    if (p.lag > maxLag) break;
    ctx.beginPath();
    ctx.arc(xOf(p.lag), yOf(p.val), 2.2, 0, Math.PI * 2);
    ctx.fill();
  }

  // 5. 绘制阶段 1 入选候选周期 (candidates)
  ctx.fillStyle = "#d29922";
  for (const c of (res.candidates || [])) {
    if (c.tau > maxLag) continue;
    const idx = Math.min(acf.length - 1, Math.max(0, Math.round(c.tau)));
    const cx = xOf(c.tau);
    const cy = yOf(acf[idx]);
    ctx.beginPath();
    ctx.arc(cx, cy, 4.5, 0, Math.PI * 2);
    ctx.fill();
  }

  // 6. 鼠标悬停十字准星
  if (hoverLag !== null && hoverLag >= 1 && hoverLag < maxLag) {
    const hx = xOf(hoverLag);
    const hy = yOf(acf[hoverLag]);
    ctx.save();
    ctx.strokeStyle = "rgba(255, 255, 255, 0.45)";
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(hx, padT);
    ctx.lineTo(hx, H - padB);
    ctx.stroke();
    ctx.fillStyle = "#FFFFFF";
    ctx.beginPath();
    ctx.arc(hx, hy, 4, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
  }
}

acfCanvas.addEventListener("mousemove", (e) => {
  const res = getActiveModeResult();
  if (!res || !res.acf_curve) return;
  const rect = acfCanvas.getBoundingClientRect();
  const padL = 52, padR = 20;
  const plotW = rect.width - padL - padR;
  if (plotW <= 0) return;
  const maxLag = Math.min(state.maxLag, res.acf_curve.length);
  const relX = e.clientX - rect.left - padL;
  if (relX < 0 || relX > plotW) {
    chartTooltip.style.display = "none";
    drawAcfChart(null);
    return;
  }
  const lag = Math.max(1, Math.min(maxLag - 1, Math.round((relX / plotW) * maxLag)));
  const bpmEquiv = (60000.0 / lag).toFixed(2);
  const val = res.acf_curve[lag].toFixed(4);

  chartTooltip.style.display = "block";
  chartTooltip.innerHTML = `Lag: <b>${lag} ms</b> (${bpmEquiv} BPM)<br>ACF: <b>${val}</b>`;
  const cursorX = padL + (lag / maxLag) * plotW;
  const tipW = chartTooltip.offsetWidth || 180;
  let leftPos = cursorX + 14;
  if (leftPos + tipW > rect.width - 8) {
    leftPos = Math.max(8, cursorX - tipW - 14);
  }
  chartTooltip.style.left = `${Math.round(leftPos)}px`;
  chartTooltip.style.top = "12px";
  drawAcfChart(lag);
});

acfCanvas.addEventListener("mouseleave", () => {
  chartTooltip.style.display = "none";
  drawAcfChart(null);
});

window.addEventListener("resize", () => {
  syncActiveMarquee(false);
  drawAcfChart();
});

(function initStartupZoomHud() {
  const hud = document.getElementById("startupZoomHud");
  if (!hud) return;
  let dismissed = false;
  let timerId = null;

  function dismissHud() {
    if (dismissed) return;
    dismissed = true;
    if (timerId !== null) {
      clearTimeout(timerId);
      timerId = null;
    }
    window.removeEventListener("mousedown", dismissHud, true);
    window.removeEventListener("wheel", dismissHud, true);
    hud.classList.add("is-hidden");
    hud.setAttribute("aria-hidden", "true");
    setTimeout(() => {
      if (hud.parentNode) {
        hud.style.display = "none";
      }
    }, 240);
  }

  timerId = setTimeout(dismissHud, 1500);
  window.addEventListener("mousedown", dismissHud, { capture: true, passive: true });
  window.addEventListener("wheel", dismissHud, { capture: true, passive: true });
})();
</script>
</body>
</html>
"""

INDEX_HTML = inject_settings_ui(_BASE_INDEX_HTML)

# 兼容重导出基础表单解析辅助函数（供 web_gui_precheck 等外部调用方复用）
_parse_form_bool = parse_form_bool
_parse_form_int = parse_form_int
_parse_form_float = parse_form_float


_RUNTIME_TEMP_PREFIXES = ("malody_bpm_", "malody_pre_", "malody_off_")


def purge_runtime_caches() -> None:
    """清理运行时音频临时缓存（含预检会话池与系统临时目录残留）及项目 __pycache__ 目录。"""
    sys.dont_write_bytecode = True

    try:
        from web_gui_precheck import _SESSION_STORE

        _SESSION_STORE.clear_all()
    except Exception:
        pass

    try:
        tmp_dir = tempfile.gettempdir()
        with os.scandir(tmp_dir) as entries:
            for entry in entries:
                if entry.name.startswith(_RUNTIME_TEMP_PREFIXES):
                    try:
                        if entry.is_file(follow_symlinks=False):
                            os.remove(entry.path)
                    except OSError:
                        pass
    except Exception:
        pass

    for cache_dir in (
        os.path.join(PROJECT_ROOT, "__pycache__"),
        os.path.join(PROJECT_ROOT, "malody_bpm", "__pycache__"),
    ):
        if os.path.isdir(cache_dir):
            try:
                shutil.rmtree(cache_dir, ignore_errors=True)
            except Exception:
                pass


atexit.register(purge_runtime_caches)


def _is_port_occupied(host: str, port: int) -> bool:
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "") else ("::1" if host == "::" else host)
    family = socket.AF_INET6 if ":" in probe_host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.settimeout(0.25)
            return s.connect_ex((probe_host, int(port))) == 0
    except OSError:
        return False


def _find_pids_listening_on_port(port: int) -> list[int]:
    port_num = int(port)
    self_pid = os.getpid()
    pids: list[int] = []

    if sys.platform == "win32":
        try:
            proc = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                capture_output=True,
                text=True,
                timeout=3.0,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            suffix = f":{port_num}"
            for line in (proc.stdout or "").splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[0].upper() == "TCP":
                    local_addr, state_str, pid_str = parts[1], parts[3].upper(), parts[4]
                    if local_addr.endswith(suffix) and state_str == "LISTENING":
                        try:
                            pid_val = int(pid_str)
                            if pid_val > 0 and pid_val != self_pid and pid_val not in pids:
                                pids.append(pid_val)
                        except ValueError:
                            pass
        except Exception:
            pass
        return pids

    for cmd in (
        ["lsof", "-nP", f"-iTCP:{port_num}", "-sTCP:LISTEN", "-t"],
        ["fuser", f"{port_num}/tcp"],
    ):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=2.5)
            raw_out = f"{proc.stdout or ''} {proc.stderr or ''}"
            for tok in raw_out.replace("/", " ").replace(":", " ").split():
                if tok.isdigit():
                    pid_val = int(tok)
                    if pid_val > 0 and pid_val != self_pid and pid_val not in pids:
                        pids.append(pid_val)
            if pids:
                break
        except Exception:
            continue
    return pids


def _reclaim_port_if_occupied(host: str, port: int, wait_sec: float = 1.8) -> bool:
    """若目标端口被占用，强杀占用进程并等待端口释放，同时清理被强杀旧实例遗留的临时缓存。"""
    if port <= 0 or not _is_port_occupied(host, port):
        return True

    target_pids = _find_pids_listening_on_port(port)
    killed_any = False
    for pid in target_pids:
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True,
                    timeout=3.0,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            killed_any = True
        except Exception:
            pass

    if killed_any:
        purge_runtime_caches()

    deadline = time.monotonic() + max(0.1, float(wait_sec))
    while time.monotonic() < deadline:
        if not _is_port_occupied(host, port):
            return True
        time.sleep(0.04)

    return not _is_port_occupied(host, port)


def create_app() -> Flask:
    from web_gui_algo_doc import inject_algo_doc_ui
    from web_gui_lifecycle import inject_lifecycle_ui, register_lifecycle_routes
    from web_gui_offset import inject_offset_ui, register_offset_routes
    from web_gui_precheck import inject_precheck_ui, register_precheck_routes

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 512 * 1024 * 1024  # 支持最大 512MB 无损音频上传
    rendered_index_html = inject_algo_doc_ui(
        inject_lifecycle_ui(inject_offset_ui(inject_precheck_ui(INDEX_HTML)))
    )
    register_precheck_routes(
        app,
        analyze_audio_fn=analyze_audio,
        parse_bool_fn=_parse_form_bool,
        parse_int_fn=_parse_form_int,
        parse_float_fn=_parse_form_float,
    )
    register_offset_routes(app)
    register_lifecycle_routes(app, cleanup_fn=purge_runtime_caches)

    @app.route("/", methods=["GET"])
    def index():
        return render_template_string(rendered_index_html)

    @app.route("/api/analyze", methods=["POST"])
    def api_analyze():
        if "file" not in request.files:
            return jsonify({"error": "未收到上传的音频文件 (缺少 file 字段)"}), 400

        up_file = request.files["file"]
        if not up_file or not up_file.filename:
            return jsonify({"error": "上传文件名为空"}), 400

        try:
            analyze_kwargs = parse_analyze_form_settings(request.form)
        except ValueError as val_err:
            return jsonify({"error": str(val_err)}), 400

        _, ext = os.path.splitext(up_file.filename)
        fd, tmp_path = tempfile.mkstemp(prefix="malody_bpm_", suffix=ext or ".tmp")
        os.close(fd)
        try:
            up_file.save(tmp_path)
            payload = analyze_audio(tmp_path, **analyze_kwargs)
            payload["filename"] = up_file.filename
            return jsonify(payload)
        except Exception as exc:
            return jsonify({"error": f"音频分析失败: {exc}"}), 500
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    return app


_default_app: Flask | None = None


def __getattr__(name: str):
    global _default_app
    if name == "app":
        if _default_app is None:
            _default_app = create_app()
        return _default_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class LocalWebServer:
    """可受控启停的本地线程化 HTTP 服务包装器（默认检测并强杀夺取 8888 端口）。"""

    def __init__(self, host: str = "127.0.0.1", start_port: int = 8888) -> None:
        self.host = host
        self.start_port = start_port
        self.port = start_port
        self._httpd = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stopped = False

    @property
    def url(self) -> str:
        display_host = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
        if ":" in display_host and not display_host.startswith("["):
            display_host = f"[{display_host}]"
        return f"http://{display_host}:{self.port}"

    @staticmethod
    def _find_free_port(host: str, start_port: int, span: int = 50) -> int:
        probe_host = "127.0.0.1" if host in ("0.0.0.0", "") else ("::1" if host == "::" else host)
        family = socket.AF_INET6 if ":" in probe_host else socket.AF_INET
        upper = min(65536, start_port + span)
        for port in range(start_port, upper):
            try:
                with socket.socket(family, socket.SOCK_STREAM) as s:
                    if s.connect_ex((probe_host, port)) != 0:
                        return port
            except OSError:
                return port
        return start_port

    def start(self) -> str:
        if not (0 <= int(self.start_port) <= 65535):
            raise RuntimeError(f"无效的本地监听端口号: {self.start_port} (允许范围 0-65535)")

        flask_app = create_app()

        last_err: Exception | None = None
        if self.start_port == 0:
            candidates = [0]
        else:
            _reclaim_port_if_occupied(self.host, self.start_port)
            preferred = self._find_free_port(self.host, self.start_port)
            upper_port = min(65536, self.start_port + 60)
            candidates = [self.start_port]
            if 1 <= preferred <= 65535 and preferred not in candidates:
                candidates.append(preferred)
            candidates.extend(
                p for p in range(self.start_port, upper_port) if p not in candidates
            )

        for cand_port in candidates:
            try:
                httpd = make_server(self.host, cand_port, flask_app, threaded=True)
                self._httpd = httpd
                self.port = int(httpd.server_port)
                break
            except (OSError, OverflowError) as exc:
                last_err = exc
                continue
            except SystemExit as exc:
                # werkzeug (>=2.x) 在 bind/listen 失败时不抛 OSError，而是打印 stderr 提示后 sys.exit(1)
                # （其内部已先 server_close 释放套接字）。不捕获则整个程序直接退出，端口顺延失效。
                # 2026-11-11 修复：吞掉该 SystemExit，继续尝试下一个候选端口。
                last_err = RuntimeError(f"端口 {cand_port} 绑定失败 (werkzeug 退出码 {exc.code})")
                continue
        else:
            raise RuntimeError(f"无法绑定本地 HTTP 端口 ({self.host}:{self.start_port}+): {last_err}")

        from web_gui_lifecycle import start_lifecycle_watchdog

        self._stopped = False
        self._thread = threading.Thread(
            target=self._serve,
            name="MalodyBPMWebServer",
            daemon=True,
        )
        self._thread.start()
        start_lifecycle_watchdog()
        return self.url

    def _serve(self) -> None:
        httpd = self._httpd
        if httpd is not None:
            try:
                httpd.serve_forever(poll_interval=0.25)
            except Exception:
                pass

    def stop(self) -> None:
        from web_gui_lifecycle import cancel_lifecycle_watchdog

        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            httpd = self._httpd
            self._httpd = None

        cancel_lifecycle_watchdog()

        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass

        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)

        purge_runtime_caches()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="启动 音频 BPM 深度测算仪 (基于 Malody 测速内核衍生增强)")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址 (默认 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8888, help="起始监听端口 (默认 8888，被占时自动强杀夺取)")
    parser.add_argument("--no-browser", action="store_true", help="启动时不自动打开浏览器")
    args, _ = parser.parse_known_args(argv)
    return args


def main(argv: list[str] | None = None) -> int:
    from web_gui_lifecycle import (
        SingleInstanceGuard,
        bind_lifecycle_runtime,
        ensure_windowless_stdio,
    )

    ensure_windowless_stdio()
    args = parse_args(argv)

    try:
        require_c_dsp()
    except Exception as exc:
        print(f"启动失败: {exc}", file=sys.stderr, flush=True)
        return 1

    # 单实例守护：抢不到锁则强杀锁记录中的老实例（与其监听端口无关），确保同一时刻最多一个进程。
    # guard 须在整个进程生命周期内保持引用。
    instance_guard = SingleInstanceGuard()
    lock_state = instance_guard.acquire(kill_wait_sec=3.0)
    if lock_state is True:
        # 2026-11-11：持锁后无条件清理一次临时缓存（原先仅在强杀过老实例时清理）。
        # 覆盖关机/注销/任务管理器结束/断电等「异常死亡且未被新实例强杀」的残留；
        # 此时已保证不存在其他存活实例，清理不会误删正在使用中的文件。
        purge_runtime_caches()
    if lock_state is False:
        # 极端情况（权限不足/无法终止）：放弃启动，改为打开仍存活老实例的页面，维持单实例
        old_url = instance_guard.blocking_info.get("url")
        will_open = bool(old_url) and not args.no_browser
        print(
            f"检测到已有实例运行且无法终止 (PID={instance_guard.blocking_info.get('pid')})，"
            f"本次启动取消。{('已为您打开其页面: ' if will_open else '其访问地址: ') + str(old_url) if old_url else ''}",
            file=sys.stderr,
            flush=True,
        )
        if will_open:
            try:
                webbrowser.open(str(old_url))
            except Exception:
                pass
            return 0
        return 1
    if lock_state is None:
        # 锁目录均不可写：无法保证单实例，降级继续运行（仍保留下方 8888 端口强杀逻辑）
        print("警告: 无法创建单实例锁文件，将跳过单实例检查继续启动。", file=sys.stderr, flush=True)

    stop_event = threading.Event()
    bind_lifecycle_runtime(stop_event=stop_event, cleanup_fn=purge_runtime_caches)

    def _handle_sig(_signum, _frame) -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_sig)
        except Exception:
            pass

    server = LocalWebServer(host=args.host, start_port=args.port)
    try:
        server.start()
    except Exception as exc:
        print(f"HTTP 服务启动失败: {exc}", file=sys.stderr, flush=True)
        instance_guard.release()
        return 1

    # 记录实际监听端口（可能因顺延而不是 8888），供后续实例兜底打开页面
    instance_guard.update_endpoint(server.port, server.url)

    arch = _detect_process_arch()
    print("============================================================", flush=True)
    print(f"  音频 BPM 深度测算仪已启动 ({arch})", flush=True)
    print("  基于 Malody 测速内核衍生增强 · 集成 Offset 包络定位", flush=True)
    print(f"  本地访问地址: {server.url}", flush=True)
    print("  按 Ctrl+C 停止本地测算服务", flush=True)
    print("============================================================", flush=True)

    if not args.no_browser:
        opener = threading.Timer(0.3, lambda: webbrowser.open(server.url))
        opener.daemon = True
        opener.start()

    try:
        while not stop_event.wait(timeout=0.25):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        purge_runtime_caches()
        instance_guard.release()
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
