#!/usr/bin/env python3
"""
Malody BPM 测算工作台 —— 独立音频头尾静音自动裁剪子模块

设计原则：
1. 独立位于 UI 同级目录，对 malody_bpm/ 后端核心库保持 100% 零改动；
2. 基于单声道浮点 PCM 10ms 短窗均方能量（RMS）相对全曲峰值分贝阈值裁剪首尾静音，
   并在首尾非静音边界外侧各保留 30ms 瞬态保护缓冲，避免切伤首拍鼓点起音斜坡；
3. 可选开启「同时处理首尾孤立的极短时间无效振幅」：基于短间隙桥接与首尾孤立短声岛剥离，
   自动剔除与正曲主体之间隔有静音断层的开头爆音或结尾孤立鼓点，同时内置防误裁保护以保留鼓棒预备拍与稀疏前奏；
4. 零临时音频文件、零二次重编码失真（纯内存切片）；
5. 未开启「解除音频时长限制」时，统一按裁剪后的有效时长校验 [45s, 600s]；
6. 提供变速区间时间戳还原工具，将裁剪后时间轴上的 transition_spans 补偿回原始音频进度条时间轴。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import subprocess
from typing import Sequence

import numpy as np

from malody_bpm.decoder import (
    MAX_PCM_DURATION_SEC,
    MIN_PCM_DURATION_SEC,
    _resolve_ff_binary,
    _subproc_extra_kwargs,
)

DEFAULT_SILENCE_THRESHOLD_DB: float = -50.0
MIN_SILENCE_THRESHOLD_DB: float = -70.0
MAX_SILENCE_THRESHOLD_DB: float = -25.0

DEFAULT_SHORT_BURST_MAX_SEC: float = 1.5
MIN_SHORT_BURST_MAX_SEC: float = 0.1
MAX_SHORT_BURST_MAX_SEC: float = 3.0

DEFAULT_SHORT_BURST_MIN_GAP_SEC: float = 0.8
MIN_SHORT_BURST_MIN_GAP_SEC: float = 0.3
MAX_SHORT_BURST_MIN_GAP_SEC: float = 3.0

SHORT_BURST_MAX_OUTER_ISLANDS: int = 2
SHORT_BURST_MIN_BODY_SEC: float = 2.0

SILENCE_WINDOW_MS: int = 10
SILENCE_GUARD_MS: int = 30
SILENCE_BADGE_MIN_TOTAL_MS: float = 100.0
RAW_SAFETY_CAP_SEC: float = 1800.0


@dataclass(frozen=True)
class SilenceTrimInfo:
    enabled: bool
    threshold_db: float
    did_trim: bool
    raw_frames: int
    trimmed_frames: int
    raw_duration_sec: float
    trimmed_duration_sec: float
    head_trim_ms: float
    tail_trim_ms: float
    trim_short_bursts: bool = False
    short_burst_max_sec: float = DEFAULT_SHORT_BURST_MAX_SEC
    short_burst_min_gap_sec: float = DEFAULT_SHORT_BURST_MIN_GAP_SEC
    short_burst_trimmed: bool = False

    @property
    def show_badge(self) -> bool:
        return bool(
            self.did_trim
            and (self.head_trim_ms + self.tail_trim_ms) >= SILENCE_BADGE_MIN_TOTAL_MS
        )

    @property
    def head_trim_sec(self) -> float:
        return round(self.head_trim_ms / 1000.0, 2)

    @property
    def tail_trim_sec(self) -> float:
        return round(self.tail_trim_ms / 1000.0, 2)


def validate_silence_threshold_db(threshold_db: float) -> float:
    """校验头尾静音判定相对阈值（单位 dB，闭区间 [-70.0, -25.0]）。"""
    if isinstance(threshold_db, (bool, np.bool_)) or not isinstance(
        threshold_db, (int, float, np.integer, np.floating)
    ):
        raise ValueError(f"silence_threshold_db 必须为数值，当前传入: {threshold_db!r}")
    val = float(threshold_db)
    if not math.isfinite(val) or not (
        MIN_SILENCE_THRESHOLD_DB - 1e-6 <= val <= MAX_SILENCE_THRESHOLD_DB + 1e-6
    ):
        raise ValueError(
            f"silence_threshold_db 必须在 [{MIN_SILENCE_THRESHOLD_DB:.0f}, {MAX_SILENCE_THRESHOLD_DB:.0f}] dB 之间，"
            f"当前传入: {threshold_db!r}"
        )
    return min(MAX_SILENCE_THRESHOLD_DB, max(MIN_SILENCE_THRESHOLD_DB, val))


def validate_short_burst_max_sec(max_sec: float) -> float:
    """校验孤立短振幅最大持续时长（单位秒，闭区间 [0.1, 3.0]）。"""
    if isinstance(max_sec, (bool, np.bool_)) or not isinstance(
        max_sec, (int, float, np.integer, np.floating)
    ):
        raise ValueError(f"short_burst_max_sec 必须为数值，当前传入: {max_sec!r}")
    val = float(max_sec)
    if not math.isfinite(val) or not (
        MIN_SHORT_BURST_MAX_SEC - 1e-6 <= val <= MAX_SHORT_BURST_MAX_SEC + 1e-6
    ):
        raise ValueError(
            f"short_burst_max_sec 必须在 [{MIN_SHORT_BURST_MAX_SEC:.1f}, {MAX_SHORT_BURST_MAX_SEC:.1f}] 秒之间，"
            f"当前传入: {max_sec!r}"
        )
    return min(MAX_SHORT_BURST_MAX_SEC, max(MIN_SHORT_BURST_MAX_SEC, val))


def validate_short_burst_min_gap_sec(min_gap_sec: float) -> float:
    """校验与正曲主体的最小静音间隔（单位秒，闭区间 [0.3, 3.0]）。"""
    if isinstance(min_gap_sec, (bool, np.bool_)) or not isinstance(
        min_gap_sec, (int, float, np.integer, np.floating)
    ):
        raise ValueError(f"short_burst_min_gap_sec 必须为数值，当前传入: {min_gap_sec!r}")
    val = float(min_gap_sec)
    if not math.isfinite(val) or not (
        MIN_SHORT_BURST_MIN_GAP_SEC - 1e-6 <= val <= MAX_SHORT_BURST_MIN_GAP_SEC + 1e-6
    ):
        raise ValueError(
            f"short_burst_min_gap_sec 必须在 [{MIN_SHORT_BURST_MIN_GAP_SEC:.1f}, {MAX_SHORT_BURST_MIN_GAP_SEC:.1f}] 秒之间，"
            f"当前传入: {min_gap_sec!r}"
        )
    return min(MAX_SHORT_BURST_MIN_GAP_SEC, max(MIN_SHORT_BURST_MIN_GAP_SEC, val))


def check_raw_safety_cap(
    file_path: str,
    allow_any_duration: bool = False,
    max_raw_sec: float = RAW_SAFETY_CAP_SEC,
) -> None:
    """
    当开启去静音且未解除时长限制时，先用 ffprobe 轻量检查原始容器/流时长是否超过内存安全上限（默认 1800s），
    既允许如 610s 含长尾静音的曲目解码后被裁剪进 [45s, 600s]，又防止数小时超大音频直接全量解码耗尽内存。
    """
    if allow_any_duration:
        return

    try:
        cmd = [
            _resolve_ff_binary("ffprobe"),
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "format=duration:stream=duration",
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
            return
        data = json.loads(res.stdout)
    except Exception:
        return

    candidates: list[float] = []
    fmt = data.get("format") if isinstance(data, dict) else None
    if isinstance(fmt, dict) and fmt.get("duration") not in (None, "", "N/A"):
        try:
            d_fmt = float(fmt["duration"])
            if math.isfinite(d_fmt) and d_fmt > 0.0:
                candidates.append(d_fmt)
        except (TypeError, ValueError):
            pass

    streams = data.get("streams") if isinstance(data, dict) else None
    if isinstance(streams, list) and streams:
        st0 = streams[0]
        if isinstance(st0, dict) and st0.get("duration") not in (None, "", "N/A"):
            try:
                d_st = float(st0["duration"])
                if math.isfinite(d_st) and d_st > 0.0:
                    candidates.append(d_st)
            except (TypeError, ValueError):
                pass

    if candidates:
        probed_dur = max(candidates)
        if probed_dur > max_raw_sec:
            raise ValueError(
                f"原始音频时长 ({probed_dur:.2f}s) 超过去静音前置解码安全上限 ({max_raw_sec:.0f}s)，"
                "不在允许范围 [45s, 600s] 内"
            )


def validate_trimmed_pcm_frames(
    n_frames: int,
    sr: int,
    allow_any_duration: bool = False,
    did_trim: bool = False,
) -> None:
    """对裁剪头尾静音后的有效单声道 PCM 帧数统一校验 [45s, 600s] 时长区间。"""
    if sr <= 0 or n_frames <= 0:
        raise ValueError("音频实际 PCM 时长为 0.00s，不在允许范围 [45s, 600s] 内")
    if allow_any_duration:
        return

    max_frames = int(round(MAX_PCM_DURATION_SEC * float(sr)))
    min_frames = int(round(MIN_PCM_DURATION_SEC * float(sr)))
    dur_sec = float(n_frames) / float(sr)
    dur_ms = dur_sec * 1000.0
    label = "去除头尾静音后的音频实际有效时长" if did_trim else "音频实际 PCM 时长"

    if n_frames > max_frames or dur_ms > MAX_PCM_DURATION_SEC * 1000.0:
        raise ValueError(f"{label} ({dur_sec:.2f}s) 超出允许范围 [45s, 600s]")
    if n_frames < min_frames or dur_ms < MIN_PCM_DURATION_SEC * 1000.0:
        raise ValueError(f"{label} ({dur_sec:.2f}s) 短于允许范围 [45s, 600s]")


def _prune_outer_short_bursts(
    active_mask: np.ndarray,
    max_burst_sec: float,
    min_gap_sec: float,
) -> tuple[int, int, bool]:
    """
    在 10ms 能量活跃掩码上执行「短间隙桥接 + 首尾孤立短声岛剥离」：
    1. 将短于 min_gap_sec 的静音间隙桥接为连续声岛（保护鼓棒预备拍与正常断奏）；
    2. 在首、尾两侧各自最多向内检查 SHORT_BURST_MAX_OUTER_ISLANDS 个孤立短声岛：
       仅当外侧声岛自身及累计跨度均 <= max_burst_sec，且剥离后落入足够长的正曲主体声岛
       （>= max(max_burst_sec, SHORT_BURST_MIN_BODY_SEC)）时才予以剔除，防止连环吃掉周期性稀疏前奏。
    """
    padded = np.empty(len(active_mask) + 2, dtype=np.int8)
    padded[0] = 0
    padded[1:-1] = active_mask.astype(np.int8, copy=False)
    padded[-1] = 0
    diffs = np.diff(padded)
    starts = np.flatnonzero(diffs == 1)
    ends = np.flatnonzero(diffs == -1)

    if len(starts) == 0:
        return 0, 0, False

    min_gap_blks = max(1, int(round(min_gap_sec * 1000.0 / float(SILENCE_WINDOW_MS))))
    max_burst_blks = max(1, int(round(max_burst_sec * 1000.0 / float(SILENCE_WINDOW_MS))))
    min_body_blks = max(
        max_burst_blks + 1,
        int(round(SHORT_BURST_MIN_BODY_SEC * 1000.0 / float(SILENCE_WINDOW_MS))),
    )

    islands: list[tuple[int, int]] = []
    cur_s = int(starts[0])
    cur_e = int(ends[0])
    for s_raw, e_raw in zip(starts[1:], ends[1:]):
        s_i = int(s_raw)
        e_i = int(e_raw)
        if s_i - cur_e < min_gap_blks:
            cur_e = e_i
        else:
            islands.append((cur_s, cur_e))
            cur_s, cur_e = s_i, e_i
    islands.append((cur_s, cur_e))

    if len(islands) <= 1:
        return islands[0][0], islands[0][1] - 1, False

    head_idx = 0
    cum_head_blks = 0
    max_head_check = min(SHORT_BURST_MAX_OUTER_ISLANDS, len(islands) - 1)
    for k in range(1, max_head_check + 1):
        span = islands[k - 1][1] - islands[k - 1][0]
        cum_head_blks += span
        if span > max_burst_blks or cum_head_blks > max_burst_blks:
            break
        land_span = islands[k][1] - islands[k][0]
        if land_span >= min_body_blks:
            head_idx = k
            break

    tail_idx = len(islands) - 1
    cum_tail_blks = 0
    max_tail_check = min(SHORT_BURST_MAX_OUTER_ISLANDS, tail_idx - head_idx)
    for k in range(1, max_tail_check + 1):
        cand_idx = len(islands) - k
        span = islands[cand_idx][1] - islands[cand_idx][0]
        cum_tail_blks += span
        if span > max_burst_blks or cum_tail_blks > max_burst_blks:
            break
        land_idx = cand_idx - 1
        land_span = islands[land_idx][1] - islands[land_idx][0]
        if land_span >= min_body_blks:
            tail_idx = land_idx
            break

    pruned = (head_idx > 0) or (tail_idx < len(islands) - 1)
    return islands[head_idx][0], islands[tail_idx][1] - 1, pruned


def trim_head_tail_silence(
    pcm: np.ndarray,
    sr: int,
    enabled: bool = False,
    threshold_db: float = DEFAULT_SILENCE_THRESHOLD_DB,
    trim_short_bursts: bool = False,
    short_burst_max_sec: float = DEFAULT_SHORT_BURST_MAX_SEC,
    short_burst_min_gap_sec: float = DEFAULT_SHORT_BURST_MIN_GAP_SEC,
) -> tuple[np.ndarray, SilenceTrimInfo]:
    """
    检测并裁剪单声道或二维多声道 (n_frames, n_channels) 浮点 PCM 首尾静音段（及可选的首尾孤立极短无效振幅）：
    - 以 10ms 短窗均方能量（RMS，多声道取全声道均方均值）计算全曲峰值能量基准 peak_msq；
    - 首个与末个高于 peak_msq * 10^(threshold_db / 10) 的窗口界定基础有效区间；
    - 若开启 trim_short_bursts，进一步剥离与正曲主体间隔 >= short_burst_min_gap_sec 且自身 <= short_burst_max_sec 的首尾孤立短声岛；
    - 在有效区间外侧各保留 30ms 保护缓冲，确保首拍鼓点瞬态不被截断；
    - 纯静音或全段有效时返回原数组与 did_trim=False。
    """
    if not isinstance(enabled, (bool, np.bool_)):
        raise ValueError(f"trim_silence 必须为布尔类型，当前传入: {enabled!r}")
    if not isinstance(trim_short_bursts, (bool, np.bool_)):
        raise ValueError(f"trim_short_bursts 必须为布尔类型，当前传入: {trim_short_bursts!r}")

    thr_db = validate_silence_threshold_db(threshold_db)
    burst_max_sec = validate_short_burst_max_sec(short_burst_max_sec)
    burst_min_gap_sec = validate_short_burst_min_gap_sec(short_burst_min_gap_sec)
    eff_burst = bool(enabled and trim_short_bursts)

    raw_frames = int(len(pcm)) if pcm is not None else 0
    raw_dur = (float(raw_frames) / float(sr)) if (sr > 0 and raw_frames > 0) else 0.0

    no_trim = SilenceTrimInfo(
        enabled=bool(enabled),
        threshold_db=thr_db,
        did_trim=False,
        raw_frames=raw_frames,
        trimmed_frames=raw_frames,
        raw_duration_sec=raw_dur,
        trimmed_duration_sec=raw_dur,
        head_trim_ms=0.0,
        tail_trim_ms=0.0,
        trim_short_bursts=eff_burst,
        short_burst_max_sec=burst_max_sec,
        short_burst_min_gap_sec=burst_min_gap_sec,
        short_burst_trimmed=False,
    )
    if not enabled or sr <= 0 or raw_frames <= 0:
        return pcm, no_trim

    if pcm.ndim == 2:
        n_ch = int(pcm.shape[1])
        if n_ch <= 0:
            return pcm, no_trim
    elif pcm.ndim == 1:
        n_ch = 1
    else:
        raise ValueError(f"pcm 维度必须为 1D 或 2D，当前 shape={pcm.shape!r}")

    win = max(1, int(round(float(sr) * (SILENCE_WINDOW_MS / 1000.0))))
    n_full = raw_frames // win
    rem = raw_frames % win

    parts: list[np.ndarray] = []
    if n_full > 0:
        if pcm.ndim == 2:
            blks = pcm[: n_full * win].astype(np.float64, copy=False).reshape(n_full, win, n_ch)
            parts.append(np.mean(blks * blks, axis=(1, 2)))
        else:
            blks = pcm[: n_full * win].astype(np.float64, copy=False).reshape(n_full, win)
            parts.append(np.mean(blks * blks, axis=1))
    if rem > 0:
        tail_seg = pcm[n_full * win :].astype(np.float64, copy=False)
        parts.append(np.array([float(np.mean(tail_seg * tail_seg))], dtype=np.float64))

    if not parts:
        return pcm, no_trim

    msq = parts[0] if len(parts) == 1 else np.concatenate(parts)
    peak_msq = float(np.max(msq)) if len(msq) > 0 else 0.0
    if not math.isfinite(peak_msq) or peak_msq <= 1e-14:
        return pcm, no_trim

    thr_msq = peak_msq * (10.0 ** (thr_db / 10.0))
    active_mask = msq > thr_msq
    active_idx = np.flatnonzero(active_mask)
    if len(active_idx) == 0:
        return pcm, no_trim

    short_burst_pruned = False
    if eff_burst:
        first_blk, last_blk, short_burst_pruned = _prune_outer_short_bursts(
            active_mask,
            max_burst_sec=burst_max_sec,
            min_gap_sec=burst_min_gap_sec,
        )
    else:
        first_blk = int(active_idx[0])
        last_blk = int(active_idx[-1])

    guard = int(round(float(sr) * (SILENCE_GUARD_MS / 1000.0)))

    start_sample = max(0, first_blk * win - guard)
    end_sample = min(raw_frames, (last_blk + 1) * win + guard)
    if end_sample <= start_sample or (start_sample == 0 and end_sample == raw_frames):
        return pcm, no_trim

    trimmed_pcm = np.ascontiguousarray(pcm[start_sample:end_sample], dtype=np.float32)
    trimmed_frames = int(len(trimmed_pcm))
    trimmed_dur = float(trimmed_frames) / float(sr)
    head_ms = (float(start_sample) / float(sr)) * 1000.0
    tail_ms = (float(raw_frames - end_sample) / float(sr)) * 1000.0

    return trimmed_pcm, SilenceTrimInfo(
        enabled=True,
        threshold_db=thr_db,
        did_trim=True,
        raw_frames=raw_frames,
        trimmed_frames=trimmed_frames,
        raw_duration_sec=raw_dur,
        trimmed_duration_sec=trimmed_dur,
        head_trim_ms=head_ms,
        tail_trim_ms=tail_ms,
        trim_short_bursts=eff_burst,
        short_burst_max_sec=burst_max_sec,
        short_burst_min_gap_sec=burst_min_gap_sec,
        short_burst_trimmed=bool(short_burst_pruned),
    )


def shift_spans_to_raw_timeline(
    spans: Sequence[Sequence[float]] | None,
    head_trim_ms: float,
    raw_duration_ms: float,
) -> list[list[float]]:
    """将裁剪后时间轴上的变速区间毫秒坐标加上 head_trim_ms，还原至原始音频文件时间轴。"""
    if not spans:
        return []
    offset = max(0.0, float(head_trim_ms))
    cap = max(0.0, float(raw_duration_ms))
    out: list[list[float]] = []
    for pair in spans:
        if len(pair) < 2:
            continue
        s_val = float(pair[0]) + offset
        e_val = float(pair[1]) + offset
        if cap > 0.0:
            s_val = min(cap, max(0.0, s_val))
            e_val = min(cap, max(s_val, e_val))
        out.append([round(s_val, 2), round(e_val, 2)])
    return out
