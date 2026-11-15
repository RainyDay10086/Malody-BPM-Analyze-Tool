"""
Malody BPM 测算工作台 —— “测算设置”独立子模块 (web_gui_settings)

职责边界：
1. 集中维护前端默认设置真值表 (DEFAULT_UI_SETTINGS) 与后端表单解析范围规格；
2. 提供 /api/analyze 表单参数解析与边界强校验函数 (parse_analyze_form_settings 及基础类型解析器)；
3. 集中承载顶栏设置组合按钮、测算设置模态弹窗 (#settingsModal)、非空队列清空确认弹窗 (#confirmClearModal)、
   设置专属 Material 3 卡片岛/单选/开关/嵌套复选框/步进器样式，以及前端草稿状态与交互控制脚本；
4. 通过 inject_settings_ui() 对基础工作台模板执行带显式锚点强校验、幂等保护与 Jinja2 定界符防冲突检查的无损注入，
   完整保留 web_gui_offset / web_gui_precheck / web_gui_lifecycle / web_gui_algo_doc 所依赖的全部锚点与全局钩子。
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any

from malody_bpm.decoder import normalize_channel_mode
from web_gui_silence import (
    DEFAULT_SHORT_BURST_MAX_SEC,
    DEFAULT_SHORT_BURST_MIN_GAP_SEC,
    DEFAULT_SILENCE_THRESHOLD_DB,
    MAX_SHORT_BURST_MAX_SEC,
    MAX_SHORT_BURST_MIN_GAP_SEC,
    MAX_SILENCE_THRESHOLD_DB,
    MIN_SHORT_BURST_MAX_SEC,
    MIN_SHORT_BURST_MIN_GAP_SEC,
    MIN_SILENCE_THRESHOLD_DB,
)

# ---------------------------------------------------------------------------
# 1. 设置参数规格与默认真值表 (Single Source of Truth)
# ---------------------------------------------------------------------------

ALLOWED_ANALYZE_MODES: tuple[str, ...] = (
    "exact",
    "enhanced_v3",
    "force_v3",
    "all",
    "enhanced",
    "enhanced_v2",
    "both",
)

# (默认值, 最小值, 最大值)
ACF_DECAY_RATIO_BOUNDS: tuple[float, float, float] = (0.7, 0.1, 1.0)
CAND_UPDATE_RATIO_BOUNDS: tuple[float, float, float] = (1.25, 1.0, 2.0)
# [新增 2026-11-14] 240~300 BPM 折半合理性判定门限 (默认值, 最小值, 最大值)
HALVE_CONTRAST_RATIO_BOUNDS: tuple[float, float, float] = (1.10, 1.0, 2.0)
TRACK_TOL_BOUNDS: tuple[int, int, int] = (8, 5, 25)
COMB_TOL_BOUNDS: tuple[int, int, int] = (35, 10, 50)
TRANSITION_MISS_BEATS_BOUNDS: tuple[int, int, int] = (4, 2, 10)
TRANSITION_RESUME_BEATS_BOUNDS: tuple[int, int, int] = (3, 2, 10)
TRANSITION_GUARD_BOUNDS: tuple[float, float, float] = (1.0, 0.0, 3.0)
SUB_TRUNK_MIN_SEC_BOUNDS: tuple[int, int, int] = (15, 8, 15)
OFFSET_INIT_SEC_BOUNDS: tuple[int, int, int] = (20, 2, 60)

DEFAULT_UI_SETTINGS: dict[str, Any] = {
    "mode": "all",
    "channel": "auto",
    "trimSilence": True,
    "silenceThresholdDb": int(DEFAULT_SILENCE_THRESHOLD_DB),
    "trimShortBursts": False,
    "shortBurstMaxSec": float(DEFAULT_SHORT_BURST_MAX_SEC),
    "shortBurstMinGapSec": float(DEFAULT_SHORT_BURST_MIN_GAP_SEC),
    "allowAnyDuration": False,
    "optimizeTripleMeter": True,
    "acfDecayRatio": ACF_DECAY_RATIO_BOUNDS[0],
    "candUpdateRatio": CAND_UPDATE_RATIO_BOUNDS[0],
    # [新增 2026-11-14] 240~300 BPM 折半合理性判定门限默认值
    "halveContrastRatio": HALVE_CONTRAST_RATIO_BOUNDS[0],
    "trackTol": TRACK_TOL_BOUNDS[0],
    "combTol": COMB_TOL_BOUNDS[0],
    "applyCombTolToForce": False,
    "transitionMissBeats": TRANSITION_MISS_BEATS_BOUNDS[0],
    "transitionResumeBeats": TRANSITION_RESUME_BEATS_BOUNDS[0],
    "transitionGuard": TRANSITION_GUARD_BOUNDS[0],
    "trimRecalc": True,
    "subTrunkMinSec": SUB_TRUNK_MIN_SEC_BOUNDS[0],
    "offsetInitSec": OFFSET_INIT_SEC_BOUNDS[0],
    "offsetCurveColor": "#3fb950ff",
}


# ---------------------------------------------------------------------------
# 2. 后端表单参数解析与范围强校验
# ---------------------------------------------------------------------------

def parse_form_bool(raw: str | None, default: bool = False) -> bool:
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("true", "1", "yes", "on")


def parse_form_int(raw: str | None, default: int, min_val: int, max_val: int, label: str) -> int:
    if raw is None or raw.strip() == "":
        return default
    try:
        val = int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"参数 {label} 必须为整数") from exc
    if not (min_val <= val <= max_val):
        raise ValueError(f"参数 {label} 超出允许范围 [{min_val}, {max_val}]")
    return val


def parse_form_float(raw: str | None, default: float, min_val: float, max_val: float, label: str) -> float:
    if raw is None or raw.strip() == "":
        return default
    try:
        val = float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"参数 {label} 必须为数值") from exc
    if not (min_val - 1e-6 <= val <= max_val + 1e-6):
        raise ValueError(f"参数 {label} 超出允许范围 [{min_val}, {max_val}]")
    return min(max_val, max(min_val, val))


def parse_analyze_form_settings(form: Mapping[str, str]) -> dict[str, Any]:
    """解析并校验 /api/analyze 请求表单中的全部设置字段，返回 analyze_audio 所需参数字典。"""
    raw_mode = form.get("mode")
    mode = (raw_mode if raw_mode is not None else "all").strip().lower()
    if mode not in ALLOWED_ANALYZE_MODES:
        mode = "all"

    octave_norm = parse_form_bool(form.get("octave_norm"), default=False)
    raw_sr = form.get("target_sr")
    sr_str = (raw_sr if raw_sr is not None else "").strip()
    target_sr = int(sr_str) if sr_str.isdigit() else None

    channel = normalize_channel_mode(form.get("channel", "auto"))
    trim_silence = parse_form_bool(form.get("trim_silence"), default=False)
    silence_threshold_db = parse_form_float(
        form.get("silence_threshold_db"),
        DEFAULT_SILENCE_THRESHOLD_DB,
        MIN_SILENCE_THRESHOLD_DB,
        MAX_SILENCE_THRESHOLD_DB,
        "silence_threshold_db",
    )
    trim_short_bursts = parse_form_bool(form.get("trim_short_bursts"), default=False)
    short_burst_max_sec = parse_form_float(
        form.get("short_burst_max_sec"),
        DEFAULT_SHORT_BURST_MAX_SEC,
        MIN_SHORT_BURST_MAX_SEC,
        MAX_SHORT_BURST_MAX_SEC,
        "short_burst_max_sec",
    )
    short_burst_min_gap_sec = parse_form_float(
        form.get("short_burst_min_gap_sec"),
        DEFAULT_SHORT_BURST_MIN_GAP_SEC,
        MIN_SHORT_BURST_MIN_GAP_SEC,
        MAX_SHORT_BURST_MIN_GAP_SEC,
        "short_burst_min_gap_sec",
    )
    allow_any_duration = parse_form_bool(form.get("allow_any_duration"), default=False)
    acf_decay_ratio = parse_form_float(
        form.get("acf_decay_ratio"),
        *ACF_DECAY_RATIO_BOUNDS,
        "acf_decay_ratio",
    )
    cand_update_ratio = parse_form_float(
        form.get("cand_update_ratio"),
        *CAND_UPDATE_RATIO_BOUNDS,
        "cand_update_ratio",
    )
    # [新增 2026-11-14] 240~300 BPM 折半强弱拍脉冲梳对比度门限
    halve_contrast_ratio = parse_form_float(
        form.get("halve_contrast_ratio"),
        *HALVE_CONTRAST_RATIO_BOUNDS,
        "halve_contrast_ratio",
    )
    track_tol = parse_form_int(
        form.get("track_tol"),
        *TRACK_TOL_BOUNDS,
        "track_tol",
    )
    comb_tol = parse_form_int(
        form.get("comb_tol"),
        *COMB_TOL_BOUNDS,
        "comb_tol",
    )
    apply_comb_tol_to_force = parse_form_bool(
        form.get("apply_comb_tol_to_force"),
        default=False,
    )
    transition_miss_beats = parse_form_int(
        form.get("transition_miss_beats"),
        *TRANSITION_MISS_BEATS_BOUNDS,
        "transition_miss_beats",
    )
    transition_resume_beats = parse_form_int(
        form.get("transition_resume_beats"),
        *TRANSITION_RESUME_BEATS_BOUNDS,
        "transition_resume_beats",
    )
    transition_guard = parse_form_float(
        form.get("transition_guard"),
        *TRANSITION_GUARD_BOUNDS,
        "transition_guard",
    )
    trim_recalc = parse_form_bool(
        form.get("trim_recalc"),
        default=False,
    )
    sub_trunk_min_sec = parse_form_int(
        form.get("sub_trunk_min_sec"),
        *SUB_TRUNK_MIN_SEC_BOUNDS,
        "sub_trunk_min_sec",
    )

    return {
        "mode": mode,
        "octave_norm": octave_norm,
        "target_sr": target_sr,
        "trim_silence": trim_silence,
        "allow_any_duration": allow_any_duration,
        "acf_decay_ratio": acf_decay_ratio,
        "cand_update_ratio": cand_update_ratio,
        "halve_contrast_ratio": halve_contrast_ratio,
        "track_tol": track_tol,
        "comb_tol": comb_tol,
        "apply_comb_tol_to_force": apply_comb_tol_to_force,
        "transition_miss_beats": transition_miss_beats,
        "transition_resume_beats": transition_resume_beats,
        "transition_guard": transition_guard,
        "trim_recalc": trim_recalc,
        "sub_trunk_min_sec": sub_trunk_min_sec,
        "silence_threshold_db": silence_threshold_db,
        "trim_short_bursts": trim_short_bursts,
        "short_burst_max_sec": short_burst_max_sec,
        "short_burst_min_gap_sec": short_burst_min_gap_sec,
        "channel": channel,
    }


# ---------------------------------------------------------------------------
# 3. 模板显式占位锚点常量
# ---------------------------------------------------------------------------

SETTINGS_CSS_ANCHOR = "  /* __SETTINGS_CSS_ANCHOR__ */"
SETTINGS_HEADER_BTN_ANCHOR = "    <!-- __SETTINGS_HEADER_BTN_ANCHOR__ -->"
SETTINGS_MODALS_ANCHOR = "<!-- __SETTINGS_MODALS_ANCHOR__ -->"
SETTINGS_CONFIG_JS_ANCHOR = "// __SETTINGS_CONFIG_JS_ANCHOR__"
SETTINGS_CONTROLLER_JS_ANCHOR = "// __SETTINGS_CONTROLLER_JS_ANCHOR__"


# ---------------------------------------------------------------------------
# 4. 前端设置专属样式、DOM 与交互脚本
# ---------------------------------------------------------------------------

_SETTINGS_CSS = """  /* 顶栏当前模式胶囊与参数修改标记 */
  .combo-status-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: #79c0ff;
    box-shadow: 0 0 7px rgba(121, 192, 255, 0.85);
    flex-shrink: 0;
  }
  .current-mode-pill {
    font-size: 14px;
    color: #d0e2ff;
    background: transparent;
    border: none;
    padding: 0;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    line-height: 1;
  }
  .current-mode-pill b {
    color: var(--text);
    font-weight: 600;
  }
  .custom-param-tag {
    font-size: 11px;
    font-weight: 600;
    color: var(--amber);
    background: rgba(210, 153, 34, 0.18);
    border: 1px solid rgba(210, 153, 34, 0.48);
    border-radius: 999px;
    padding: 2px 7px;
    margin-left: 2px;
    line-height: 1.15;
  }
  .combo-sep {
    width: 1px;
    height: 14px;
    background: rgba(88, 166, 255, 0.38);
    flex-shrink: 0;
  }
  .combo-action {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    font-size: 14px;
    font-weight: 600;
    color: #79c0ff;
    line-height: 1;
    transition: color 0.16s ease;
  }
  .header-combo-btn:hover:not(:disabled) .combo-action {
    color: var(--text);
  }

  /* Android 分组标题与圆角卡片岛 */
  .m3-group {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .m3-group-header {
    padding: 0 12px;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .m3-group-title {
    font-size: 13px;
    font-weight: 600;
    color: #8ab4f8;
    letter-spacing: 0.3px;
  }
  .m3-group-desc {
    font-size: 11.5px;
    color: #7d8794;
    line-height: 1.45;
  }
  .m3-card {
    background: #1e2026; /* 设置卡片深空灰背景，提升与弹窗底色层次 */
    border: 1px solid rgba(255, 255, 255, 0.38); /* 柔和半透白过渡边框 */
    border-radius: 18px;
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }
  .m3-item {
    padding: 15px 18px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 18px;
    transition: background 0.15s ease;
  }
  .m3-item + .m3-item {
    border-top: 1px solid rgba(255, 255, 255, 0.06);
  }
  .m3-item:hover {
    background: rgba(255, 255, 255, 0.025);
  }
  .m3-item.is-disabled {
    opacity: 0.52;
    cursor: not-allowed;
  }
  .m3-item.is-disabled:hover {
    background: transparent;
  }
  .setting-info {
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-width: 0;
    flex: 1;
  }
  .setting-title {
    font-size: 14.5px;
    font-weight: 500;
    color: #FFFFFF;
    line-height: 1.4;
  }
  .setting-sub {
    font-size: 12.5px;
    color: #9aa4b2;
    line-height: 1.5;
  }
  .setting-risk {
    font-size: 11.5px;
    color: #d4a756;
    line-height: 1.48;
    margin-top: 3px;
    padding-left: 8px;
    border-left: 2px solid rgba(212, 167, 86, 0.45);
  }

  /* 三拍子与 3:2 三连音消歧说明气泡与小链接 */
  .setting-help-link {
    font-size: 11.5px;
    color: #8ab4f8;
    cursor: pointer;
    user-select: none;
    line-height: 1.2;
    margin-left: 6px;
    white-space: nowrap;
    text-decoration: underline;
    text-underline-offset: 2px;
    font-weight: normal;
    transition: color 0.12s ease;
  }
  .setting-help-link:hover,
  .setting-help-link:focus-visible {
    color: #a8c7fa;
    outline: none;
  }
  .settings-help-popover {
    position: fixed;
    z-index: 1050;
    display: none;
    pointer-events: auto;
    width: max-content;
    max-width: min(390px, calc(100vw - 32px));
    background: rgba(22, 24, 28, 0.98);
    border: 1px solid rgba(138, 180, 248, 0.38);
    border-radius: 12px;
    padding: 12px 14px;
    box-shadow: 0 14px 36px rgba(0, 0, 0, 0.65);
    backdrop-filter: blur(8px);
  }
  .settings-help-popover-title {
    font-size: 13px;
    font-weight: 600;
    color: #FFFFFF;
    margin-bottom: 8px;
    padding-bottom: 6px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  }
  .settings-help-popover-body {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .settings-help-popover-item {
    font-size: 12px;
    line-height: 1.55;
    color: #cbd5e1;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .settings-help-popover-tag {
    font-size: 11.5px;
    font-weight: 600;
    color: #8ab4f8;
    display: inline-flex;
    align-items: center;
  }
  .settings-help-popover-tag::before {
    content: "•";
    margin-right: 5px;
    color: #8ab4f8;
  }
  .settings-help-popover-item b {
    color: #FFFFFF;
  }
  .settings-help-popover-item i {
    font-style: normal;
    color: #f6ad55;
  }

  /* Android 单选列表行与双环圆点 */
  .m3-radio-item {
    cursor: pointer;
    user-select: none;
    position: relative;
  }
  .m3-radio-item input[type="radio"] {
    position: absolute;
    opacity: 0;
    width: 0;
    height: 0;
    pointer-events: none;
  }
  .m3-radio-item:hover {
    background: rgba(138, 180, 248, 0.06);
  }
  .m3-radio-item.selected {
    background: rgba(138, 180, 248, 0.10);
  }
  .m3-radio-item.selected .setting-title {
    color: #a8c7fa;
    font-weight: 600;
  }
  .m3-radio-mark {
    width: 20px;
    height: 20px;
    border-radius: 50%;
    border: 2px solid #8b949e;
    flex-shrink: 0;
    position: relative;
    transition: border-color 0.16s ease;
  }
  .m3-radio-mark::after {
    content: "";
    position: absolute;
    inset: 3px;
    border-radius: 50%;
    background: #8ab4f8;
    transform: scale(0);
    transition: transform 0.16s cubic-bezier(0.2, 0, 0, 1);
  }
  .m3-radio-item.selected .m3-radio-mark {
    border-color: #8ab4f8;
  }
  .m3-radio-item.selected .m3-radio-mark::after {
    transform: scale(1);
  }

  /* Material 3 胶囊滑动开关 */
  .toggle-switch {
    position: relative;
    display: inline-block;
    width: 50px;
    height: 28px;
    flex-shrink: 0;
    cursor: pointer;
  }
  .toggle-switch input {
    opacity: 0;
    width: 0;
    height: 0;
  }
  .toggle-slider {
    position: absolute;
    inset: 0;
    background: #1c1c1c;
    border: 2px solid #6e6e6e;
    border-radius: 999px;
    transition: background 0.2s ease, border-color 0.2s ease;
  }
  .toggle-slider::before {
    content: "";
    position: absolute;
    width: 16px;
    height: 16px;
    left: 4px;
    top: 4px;
    background: #9aa4b2;
    border-radius: 50%;
    transition: transform 0.2s cubic-bezier(0.2, 0, 0, 1), width 0.2s ease, height 0.2s ease, top 0.2s ease, background 0.2s ease;
  }
  .toggle-switch input:checked + .toggle-slider {
    background: #8ab4f8;
    border-color: #8ab4f8;
  }
  .toggle-switch input:checked + .toggle-slider::before {
    width: 20px;
    height: 20px;
    top: 2px;
    transform: translateX(20px);
    background: #000000;
  }
  .toggle-switch.is-disabled {
    cursor: not-allowed;
    pointer-events: none;
  }

  /* 二级嵌套设置区与 Material 3 方形勾框 (Checkbox) */
  .m3-sub-group {
    border-top: 1px solid rgba(255, 255, 255, 0.06);
    background: rgba(0, 0, 0, 0.22); /* 适度透光暗层，避免深死黑 */
    display: flex;
    flex-direction: column;
  }
  .m3-sub-group + .m3-item {
    border-top: 1px solid rgba(255, 255, 255, 0.06);
  }
  .m3-item.m3-sub-item {
    padding: 13px 18px 13px 32px;
    position: relative;
  }
  .m3-item.m3-sub-item::before {
    content: "";
    position: absolute;
    left: 18px;
    top: 12px;
    bottom: 12px;
    width: 2px;
    border-radius: 2px;
    background: rgba(138, 180, 248, 0.28);
  }
  .m3-item.m3-sub2-item {
    padding: 12px 18px 12px 46px;
    position: relative;
    background: rgba(0, 0, 0, 0.28);
  }
  .m3-item.m3-sub2-item::before {
    content: "";
    position: absolute;
    left: 32px;
    top: 11px;
    bottom: 11px;
    width: 2px;
    border-radius: 2px;
    background: rgba(138, 180, 248, 0.18);
  }
  .m3-check-item {
    cursor: pointer;
    user-select: none;
  }
  .m3-check-item.is-disabled {
    cursor: not-allowed;
  }
  .m3-checkbox-wrap {
    position: relative;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 20px;
    height: 20px;
    flex-shrink: 0;
  }
  .m3-checkbox-wrap input[type="checkbox"] {
    position: absolute;
    opacity: 0;
    width: 0;
    height: 0;
    pointer-events: none;
  }
  .m3-checkbox-box {
    width: 20px;
    height: 20px;
    border-radius: 5px;
    border: 2px solid #8b949e;
    background: rgba(0, 0, 0, 0.55);
    position: relative;
    transition: background 0.16s ease, border-color 0.16s ease;
  }
  .m3-check-item:hover:not(.is-disabled) .m3-checkbox-box {
    border-color: #a8c7fa;
  }
  .m3-checkbox-wrap input[type="checkbox"]:checked + .m3-checkbox-box {
    background: #8ab4f8;
    border-color: #8ab4f8;
  }
  .m3-checkbox-box::after {
    content: "";
    position: absolute;
    left: 5px;
    top: 1px;
    width: 5px;
    height: 10px;
    border-right: 2.3px solid #000000;
    border-bottom: 2.3px solid #000000;
    transform: rotate(45deg) scale(0);
    transition: transform 0.14s cubic-bezier(0.2, 0, 0, 1);
  }
  .m3-checkbox-wrap input[type="checkbox"]:checked + .m3-checkbox-box::after {
    transform: rotate(45deg) scale(1);
  }

  /* Material 圆角胶囊步进调节器 */
  .stepper {
    display: inline-flex;
    align-items: center;
    background: #16161a; /* 胶囊步进器深灰外框 */
    border: 1px solid rgba(255, 255, 255, 0.10);
    border-radius: 999px;
    padding: 3px;
    gap: 2px;
    flex-shrink: 0;
    user-select: none;
  }
  .stepper-btn {
    background: #1c1c1c;
    border: none;
    color: #FFFFFF;
    width: 28px;
    height: 28px;
    border-radius: 50%;
    font-size: 16px;
    font-weight: 500;
    cursor: pointer;
    touch-action: manipulation;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    transition: background 0.14s ease, color 0.14s ease, transform 0.1s ease;
  }
  .stepper-btn:hover:not(:disabled) {
    background: rgba(138, 180, 248, 0.22);
    color: #8ab4f8;
  }
  .stepper-btn:active:not(:disabled),
  .stepper-btn.is-pressing:not(:disabled) {
    background: rgba(138, 180, 248, 0.28);
    color: #8ab4f8;
    transform: scale(0.92);
  }
  .stepper-btn:disabled {
    opacity: 0.28;
    background: transparent;
    cursor: not-allowed;
  }
  .stepper-val {
    min-width: 68px;
    padding: 0 8px;
    text-align: center;
    font-size: 13px;
    font-weight: 600;
    color: #FFFFFF;
  }"""

_SETTINGS_HEADER_BTN_HTML = (
    '    <button type="button" class="header-combo-btn" id="openSettingsBtn" title="查看当前测算模式并打开参数设置">\n'
    '      <span class="combo-status-dot" aria-hidden="true"></span>\n'
    '      <span class="current-mode-pill" id="currentModePill"><span>当前模式：<b>全部</b></span></span>\n'
    '      <span class="combo-sep" aria-hidden="true"></span>\n'
    # 2026-11-11：原"太阳"形图标改为线条描边设置齿轮（24 视框，Lucide settings 造型）。
    # 注意：web_gui_lifecycle.py 的 _HEADER_BUTTON_ANCHOR 以本段 HTML 作为注入锚点，两处必须逐字一致。
    '      <span class="combo-action"><svg class="header-btn-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/></svg><span>点按以打开程序设置</span></span>\n'
    '    </button>'
)

_SETTINGS_MODALS_HTML = """<!-- 设置模态弹窗 -->
<div class="modal-backdrop" id="settingsModal" role="dialog" aria-modal="true" aria-labelledby="settingsModalTitle">
  <div class="modal-dialog">
    <div class="modal-header">
      <h2 id="settingsModalTitle">设置</h2>
      <button class="modal-close-btn" id="closeSettingsModalBtn" title="关闭">✕</button>
    </div>
    <div class="modal-body">
      <!-- 分组: BPM 计算模式 -->
      <section class="m3-group">
        <div class="m3-group-header">
          <span class="m3-group-title">BPM 计算模式</span>
        </div>
        <div class="m3-card" id="modeRadioList">
          <label class="m3-item m3-radio-item" data-mode-opt="exact">
            <input type="radio" name="bpmModeRadio" value="exact">
            <div class="setting-info">
              <div class="setting-title">Malody模式</div>
              <div class="setting-sub">Malody官方原版的计算方法。</div>
            </div>
            <span class="m3-radio-mark" aria-hidden="true"></span>
          </label>
          <label class="m3-item m3-radio-item" data-mode-opt="enhanced_v3">
            <input type="radio" name="bpmModeRadio" value="enhanced_v3">
            <div class="setting-info">
              <div class="setting-title">增强模式</div>
              <div class="setting-sub">使用在Malody原版基础上进行了大幅优化的算法。附带“二次精修”功能。没有原版的7阶量化吸附处理。</div>
            </div>
            <span class="m3-radio-mark" aria-hidden="true"></span>
          </label>
          <label class="m3-item m3-radio-item" data-mode-opt="force_v3">
            <input type="radio" name="bpmModeRadio" value="force_v3">
            <div class="setting-info">
              <div class="setting-title">强制线性回归模式</div>
              <div class="setting-sub">如果歌曲是真人演奏，BPM网格不够稳定，以上两个模式将完全无法计算。此模式使用数学回归算法，强制拟合出主干BPM。</div>
            </div>
            <span class="m3-radio-mark" aria-hidden="true"></span>
          </label>
          <label class="m3-item m3-radio-item selected" data-mode-opt="all">
            <input type="radio" name="bpmModeRadio" value="all" checked>
            <div class="setting-info">
              <div class="setting-title">全部</div>
              <div class="setting-sub">三模式同屏对比。</div>
            </div>
            <span class="m3-radio-mark" aria-hidden="true"></span>
          </label>
        </div>
      </section>

      <!-- 分组: 声道分析策略 -->
      <section class="m3-group">
        <div class="m3-group-header">
          <span class="m3-group-title">声道分析策略</span>
          <span class="m3-group-desc">同时作用于增强模式、强制线性回归模式与起峰包络（Offset）二级视图（Malody模式固定保持原版混音）</span>
        </div>
        <div class="m3-card" id="channelRadioList">
          <label class="m3-item m3-radio-item selected" data-channel-opt="auto">
            <input type="radio" name="channelModeRadio" value="auto" checked>
            <div class="setting-info">
              <div class="setting-title">多声道能量融合</div>
              <div class="setting-sub">对各声道分别提取频段能量后再求均值融合。可避免立体声反相拓宽导致的波形正负抵消，并完整保留环绕声低频（LFE）鼓点。双声道内容一致时与单声道结果完全等价。</div>
            </div>
            <span class="m3-radio-mark" aria-hidden="true"></span>
          </label>
          <label class="m3-item m3-radio-item" data-channel-opt="left">
            <input type="radio" name="channelModeRadio" value="left">
            <div class="setting-info">
              <div class="setting-title">仅分析左声道</div>
              <div class="setting-sub">仅提取左声道计算节拍（大于 2 声道时取前置左声道 L / FL；单声道音频自动使用该唯一声道）。适用于左右声道分轨伴奏、单侧带节拍器或右声道干扰较强的音频。</div>
            </div>
            <span class="m3-radio-mark" aria-hidden="true"></span>
          </label>
          <label class="m3-item m3-radio-item" data-channel-opt="right">
            <input type="radio" name="channelModeRadio" value="right">
            <div class="setting-info">
              <div class="setting-title">仅分析右声道</div>
              <div class="setting-sub">仅提取右声道计算节拍（大于 2 声道时取前置右声道 R / FR；单声道音频自动使用该唯一声道）。适用于左右声道分轨伴奏、单侧带节拍器或左声道干扰较强的音频。</div>
            </div>
            <span class="m3-radio-mark" aria-hidden="true"></span>
          </label>
          <label class="m3-item m3-radio-item" data-channel-opt="downmix">
            <input type="radio" name="channelModeRadio" value="downmix">
            <div class="setting-info">
              <div class="setting-title">传统单声道混音（旧版）</div>
              <div class="setting-sub">先由 FFmpeg 将全部声道按波形线性叠加转换为单声道，再提取节拍包络（仅建议用于与旧版测算结果对比回退）。</div>
              <div class="setting-risk">风险提示：若左右声道含反相立体声效果，正负波形叠加可能削弱鼓点瞬态。多声道环绕声默认混音也会舍弃低音炮（LFE）声道。</div>
            </div>
            <span class="m3-radio-mark" aria-hidden="true"></span>
          </label>
        </div>
      </section>

      <!-- 分组: 音频输入与时长限制 -->
      <section class="m3-group">
        <div class="m3-group-header">
          <span class="m3-group-title">音频输入与时长限制</span>
        </div>
        <div class="m3-card">
          <div class="m3-item">
            <div class="setting-info">
              <div class="setting-title">开始计算前，自动去除音频头尾的静音</div>
              <div class="setting-sub">开启后，在进入三种模式的节拍计算前自动裁去首尾静音（保留 30 毫秒起音保护缓冲），并以裁剪后的有效时长参与 45~600 秒时长判定与占比统计。变速区域时间戳会自动还原至原音频进度条，且不影响起峰包络（Offset）二级界面对原始音频的分析。</div>
            </div>
            <label class="toggle-switch">
              <input type="checkbox" id="chkTrimSilence" checked>
              <span class="toggle-slider"></span>
            </label>
          </div>
          <div class="m3-sub-group" id="silenceSubGroup">
            <div class="m3-item m3-sub-item" id="itemSilenceThreshold">
              <div class="setting-info">
                <div class="setting-title">头尾静音判定阈值（相对曲目峰值分贝）</div>
                <div class="setting-sub">以 10 毫秒短窗均方能量（RMS）计算全曲峰值基准，首尾低于「峰值 + 该负分贝值」的连续区间将被视为静音裁去。</div>
                <div class="setting-risk">风险提示：调得过高（如 -30 dB）可能误裁极轻柔的渐入前奏或渐弱尾奏；调得过低（如 -65 dB）在录音含微弱底噪时可能无法完全去除头尾留白。</div>
              </div>
              <div class="stepper" data-stepper="silenceThresholdDb">
                <button type="button" class="stepper-btn" data-dir="-1" aria-label="减小">−</button>
                <span class="stepper-val" id="val_silenceThresholdDb">-50 dB</span>
                <button type="button" class="stepper-btn" data-dir="1" aria-label="增大">+</button>
              </div>
            </div>
            <label class="m3-item m3-sub-item m3-check-item" id="itemTrimShortBursts">
              <div class="setting-info">
                <div class="setting-title">同时处理开头或结尾孤立的极短时间无效振幅</div>
                <div class="setting-sub">勾选后，若开头响了一下马上恢复静音，或结尾渐弱落入静音阈值后又突然出现孤立鼓点/杂音，将一并作为无效边缘剔除（内置防误裁保护，不影响正常鼓棒预备拍与连续稀疏前奏）。</div>
                <div class="setting-risk">边界说明：仅剔除与正曲主体之间存在低于静音阈值断层的首尾孤立短声。若结尾渐弱尚未低于阈值，可适当调高上方分贝阈值配合使用。</div>
              </div>
              <span class="m3-checkbox-wrap">
                <input type="checkbox" id="chkTrimShortBursts">
                <span class="m3-checkbox-box" aria-hidden="true"></span>
              </span>
            </label>
            <div class="m3-item m3-sub2-item is-disabled" id="itemShortBurstMax">
              <div class="setting-info">
                <div class="setting-title">孤立短振幅最大持续时长（秒）</div>
                <div class="setting-sub">首尾孤立发声段（含混响拖尾）不超过该时长时，视为极短无效振幅予以剔除。</div>
              </div>
              <div class="stepper" data-stepper="shortBurstMaxSec">
                <button type="button" class="stepper-btn" data-dir="-1" aria-label="减小" disabled>−</button>
                <span class="stepper-val" id="val_shortBurstMaxSec">1.5 s</span>
                <button type="button" class="stepper-btn" data-dir="1" aria-label="增大" disabled>+</button>
              </div>
            </div>
            <div class="m3-item m3-sub2-item is-disabled" id="itemShortBurstMinGap">
              <div class="setting-info">
                <div class="setting-title">与正曲主体的最小静音间隔（秒）</div>
                <div class="setting-sub">短于该间隔的节拍空隙（如鼓棒预备拍）会自动桥接并入正曲保护。达到该静音间隔的外侧短振幅才会被剔除。</div>
              </div>
              <div class="stepper" data-stepper="shortBurstMinGapSec">
                <button type="button" class="stepper-btn" data-dir="-1" aria-label="减小" disabled>−</button>
                <span class="stepper-val" id="val_shortBurstMinGapSec">0.8 s</span>
                <button type="button" class="stepper-btn" data-dir="1" aria-label="增大" disabled>+</button>
              </div>
            </div>
          </div>
          <div class="m3-item">
            <div class="setting-info">
              <div class="setting-title">我愿意承担精确度降低和死机的风险，以测量超短或超长音频</div>
              <div class="setting-sub">开启后将取消 45~600 秒的音频时长限制。</div>
              <div class="setting-risk">风险提示：超短音频因有效节拍过少极易测算失准或无法计算；超长音频会占用大量内存与运算时间，可能导致程序卡顿甚至死机。</div>
            </div>
            <label class="toggle-switch">
              <input type="checkbox" id="chkAllowAnyDuration">
              <span class="toggle-slider"></span>
            </label>
          </div>
        </div>
      </section>

      <!-- 分组: 仅限增强模式 -->
      <section class="m3-group">
        <div class="m3-group-header">
          <span class="m3-group-title">仅限增强模式</span>
        </div>
        <div class="m3-card">
          <div class="m3-item">
            <div class="setting-info">
              <div class="setting-title">展示疑似三拍子曲目、3:2三连音消歧曲目的备选计算结果<a href="javascript:void(0)" class="setting-help-link" id="helpTripleLink" role="button" aria-expanded="false" aria-describedby="settingsHelpPopover">什么是三拍子、什么是3:2消歧？</a></div>
              <div class="setting-sub">开启后，当增强模式检测到疑似三拍子或触发 3:2 三连音消歧时，直接在结果卡片上同时展示主 BPM 与原始 ×1.5、÷1.5 备选值供一键复制。关闭则隐藏备选值（仅控制前端卡片展示，不会停用底层检测三拍子、3:2逻辑）。</div>
              <div class="setting-risk">风险提示：强附点切分的 6/8、3/4 拍曲目与 4/4（12/8）三连音曲目在包络与自相关上呈 3:2 声学同构，增强模式可能误收敛至 ÷1.5 一侧（如 192 BPM 误判为 128 BPM）。建议保持开启。一旦卡片出现「疑似三拍子 / 3:2 消歧触发」提示（或与 Malody 模式相差 1.5 倍），可结合听感核对并选用备选栏中的 ×1.5 或 ÷1.5 数值。</div>
            </div>
            <label class="toggle-switch">
              <input type="checkbox" id="chkOptimizeTriple">
              <span class="toggle-slider"></span>
            </label>
          </div>
          <div class="m3-item">
            <div class="setting-info">
              <div class="setting-title">候选周期后续拍峰平均强度保留比</div>
              <div class="setting-sub">要求候选节拍后续各拍的平均脉冲强度至少达到第一拍强度的该比例。</div>
              <div class="setting-risk">风险提示：调得过高会误杀鼓点轻柔或音量起伏大的正常歌曲导致“无法计算”；调得过低则容易混入杂乱噪声伪周期导致测错 BPM。</div>
            </div>
            <div class="stepper" data-stepper="acfDecayRatio">
              <button type="button" class="stepper-btn" data-dir="-1" aria-label="减小">−</button>
              <span class="stepper-val" id="val_acfDecayRatio">0.70</span>
              <button type="button" class="stepper-btn" data-dir="1" aria-label="增大">+</button>
            </div>
          </div>
          <div class="m3-item">
            <div class="setting-info">
              <div class="setting-title">慢速长周期取代快速短周期所需的强度倍率</div>
              <div class="setting-sub">更慢的节拍周期（低 BPM）平均强度必须超过当前最快最优周期（高 BPM）的该倍数，才会被选为主速度。</div>
              <div class="setting-risk">风险提示：调得过低容易把正常快歌误判成半速（×0.5）；调得过高则容易把慢歌或强弱拍交替曲目误锁在倍速（×2）。</div>
            </div>
            <div class="stepper" data-stepper="candUpdateRatio">
              <button type="button" class="stepper-btn" data-dir="-1" aria-label="减小">−</button>
              <span class="stepper-val" id="val_candUpdateRatio">1.25</span>
              <button type="button" class="stepper-btn" data-dir="1" aria-label="增大">+</button>
            </div>
          </div>
          <!-- [新增 2026-11-14] 分段门控亚谐波前瞻：240~300 BPM 折半对比度门限 -->
          <div class="m3-item">
            <div class="setting-info">
              <div class="setting-title">240~300 BPM 高速候选折半所需的强弱拍对比度门限</div>
              <div class="setting-sub">当候选初测落入 240~300 BPM 时，要求正反拍脉冲强度对比度达到该门限（默认 1.10 约对应反拍比正拍弱 6 dB）才允许折半至 120~150 BPM（&gt;300 BPM 始终自动折半，220~240 BPM 原样保留不折半；调至 1.00 为遇到半速候选即折半，调至 2.00 为几乎不折半）。</div>
              <div class="setting-risk">风险提示：调得过低容易把每拍重音均匀的 240~300 BPM 真高速曲目误折半成 120~150 BPM；调得过高则容易让本为 120~150 BPM 但八分反拍较强的常速曲目卡在 240~300 BPM 倍速。</div>
            </div>
            <div class="stepper" data-stepper="halveContrastRatio">
              <button type="button" class="stepper-btn" data-dir="-1" aria-label="减小">−</button>
              <span class="stepper-val" id="val_halveContrastRatio">1.10</span>
              <button type="button" class="stepper-btn" data-dir="1" aria-label="增大">+</button>
            </div>
          </div>
          <div class="m3-item">
            <div class="setting-info">
              <div class="setting-title">自相关谐波峰值追踪与线性回归容差（毫秒）</div>
              <div class="setting-sub">在自相关图谱上沿着每一拍整数倍位置寻找峰值并拟合斜率时，允许左右偏移的毫秒范围。</div>
              <div class="setting-risk">风险提示：调得过小会让轻微速度波动的曲目提前断锁失败；调得过大容易吸附到切分音或邻近杂峰，拉偏 BPM 小数精度。</div>
            </div>
            <div class="stepper" data-stepper="trackTol">
              <button type="button" class="stepper-btn" data-dir="-1" aria-label="减小">−</button>
              <span class="stepper-val" id="val_trackTol">±8 ms</span>
              <button type="button" class="stepper-btn" data-dir="1" aria-label="增大">+</button>
            </div>
          </div>
        </div>
      </section>

      <!-- 分组: 节拍网格判定 -->
      <section class="m3-group">
        <div class="m3-group-header">
          <span class="m3-group-title">节拍网格判定</span>
          <span class="m3-group-desc">默认作用于增强模式。开启下方同步开关后一并作用于强制线性回归模式。</span>
        </div>
        <div class="m3-card">
          <div class="m3-item">
            <div class="setting-info">
              <div class="setting-title">全曲真实起音踩拍判定的允许偏差半宽（毫秒）</div>
              <div class="setting-sub">在全曲时间轴上判定真实鼓点/起音是否踩在理论节拍点上（及评估 240~300 BPM 折半与 3:2 消歧能量）时的左右允许偏差毫秒数。</div>
              <div class="setting-risk">风险提示：调得过小会将轻微摇摆判为漏拍、拉低覆盖率；调得过大则会把非节拍重音误算进网格，掩盖真实不稳定度或影响 240~300 BPM 折半与 3:2 消歧判断。</div>
            </div>
            <div class="stepper" data-stepper="combTol">
              <button type="button" class="stepper-btn" data-dir="-1" aria-label="减小">−</button>
              <span class="stepper-val" id="val_combTol">±35 ms</span>
              <button type="button" class="stepper-btn" data-dir="1" aria-label="增大">+</button>
            </div>
          </div>
          <div class="m3-item">
            <div class="setting-info">
              <div class="setting-title">将「全曲真实起音踩拍判定的允许偏差半宽」应用于强制线性回归模式</div>
              <div class="setting-sub">开启后，上一项设置的毫秒偏差范围将同步用于强制线性回归模式的节拍命中与不稳定度评估。关闭时强制模式保持默认 35 毫秒。</div>
              <div class="setting-risk">风险提示：对真人演奏曲目若将窗口调得过窄并同步应用，可能导致强制模式命中拍过少而使不稳定度升满（1.000）。</div>
            </div>
            <label class="toggle-switch">
              <input type="checkbox" id="chkApplyCombToForce">
              <span class="toggle-slider"></span>
            </label>
          </div>
        </div>
      </section>

      <!-- 分组: 中途变速与恢复原速边界净化 -->
      <section class="m3-group">
        <div class="m3-group-header">
          <span class="m3-group-title">中途变速与a tempo（恢复原速）边界净化</span>
          <span class="m3-group-desc">同时作用于增强模式与强制线性回归模式</span>
        </div>
        <div class="m3-card">
          <div class="m3-item">
            <div class="setting-info">
              <div class="setting-title">判定歌曲中途已进入变速所需的连续漏配拍数</div>
              <div class="setting-sub">连续多少拍未匹配到真实起音后，视为歌曲已发生中途变速并开始剔除该过渡段（八分加密、正反拍半速 Half-time、≥80 BPM 每小节一击及 ≤150 BPM 十六分切分等 2^x 同源疏密变化会自动豁免，不作漏拍断锁）。</div>
              <div class="setting-risk">风险提示：调得过小（如 2 拍）容易把正常的短暂停顿或空拍误判为变速。默认 4 拍可兼容“每小节一拍底鼓 + 自由切分人声”的常速编曲，但若曲中含有与原拍同相切入的 5:4 / 3:4 复节奏桥段（每 4 拍重合一次、中间仅连漏 3 拍），需调至 ≤ 3 拍才会触发剔除。</div>
            </div>
            <div class="stepper" data-stepper="transitionMissBeats">
              <button type="button" class="stepper-btn" data-dir="-1" aria-label="减小">−</button>
              <span class="stepper-val" id="val_transitionMissBeats">4 拍</span>
              <button type="button" class="stepper-btn" data-dir="1" aria-label="增大">+</button>
            </div>
          </div>
          <div class="m3-item">
            <div class="setting-info">
              <div class="setting-title">变速后判定已经a tempo所需的连续命中拍数</div>
              <div class="setting-sub">被判定变速后，重新连续踩准原速或 2^x 同源网格多少拍（且小节内无异速复节奏伪峰）才视为已经a tempo并恢复统计。</div>
              <div class="setting-risk">风险提示：调得过小可能把变速段里偶然撞上网格的散点误认为已恢复原速；调得过大则会把恢复原速后的正常稳定段落也多裁掉几拍。</div>
            </div>
            <div class="stepper" data-stepper="transitionResumeBeats">
              <button type="button" class="stepper-btn" data-dir="-1" aria-label="减小">−</button>
              <span class="stepper-val" id="val_transitionResumeBeats">3 拍</span>
              <button type="button" class="stepper-btn" data-dir="1" aria-label="增大">+</button>
            </div>
          </div>
          <div class="m3-item">
            <div class="setting-info">
              <div class="setting-title">变速与a tempo交界处的窗口内缩时间（秒）</div>
              <div class="setting-sub">设置检测到变速或 a tempo 时，从变速起点向开头方向、从恢复原速起点向结尾方向额外裁掉的交界缓冲秒数。此选项用于净化匹配覆盖率与强制模式不稳定度，且在开启下方「分段错相重锁与二次精修」时还会同步从参与二轮重算的净稳定段两头削去渐快/渐慢入弯与出弯微漂边缘，直接提纯最终 BPM 小数位精度（开启二次精修时调至 0.0 秒仅剔除变速失锁段本体，不额外内缩缓冲；关闭二次精修时调至 0.0 秒则完全关闭裁剪）。</div>
              <div class="setting-risk">风险提示：调得过小可能让渐快/渐慢交界处尚未断锁的微漂拍混入净稳定段拉偏小数位；调得过大则可能把净稳定段削短至不足二次重算时长门槛（增强：主段 16.5 秒、副段 8~15 秒 / 强制：8 秒）而退回初测 BPM，甚至裁空有效节拍导致覆盖率归零。</div>
            </div>
            <div class="stepper" data-stepper="transitionGuard">
              <button type="button" class="stepper-btn" data-dir="-1" aria-label="减小">−</button>
              <span class="stepper-val" id="val_transitionGuard">1.0 s</span>
              <button type="button" class="stepper-btn" data-dir="1" aria-label="增大">+</button>
            </div>
          </div>
        </div>
      </section>

      <!-- 分组: 分段错相重锁与二次精修 -->
      <section class="m3-group">
        <div class="m3-group-header">
          <span class="m3-group-title">分段错相重锁与二次精修</span>
          <span class="m3-group-desc">主开关同时作用于增强模式与强制线性回归模式，下方副主干时长仅作用于增强模式</span>
        </div>
        <div class="m3-card">
          <div class="m3-item">
            <div class="setting-info">
              <div class="setting-title">对中途错相或含变速过渡段的曲目执行分段重锁与剔除变速二次重算</div>
              <div class="setting-sub">开启后，当歌曲中途出现停顿错拍、插拍，或经历变速后a tempo时，会自动为各稳定段重新对齐节拍相位（兼容 0.25x~4x 编曲疏密同源变化）。当剔除变速段后的最长净稳定段足够长时（增强模式主段需满 16.5 秒，并联合其余达到下方设定时长的副主干各自独立自相关做 2^x 同源跨段回归精修；强制模式需满 8 秒），将重算净化主干 BPM 小数位与不稳定度，且在 2^x 同源时自动锁定初测主八度不跳倍频。全程匀速稳定的曲目会自动跳过，不受影响。</div>
              <div class="setting-risk">风险提示：若上方变速判定条件调得过于敏感，把正常的自由切分或短暂留白误判成了变速，可能会掩盖真实的跨段跑拍，或因将净稳定段切碎至不足重算时长门槛（增强主段 16.5 秒 / 强制 8 秒）而保留初测结果。</div>
            </div>
            <label class="toggle-switch">
              <input type="checkbox" id="chkTrimRecalc" checked>
              <span class="toggle-slider"></span>
            </label>
          </div>
          <div class="m3-sub-group" id="trimRecalcSubGroup">
            <div class="m3-item m3-sub-item" id="itemSubTrunkMinSec">
              <div class="setting-info">
                <div class="setting-title">联合回归副主干最小净时长（秒）</div>
                <div class="setting-sub">设置增强模式在二次精修时，除满 16.5 秒的最长主稳定段之外，其余被变速段切开的净稳定段至少需要达到多少秒，才允许作为独立副主干参与跨段联合自相关与回归精修（上限对齐增强模式 15 秒标准滑窗长度）。仅作用于增强模式，强制线性回归模式不受此项影响。</div>
                <div class="setting-risk">风险提示：调小（如 8~10 秒）可把被频繁变速切碎的较短稳定段也纳入联合精修以挽救有效时长，但过短片段的自相关抗噪力下降，可能混入轻微局部抖动；调大（如 15 秒）则仅接纳完整达到标准滑窗长度的高纯度长净段，精度最稳健但会舍弃较短的分散主干。</div>
              </div>
              <div class="stepper" data-stepper="subTrunkMinSec">
                <button type="button" class="stepper-btn" data-dir="-1" aria-label="减小">−</button>
                <span class="stepper-val" id="val_subTrunkMinSec">15 s</span>
                <button type="button" class="stepper-btn" data-dir="1" aria-label="增大">+</button>
              </div>
            </div>
          </div>
        </div>
      </section>
    </div>
    <div class="modal-footer">
      <button type="button" class="m3-btn m3-btn-text" id="resetSettingsBtn">恢复默认</button>
      <div class="modal-footer-right">
        <button type="button" class="m3-btn m3-btn-tonal" id="cancelSettingsBtn">取消</button>
        <button type="button" class="m3-btn m3-btn-filled" id="saveSettingsBtn">保存并应用</button>
      </div>
    </div>
  </div>
</div>

<!-- 什么是三拍子、什么是3:2消歧说明气泡浮层 -->
<div class="settings-help-popover" id="settingsHelpPopover" role="tooltip" aria-hidden="true">
  <div class="settings-help-popover-title">什么是三拍子、什么是 3:2 消歧？</div>
  <div class="settings-help-popover-body">
    <div class="settings-help-popover-item">
      <span class="settings-help-popover-tag">三拍子</span>
      <span>指 <b>3/4、3/8、6/8</b> 拍等强拍/重音周期出现在每 3 的倍数拍上的曲目。</span>
    </div>
    <div class="settings-help-popover-item">
      <span class="settings-help-popover-tag">3:2 消歧</span>
      <span>对于 <b>4/4</b> 拍但全曲主要以三连音律动为主的曲目（典例：<i>《沸き上がる闘志》</i>），算法容易误吸附至三连音微节奏上，导致测出 BPM 错误变为 <b>1.5 倍（或 2/3 倍）</b>。3:2消歧即程序对此情况的纠偏处理机制，此处直接沿用了底层算法命名。</span>
    </div>
  </div>
</div>

<!-- 非空队列清空确认弹窗 -->
<div class="modal-backdrop confirm-layer" id="confirmClearModal" role="dialog" aria-modal="true">
  <div class="modal-dialog confirm-dialog">
    <div class="modal-header">
      <h2>确认清空队列？</h2>
    </div>
    <div class="modal-body">
      <p style="font-size:14px; line-height:1.6; color:#FFFFFF;">需要清空当前测算队列才可应用新设置。立即清空？</p>
    </div>
    <div class="modal-footer" style="justify-content:flex-end;">
      <div class="modal-footer-right">
        <button type="button" class="m3-btn m3-btn-tonal" id="cancelClearConfirmBtn">取消</button>
        <button type="button" class="m3-btn m3-btn-danger" id="confirmClearQueueBtn">清空并应用</button>
      </div>
    </div>
  </div>
</div>"""


def _build_settings_config_js() -> str:
    default_json = json.dumps(DEFAULT_UI_SETTINGS, ensure_ascii=False, indent=2)
    return f"""const DEFAULT_SETTINGS = Object.freeze({default_json});

function createDefaultSettings() {{
  return Object.assign({{}}, DEFAULT_SETTINGS);
}}

const MODE_DISPLAY_NAMES = {{
  exact: "Malody模式",
  enhanced_v3: "增强模式",
  force_v3: "强制线性回归模式",
  all: "全部",
}};

const CHANNEL_PILL_LABELS = {{
  left: "只分析左声道",
  right: "只分析右声道",
  downmix: "传统单声道混音",
}};

const STEPPER_SPECS = {{
  silenceThresholdDb: {{ min: {int(MIN_SILENCE_THRESHOLD_DB)}, max: {int(MAX_SILENCE_THRESHOLD_DB)}, step: 5, decimals: 0, fmt: (v) => `${{Math.round(v)}} dB` }},
  shortBurstMaxSec: {{ min: {MIN_SHORT_BURST_MAX_SEC:.1f}, max: {MAX_SHORT_BURST_MAX_SEC:.1f}, step: 0.1, decimals: 1, fmt: (v) => `${{v.toFixed(1)}} s` }},
  shortBurstMinGapSec: {{ min: {MIN_SHORT_BURST_MIN_GAP_SEC:.1f}, max: {MAX_SHORT_BURST_MIN_GAP_SEC:.1f}, step: 0.1, decimals: 1, fmt: (v) => `${{v.toFixed(1)}} s` }},
  acfDecayRatio: {{ min: {ACF_DECAY_RATIO_BOUNDS[1]:.2f}, max: {ACF_DECAY_RATIO_BOUNDS[2]:.2f}, step: 0.05, decimals: 2, fmt: (v) => v.toFixed(2) }},
  candUpdateRatio: {{ min: {CAND_UPDATE_RATIO_BOUNDS[1]:.2f}, max: {CAND_UPDATE_RATIO_BOUNDS[2]:.2f}, step: 0.05, decimals: 2, fmt: (v) => v.toFixed(2) }},
  halveContrastRatio: {{ min: {HALVE_CONTRAST_RATIO_BOUNDS[1]:.2f}, max: {HALVE_CONTRAST_RATIO_BOUNDS[2]:.2f}, step: 0.02, decimals: 2, fmt: (v) => v.toFixed(2) }},
  trackTol: {{ min: {TRACK_TOL_BOUNDS[1]}, max: {TRACK_TOL_BOUNDS[2]}, step: 1, decimals: 0, fmt: (v) => `±${{Math.round(v)}} ms` }},
  combTol: {{ min: {COMB_TOL_BOUNDS[1]}, max: {COMB_TOL_BOUNDS[2]}, step: 1, decimals: 0, fmt: (v) => `±${{Math.round(v)}} ms` }},
  transitionMissBeats: {{ min: {TRANSITION_MISS_BEATS_BOUNDS[1]}, max: {TRANSITION_MISS_BEATS_BOUNDS[2]}, step: 1, decimals: 0, fmt: (v) => `${{Math.round(v)}} 拍` }},
  transitionResumeBeats: {{ min: {TRANSITION_RESUME_BEATS_BOUNDS[1]}, max: {TRANSITION_RESUME_BEATS_BOUNDS[2]}, step: 1, decimals: 0, fmt: (v) => `${{Math.round(v)}} 拍` }},
  transitionGuard: {{ min: {TRANSITION_GUARD_BOUNDS[1]:.1f}, max: {TRANSITION_GUARD_BOUNDS[2]:.1f}, step: 0.1, decimals: 1, fmt: (v) => `${{v.toFixed(1)}} s` }},
  subTrunkMinSec: {{ min: {SUB_TRUNK_MIN_SEC_BOUNDS[1]}, max: {SUB_TRUNK_MIN_SEC_BOUNDS[2]}, step: 1, decimals: 0, fmt: (v) => `${{Math.round(v)}} s` }},
  offsetInitSec: {{ min: {OFFSET_INIT_SEC_BOUNDS[1]}, max: {OFFSET_INIT_SEC_BOUNDS[2]}, step: 2, decimals: 0, fmt: (v) => `${{Math.round(v)}} s` }},
}};"""


_SETTINGS_CONTROLLER_JS = """const currentModePill = document.getElementById("currentModePill");

const settingsModal = document.getElementById("settingsModal");
const openSettingsBtn = document.getElementById("openSettingsBtn");
const closeSettingsModalBtn = document.getElementById("closeSettingsModalBtn");
const resetSettingsBtn = document.getElementById("resetSettingsBtn");
const cancelSettingsBtn = document.getElementById("cancelSettingsBtn");
const saveSettingsBtn = document.getElementById("saveSettingsBtn");

const confirmClearModal = document.getElementById("confirmClearModal");
const cancelClearConfirmBtn = document.getElementById("cancelClearConfirmBtn");
const confirmClearQueueBtn = document.getElementById("confirmClearQueueBtn");

// 三拍子与 3:2 三连音消歧说明气泡控制器元素与状态
const helpTripleLink = document.getElementById("helpTripleLink");
const settingsHelpPopover = document.getElementById("settingsHelpPopover");
let isTriplePopoverPinned = false;
let triplePopoverHideTimer = null;

function hideSettingsHelpPopover() {
  if (!settingsHelpPopover) return;
  if (triplePopoverHideTimer) {
    clearTimeout(triplePopoverHideTimer);
    triplePopoverHideTimer = null;
  }
  isTriplePopoverPinned = false;
  settingsHelpPopover.style.display = "none";
  settingsHelpPopover.setAttribute("aria-hidden", "true");
  if (helpTripleLink) {
    helpTripleLink.setAttribute("aria-expanded", "false");
  }
}

function showSettingsHelpPopover() {
  if (!helpTripleLink || !settingsHelpPopover) return;
  if (triplePopoverHideTimer) {
    clearTimeout(triplePopoverHideTimer);
    triplePopoverHideTimer = null;
  }
  settingsHelpPopover.style.visibility = "hidden";
  settingsHelpPopover.style.display = "block";
  settingsHelpPopover.setAttribute("aria-hidden", "false");
  helpTripleLink.setAttribute("aria-expanded", "true");

  const linkRect = helpTripleLink.getBoundingClientRect();
  const popRect = settingsHelpPopover.getBoundingClientRect();
  const vw = window.innerWidth;
  const vh = window.innerHeight;

  let left = linkRect.left;
  if (left + popRect.width > vw - 12) {
    left = vw - popRect.width - 12;
  }
  left = Math.max(12, left);

  let top = linkRect.bottom + 6;
  if (top + popRect.height > vh - 12) {
    const aboveTop = linkRect.top - popRect.height - 6;
    if (aboveTop >= 12) {
      top = aboveTop;
    }
  }

  settingsHelpPopover.style.left = `${Math.round(left)}px`;
  settingsHelpPopover.style.top = `${Math.round(top)}px`;
  settingsHelpPopover.style.visibility = "visible";
}

function hasCustomAlgorithmParams(settingsObj) {
  const trimOn = !!settingsObj.trimSilence;
  const burstOn = trimOn && !!settingsObj.trimShortBursts;
  const recalcOn = !!settingsObj.trimRecalc;
  for (const key of Object.keys(DEFAULT_SETTINGS)) {
    if (key === "mode" || key === "offsetInitSec" || key === "offsetCurveColor") continue;
    if ((key === "silenceThresholdDb" || key === "trimShortBursts") && !trimOn) continue;
    if ((key === "shortBurstMaxSec" || key === "shortBurstMinGapSec") && !burstOn) continue;
    if (key === "subTrunkMinSec" && !recalcOn) continue;
    if (typeof DEFAULT_SETTINGS[key] === "number") {
      if (Math.abs(settingsObj[key] - DEFAULT_SETTINGS[key]) > 1e-6) return true;
    } else if (settingsObj[key] !== DEFAULT_SETTINGS[key]) {
      return true;
    }
  }
  return false;
}

function updateHeaderModeBadge() {
  const label = MODE_DISPLAY_NAMES[state.settings.mode] || "全部";
  const customTag = hasCustomAlgorithmParams(state.settings)
    ? `<span class="custom-param-tag">已自定义参数</span>`
    : "";
  currentModePill.innerHTML = `<span>当前模式：<b>${escapeHtml(label)}</b></span>${customTag}`;
}

function areSettingsEqual(a, b) {
  for (const key of Object.keys(DEFAULT_SETTINGS)) {
    if (typeof DEFAULT_SETTINGS[key] === "number") {
      if (Math.abs(a[key] - b[key]) > 1e-6) return false;
    } else if (a[key] !== b[key]) {
      return false;
    }
  }
  return true;
}

function areAlgorithmSettingsEqual(a, b) {
  const aTrim = !!a.trimSilence;
  const bTrim = !!b.trimSilence;
  const aBurst = aTrim && !!a.trimShortBursts;
  const bBurst = bTrim && !!b.trimShortBursts;
  const aRecalc = !!a.trimRecalc;
  const bRecalc = !!b.trimRecalc;
  if (aTrim !== bTrim || aBurst !== bBurst || aRecalc !== bRecalc) return false;
  for (const key of Object.keys(DEFAULT_SETTINGS)) {
    if (key === "offsetInitSec" || key === "offsetCurveColor") continue;
    if (key === "trimSilence" || key === "trimShortBursts" || key === "trimRecalc") continue;
    if (key === "silenceThresholdDb" && !aTrim && !bTrim) continue;
    if ((key === "shortBurstMaxSec" || key === "shortBurstMinGapSec") && !aBurst && !bBurst) continue;
    if (key === "subTrunkMinSec" && !aRecalc && !bRecalc) continue;
    if (typeof DEFAULT_SETTINGS[key] === "number") {
      if (Math.abs(a[key] - b[key]) > 1e-6) return false;
    } else if (a[key] !== b[key]) {
      return false;
    }
  }
  return true;
}

function renderSettingsDraft() {
  const d = state.draftSettings;
  const trimOn = !!d.trimSilence;
  const burstOn = trimOn && !!d.trimShortBursts;
  const recalcOn = !!d.trimRecalc;

  // 单选模式
  document.querySelectorAll("#modeRadioList [data-mode-opt]").forEach(lbl => {
    const val = lbl.dataset.modeOpt;
    const isSel = (val === d.mode);
    lbl.classList.toggle("selected", isSel);
    const input = lbl.querySelector('input[type="radio"]');
    if (input) input.checked = isSel;
  });

  // 单选声道策略
  const curChannel = d.channel || "auto";
  document.querySelectorAll("#channelRadioList [data-channel-opt]").forEach(lbl => {
    const val = lbl.dataset.channelOpt;
    const isSel = (val === curChannel);
    lbl.classList.toggle("selected", isSel);
    const input = lbl.querySelector('input[type="radio"]');
    if (input) input.checked = isSel;
  });

  // 开关与勾框项
  document.getElementById("chkTrimSilence").checked = trimOn;
  const chkShortBursts = document.getElementById("chkTrimShortBursts");
  if (chkShortBursts) {
    chkShortBursts.checked = !!d.trimShortBursts;
    chkShortBursts.disabled = !trimOn;
  }
  document.getElementById("chkAllowAnyDuration").checked = !!d.allowAnyDuration;
  document.getElementById("chkOptimizeTriple").checked = !!d.optimizeTripleMeter;
  document.getElementById("chkApplyCombToForce").checked = !!d.applyCombTolToForce;
  document.getElementById("chkTrimRecalc").checked = recalcOn;

  const silenceThrItem = document.getElementById("itemSilenceThreshold");
  if (silenceThrItem) silenceThrItem.classList.toggle("is-disabled", !trimOn);
  const shortBurstsItem = document.getElementById("itemTrimShortBursts");
  if (shortBurstsItem) shortBurstsItem.classList.toggle("is-disabled", !trimOn);
  const shortBurstMaxItem = document.getElementById("itemShortBurstMax");
  if (shortBurstMaxItem) shortBurstMaxItem.classList.toggle("is-disabled", !burstOn);
  const shortBurstGapItem = document.getElementById("itemShortBurstMinGap");
  if (shortBurstGapItem) shortBurstGapItem.classList.toggle("is-disabled", !burstOn);
  const subTrunkMinSecItem = document.getElementById("itemSubTrunkMinSec");
  if (subTrunkMinSecItem) subTrunkMinSecItem.classList.toggle("is-disabled", !recalcOn);

  // 步进框项
  for (const [key, spec] of Object.entries(STEPPER_SPECS)) {
    const val = d[key];
    const valEl = document.getElementById(`val_${key}`);
    if (valEl) valEl.textContent = spec.fmt(val);
    const wrap = document.querySelector(`.stepper[data-stepper="${key}"]`);
    if (wrap) {
      const forceDisabled = (
        (key === "silenceThresholdDb" && !trimOn)
        || ((key === "shortBurstMaxSec" || key === "shortBurstMinGapSec") && !burstOn)
        || (key === "subTrunkMinSec" && !recalcOn)
      );
      const minusBtn = wrap.querySelector('[data-dir="-1"]');
      const plusBtn = wrap.querySelector('[data-dir="1"]');
      if (minusBtn) minusBtn.disabled = forceDisabled || (val <= spec.min + 1e-7);
      if (plusBtn) plusBtn.disabled = forceDisabled || (val >= spec.max - 1e-7);
    }
  }
}

function openSettingsModal() {
  hideMetricNotePopover();
  state.draftSettings = createDefaultSettings();
  Object.assign(state.draftSettings, state.settings);
  renderSettingsDraft();
  settingsModal.classList.add("open");
}

const STEPPER_HOLD_DELAY_MS = 350;
const STEPPER_REPEAT_INTERVAL_MS = 75;
let activeStepperHold = null;

function stopActiveStepperHold() {
  if (!activeStepperHold) return;
  if (activeStepperHold.delayTimer) clearTimeout(activeStepperHold.delayTimer);
  if (activeStepperHold.repeatTimer) clearInterval(activeStepperHold.repeatTimer);
  if (activeStepperHold.btn) activeStepperHold.btn.classList.remove("is-pressing");
  activeStepperHold = null;
}

window.addEventListener("pointerup", stopActiveStepperHold);
window.addEventListener("pointercancel", stopActiveStepperHold);
window.addEventListener("blur", stopActiveStepperHold);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopActiveStepperHold();
});

function closeSettingsModal() {
  stopActiveStepperHold();
  hideSettingsHelpPopover();
  settingsModal.classList.remove("open");
}

function closeConfirmClearModal() {
  confirmClearModal.classList.remove("open");
}

function isSettingsModalOpen() {
  return settingsModal.classList.contains("open") || confirmClearModal.classList.contains("open");
}

function handleSettingsEscapeKey(e) {
  if (confirmClearModal.classList.contains("open")) {
    e.preventDefault();
    closeConfirmClearModal();
    return true;
  }
  if (settingsHelpPopover && settingsHelpPopover.style.display === "block") {
    e.preventDefault();
    hideSettingsHelpPopover();
    return true;
  }
  if (settingsModal.classList.contains("open")) {
    e.preventDefault();
    closeSettingsModal();
    return true;
  }
  return false;
}

function appendSettingsToAnalyzeFormData(formData, settingsObj) {
  const s = settingsObj || state.settings;
  formData.append("mode", s.mode);
  formData.append("channel", s.channel || "auto");
  formData.append("octave_norm", "false");
  formData.append("target_sr", "");
  formData.append("trim_silence", s.trimSilence ? "true" : "false");
  formData.append("silence_threshold_db", String(Math.round(s.silenceThresholdDb ?? -50)));
  formData.append("trim_short_bursts", (s.trimSilence && s.trimShortBursts) ? "true" : "false");
  formData.append("short_burst_max_sec", Number(s.shortBurstMaxSec ?? 1.5).toFixed(1));
  formData.append("short_burst_min_gap_sec", Number(s.shortBurstMinGapSec ?? 0.8).toFixed(1));
  formData.append("allow_any_duration", s.allowAnyDuration ? "true" : "false");
  formData.append("acf_decay_ratio", s.acfDecayRatio.toFixed(2));
  formData.append("cand_update_ratio", s.candUpdateRatio.toFixed(2));
  // [新增 2026-11-14] 透传 240~300 BPM 折半强弱拍对比度门限
  formData.append("halve_contrast_ratio", Number(s.halveContrastRatio ?? 1.10).toFixed(2));
  formData.append("track_tol", String(Math.round(s.trackTol)));
  formData.append("comb_tol", String(Math.round(s.combTol)));
  formData.append("apply_comb_tol_to_force", s.applyCombTolToForce ? "true" : "false");
  formData.append("transition_miss_beats", String(Math.round(s.transitionMissBeats)));
  formData.append("transition_resume_beats", String(Math.round(s.transitionResumeBeats)));
  formData.append("transition_guard", s.transitionGuard.toFixed(1));
  formData.append("trim_recalc", s.trimRecalc ? "true" : "false");
  formData.append("sub_trunk_min_sec", String(Math.round(s.subTrunkMinSec ?? 15)));
}

function commitDraftSettings(clearQueueFirst) {
  if (clearQueueFirst) {
    state.items = [];
    state.activeIndex = -1;
  }
  state.settings = Object.assign(createDefaultSettings(), state.draftSettings);
  if (state.settings.mode === "exact") state.acfSource = "exact";
  else if (state.settings.mode === "force_v3") state.acfSource = "force_v3";
  else state.acfSource = "enhanced_v3";
  updateHeaderModeBadge();
  syncAcfSourceButtons();
  closeConfirmClearModal();
  closeSettingsModal();
  renderFileList(true);
  renderActiveDetail();
}

openSettingsBtn.addEventListener("click", openSettingsModal);
closeSettingsModalBtn.addEventListener("click", closeSettingsModal);
cancelSettingsBtn.addEventListener("click", closeSettingsModal);

settingsModal.addEventListener("click", (e) => {
  if (e.target === settingsModal) closeSettingsModal();
});

resetSettingsBtn.addEventListener("click", () => {
  stopActiveStepperHold();
  state.draftSettings = createDefaultSettings();
  renderSettingsDraft();
});

document.querySelectorAll('#modeRadioList input[name="bpmModeRadio"]').forEach(radio => {
  radio.addEventListener("change", (e) => {
    state.draftSettings.mode = e.target.value;
    renderSettingsDraft();
  });
});

document.querySelectorAll('#channelRadioList input[name="channelModeRadio"]').forEach(radio => {
  radio.addEventListener("change", (e) => {
    state.draftSettings.channel = e.target.value;
    renderSettingsDraft();
  });
});

document.getElementById("chkTrimSilence").addEventListener("change", (e) => {
  stopActiveStepperHold();
  state.draftSettings.trimSilence = e.target.checked;
  renderSettingsDraft();
});
document.getElementById("chkTrimShortBursts").addEventListener("change", (e) => {
  stopActiveStepperHold();
  state.draftSettings.trimShortBursts = e.target.checked;
  renderSettingsDraft();
});
document.getElementById("chkAllowAnyDuration").addEventListener("change", (e) => {
  state.draftSettings.allowAnyDuration = e.target.checked;
});
document.getElementById("chkOptimizeTriple").addEventListener("change", (e) => {
  state.draftSettings.optimizeTripleMeter = e.target.checked;
});
document.getElementById("chkApplyCombToForce").addEventListener("change", (e) => {
  state.draftSettings.applyCombTolToForce = e.target.checked;
});
document.getElementById("chkTrimRecalc").addEventListener("change", (e) => {
  stopActiveStepperHold();
  state.draftSettings.trimRecalc = e.target.checked;
  renderSettingsDraft();
});

function applyStepperDelta(key, spec, dir) {
  const scale = Math.pow(10, spec.decimals);
  let next = Math.round((state.draftSettings[key] + dir * spec.step) * scale) / scale;
  next = Math.min(spec.max, Math.max(spec.min, next));
  state.draftSettings[key] = next;
  renderSettingsDraft();
}

document.querySelectorAll(".stepper").forEach(wrap => {
  const key = wrap.dataset.stepper;
  const spec = STEPPER_SPECS[key];
  if (!spec) return;
  wrap.querySelectorAll(".stepper-btn").forEach(btn => {
    const dir = parseInt(btn.dataset.dir, 10);

    btn.addEventListener("pointerdown", (e) => {
      if (e.button !== 0 || btn.disabled) return;
      e.preventDefault();
      stopActiveStepperHold();

      applyStepperDelta(key, spec, dir);
      if (btn.disabled) return;

      btn.classList.add("is-pressing");
      const holdState = { btn, delayTimer: null, repeatTimer: null };
      activeStepperHold = holdState;

      holdState.delayTimer = setTimeout(() => {
        if (activeStepperHold !== holdState || btn.disabled) {
          stopActiveStepperHold();
          return;
        }
        applyStepperDelta(key, spec, dir);
        if (btn.disabled) {
          stopActiveStepperHold();
          return;
        }
        holdState.repeatTimer = setInterval(() => {
          if (activeStepperHold !== holdState || btn.disabled) {
            stopActiveStepperHold();
            return;
          }
          applyStepperDelta(key, spec, dir);
          if (btn.disabled) {
            stopActiveStepperHold();
          }
        }, STEPPER_REPEAT_INTERVAL_MS);
      }, STEPPER_HOLD_DELAY_MS);
    });

    btn.addEventListener("pointerleave", () => {
      if (activeStepperHold && activeStepperHold.btn === btn) {
        stopActiveStepperHold();
      }
    });

    btn.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      stopActiveStepperHold();
    });

    btn.addEventListener("keydown", (e) => {
      if ((e.key === "Enter" || e.key === " ") && !btn.disabled) {
        e.preventDefault();
        applyStepperDelta(key, spec, dir);
      }
    });
  });
});

