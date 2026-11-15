#!/usr/bin/env python3
"""
Malody BPM 测算工作台 —— UI 层「未匹配主干网格区段」子模块

背景（2026-11 改动日志）：
    结果卡片上方的「节拍匹配占比」来自后端逐拍网格追踪（compute_best_phase_and_rmse）的命中统计，
    而「变速区域」由 web_gui_tempo_regions.py 的独立相位锁定检测器给出，两者标准不同，
    常出现「匹配占比远低于 100%，但变速区域显示无」的情况。
    本模块直接复用计算匹配占比的同一套逐拍命中序列（后端只读导出的 matched_beat_times），
    生成未匹配区段与汇总百分比，保证：显示的匹配% + 未匹配合计% = 100%。

计算规则：
    1. 每个计入 n_matched 的命中拍占据以其理论网格时刻为中心、宽度为一个追踪周期 tau 的「命中格」；
    2. 有效时长 [0, D] 内命中格并集的补集即为未匹配时间；
    3. 被短于 merge_gap_ms 的命中岛隔开的相邻未匹配段合并；合并后跨度 ≥ min_span_ms 的列为「区段」；
    4. 合计 X = 100 - 匹配%（与卡片显示值严格互补）；列出区段 Y = 区段内真实未匹配时长占比（不含被并入的命中岛），
       并钳制 Y ≤ X；零散丢拍 Z = X - Y。
"""

from __future__ import annotations

import math
from typing import Sequence

DEFAULT_MIN_SPAN_MS: float = 3000.0   # 列出区段的最小跨度
DEFAULT_MERGE_GAP_MS: float = 1000.0  # 相邻未匹配段之间命中岛短于该值时合并


def _union_intervals(intervals: list[tuple[float, float]]) -> list[list[float]]:
    """对区间列表求并集（输入无需有序），返回升序且互不重叠的区间。"""
    out: list[list[float]] = []
    for s, e in sorted(intervals):
        if e <= s:
            continue
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def build_unmatched_grid_info(
    matched_beat_times: Sequence[float] | None,
    beat_tau_ms: float | None,
    eff_duration_ms: float,
    matched_pct: float,
    min_span_ms: float = DEFAULT_MIN_SPAN_MS,
    merge_gap_ms: float = DEFAULT_MERGE_GAP_MS,
) -> dict:
    """
    生成未匹配主干网格信息（时间轴为去静音后的裁剪时间轴，单位 ms）。

    :param matched_beat_times: 计入 n_matched 的命中拍理论网格时刻（后端只读导出）
    :param beat_tau_ms: 追踪所用节拍周期
    :param eff_duration_ms: 有效（裁剪后）音频时长
    :param matched_pct: 卡片上显示的「节拍匹配占总时长比例」（%，已保留 1 位小数）
    :return: {
        "available": 是否有逐拍序列可用于列出区段,
        "spans": [[s_ms, e_ms], ...],
        "total_pct": X, "listed_pct": Y, "scattered_pct": Z,
    }
    """
    # 合计严格取匹配占比的补数，保证与卡片显示值相加恰为 100%
    mp = float(matched_pct) if math.isfinite(float(matched_pct)) else 0.0
    total_pct = round(min(100.0, max(0.0, 100.0 - mp)), 1)
    base = {"available": False, "spans": [], "total_pct": total_pct, "listed_pct": 0.0, "scattered_pct": total_pct}

    D = float(eff_duration_ms) if eff_duration_ms is not None else 0.0
    if (
        matched_beat_times is None
        or beat_tau_ms is None
        or not math.isfinite(float(beat_tau_ms))
        or float(beat_tau_ms) <= 0.0
        or not math.isfinite(D)
        or D <= 0.0
    ):
        return base

    tau = float(beat_tau_ms)
    half = 0.5 * tau

    # 1. 命中格并集（裁剪到 [0, D]）
    cells = [
        (max(0.0, float(t) - half), min(D, float(t) + half))
        for t in matched_beat_times
        if math.isfinite(float(t))
    ]
    hit_union = _union_intervals(cells)

    # 2. 补集：未匹配时间段
    #    丢弃短于 1/4 拍的碎缝：导出时刻保留 2 位小数及浮点累加会在相邻命中格之间留下约 0.01 ms 的微缝，
    #    分段重锁交界处也可能出现亚拍级缝隙；真正的丢拍缝隙至少约 1 拍。若不丢弃，这些微缝会经第 3 步
    #    「短命中岛合并」被串成覆盖全曲的伪区段（2026-11 合成音频验证中发现）。被丢弃的碎缝自然计入零散丢拍 Z。
    min_gap_ms = max(1.0, 0.25 * tau)
    gaps: list[list[float]] = []
    cursor = 0.0
    for s, e in hit_union:
        if s - cursor >= min_gap_ms:
            gaps.append([cursor, s])
        cursor = max(cursor, e)
    if D - cursor >= min_gap_ms:
        gaps.append([cursor, D])

    # 3. 合并被短命中岛隔开的相邻未匹配段；同时累计各合并段内的真实未匹配时长
    merged: list[list[float]] = []  # [start, end, unmatched_ms]
    for s, e in gaps:
        if merged and (s - merged[-1][1]) < merge_gap_ms:
            merged[-1][1] = e
            merged[-1][2] += e - s
        else:
            merged.append([s, e, e - s])

    listed = [m for m in merged if (m[1] - m[0]) >= min_span_ms]

    # 4. 汇总：Y 只计区段内真实未匹配时长，并钳制 Y ≤ X（追踪周期与占比折算周期不一致等极端情况下兜底）
    listed_ms = sum(m[2] for m in listed)
    listed_pct = round(min(total_pct, max(0.0, listed_ms / D * 100.0)), 1)
    scattered_pct = round(max(0.0, total_pct - listed_pct), 1)

    return {
        "available": True,
        "spans": [[round(m[0], 2), round(m[1], 2)] for m in listed],
        "total_pct": total_pct,
        "listed_pct": listed_pct,
        "scattered_pct": scattered_pct,
    }