saveSettingsBtn.addEventListener("click", () => {
  if (areSettingsEqual(state.draftSettings, state.settings)) {
    closeSettingsModal();
    return;
  }
  if (state.items.length > 0 && !areAlgorithmSettingsEqual(state.draftSettings, state.settings)) {
    confirmClearModal.classList.add("open");
    return;
  }
  commitDraftSettings(false);
});

cancelClearConfirmBtn.addEventListener("click", closeConfirmClearModal);
confirmClearModal.addEventListener("click", (e) => {
  if (e.target === confirmClearModal) closeConfirmClearModal();
});
confirmClearQueueBtn.addEventListener("click", () => {
  commitDraftSettings(true);
});

// 绑定三拍子与 3:2 三连音消歧小链接的悬浮与轻触交互
if (helpTripleLink && settingsHelpPopover) {
  helpTripleLink.addEventListener("pointerenter", () => {
    if (triplePopoverHideTimer) {
      clearTimeout(triplePopoverHideTimer);
      triplePopoverHideTimer = null;
    }
    showSettingsHelpPopover();
  });

  helpTripleLink.addEventListener("pointerleave", () => {
    if (isTriplePopoverPinned) return;
    triplePopoverHideTimer = setTimeout(() => {
      hideSettingsHelpPopover();
    }, 150);
  });

  settingsHelpPopover.addEventListener("pointerenter", () => {
    if (triplePopoverHideTimer) {
      clearTimeout(triplePopoverHideTimer);
      triplePopoverHideTimer = null;
    }
  });

  settingsHelpPopover.addEventListener("pointerleave", () => {
    if (isTriplePopoverPinned) return;
    triplePopoverHideTimer = setTimeout(() => {
      hideSettingsHelpPopover();
    }, 150);
  });

  helpTripleLink.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (settingsHelpPopover.style.display === "block" && isTriplePopoverPinned) {
      hideSettingsHelpPopover();
    } else {
      showSettingsHelpPopover();
      isTriplePopoverPinned = true;
    }
  });

  document.addEventListener("pointerdown", (e) => {
    if (!settingsHelpPopover || settingsHelpPopover.style.display !== "block") return;
    if (e.target.closest("#settingsHelpPopover") || e.target.closest("#helpTripleLink")) return;
    hideSettingsHelpPopover();
  });

  const modalBodyEl = settingsModal ? settingsModal.querySelector(".modal-body") : null;
  if (modalBodyEl) {
    modalBodyEl.addEventListener("scroll", hideSettingsHelpPopover);
  }
}

updateHeaderModeBadge();"""


# ---------------------------------------------------------------------------
# 5. 安全无损注入入口
# ---------------------------------------------------------------------------

def inject_settings_ui(index_html: str) -> str:
    """将顶栏设置按钮、设置与确认弹窗、设置专属样式及前端设置控制器注入工作台基础模板。"""
    anchors = (
        SETTINGS_CSS_ANCHOR,
        SETTINGS_HEADER_BTN_ANCHOR,
        SETTINGS_MODALS_ANCHOR,
        SETTINGS_CONFIG_JS_ANCHOR,
        SETTINGS_CONTROLLER_JS_ANCHOR,
    )

    # 幂等保护：若已注入完成且不存在任何未替换占位锚点，直接原样返回
    if 'id="settingsModal"' in index_html and not any(a in index_html for a in anchors):
        return index_html

    config_js = _build_settings_config_js()

    for bad_token in ("{{", "}}", "{%", "%}", "{#", "#}"):
        if (
            bad_token in _SETTINGS_CSS
            or bad_token in _SETTINGS_HEADER_BTN_HTML
            or bad_token in _SETTINGS_MODALS_HTML
            or bad_token in config_js
            or bad_token in _SETTINGS_CONTROLLER_JS
        ):
            raise RuntimeError(f"inject_settings_ui 检测到潜在 Jinja2 冲突定界符: {bad_token}")

    html = index_html
    replacements = (
        (SETTINGS_CSS_ANCHOR, _SETTINGS_CSS),
        (SETTINGS_HEADER_BTN_ANCHOR, _SETTINGS_HEADER_BTN_HTML),
        (SETTINGS_MODALS_ANCHOR, _SETTINGS_MODALS_HTML),
        (SETTINGS_CONFIG_JS_ANCHOR, config_js),
        (SETTINGS_CONTROLLER_JS_ANCHOR, _SETTINGS_CONTROLLER_JS),
    )
    for anchor, fragment in replacements:
        if anchor not in html:
            raise RuntimeError(f"inject_settings_ui 未找到占位锚点 {anchor!r}，拒绝静默注入失败")
        html = html.replace(anchor, fragment, 1)

    return html
