#!/usr/bin/env python3
"""
Malody BPM 测算工作台 —— 独立前 1 分钟 diff=1000Hz 起峰包络与慢放手动确定 Offset 二级页面模块

设计原则：
1. 独立承载所有 Offset 辅助分析的前后端代码，原 web_gui.py 仅保留极简挂载调用；
2. 严格作为内联二级视图运行（不能单独打开，内部不提供导入新音频入口，仅处理当前已测算曲目）；
3. 音频实际有效时长不足 60 秒不予分析；达标音频严格截取前 60.000 秒单声道 32 位浮点 PCM (f32le)，
   同源提取 diff=1000Hz 起峰包络并以紧凑二进制流 (MOFS) 下发，消除浏览器解码器编码延迟；
4. 纯手动确定 Offset（无自动测算、无节拍网格线、无节拍器提示音）：支持仿 AU 滚轮无级缩放、
   全局缩略导航条、0.25x/0.5x/0.75x/1.0x 慢放（默认采样级原速重采样零涂抹，可选开启保持原音调）、
   纯播放光标定位与正数毫秒值悬停/光标一键复制。
"""

from __future__ import annotations

import os
import struct
import tempfile
import numpy as np
from flask import Flask, Response, jsonify, request

from malody_bpm.decoder import (
    _probe_audio_stream_full,
    _resolve_ff_binary,
    _run_ffmpeg_decode,
    _single_channel_pan_filter,
    normalize_channel_mode,
)
from malody_bpm.envelope import extract_onset_envelope
from malody_bpm.filters import _c_dsp, require_c_dsp
from web_gui_precheck import probe_and_validate_audio

OFFSET_CLIP_SECONDS = 60
OFFSET_CLIP_MS = OFFSET_CLIP_SECONDS * 1000  # 60000 ms
MOFS_MAGIC = b"MOFS"


class ShortAudioForOffsetError(ValueError):
    """音频实际有效 PCM 时长不足 60 秒时抛出的受控业务异常。"""


def _decode_f32_clip(
    ffmpeg_bin: str,
    file_path: str,
    sr: int,
    out_channels: int | None,
    decode_cap_frames: int,
    pan_expr: str | None = None,
    raw_channels_hint: int = 1,
) -> tuple[np.ndarray, int]:
    """解码前 60.6 秒 f32le 裸流并返回 (一维或二维连续数组, 有效帧数)。"""
    eff_ch = out_channels if out_channels is not None else max(1, int(raw_channels_hint))
    cmd = [
        ffmpeg_bin,
        "-v",
        "error",
        "-i",
        file_path,
        "-t",
        "60.6",
    ]
    if pan_expr:
        cmd.extend(["-af", pan_expr])
        eff_ch = 1
    elif out_channels is not None:
        cmd.extend(["-ac", str(out_channels)])
    cmd.extend([
        "-f",
        "f32le",
        "-acodec",
        "pcm_f32le",
        "-ar",
        str(sr),
        "-",
    ])
    max_bytes = decode_cap_frames * eff_ch * 4
    stdout, stderr, returncode, capped = _run_ffmpeg_decode(cmd, file_path, max_bytes=max_bytes)
    if not capped and returncode != 0 and len(stdout) == 0:
        err_msg = stderr.decode("utf-8", errors="ignore").strip()
        raise RuntimeError(f"FFmpeg 解码 32 位浮点 PCM 失败: {err_msg}")

    frame_bytes = eff_ch * 4
    usable_bytes = (len(stdout) // frame_bytes) * frame_bytes
    f32 = np.frombuffer(stdout[:usable_bytes], dtype=np.float32)
    n_frames = len(f32) // eff_ch
    capped_frames = min(n_frames, decode_cap_frames)
    if eff_ch == 1:
        return np.ascontiguousarray(f32[:capped_frames], dtype=np.float32), n_frames
    return (
        np.ascontiguousarray(f32[: capped_frames * eff_ch].reshape(capped_frames, eff_ch), dtype=np.float32),
        n_frames,
    )


def extract_first_minute_offset_packet(
    file_path: str,
    original_filename: str = "",
    channel: str = "auto",
) -> bytes:
    """
    校验音频并提取前 60.000 秒未静音处理的真实单/双声道 32 位浮点 PCM 与按声道策略同步的 diff=1000Hz 起峰包络：
    - 若实际解码有效时长 < 60.000 秒，抛出 ShortAudioForOffsetError 不予分析；
    - 慢放音频 (pcm_f32)：严格从 0.000s 截取前 60.000 秒（不做头尾去静音裁剪），1ch 原样单声道、2ch 原样双声道立体声、
      >2ch 安全转为双声道立体声，采样率沿用 32k/44.1k/48k Hz；
    - 起峰包络 (diff_1000hz)：多解码最多 0.5 秒尾部缓冲消除末端滤波器群延迟边界效应，按 channel ('auto' | 'left' | 'right' | 'downmix')
      提取 diff=1000Hz 并切片至前 60000 ms；
    - 返回紧凑小端二进制包：
        Header (20 bytes): <4sIIII -> magic("MOFS"), sample_rate, play_channels, n_diff(60000), n_pcm(60*sr*play_channels)
        Body 1: diff_1000hz float32[60000]
        Body 2: pcm_f32     float32[60 * sr * play_channels] (交织排布)
    """
    norm_channel = normalize_channel_mode(channel)
    require_c_dsp(_c_dsp)

    pre_res = probe_and_validate_audio(file_path, original_filename or os.path.basename(file_path))
    if not pre_res.get("ok"):
        raise ValueError(pre_res.get("message") or "音频预检未通过，无法生成起峰包络")

    matched_sr, raw_channels, _, _, channel_layout = _probe_audio_stream_full(file_path)
    sr = int(matched_sr)
    if sr <= 0:
        raise ValueError(f"无效的采样率: {sr}")
    channels = max(1, int(raw_channels))

    required_frames = OFFSET_CLIP_SECONDS * sr
    # 多读取最多 0.5 秒作为滤波器末端群延迟与零相位反演缓冲，避免第 59.99 秒边缘失真
    decode_cap_frames = required_frames + (sr // 2)
    ffmpeg_bin = _resolve_ff_binary("ffmpeg")

    if channels == 1:
        play_channels = 1
        pcm_1d, n_frames = _decode_f32_clip(
            ffmpeg_bin, file_path, sr, out_channels=1, decode_cap_frames=decode_cap_frames
        )
        if n_frames < required_frames:
            actual_sec = n_frames / float(sr)
            raise ShortAudioForOffsetError(
                f"音频实际有效时长不足 60 秒（当前 {actual_sec:.2f} 秒），不予分析前 1 分钟起峰包络图。"
            )
        pcm_60s = np.ascontiguousarray(pcm_1d[:required_frames], dtype="<f4")
        pcm_for_env = pcm_1d
    elif channels == 2:
        play_channels = 2
        pcm_2d, n_frames = _decode_f32_clip(
            ffmpeg_bin,
            file_path,
            sr,
            out_channels=None,
            decode_cap_frames=decode_cap_frames,
            raw_channels_hint=2,
        )
        if n_frames < required_frames:
            actual_sec = n_frames / float(sr)
            raise ShortAudioForOffsetError(
                f"音频实际有效时长不足 60 秒（当前 {actual_sec:.2f} 秒），不予分析前 1 分钟起峰包络图。"
            )
        pcm_60s = np.ascontiguousarray(pcm_2d[:required_frames].reshape(-1), dtype="<f4")
        if norm_channel == "auto":
            pcm_for_env = pcm_2d
        elif norm_channel == "left":
            pcm_for_env = np.ascontiguousarray(pcm_2d[:, 0], dtype=np.float32)
        elif norm_channel == "right":
            pcm_for_env = np.ascontiguousarray(pcm_2d[:, 1], dtype=np.float32)
        else:
            pcm_for_env, _ = _decode_f32_clip(
                ffmpeg_bin, file_path, sr, out_channels=1, decode_cap_frames=decode_cap_frames
            )
    else:
        play_channels = 2
        pcm_play_2d, n_frames = _decode_f32_clip(
            ffmpeg_bin, file_path, sr, out_channels=2, decode_cap_frames=decode_cap_frames
        )
        if n_frames < required_frames:
            actual_sec = n_frames / float(sr)
            raise ShortAudioForOffsetError(
                f"音频实际有效时长不足 60 秒（当前 {actual_sec:.2f} 秒），不予分析前 1 分钟起峰包络图。"
            )
        pcm_60s = np.ascontiguousarray(pcm_play_2d[:required_frames].reshape(-1), dtype="<f4")
        if norm_channel == "auto":
            if channels <= 8:
                pcm_for_env, _ = _decode_f32_clip(
                    ffmpeg_bin,
                    file_path,
                    sr,
                    out_channels=None,
                    decode_cap_frames=decode_cap_frames,
                    raw_channels_hint=channels,
                )
            else:
                pcm_for_env = pcm_play_2d
        elif norm_channel in ("left", "right"):
            pan_expr = _single_channel_pan_filter(norm_channel, channels, channel_layout)
            pcm_for_env, _ = _decode_f32_clip(
                ffmpeg_bin,
                file_path,
                sr,
                out_channels=None,
                decode_cap_frames=decode_cap_frames,
                pan_expr=pan_expr,
            )
        else:
            pcm_for_env, _ = _decode_f32_clip(
                ffmpeg_bin, file_path, sr, out_channels=1, decode_cap_frames=decode_cap_frames
            )

    diff_full, _ = extract_onset_envelope(pcm_for_env, sr, exact=False)

    if len(diff_full) < OFFSET_CLIP_MS:
        raise ShortAudioForOffsetError("起峰包络有效长度不足 60000 ms，不予分析。")

    diff_60s = np.ascontiguousarray(diff_full[:OFFSET_CLIP_MS], dtype="<f4")

    header = struct.pack(
        "<4sIIII",
        MOFS_MAGIC,
        sr,
        play_channels,
        int(len(diff_60s)),
        int(len(pcm_60s)),
    )
    return header + diff_60s.tobytes() + pcm_60s.tobytes()


def register_offset_routes(app: Flask) -> None:
    """注册前 1 分钟 diff=1000Hz 起峰包络与 32-bit PCM 二进制获取接口（不注册任何独立可直达 HTML 页面路由）。"""

    @app.route("/api/offset_envelope_pcm", methods=["POST"])
    def api_offset_envelope_pcm():
        if "file" not in request.files:
            return jsonify({"error": "未收到音频文件"}), 400

        up_file = request.files["file"]
        if not up_file or not up_file.filename:
            return jsonify({"error": "音频文件名为空"}), 400

        _, ext = os.path.splitext(up_file.filename)
        safe_ext = ext if (ext and len(ext) <= 12 and ext.replace(".", "").isalnum()) else ".tmp"
        fd, tmp_path = tempfile.mkstemp(prefix="malody_off_", suffix=safe_ext)
        os.close(fd)

        try:
            channel = normalize_channel_mode(request.form.get("channel", "auto"))
            up_file.save(tmp_path)
            binary_payload = extract_first_minute_offset_packet(
                tmp_path, up_file.filename, channel=channel
            )
            return Response(
                binary_payload,
                mimetype="application/octet-stream",
                headers={"Cache-Control": "no-store"},
            )
        except ShortAudioForOffsetError as short_err:
            return jsonify({"error": str(short_err), "code": "duration_under_60s"}), 400
        except ValueError as val_err:
            return jsonify({"error": str(val_err)}), 400
        except Exception as exc:
            return jsonify({"error": f"提取起峰包络失败: {exc}"}), 500
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass


# ------------------------------------------------------------------------------
# 前端样式、二级视图 DOM 与仿 AU 起峰包络慢放控制器
# ------------------------------------------------------------------------------
_OFFSET_CSS = r"""
  /* --- 前 1 分钟 diff=1000Hz 起峰包络二级页面样式 (web_gui_offset.py) --- */
  .offset-entry-bar {
    background: var(--panel);
    border: 1px solid rgba(255, 255, 255, 0.38); /* 柔和半透白过渡边框 */
    border-radius: 12px;
    padding: 13px 18px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 6px 12px;
  }
  .offset-entry-link {
    background: transparent;
    border: none;
    color: var(--accent);
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
    padding: 0;
    text-align: left;
    display: inline-flex;
    align-items: center;
    gap: 8px;
    transition: color 0.15s ease;
  }
  .offset-entry-link:hover {
    color: #79c0ff;
    text-decoration: underline;
    text-underline-offset: 3px;
  }
  .offset-entry-hint {
    font-size: 12px;
    color: var(--muted);
  }

  #offsetSecondaryView {
    display: none;
    flex-direction: column;
    height: 100vh;
    width: 100vw;
    background: var(--bg);
    color: var(--text);
    overflow: hidden;
    user-select: none;
  }
  #offsetSecondaryView.active {
    display: flex;
  }
  .offset-topbar {
    min-height: var(--header-h, 64px);
    padding: 11px 24px;
    background: rgba(12, 12, 12, 0.92);
    backdrop-filter: blur(14px);
    -webkit-backdrop-filter: blur(14px);
    border-bottom: 1px solid rgba(255, 255, 255, 0.38); /* Offset 二级页面顶栏底部分界线 */
    box-shadow: 0 1px 0 rgba(255, 255, 255, 0.03), 0 6px 20px rgba(0, 0, 0, 0.45);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
    flex-shrink: 0;
  }
  .offset-topbar-left {
    display: flex;
    align-items: center;
    gap: 14px;
    min-width: 0;
    flex: 1;
  }
  .offset-back-btn {
    height: 36px;
    padding: 0 14px;
    border-radius: 999px;
    background: #121212;
    border: 1px solid #262626;
    color: var(--text);
    font-size: 12.5px;
    font-weight: 600;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    cursor: pointer;
    flex-shrink: 0;
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04);
    transition: border-color 0.16s ease, background 0.16s ease, box-shadow 0.16s ease;
  }
  .offset-back-btn:hover {
    background: #181818;
    border-color: rgba(88, 166, 255, 0.52);
    box-shadow: 0 0 0 3px rgba(88, 166, 255, 0.10);
  }
  .offset-back-btn:active {
    background: #1e1e1e;
  }
  .offset-song-title-wrap {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }
  .offset-song-title {
    font-size: 14.5px;
    font-weight: 600;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .offset-song-sub {
    font-size: 11.5px;
    color: var(--muted);
  }
  .offset-readout-group {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
  }
  .offset-readout-box {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 6px 11px;
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-size: 12.5px;
  }
  .offset-readout-label {
    color: var(--muted);
  }
  .offset-readout-val {
    font-size: 14px;
    font-weight: 700;
    color: var(--text);
    min-width: 68px;
  }
  .offset-readout-val.hover-val {
    color: #58a6ff;
  }
  .offset-copy-toast {
    font-size: 12px;
    font-weight: 600;
    color: var(--green);
    opacity: 0;
    transition: opacity 0.15s ease;
  }
  .offset-copy-toast.show {
    opacity: 1;
  }

  .offset-toolbar {
    padding: 10px 22px;
    background: #121214; /* 偏移微调工具栏深空灰过渡背景 */
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 14px;
    flex-wrap: wrap;
    flex-shrink: 0;
  }
  .offset-tool-cluster {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }
  .offset-play-btn {
    background: var(--accent);
    color: #000000;
    border: none;
    border-radius: 7px;
    padding: 6px 15px;
    font-size: 13px;
    font-weight: 700;
    cursor: pointer;
    min-width: 92px;
    transition: filter 0.12s;
  }
  .offset-play-btn:hover:not(:disabled) {
    filter: brightness(1.08);
  }
  .offset-play-btn:disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }
  .offset-tool-btn {
    background: var(--panel-alt);
    border: 1px solid var(--border);
    color: var(--text);
    border-radius: 6px;
    padding: 5px 10px;
    font-size: 12px;
    cursor: pointer;
    transition: border-color 0.12s, color 0.12s;
  }
  .offset-tool-btn:hover:not(:disabled) {
    border-color: var(--accent);
  }
  .offset-pitch-label {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    color: var(--muted);
    cursor: pointer;
    padding: 3px 8px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--panel-alt);
  }
  .offset-pitch-label input {
    accent-color: var(--accent);
    cursor: pointer;
  }
  .offset-pitch-warn {
    font-size: 11px;
    color: var(--amber);
    display: none;
  }
  .offset-pitch-warn.show {
    display: inline;
  }

  .offset-stage-body {
    flex: 1;
    display: flex;
    flex-direction: column;
    padding: 14px 22px 16px 22px;
    gap: 12px;
    min-height: 0;
  }
  .offset-loading-panel {
    flex: 1;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 12px;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 12px;
    color: var(--muted);
    font-size: 14px;
  }
  .offset-workspace {
    flex: 1;
    display: none;
    flex-direction: column;
    gap: 10px;
    min-height: 0;
  }
  .offset-minimap-card {
    background: var(--panel);
    border: 1px solid rgba(255, 255, 255, 0.38); /* 柔和半透白过渡边框 */
    border-radius: 10px;
    padding: 8px 12px;
    display: flex;
    flex-direction: column;
    gap: 5px;
    flex-shrink: 0;
  }
  .offset-minimap-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 11.5px;
    color: var(--muted);
  }
  .offset-minimap-wrap {
    position: relative;
    height: 54px;
    width: 100%;
    background: #000000;
    border: 1px solid var(--border);
    border-radius: 6px;
    overflow: hidden;
    cursor: pointer;
  }
  #offsetMinimapCanvas {
    width: 100%;
    height: 100%;
    display: block;
  }
  .offset-main-card {
    flex: 1;
    background: var(--panel);
    border: 1px solid rgba(255, 255, 255, 0.38); /* 柔和半透白过渡边框 */
    border-radius: 12px;
    padding: 12px 14px 10px 14px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    min-height: 240px;
  }
  .offset-main-canvas-wrap {
    flex: 1;
    position: relative;
    width: 100%;
    min-height: 200px;
    background: #000000;
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
  }
  .offset-main-scroll-host {
    position: absolute;
    inset: 0;
    overflow: scroll;
    overscroll-behavior: contain;
    color-scheme: dark;
  }
  .offset-main-scroll-sizer {
    position: relative;
    min-width: 100%;
    min-height: 100%;
  }
  .offset-main-sticky-stage {
    position: sticky;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    overflow: hidden;
    cursor: crosshair;
  }
  .offset-main-sticky-stage.is-dragging {
    cursor: grabbing;
  }
  .offset-main-sticky-stage.is-adjusting-marker {
    cursor: ew-resize;
  }
  #offsetMainCanvas {
    width: 100%;
    height: 100%;
    display: block;
  }
  .offset-hover-tooltip {
    position: absolute;
    top: 12px;
    left: 12px;
    background: rgba(12, 12, 12, 0.95);
    border: 1px solid rgba(88, 166, 255, 0.45);
    border-radius: 7px;
    padding: 6px 10px;
    font-size: 12px;
    line-height: 1.45;
    color: var(--text);
    pointer-events: none;
    display: none;
    box-shadow: 0 6px 20px rgba(0, 0, 0, 0.45);
  }
  .offset-bottom-tips {
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
    font-size: 12px;
    color: var(--muted);
    flex-shrink: 0;
  }
  .offset-color-ctrl {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    flex-shrink: 0;
  }
  .offset-color-swatch-wrap {
    position: relative;
    width: 32px;
    height: 32px;
    border-radius: 8px;
    border: 1px solid rgba(255, 255, 255, 0.24);
    overflow: hidden;
    cursor: pointer;
    flex-shrink: 0;
    background-image: repeating-conic-gradient(#242424 0% 25%, #0c0c0c 0% 50%);
    background-size: 8px 8px;
  }
  .offset-color-swatch-wrap input[type="color"] {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    opacity: 0;
    cursor: pointer;
    border: none;
    padding: 0;
    margin: 0;
  }
  .offset-color-swatch {
    display: block;
    width: 100%;
    height: 100%;
    pointer-events: none;
  }
  .offset-color-hex-input {
    width: 106px;
    height: 32px;
    padding: 0 8px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: #000000;
    color: var(--text);
    font-size: 13px;
    text-align: center;
    outline: none;
    transition: border-color 0.15s;
  }
  .offset-color-hex-input:focus,
  .offset-alpha-box:focus-within {
    border-color: var(--accent);
  }
  .offset-alpha-box {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    height: 32px;
    padding: 0 8px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: #000000;
    transition: border-color 0.15s;
  }
  .offset-alpha-label {
    font-size: 11.5px;
    color: var(--muted);
    user-select: none;
  }
  .offset-alpha-input {
    width: 44px;
    background: transparent;
    border: none;
    color: var(--text);
    font-size: 13px;
    text-align: right;
    outline: none;
    -moz-appearance: textfield;
  }
  .offset-alpha-input::-webkit-outer-spin-button,
  .offset-alpha-input::-webkit-inner-spin-button {
    -webkit-appearance: none;
    margin: 0;
  }
  .modal-dialog.offset-pre-notice-dialog {
    background: #161618; /* 二级模态弹窗统一深空灰调 */
    width: min(680px, calc(100vw - 32px));
    max-height: min(86vh, 760px);
    border-radius: 24px;
    display: flex;
    flex-direction: column;
  }
  .modal-dialog.offset-pre-notice-dialog .modal-header {
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    padding: 20px 24px 14px 24px;
    flex-shrink: 0;
  }
  .modal-dialog.offset-pre-notice-dialog .modal-body.offset-notice-body {
    padding: 16px 24px 18px 24px;
    display: flex;
    flex-direction: column;
    gap: 12px;
    overflow-y: auto;
    color-scheme: dark;
  }
  .offset-notice-item {
    background: #1e2026; /* 须知条目卡片深空灰 */
    border: 1px solid rgba(255, 255, 255, 0.09);
    border-radius: 14px;
    padding: 12px 15px;
    display: flex;
    gap: 10px;
    align-items: flex-start;
  }
  .offset-notice-num {
    flex-shrink: 0;
    width: 22px;
    height: 22px;
    margin-top: 1px;
    border-radius: 50%;
    background: rgba(88, 166, 255, 0.16);
    border: 1px solid rgba(88, 166, 255, 0.36);
    color: #79c0ff;
    font-size: 12.5px;
    font-weight: 700;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    user-select: none;
  }
  .offset-notice-content {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
    font-size: 13.5px;
    line-height: 1.68;
    color: #FFFFFF;
  }
  .offset-notice-p {
    margin: 0;
  }
  .offset-notice-emph-red {
    color: #ff6b6b;
    font-weight: 600;
  }
  .offset-notice-sublist {
    margin: 2px 0 0 0;
    padding-left: 18px;
    display: flex;
    flex-direction: column;
    gap: 7px;
  }
  .offset-notice-sublist li {
    line-height: 1.66;
  }
  .offset-notice-code {
    font-size: 12.5px;
    padding: 1px 6px;
    border-radius: 5px;
    background: rgba(255, 255, 255, 0.09);
    border: 1px solid rgba(255, 255, 255, 0.12);
    color: #FFFFFF;
  }
  .modal-dialog.offset-pre-notice-dialog .modal-footer.offset-notice-footer {
    border-top: 1px solid rgba(255, 255, 255, 0.08);
    background: transparent;
    padding: 14px 24px 18px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 12px;
    flex-shrink: 0;
  }
  .offset-notice-skip-wrap {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    font-size: 13px;
    color: var(--muted);
    cursor: pointer;
    user-select: none;
  }
  .offset-notice-skip-wrap:hover {
    color: var(--text);
  }
  .offset-notice-skip-wrap input[type="checkbox"] {
    width: 15px;
    height: 15px;
    accent-color: var(--accent);
    cursor: pointer;
    margin: 0;
  }
"""

_OFFSET_SETTINGS_GROUP_HTML = """
      <!-- 分组: 起峰包络图辅助定位（Offset） -->
      <section class="m3-group">
        <div class="m3-group-header">
          <span class="m3-group-title">起峰包络图辅助定位（Offset）</span>
          <span class="m3-group-desc">仅控制二级包络视图的显示范围与曲线配色，不影响 BPM 测算且修改无需清空队列</span>
        </div>
        <div class="m3-card">
          <div class="m3-item">
            <div class="setting-info">
              <div class="setting-title">进入包络图时默认显示开头的秒数</div>
              <div class="setting-sub">设置打开 diff_1000Hz 起峰包络二级页面时的初始时间轴视野（2~60 秒，步进 2 秒，默认 20 秒）。底层仍要求音频满 60 秒并完整提取前 60 秒数据。进入后仍可随时滚轮缩放或点按「60s 全览」查看完整 1 分钟。</div>
            </div>
            <div class="stepper" data-stepper="offsetInitSec">
              <button type="button" class="stepper-btn" data-dir="-1" aria-label="减小">−</button>
              <span class="stepper-val" id="val_offsetInitSec">20 s</span>
              <button type="button" class="stepper-btn" data-dir="1" aria-label="增大">+</button>
            </div>
          </div>
          <div class="m3-item">
            <div class="setting-info">
              <div class="setting-title">自定义起峰包络曲线颜色与不透明度（Alpha）</div>
              <div class="setting-sub">点按色块进入系统调色盘，或直接输入 8 位 #RRGGBBAA 色号与 0~255 Alpha 整数（默认 #3FB950FF / 255）。仅作用于下方主起峰包络图曲线与采样点，顶部缩略导航条保持原蓝色剪影不变。</div>
            </div>
            <div class="offset-color-ctrl">
              <label class="offset-color-swatch-wrap" title="点按打开系统调色盘选择颜色">
                <input type="color" id="inputOffsetCurveColor" value="#3fb950" aria-label="选择起峰图颜色">
                <span class="offset-color-swatch" id="offsetCurveColorSwatch" style="background:rgba(63, 185, 80, 1);"></span>
              </label>
              <input type="text" class="offset-color-hex-input" id="inputOffsetCurveHex" value="#3FB950FF" maxlength="9" spellcheck="false" autocomplete="off" title="8位十六进制色号 #RRGGBBAA" aria-label="8位十六进制颜色代码 #RRGGBBAA">
              <label class="offset-alpha-box" title="Alpha 不透明度（0~255 整数，对应 #RRGGBBAA 末两位）">
                <span class="offset-alpha-label">Alpha</span>
                <input type="number" class="offset-alpha-input" id="inputOffsetCurveAlpha" min="0" max="255" step="1" value="255" aria-label="Alpha 不透明度 0 到 255">
              </label>
            </div>
          </div>
        </div>
      </section>"""

_OFFSET_ENTRY_HTML = """
      <div class="offset-entry-bar" id="offsetEntryBar">
        <button type="button" class="offset-entry-link" id="openOffsetViewLink">
          <span>查看diff_1000Hz起峰包络图，手动确定Offset</span>
          <span aria-hidden="true">→</span>
        </button>
        <span class="offset-entry-hint" id="offsetEntryHint">默认显示前 20 秒（可在设置中调整） · 32 位浮点 PCM 音画同源慢放（不足 60 秒不予分析）</span>
      </div>
"""

_OFFSET_SECONDARY_HTML = """
<!-- 前 1 分钟 diff=1000Hz 起峰包络与慢放手动确定 Offset 封闭二级页面 (web_gui_offset.py) -->
<section id="offsetSecondaryView" aria-label="前1分钟起峰包络手动确定Offset视图">
  <div class="offset-topbar">
    <div class="offset-topbar-left">
      <button type="button" class="offset-back-btn" id="offsetBackBtn" title="返回 BPM 测算界面 (Esc)">← 返回 BPM 测算界面</button>
      <div class="offset-song-title-wrap">
        <div class="offset-song-title" id="offsetSongTitle">-</div>
        <div class="offset-song-sub" id="offsetSongSub">前 60.000 秒 · diff_1000Hz (1ms 精度) · 32-bit Float PCM</div>
      </div>
    </div>
    <div class="offset-readout-group">
      <span class="offset-copy-toast" id="offsetCopyToast">已复制 ✓</span>
      <div class="offset-readout-box">
        <span class="offset-readout-label">悬停位置</span>
        <span class="offset-readout-val hover-val" id="offsetHoverMsVal">-</span>
      </div>
      <div class="offset-readout-box">
        <span class="offset-readout-label">播放光标</span>
        <span class="offset-readout-val" id="offsetCursorMsVal">0 ms</span>
        <button type="button" class="copy-btn" id="copyCursorMsBtn" title="复制光标所在的正数毫秒值">复制毫秒值</button>
      </div>
    </div>
  </div>

  <div class="offset-toolbar">
    <div class="offset-tool-cluster">
      <button type="button" class="offset-play-btn" id="offsetPlayPauseBtn" disabled>▶ 播放</button>
      <button type="button" class="offset-tool-btn" id="offsetRewindCursorBtn" disabled title="停止并回到蓝色播放起点光标">回到起点光标</button>
      <div class="seg-group" id="offsetSpeedGroup" title="播放倍速">
        <button type="button" class="seg-btn" data-rate="0.25">0.25x</button>
        <button type="button" class="seg-btn" data-rate="0.5">0.5x</button>
        <button type="button" class="seg-btn" data-rate="0.75">0.75x</button>
        <button type="button" class="seg-btn active" data-rate="1">1.0x</button>
      </div>
      <label class="offset-pitch-label" title="默认关闭以保证采样级零瞬态涂抹。开启后保持原音高，但瞬态可能有15~30ms时域拉伸涂抹。">
        <input type="checkbox" id="offsetPreservePitchChk">
        <span>保持原音调</span>
      </label>
      <span class="offset-pitch-warn" id="offsetPitchWarn">已开启保调拉伸（鼓点瞬态可能有 ~20ms 轻微涂抹，精确定点建议关闭）。</span>
    </div>

    <div class="offset-tool-cluster">
      <div class="seg-group" id="offsetPolarityGroup" title="diff_1000hz 极性展示方式">
        <button type="button" class="seg-btn active" data-pol="pos">仅正向起峰</button>
        <button type="button" class="seg-btn" data-pol="full">完整正负 diff</button>
      </div>
      <div class="seg-group" id="offsetGainGroup" title="纵向振幅放大倍率（亦可按住 Alt/Option + 滚轮 1×~16× 无级缩放）">
        <button type="button" class="seg-btn active" data-gain="1">1倍</button>
        <button type="button" class="seg-btn" data-gain="2">2倍</button>
        <button type="button" class="seg-btn" data-gain="4">4倍</button>
        <button type="button" class="seg-btn" data-gain="8">8倍</button>
        <button type="button" class="seg-btn" data-gain="16">16倍</button>
      </div>
      <button type="button" class="offset-tool-btn" id="offsetZoomInBtn" title="放大时间轴">放大 +</button>
      <button type="button" class="offset-tool-btn" id="offsetZoomOutBtn" title="缩小时间轴">缩小 −</button>
      <button type="button" class="offset-tool-btn" id="offsetZoomInitBtn" title="回到初始 0~20s 视野">前 20s</button>
      <button type="button" class="offset-tool-btn" id="offsetZoomResetBtn" title="恢复 0~60s 全览">60s 全览</button>
    </div>
  </div>

  <div class="offset-stage-body">
    <div class="offset-loading-panel" id="offsetLoadingPanel">
      <span class="precheck-spinner"></span>
      <span id="offsetLoadingText">正在解码前 60 秒 32 位浮点 PCM 并提取 diff_1000Hz 起峰包络…</span>
    </div>

    <div class="offset-workspace" id="offsetWorkspace">
      <div class="offset-minimap-card">
        <div class="offset-minimap-head">
          <span>00:00.000 ~ 01:00.000 全局起峰缩略导航（点按跳转至对应位置，或拖动高亮窗平移）</span>
          <span id="offsetViewportSpanLabel">当前视野：0.000s - 60.000s (跨度 60.000s) · 纵向1倍</span>
        </div>
        <div class="offset-minimap-wrap">
          <canvas id="offsetMinimapCanvas"></canvas>
        </div>
      </div>

      <div class="offset-main-card">
        <div class="offset-main-canvas-wrap" id="offsetMainCanvasWrap">
          <div class="offset-main-scroll-host" id="offsetMainScrollHost">
            <div class="offset-main-scroll-sizer" id="offsetMainScrollSizer">
              <div class="offset-main-sticky-stage" id="offsetMainStickyStage">
                <canvas id="offsetMainCanvas"></canvas>
                <div class="offset-hover-tooltip" id="offsetHoverTooltip"></div>
              </div>
            </div>
          </div>
        </div>
        <div class="offset-bottom-tips">
          <span>操作说明：滚轮横向缩放 · <b>按住Alt/Option并滑动滚轮</b>纵向缩放 · 点按并拖动以上下左右切换视野 · 点按以定起点 · <b>按住Ctrl/Cmd并拖动</b>微调起点线（靠近 ±12px 阻尼微调 / 远处跳移跟随） · <b>按 Esc</b> 返回主界面</span>
          <span>快捷复制：指在目标起峰尖刺上按 <b>C</b> 键或<b>右键点按</b>，可直接复制悬停处的正数毫秒值</span>
        </div>
      </div>
    </div>
  </div>
</section>

<!-- 不足 60 秒不予分析提示弹窗 (web_gui_offset.py) -->
<div class="modal-backdrop intercept-alert-layer" id="offsetShortAudioModal" role="dialog" aria-modal="true" aria-labelledby="offsetShortAudioTitle">
  <div class="modal-dialog quality-dialog">
    <div class="modal-header">
      <h2 id="offsetShortAudioTitle">音频时长不足 60 秒</h2>
    </div>
    <div class="modal-body">
      <p class="quality-copy-p" id="offsetShortAudioMsg">当前音频实际有效时长不足 60 秒，不予分析前 1 分钟起峰包络图。</p>
    </div>
    <div class="modal-footer">
      <div class="modal-footer-right">
        <button type="button" class="m3-btn m3-btn-filled" id="closeOffsetShortModalBtn">我知道了</button>
      </div>
    </div>
  </div>
</div>

<!-- 进入 Offset 测算前的重要提示弹窗 (web_gui_offset.py) -->
<div class="modal-backdrop intercept-alert-layer" id="offsetPreNoticeModal" role="dialog" aria-modal="true" aria-labelledby="offsetPreNoticeTitle">
  <div class="modal-dialog offset-pre-notice-dialog">
    <div class="modal-header">
      <h2 id="offsetPreNoticeTitle">进入 Offset 测算前的重要提示</h2>
    </div>
    <div class="modal-body offset-notice-body">
      <div class="offset-notice-item">
        <span class="offset-notice-num">1</span>
        <div class="offset-notice-content">
          <p class="offset-notice-p">受数字音频底层特性的影响，任何经转码后成为有损编码的音频（除原生 <code class="offset-notice-code">.wav</code>、<code class="offset-notice-code">.flac</code>、<code class="offset-notice-code">.ape</code>、<code class="offset-notice-code">.alac</code> 格式外，绝大多数常见原生扩展名的音频均属此类），其波形在时间轴上的分布相较于转码前必然会发生一定变化，即使转码前后参数完全一致（例如将 320kbps 的 MP3 再次转码为 320kbps 的 MP3）亦不例外。这意味着，其 offset 值必然与转码前的音频有所差异<span class="offset-notice-emph-red">（特别强调，此为必然情况）</span>。</p>
          <p class="offset-notice-p">鉴于 Offset 属于容错率极低的谱面元数据，在进入下一个 Offset 测算界面之前，请务必确保您此前导入的音频，为最终即将上传游戏服务器（或即将向公众分发）时所使用的版本，以确保 Offset 尽可能准确。若非如此，您应当返回之前的 BPM 计算窗口，导入对应音频后，再重新进入 Offset 测算界面。</p>
        </div>
      </div>
      <div class="offset-notice-item">
        <span class="offset-notice-num">2</span>
        <div class="offset-notice-content">
          <p class="offset-notice-p"><b>Offset 换算方法：</b></p>
          <ul class="offset-notice-sublist">
            <li><b>直接定位首键（第 1 拍）</b>：在包络图中选定首个按键的起峰位置并读取其毫秒值，再减去谱面开头预留空拍所对应的时长即可（即：<code class="offset-notice-code">所读毫秒值 − 60 ÷ BPM × 预留空拍数 × 1000</code>）。</li>
            <li><b>借后续重拍反推（当开头第 1 拍起峰模糊时）</b>：若曲目开头渐入柔和、难以直接看清第 1 拍起峰，可改找后续任意起峰清晰的重拍（例如曲目的<b>第 5 拍</b>）读取毫秒值；由于第 5 拍比第 1 拍晚了 <code class="offset-notice-code">5 − 1 = 4</code> 拍，只需在开头预留空拍数的基础上<b>额外多减去 <code class="offset-notice-code">(所选拍序 − 1)</code> 拍</b>的时长，即可倒推出准确的谱面起始 Offset。</li>
            <li><b>计算示例</b>：假设读取到首个按键（第 1 拍）位于 <code class="offset-notice-code">6000</code> 毫秒处，BPM 为 <code class="offset-notice-code">180</code>，开头预留 <code class="offset-notice-code">16</code> 拍，则列式：<code class="offset-notice-code">6000 − (60 ÷ 180 × 16 × 1000)</code>，取整得 <code class="offset-notice-code">667</code>，故您的 Offset 应设为 <b><code class="offset-notice-code">667</code> 毫秒</b>（若该 <code class="offset-notice-code">6000</code> 毫秒处实际上是曲目的<b>第 5 拍</b>而非第 1 拍，则将式中的 <code class="offset-notice-code">16</code> 换为 <code class="offset-notice-code">16 + (5 − 1) = 20</code> 拍进行计算即可）。</li>
            <li>（弹窗里不可能大篇幅写高阶教程。这里只说明基础技巧。）</li>
          </ul>
        </div>
      </div>
      <div class="offset-notice-item">
        <span class="offset-notice-num">3</span>
        <div class="offset-notice-content">
          <p class="offset-notice-p">若 Offset 算出负数，您可能需要先在音频开头添加静音，或退而求其次，将第一个按键延后放置。</p>
        </div>
      </div>
    </div>
    <div class="modal-footer offset-notice-footer">
      <label class="offset-notice-skip-wrap">
        <input type="checkbox" id="chkSkipOffsetPreNotice">
        <span>本次运行期间不再提示</span>
      </label>
      <div class="modal-footer-right">
        <button type="button" class="m3-btn m3-btn-tonal" id="cancelOffsetPreNoticeBtn">返回检查音频</button>
        <button type="button" class="m3-btn m3-btn-filled" id="confirmOffsetPreNoticeBtn">已确认，进入测算</button>
      </div>
    </div>
  </div>
</div>
"""

_OFFSET_JS = r"""
<script>
(() => {
  const TOTAL_MS = 60000;
  const MIN_VIEW_SPAN_MS = 200;

  const headerEl = document.querySelector("header");
  const mainLayoutEl = document.querySelector(".main-layout");
  const offsetEntryBar = document.getElementById("offsetEntryBar");
  const offsetEntryHint = document.getElementById("offsetEntryHint");
  const openOffsetViewLink = document.getElementById("openOffsetViewLink");

  const secondaryView = document.getElementById("offsetSecondaryView");
  const backBtn = document.getElementById("offsetBackBtn");
  const songTitleEl = document.getElementById("offsetSongTitle");
  const songSubEl = document.getElementById("offsetSongSub");
  const hoverMsValEl = document.getElementById("offsetHoverMsVal");
  const cursorMsValEl = document.getElementById("offsetCursorMsVal");
  const copyCursorMsBtn = document.getElementById("copyCursorMsBtn");
  const copyToastEl = document.getElementById("offsetCopyToast");

  const playPauseBtn = document.getElementById("offsetPlayPauseBtn");
  const rewindCursorBtn = document.getElementById("offsetRewindCursorBtn");
  const preservePitchChk = document.getElementById("offsetPreservePitchChk");
  const pitchWarnEl = document.getElementById("offsetPitchWarn");
  const zoomInBtn = document.getElementById("offsetZoomInBtn");
  const zoomOutBtn = document.getElementById("offsetZoomOutBtn");
  const zoomInitBtn = document.getElementById("offsetZoomInitBtn");
  const zoomResetBtn = document.getElementById("offsetZoomResetBtn");

  const colorPickerInput = document.getElementById("inputOffsetCurveColor");
  const colorSwatchEl = document.getElementById("offsetCurveColorSwatch");
  const colorHexInput = document.getElementById("inputOffsetCurveHex");
  const colorAlphaInput = document.getElementById("inputOffsetCurveAlpha");

  const DEFAULT_CURVE_HEX8 = "#3fb950ff";
  const OFFSET_CHANNEL_LABELS = {
    auto: "多声道能量融合",
    left: "只分析左声道",
    right: "只分析右声道",
    downmix: "传统单声道混音（旧版）",
  };

  function getCommittedChannelMode() {
    const raw = (state && state.settings && typeof state.settings.channel === "string")
      ? state.settings.channel.trim().toLowerCase()
      : "auto";
    return Object.prototype.hasOwnProperty.call(OFFSET_CHANNEL_LABELS, raw) ? raw : "auto";
  }

  function parseHex8Color(raw, fallbackAlphaInt = 255) {
    if (typeof raw !== "string") return null;
    let s = raw.trim();
    if (s.startsWith("#")) s = s.slice(1);
    const fbA = Math.max(0, Math.min(255, Math.round(Number.isFinite(fallbackAlphaInt) ? fallbackAlphaInt : 255)));

    let r, g, b, a;
    if (/^[0-9a-fA-F]{8}$/.test(s)) {
      r = parseInt(s.slice(0, 2), 16);
      g = parseInt(s.slice(2, 4), 16);
      b = parseInt(s.slice(4, 6), 16);
      a = parseInt(s.slice(6, 8), 16);
    } else if (/^[0-9a-fA-F]{6}$/.test(s)) {
      r = parseInt(s.slice(0, 2), 16);
      g = parseInt(s.slice(2, 4), 16);
      b = parseInt(s.slice(4, 6), 16);
      a = fbA;
    } else if (/^[0-9a-fA-F]{4}$/.test(s)) {
      r = parseInt(s[0] + s[0], 16);
      g = parseInt(s[1] + s[1], 16);
      b = parseInt(s[2] + s[2], 16);
      a = parseInt(s[3] + s[3], 16);
    } else if (/^[0-9a-fA-F]{3}$/.test(s)) {
      r = parseInt(s[0] + s[0], 16);
      g = parseInt(s[1] + s[1], 16);
      b = parseInt(s[2] + s[2], 16);
      a = fbA;
    } else {
      return null;
    }

    const hx = (n) => n.toString(16).padStart(2, "0");
    const hex6 = `#${hx(r)}${hx(g)}${hx(b)}`;
    const hex8 = `${hex6}${hx(a)}`;
    return { r, g, b, a, hex6, hex8 };
  }

  function formatCssRgba(r, g, b, aInt) {
    const alpha = Math.round((Math.max(0, Math.min(255, aInt)) / 255) * 10000) / 10000;
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
  }

  function formatBrightDotCssRgba(r, g, b, aInt, ratio = 0.35) {
    const br = Math.round(r + (255 - r) * ratio);
    const bg = Math.round(g + (255 - g) * ratio);
    const bb = Math.round(b + (255 - b) * ratio);
    return formatCssRgba(br, bg, bb, aInt);
  }

  function getCommittedCurveColorParts() {
    const raw = (state && state.settings && typeof state.settings.offsetCurveColor === "string")
      ? state.settings.offsetCurveColor
      : DEFAULT_CURVE_HEX8;
    return parseHex8Color(raw, 255) || parseHex8Color(DEFAULT_CURVE_HEX8, 255);
  }

  function getDraftCurveColorParts() {
    const raw = (state && state.draftSettings && typeof state.draftSettings.offsetCurveColor === "string")
      ? state.draftSettings.offsetCurveColor
      : DEFAULT_CURVE_HEX8;
    return parseHex8Color(raw, 255) || parseHex8Color(DEFAULT_CURVE_HEX8, 255);
  }

  function getOffsetInitViewSec() {
    const raw = (state && state.settings && typeof state.settings.offsetInitSec === "number")
      ? Math.round(state.settings.offsetInitSec)
      : 20;
    return Math.max(2, Math.min(60, raw));
  }

  function syncOffsetInitSecHints() {
    const initSec = getOffsetInitViewSec();
    if (offsetEntryHint) {
      offsetEntryHint.textContent = initSec < 60
        ? `默认显示前 ${initSec} 秒（可缩放到满 60 秒） · 32 位浮点 PCM 音画同源慢放（不足 60 秒不予分析）`
        : "默认显示前 60 秒全览 · 32 位浮点 PCM 音画同源慢放（不足 60 秒不予分析）";
    }
    if (zoomInitBtn) {
      zoomInitBtn.textContent = `前 ${initSec}s`;
      zoomInitBtn.title = `回到初始 0~${initSec}s 视野`;
      zoomInitBtn.style.display = initSec < 60 ? "" : "none";
    }
  }

  const loadingPanel = document.getElementById("offsetLoadingPanel");
  const loadingTextEl = document.getElementById("offsetLoadingText");
  const workspaceEl = document.getElementById("offsetWorkspace");
  const viewportSpanLabel = document.getElementById("offsetViewportSpanLabel");
  const minimapCanvas = document.getElementById("offsetMinimapCanvas");
  const mainCanvasWrap = document.getElementById("offsetMainCanvasWrap");
  const scrollHost = document.getElementById("offsetMainScrollHost");
  const scrollSizer = document.getElementById("offsetMainScrollSizer");
  const stickyStage = document.getElementById("offsetMainStickyStage");
  const mainCanvas = document.getElementById("offsetMainCanvas");
  const hoverTooltip = document.getElementById("offsetHoverTooltip");

  const shortModal = document.getElementById("offsetShortAudioModal");
  const shortModalMsg = document.getElementById("offsetShortAudioMsg");
  const closeShortModalBtn = document.getElementById("closeOffsetShortModalBtn");

  const preNoticeModal = document.getElementById("offsetPreNoticeModal");
  const chkSkipPreNotice = document.getElementById("chkSkipOffsetPreNotice");
  const cancelPreNoticeBtn = document.getElementById("cancelOffsetPreNoticeBtn");
  const confirmPreNoticeBtn = document.getElementById("confirmOffsetPreNoticeBtn");

  const MAX_Y_GAIN = 16.0;

  const offState = {
    isOpen: false,
    skipPreNoticeInSession: false,
    abortCtrl: null,
    sampleRate: 44100,
    pcmChannels: 1,
    diffF32: null,
    pcmF32: null,
    maxPosPeak: 1.0,
    maxAbsPeak: 1.0,

    // 视图状态
    viewStartMs: 0,
    viewEndMs: TOTAL_MS,
    polarity: "pos", // "pos" | "full"
    yGain: 1,
    yPanNorm: 1.0,       // 0.0=最顶端, 1.0=最底端（pos 默认锚定底部 1.0，full 默认锚定中部 0.5）
    yPannedByUser: false,
    cursorMs: 0,
    hoverMs: null,

    // 音频播放状态
    audioCtx: null,
    audioBuffer: null,
    bufSource: null,
    wavBlobUrl: null,
    htmlAudio: null,
    isPlaying: false,
    playbackRate: 1.0,
    preservePitch: false,
    playStartCtxTime: 0,
    playStartSongSec: 0,
    livePlayheadMs: 0,
    rafId: null,
    toastTimer: null,
  };

  function showShortAudioModal(msg) {
    if (!shortModal || !shortModalMsg) return;
    shortModalMsg.textContent = msg || "当前音频实际有效时长不足 60 秒，不予分析前 1 分钟起峰包络图。";
    shortModal.classList.add("open");
  }

  function closeShortAudioModal() {
    if (shortModal) shortModal.classList.remove("open");
  }

  function showPreNoticeModal() {
    if (!preNoticeModal) return;
    if (chkSkipPreNotice) chkSkipPreNotice.checked = false;
    preNoticeModal.classList.add("open");
  }

  function closePreNoticeModal() {
    if (preNoticeModal) preNoticeModal.classList.remove("open");
  }

  if (closeShortModalBtn) {
    closeShortModalBtn.addEventListener("click", closeShortAudioModal);
  }
  if (shortModal) {
    shortModal.addEventListener("click", (e) => {
      if (e.target === shortModal) closeShortAudioModal();
    });
  }

  if (cancelPreNoticeBtn) {
    cancelPreNoticeBtn.addEventListener("click", closePreNoticeModal);
  }
  if (preNoticeModal) {
    preNoticeModal.addEventListener("click", (e) => {
      if (e.target === preNoticeModal) closePreNoticeModal();
    });
  }
  if (confirmPreNoticeBtn) {
    confirmPreNoticeBtn.addEventListener("click", () => {
      if (chkSkipPreNotice && chkSkipPreNotice.checked) {
        offState.skipPreNoticeInSession = true;
      }
      closePreNoticeModal();
      openOffsetSecondaryView();
    });
  }

  function getKnownItemDurationSec(item) {
    if (!item || !item.data || !item.data.results) return null;
    const res = item.data.results;
    for (const k of ["enhanced_v3", "exact", "force_v3", "enhanced_v2", "enhanced"]) {
      if (res[k] && typeof res[k].duration_sec === "number" && res[k].duration_sec > 0) {
        return res[k].duration_sec;
      }
    }
    return null;
  }

  function flashCopyToast(msInt) {
    if (!copyToastEl) return;
    if (offState.toastTimer) clearTimeout(offState.toastTimer);
    copyToastEl.textContent = `已复制 ${msInt} ✓`;
    copyToastEl.classList.add("show");
    offState.toastTimer = setTimeout(() => {
      copyToastEl.classList.remove("show");
      offState.toastTimer = null;
    }, 1300);
  }

  function copyPositiveMs(msVal) {
    const cleanInt = String(Math.max(0, Math.min(TOTAL_MS, Math.round(msVal))));
    const done = () => flashCopyToast(cleanInt);
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(cleanInt).then(done).catch(() => fallbackCopy(cleanInt, done));
    } else {
      fallbackCopy(cleanInt, done);
    }
  }

  function formatMmSsMs(ms) {
    const clamped = Math.max(0, Math.min(TOTAL_MS, Math.round(ms)));
    const sec = Math.floor(clamped / 1000);
    const remMs = clamped % 1000;
    const mm = String(Math.floor(sec / 60)).padStart(2, "0");
    const ss = String(sec % 60).padStart(2, "0");
    const mmm = String(remMs).padStart(3, "0");
    return `${mm}:${ss}.${mmm}`;
  }

  // 构造内存 44 字节 IEEE Float 32-bit 单/双声道 WAV (wFormatTag = 3)，零重编码、零编码延迟
  function ensureFloatWavAudioElement() {
    if (offState.htmlAudio && offState.wavBlobUrl) {
      return offState.htmlAudio;
    }
    const pcm = offState.pcmF32;
    const sr = offState.sampleRate;
    const nCh = Math.max(1, offState.pcmChannels || 1);
    const dataBytes = pcm.byteLength;
    const header = new ArrayBuffer(44);
    const dv = new DataView(header);
    const writeAscii = (off, str) => {
      for (let i = 0; i < str.length; i++) dv.setUint8(off + i, str.charCodeAt(i));
    };
    writeAscii(0, "RIFF");
    dv.setUint32(4, 36 + dataBytes, true);
    writeAscii(8, "WAVE");
    writeAscii(12, "fmt ");
    dv.setUint32(16, 16, true);
    dv.setUint16(20, 3, true);             // WAVE_FORMAT_IEEE_FLOAT = 3
    dv.setUint16(22, nCh, true);           // 声道数 (1 或 2)
    dv.setUint32(24, sr, true);
    dv.setUint32(28, sr * nCh * 4, true);  // byteRate
    dv.setUint16(32, nCh * 4, true);       // blockAlign
    dv.setUint16(34, 32, true);            // bitsPerSample = 32
    writeAscii(36, "data");
    dv.setUint32(40, dataBytes, true);

    const blob = new Blob([header, pcm], { type: "audio/wav" });
    offState.wavBlobUrl = URL.createObjectURL(blob);
    const audio = new Audio(offState.wavBlobUrl);
    audio.preload = "auto";
    offState.htmlAudio = audio;
    audio.addEventListener("ended", () => {
      if (offState.isPlaying && offState.preservePitch) {
        stopPlayback(true);
      }
    });
    return audio;
  }

  function getLivePlayheadSec() {
    if (!offState.isPlaying) {
      return offState.livePlayheadMs / 1000.0;
    }
    if (offState.preservePitch && offState.htmlAudio) {
      return Math.min(60.0, Math.max(0.0, offState.htmlAudio.currentTime));
    }
    const ctx = offState.audioCtx;
    if (!ctx) return offState.livePlayheadMs / 1000.0;

    let outTime = ctx.currentTime;
    if (typeof ctx.getOutputTimestamp === "function") {
      try {
        const ts = ctx.getOutputTimestamp();
        if (ts && typeof ts.contextTime === "number" && ts.contextTime > 0) {
          outTime = ts.contextTime;
        }
      } catch (_) {}
    } else if (typeof ctx.baseLatency === "number") {
      outTime = Math.max(0, ctx.currentTime - (ctx.baseLatency + (ctx.outputLatency || 0)));
    }
    const elapsedAudio = Math.max(0, outTime - offState.playStartCtxTime);
    return Math.min(60.0, offState.playStartSongSec + elapsedAudio * offState.playbackRate);
  }

  function stopPlayback(resetToCursor = false) {
    if (offState.rafId !== null) {
      cancelAnimationFrame(offState.rafId);
      offState.rafId = null;
    }
    if (offState.bufSource) {
      try {
        offState.bufSource.onended = null;
        offState.bufSource.stop();
        offState.bufSource.disconnect();
      } catch (_) {}
      offState.bufSource = null;
    }
    if (offState.htmlAudio) {
      try {
        offState.htmlAudio.pause();
      } catch (_) {}
    }
    offState.isPlaying = false;
    if (resetToCursor) {
      offState.livePlayheadMs = offState.cursorMs;
    }
    if (playPauseBtn) {
      playPauseBtn.textContent = "▶ 播放";
    }
    if (offState.isOpen && offState.diffF32) {
      drawOffsetAll();
    }
  }

  function tickPlayheadLoop() {
    if (!offState.isPlaying || !offState.isOpen) return;
    const curSec = getLivePlayheadSec();
    const curMs = curSec * 1000.0;
    offState.livePlayheadMs = Math.min(TOTAL_MS, curMs);

    if (curMs >= TOTAL_MS - 1) {
      stopPlayback(true);
      return;
    }

    // 若走动指针超出当前放大视野右侧 92%，自动平滑翻页跟随并同步系统横向滚动条
    const span = offState.viewEndMs - offState.viewStartMs;
    if (curMs > offState.viewStartMs + span * 0.92 && offState.viewEndMs < TOTAL_MS) {
      const shift = Math.min(TOTAL_MS - offState.viewEndMs, span * 0.80);
      offState.viewStartMs += shift;
      offState.viewEndMs += shift;
      updateViewportSpanReadout();
      syncScrollHostFromState();
    }

    drawOffsetAll();
    offState.rafId = requestAnimationFrame(tickPlayheadLoop);
  }

  async function startPlaybackFrom(startMs) {
    if (!offState.pcmF32) return;
    stopPlayback(false);

    const clampedStartMs = (startMs >= TOTAL_MS - 20) ? 0 : Math.max(0, startMs);
    const startSec = clampedStartMs / 1000.0;
    offState.livePlayheadMs = clampedStartMs;

    if (offState.preservePitch) {
      const audio = ensureFloatWavAudioElement();
      audio.preservesPitch = true;
      if ("mozPreservesPitch" in audio) audio.mozPreservesPitch = true;
      if ("webkitPreservesPitch" in audio) audio.webkitPreservesPitch = true;
      audio.playbackRate = offState.playbackRate;
      audio.currentTime = startSec;
      try {
        await audio.play();
      } catch (_) {
        return;
      }
      offState.isPlaying = true;
    } else {
      if (!offState.audioCtx) {
        const AC = window.AudioContext || window.webkitAudioContext;
        offState.audioCtx = new AC();
      }
      const ctx = offState.audioCtx;
      if (ctx.state === "suspended") {
        await ctx.resume();
      }
      if (!offState.audioBuffer) {
        const nCh = Math.max(1, offState.pcmChannels || 1);
        const nFrames = Math.floor(offState.pcmF32.length / nCh);
        const buf = ctx.createBuffer(nCh, nFrames, offState.sampleRate);
        if (nCh === 1) {
          buf.copyToChannel(offState.pcmF32, 0);
        } else {
          const srcInterleaved = offState.pcmF32;
          for (let c = 0; c < nCh; c++) {
            const chArr = buf.getChannelData(c);
            for (let i = 0; i < nFrames; i++) {
              chArr[i] = srcInterleaved[i * nCh + c];
            }
          }
        }
        offState.audioBuffer = buf;
      }
      const src = ctx.createBufferSource();
      src.buffer = offState.audioBuffer;
      src.playbackRate.value = offState.playbackRate;
      src.connect(ctx.destination);
      offState.bufSource = src;
      offState.playStartCtxTime = ctx.currentTime;
      offState.playStartSongSec = startSec;
      offState.isPlaying = true;
      src.onended = () => {
        if (offState.bufSource === src) {
          stopPlayback(true);
        }
      };
      src.start(0, startSec);
    }

    if (playPauseBtn) {
      playPauseBtn.textContent = "⏸ 暂停";
    }
    offState.rafId = requestAnimationFrame(tickPlayheadLoop);
  }

  function togglePlayPause() {
    if (!offState.pcmF32) return;
    if (offState.isPlaying) {
      const pausedAt = Math.round(getLivePlayheadSec() * 1000.0);
      stopPlayback(false);
      offState.livePlayheadMs = Math.max(0, Math.min(TOTAL_MS, pausedAt));
      drawOffsetAll();
    } else {
      // 若上次暂停在半途则从暂停处续播，否则从蓝色播放起点光标开始
      const resumeFrom = (offState.livePlayheadMs > offState.cursorMs && offState.livePlayheadMs < TOTAL_MS - 20)
        ? offState.livePlayheadMs
        : offState.cursorMs;
      startPlaybackFrom(resumeFrom);
    }
  }

  function releaseAudioAndBuffers() {
    stopPlayback(false);
    if (offState.abortCtrl) {
      try { offState.abortCtrl.abort(); } catch (_) {}
      offState.abortCtrl = null;
    }
    if (offState.htmlAudio) {
      try {
        offState.htmlAudio.pause();
        offState.htmlAudio.removeAttribute("src");
        offState.htmlAudio.load();
      } catch (_) {}
      offState.htmlAudio = null;
    }
    if (offState.wavBlobUrl) {
      URL.revokeObjectURL(offState.wavBlobUrl);
      offState.wavBlobUrl = null;
    }
    offState.audioBuffer = null;
    offState.diffF32 = null;
    offState.pcmF32 = null;
  }

  function updateCursorReadout() {
    if (cursorMsValEl) {
      cursorMsValEl.textContent = `${Math.round(offState.cursorMs)} ms`;
    }
  }

  let lastProgSL = 0;
  let lastProgST = 0;

  function getDefaultYPanNorm(pol = offState.polarity) {
    return pol === "pos" ? 1.0 : 0.5;
  }

  function getStageViewportSize() {
    if (scrollHost && scrollHost.clientWidth > 4 && scrollHost.clientHeight > 4) {
      return { w: scrollHost.clientWidth, h: scrollHost.clientHeight };
    }
    if (mainCanvasWrap) {
      const r = mainCanvasWrap.getBoundingClientRect();
      if (r.width > 4 && r.height > 4) {
        return { w: r.width, h: r.height };
      }
    }
    return { w: 0, h: 0 };
  }

  function formatGainText(g) {
    const r = Math.round(g * 100) / 100;
    return Number.isInteger(r) ? String(r) : r.toFixed(1).replace(/\.0$/, "");
  }

  function updateViewportSpanReadout() {
    if (!viewportSpanLabel) return;
    const s = offState.viewStartMs;
    const span = offState.viewEndMs - offState.viewStartMs;
    /* 修正 JS 模板字符串花括号闭合位置，避免语法解析错误阻断后续事件绑定 */
    viewportSpanLabel.textContent =
      `当前视野：${(s / 1000).toFixed(3)}s - ${((s + span) / 1000).toFixed(3)}s (跨度 ${(span / 1000).toFixed(3)}s) · 纵向 ${formatGainText(offState.yGain)}倍`;
  }

  function syncGainButtonsUI() {
    document.querySelectorAll("#offsetGainGroup .seg-btn").forEach((b) => {
      const btnG = parseFloat(b.dataset.gain || "0");
      b.classList.toggle("active", Math.abs(btnG - offState.yGain) < 0.03);
    });
  }

  function syncScrollHostFromState() {
    if (!scrollHost || !scrollSizer || !stickyStage) return;
    const { w: clientW, h: clientH } = getStageViewportSize();
    if (clientW <= 4 || clientH <= 4) return;

    const spanMs = Math.max(MIN_VIEW_SPAN_MS, offState.viewEndMs - offState.viewStartMs);
    const xZoom = TOTAL_MS / spanMs;
    const yZoom = Math.max(1.0, offState.yGain);

    const virtualW = Math.max(clientW, Math.round(clientW * xZoom));
    const virtualH = Math.max(clientH, Math.round(clientH * yZoom));

    scrollSizer.style.width = `${virtualW}px`;
    scrollSizer.style.height = `${virtualH}px`;
    stickyStage.style.width = `${clientW}px`;
    stickyStage.style.height = `${clientH}px`;

    const maxSL = Math.max(0, virtualW - clientW);
    const maxST = Math.max(0, virtualH - clientH);

    const targetSL = (TOTAL_MS > spanMs && maxSL > 0)
      ? Math.round((offState.viewStartMs / (TOTAL_MS - spanMs)) * maxSL)
      : 0;
    const targetST = (yZoom > 1.0001 && maxST > 0)
      ? Math.round(Math.max(0, Math.min(1, offState.yPanNorm)) * maxST)
      : 0;

    if (Math.abs(scrollHost.scrollLeft - targetSL) > 0.5) {
      scrollHost.scrollLeft = targetSL;
    }
    if (Math.abs(scrollHost.scrollTop - targetST) > 0.5) {
      scrollHost.scrollTop = targetST;
    }
    lastProgSL = scrollHost.scrollLeft;
    lastProgST = scrollHost.scrollTop;
  }

  if (scrollHost) {
    scrollHost.addEventListener("scroll", () => {
      if (!offState.isOpen || !offState.diffF32) return;
      const curSL = scrollHost.scrollLeft;
      const curST = scrollHost.scrollTop;
      const xChanged = Math.abs(curSL - lastProgSL) > 0.5;
      const yChanged = Math.abs(curST - lastProgST) > 0.5;
      if (!xChanged && !yChanged) return;

      const { w: clientW, h: clientH } = getStageViewportSize();
      const virtualW = parseFloat(scrollSizer && scrollSizer.style.width) || scrollHost.scrollWidth || clientW;
      const virtualH = parseFloat(scrollSizer && scrollSizer.style.height) || scrollHost.scrollHeight || clientH;
      const maxSL = Math.max(0, virtualW - clientW);
      const maxST = Math.max(0, virtualH - clientH);

      if (xChanged) {
        lastProgSL = curSL;
        const spanMs = offState.viewEndMs - offState.viewStartMs;
        if (TOTAL_MS > spanMs && maxSL > 0) {
          const ratioX = Math.max(0, Math.min(1, curSL / maxSL));
          const nextStart = ratioX * (TOTAL_MS - spanMs);
          offState.viewStartMs = nextStart;
          offState.viewEndMs = nextStart + spanMs;
        }
      }
      if (yChanged) {
        lastProgST = curST;
        if (offState.yGain > 1.0001 && maxST > 0) {
          offState.yPanNorm = Math.max(0, Math.min(1, curST / maxST));
          offState.yPannedByUser = true;
        }
      }
      updateViewportSpanReadout();
      drawOffsetAll();
    });
  }

  function setViewport(startMs, endMs) {
    let span = Math.max(MIN_VIEW_SPAN_MS, Math.min(TOTAL_MS, endMs - startMs));
    let s = Math.max(0, Math.min(TOTAL_MS - span, startMs));
    offState.viewStartMs = s;
    offState.viewEndMs = s + span;
    updateViewportSpanReadout();
    syncScrollHostFromState();
    drawOffsetAll();
  }

  function applyVerticalGain(nextGainRaw, anchorClientY = null) {
    const clamped = nextGainRaw < 1.02
      ? 1.0
      : Math.max(1.0, Math.min(MAX_Y_GAIN, Math.round(nextGainRaw * 100) / 100));

    if (clamped <= 1.0001) {
      offState.yGain = 1.0;
      offState.yPanNorm = getDefaultYPanNorm();
      offState.yPannedByUser = false;
    } else if (!offState.yPannedByUser) {
      offState.yGain = clamped;
      offState.yPanNorm = getDefaultYPanNorm();
    } else {
      const { h: stageH } = getStageViewportSize();
      const padT = 26, padB = 30;
      const plotH = Math.max(10, stageH - padT - padB);
      let relY = plotH * 0.5;
      if (anchorClientY !== null && mainCanvas) {
        const rect = mainCanvas.getBoundingClientRect();
        relY = Math.max(0, Math.min(plotH, anchorClientY - rect.top - padT));
      }
      const curVirtualPlotH = plotH * offState.yGain;
      const curPanPx = offState.yPanNorm * Math.max(0, curVirtualPlotH - plotH);
      const anchorRatio = (curPanPx + relY) / Math.max(1e-6, curVirtualPlotH);

      const nextVirtualPlotH = plotH * clamped;
      const nextMaxPanPx = Math.max(1e-6, nextVirtualPlotH - plotH);
      const nextPanPx = anchorRatio * nextVirtualPlotH - relY;
      offState.yGain = clamped;
      offState.yPanNorm = Math.max(0, Math.min(1, nextPanPx / nextMaxPanPx));
    }

    syncGainButtonsUI();
    updateViewportSpanReadout();
    syncScrollHostFromState();
    drawOffsetAll();
  }

  function zoomAroundMs(anchorMs, factor) {
    const curSpan = offState.viewEndMs - offState.viewStartMs;
    const nextSpan = Math.max(MIN_VIEW_SPAN_MS, Math.min(TOTAL_MS, curSpan * factor));
    const ratio = (anchorMs - offState.viewStartMs) / curSpan;
    const nextStart = anchorMs - ratio * nextSpan;
    setViewport(nextStart, nextStart + nextSpan);
  }

  function drawMinimap() {
    if (!minimapCanvas || !offState.diffF32) return;
    const rect = minimapCanvas.parentElement.getBoundingClientRect();
    if (rect.width <= 2 || rect.height <= 2) return;
    const dpr = window.devicePixelRatio || 1;
    minimapCanvas.width = Math.round(rect.width * dpr);
    minimapCanvas.height = Math.round(rect.height * dpr);
    const ctx = minimapCanvas.getContext("2d");
    ctx.scale(dpr, dpr);

    const W = rect.width;
    const H = rect.height;
    ctx.clearRect(0, 0, W, H);

    const diff = offState.diffF32;
    // 缩略导航条纵向比例始终固定为 1× 全局正峰值，不随下方大图纵向缩放变化
    const peakScale = Math.max(1e-6, offState.maxPosPeak);
    const cols = Math.max(1, Math.floor(W));

    ctx.fillStyle = "rgba(88, 166, 255, 0.55)";
    for (let px = 0; px < cols; px++) {
      const m0 = Math.floor((px / cols) * TOTAL_MS);
      const m1 = Math.min(TOTAL_MS, Math.max(m0 + 1, Math.floor(((px + 1) / cols) * TOTAL_MS)));
      let maxV = 0;
      for (let m = m0; m < m1; m++) {
        if (diff[m] > maxV) maxV = diff[m];
      }
      if (maxV > 0) {
        const normH = Math.min(1.0, maxV / peakScale) * (H - 6);
        ctx.fillRect(px, H - 2 - normH, 1, normH);
      }
    }

    // 视野窗口高亮框
    const vx0 = (offState.viewStartMs / TOTAL_MS) * W;
    const vx1 = (offState.viewEndMs / TOTAL_MS) * W;
    const vw = Math.max(4, vx1 - vx0);
    ctx.fillStyle = "rgba(255, 255, 255, 0.09)";
    ctx.fillRect(vx0, 0, vw, H);
    ctx.strokeStyle = "rgba(138, 180, 248, 0.9)";
    ctx.lineWidth = 1.5;
    ctx.strokeRect(vx0 + 0.5, 0.5, Math.max(2, vw - 1), H - 1);

    // 播放起点光标与实时播放指针
    const cx = (offState.cursorMs / TOTAL_MS) * W;
    ctx.strokeStyle = "#58a6ff";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(cx, 0);
    ctx.lineTo(cx, H);
    ctx.stroke();

    if (offState.isPlaying || Math.abs(offState.livePlayheadMs - offState.cursorMs) > 0.5) {
      const px = (offState.livePlayheadMs / TOTAL_MS) * W;
      ctx.strokeStyle = "#f85149";
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(px, 0);
      ctx.lineTo(px, H);
      ctx.stroke();
    }
  }

  function pickNiceTickStepMs(spanMs, plotW) {
    const targetTicks = Math.max(4, Math.floor(plotW / 110));
    const rawStep = spanMs / targetTicks;
    const steps = [5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000];
    for (const s of steps) {
      if (s >= rawStep) return s;
    }
    return 10000;
  }

  function drawMainEnvelope() {
    if (!mainCanvas || !offState.diffF32) return;
    const { w: W, h: H } = getStageViewportSize();
    if (W <= 4 || H <= 4) return;
    if (stickyStage) {
      stickyStage.style.width = `${W}px`;
      stickyStage.style.height = `${H}px`;
    }
    const dpr = window.devicePixelRatio || 1;
    mainCanvas.width = Math.round(W * dpr);
    mainCanvas.height = Math.round(H * dpr);
    const ctx = mainCanvas.getContext("2d");
    ctx.scale(dpr, dpr);

    ctx.clearRect(0, 0, W, H);

    const padL = 16, padR = 16, padT = 26, padB = 30;
    const plotW = Math.max(10, W - padL - padR);
    const plotH = Math.max(10, H - padT - padB);

    const startMs = offState.viewStartMs;
    const endMs = offState.viewEndMs;
    const spanMs = endMs - startMs;
    const isPosOnly = (offState.polarity === "pos");
    const diff = offState.diffF32;

    const xOfMs = (ms) => padL + ((ms - startMs) / spanMs) * plotW;

    // 真实无截平纵向虚拟高度映射（支持 Y×1 ~ Y×16 全景上下平移查看波峰尖顶）
    const virtualPlotH = plotH * Math.max(1.0, offState.yGain);
    const panOffsetPx = (offState.yGain > 1.0001)
      ? Math.max(0, Math.min(1, offState.yPanNorm)) * (virtualPlotH - plotH)
      : 0;

    let zeroY;
    let yOfVal;
    if (isPosOnly) {
      const safePeak = Math.max(1e-6, offState.maxPosPeak);
      zeroY = padT + virtualPlotH - panOffsetPx;
      yOfVal = (v) => {
        const posV = Math.max(0, v);
        const virtualY = virtualPlotH * (1 - posV / safePeak);
        return padT + virtualY - panOffsetPx;
      };
    } else {
      const safePeak = Math.max(1e-6, offState.maxAbsPeak);
      const virtualZeroY = virtualPlotH * 0.5;
      const halfAmpPx = Math.max(4, virtualPlotH * 0.5 - plotH * 0.02);
      zeroY = padT + virtualZeroY - panOffsetPx;
      yOfVal = (v) => padT + (virtualZeroY - (v / safePeak) * halfAmpPx) - panOffsetPx;
    }

    // 1. 时间网格刻度（保持固定在可视窗口内）
    const stepMs = pickNiceTickStepMs(spanMs, plotW);
    const firstTick = Math.ceil(startMs / stepMs) * stepMs;
    ctx.font = '11px "Helvetica Neue", Arial, "PingFang SC", "Microsoft YaHei", sans-serif';
    ctx.textAlign = "center";

    for (let m = firstTick; m <= endMs; m += stepMs) {
      const x = xOfMs(m);
      ctx.strokeStyle = "rgba(48, 48, 48, 0.52)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, padT - 4);
      ctx.lineTo(x, H - padB);
      ctx.stroke();

      ctx.fillStyle = "#8b949e";
      /* 时间轴刻度精简：统一采用秒单位「Xs」（放大后自适应呈现 0.1s/0.05s 等），去除冗余括号与毫秒 */
      const digits = stepMs % 1000 === 0 ? 0 : (stepMs % 100 === 0 ? 1 : (stepMs % 10 === 0 ? 2 : 3));
      const label = `${(m / 1000).toFixed(digits)}s`;
      ctx.fillText(label, x, H - 9);
    }

    // 2. 绘图矩形裁剪区内绘制零基线与 diff=1000Hz 纯折线轮廓（超出窗口的高波峰自然裁剪不截平，不遮挡底部时间刻度）
    const curveClr = getCommittedCurveColorParts();
    ctx.save();
    ctx.beginPath();
    ctx.rect(padL, padT, plotW, plotH);
    ctx.clip();

    if (zeroY >= padT - 2 && zeroY <= padT + plotH + 2) {
      ctx.strokeStyle = "rgba(139, 148, 158, 0.42)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(padL, zeroY);
      ctx.lineTo(W - padR, zeroY);
      ctx.stroke();
    }

    ctx.strokeStyle = formatCssRgba(curveClr.r, curveClr.g, curveClr.b, curveClr.a);
    ctx.lineJoin = "round";

    if (spanMs > plotW * 1.4) {
      // 远景：按像素桶提取防漏峰极值，用单根连续折线串联轮廓（不向 zeroY 画实心竖柱）
      const cols = Math.max(1, Math.floor(plotW));
      ctx.lineWidth = 1.35;
      ctx.beginPath();
      for (let px = 0; px < cols; px++) {
        const m0 = Math.max(0, Math.floor(startMs + (px / cols) * spanMs));
        const m1 = Math.min(TOTAL_MS, Math.max(m0 + 1, Math.ceil(startMs + ((px + 1) / cols) * spanMs)));
        let minV = 0, maxV = 0;
        for (let m = m0; m < m1; m++) {
          const v = diff[m];
          if (v > maxV) maxV = v;
          if (!isPosOnly && v < minV) minV = v;
        }
        const repV = isPosOnly
          ? maxV
          : (Math.abs(maxV) >= Math.abs(minV) ? maxV : minV);
        const drawX = padL + px + 0.5;
        const drawY = yOfVal(repV);
        if (px === 0) ctx.moveTo(drawX, drawY);
        else ctx.lineTo(drawX, drawY);
      }
      ctx.stroke();
    } else {
      // 近景：直接按 1ms 采样点连接纯折线（不向 zeroY 做多边形面积填充）
      const mStart = Math.max(0, Math.floor(startMs));
      const mEnd = Math.min(TOTAL_MS - 1, Math.ceil(endMs));

      ctx.lineWidth = 1.5;
      ctx.beginPath();
      for (let m = mStart; m <= mEnd; m++) {
        const v = isPosOnly ? Math.max(0, diff[m]) : diff[m];
        const x = xOfMs(m);
        const y = yOfVal(v);
        if (m === mStart) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.stroke();

      // 超近景（视野 <= 450ms）直接画出每一个 1ms 采样圆点
      if (spanMs <= 450) {
        ctx.fillStyle = formatBrightDotCssRgba(curveClr.r, curveClr.g, curveClr.b, curveClr.a, 0.35);
        for (let m = mStart; m <= mEnd; m++) {
          const v = isPosOnly ? Math.max(0, diff[m]) : diff[m];
          ctx.beginPath();
          ctx.arc(xOfMs(m), yOfVal(v), 2.3, 0, Math.PI * 2);
          ctx.fill();
        }
      }
    }
    ctx.restore();

    // 3. 蓝色播放起点光标 (cursorMs)
    if (offState.cursorMs >= startMs && offState.cursorMs <= endMs) {
      const cx = xOfMs(offState.cursorMs);
      ctx.save();
      ctx.strokeStyle = "#58a6ff";
      ctx.lineWidth = 1.8;
      ctx.beginPath();
      ctx.moveTo(cx, padT - 10);
      ctx.lineTo(cx, H - padB);
      ctx.stroke();

      ctx.fillStyle = "#58a6ff";
      ctx.textAlign = "left";
      ctx.fillText(`起点 ${Math.round(offState.cursorMs)} ms`, Math.min(cx + 5, W - padR - 95), padT - 8);
      ctx.restore();
    }

    // 4. 实时走动播放指针 (livePlayheadMs)
    if (offState.isPlaying || Math.abs(offState.livePlayheadMs - offState.cursorMs) > 0.5) {
      if (offState.livePlayheadMs >= startMs && offState.livePlayheadMs <= endMs) {
        const px = xOfMs(offState.livePlayheadMs);
        ctx.save();
        ctx.strokeStyle = "#ff7b72";
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(px, padT - 10);
        ctx.lineTo(px, H - padB);
        ctx.stroke();
        ctx.restore();
      }
    }

    // 5. 鼠标悬停毫秒十字准星
    if (offState.hoverMs !== null && offState.hoverMs >= startMs && offState.hoverMs <= endMs) {
      const hm = offState.hoverMs;
      const hx = xOfMs(hm);
      const rawVal = diff[Math.min(TOTAL_MS - 1, Math.max(0, hm))];
      const visVal = isPosOnly ? Math.max(0, rawVal) : rawVal;
      const hy = yOfVal(visVal);

      ctx.save();
      ctx.strokeStyle = "rgba(255, 255, 255, 0.55)";
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(hx, padT - 6);
      ctx.lineTo(hx, H - padB);
      ctx.stroke();

      if (hy >= padT - 4 && hy <= padT + plotH + 4) {
        ctx.beginPath();
        ctx.rect(padL, padT, plotW, plotH);
        ctx.clip();
        ctx.fillStyle = "#FFFFFF";
        ctx.beginPath();
        ctx.arc(hx, hy, 4, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.restore();
    }
  }

  function drawOffsetAll() {
    drawMinimap();
    drawMainEnvelope();
  }

  // 主画布交互：普通滚轮横向缩放、Alt+滚轮纵向缩放、按住鼠标二维全向拖拽平移、Ctrl/Cmd+拖动智能混合微调起点线、单击定位、右键复制
  if (mainCanvas) {
    let dragState = null;
    let suppressNextContextMenu = false;

    const updateHoverReadoutAtMs = (hm, clientX, rectWidth) => {
      offState.hoverMs = hm;
      if (hoverMsValEl) hoverMsValEl.textContent = `${hm} ms`;
      if (hoverTooltip && offState.diffF32) {
        const val = offState.diffF32[hm];
        hoverTooltip.style.display = "block";
        hoverTooltip.innerHTML = `时间: <b>${hm} ms</b> (${formatMmSsMs(hm)})<br>diff: <b>${val.toFixed(3)}</b>`;
        const leftPx = Math.min(rectWidth - 235, Math.max(10, clientX - mainCanvas.getBoundingClientRect().left + 14));
        hoverTooltip.style.left = `${leftPx}px`;
      }
    };

    mainCanvas.addEventListener("wheel", (e) => {
      if (!offState.diffF32) return;
      e.preventDefault();
      const rect = mainCanvas.getBoundingClientRect();
      const padL = 16, padR = 16;
      const plotW = Math.max(10, rect.width - padL - padR);
      const span = offState.viewEndMs - offState.viewStartMs;

      // Alt / Option + 滚轮：纵向振幅 1× ~ 16× 无级缩放
      if (e.altKey) {
        const gainFactor = e.deltaY < 0 ? 1.25 : 0.8;
        applyVerticalGain(offState.yGain * gainFactor, e.clientY);
        return;
      }

      // 触控板横向双指滑动直接平移时间轴
      if (!e.ctrlKey && !e.metaKey && Math.abs(e.deltaX) > Math.abs(e.deltaY) && Math.abs(e.deltaX) > 2) {
        const shiftMs = (e.deltaX / plotW) * span;
        setViewport(offState.viewStartMs + shiftMs, offState.viewEndMs + shiftMs);
        return;
      }

      const relX = Math.max(0, Math.min(plotW, e.clientX - rect.left - padL));
      const anchorMs = offState.viewStartMs + (relX / plotW) * span;
      const zoomFactor = e.deltaY < 0 ? 0.76 : 1.32;
      zoomAroundMs(anchorMs, zoomFactor);
    }, { passive: false });

    mainCanvas.addEventListener("pointerdown", (e) => {
      if (!offState.diffF32) return;
      const isModMarker = !!(e.ctrlKey || e.metaKey);
      if (e.button === 2 && !isModMarker) return; // 纯右键留给 contextmenu 复制悬停毫秒
      if (!isModMarker && e.button !== 0 && e.button !== 1) return;

      const rect = mainCanvas.getBoundingClientRect();
      const padL = 16, padR = 16;
      const plotW = Math.max(10, rect.width - padL - padR);
      const span = offState.viewEndMs - offState.viewStartMs;

      if (isModMarker) {
        // Ctrl / Command + 拖动：智能混合微调蓝色定位标记线（靠近 ±12px 内 1/5 阻尼微调，远处先跳至鼠标处再跟随滑动）
        suppressNextContextMenu = true;
        e.preventDefault();
        mainCanvas.setPointerCapture(e.pointerId);

        const pointerX = e.clientX - rect.left;
        const cursorX = padL + ((offState.cursorMs - offState.viewStartMs) / span) * plotW;
        const isNearMarker = (
          offState.cursorMs >= offState.viewStartMs &&
          offState.cursorMs <= offState.viewEndMs &&
          Math.abs(pointerX - cursorX) <= 12
        );

        if (stickyStage) stickyStage.classList.add("is-adjusting-marker");

        if (isNearMarker) {
          dragState = {
            pointerId: e.pointerId,
            mode: "marker-fine",
            startX: e.clientX,
            origCursorMs: offState.cursorMs,
            moved: true,
          };
          updateHoverReadoutAtMs(Math.round(offState.cursorMs), e.clientX, rect.width);
          drawOffsetAll();
        } else {
          const relX = Math.max(0, Math.min(plotW, pointerX - padL));
          const jumpedMs = Math.max(0, Math.min(TOTAL_MS - 1, Math.round(offState.viewStartMs + (relX / plotW) * span)));
          offState.cursorMs = jumpedMs;
          if (!offState.isPlaying) offState.livePlayheadMs = jumpedMs;
          updateCursorReadout();
          updateHoverReadoutAtMs(jumpedMs, e.clientX, rect.width);
          dragState = {
            pointerId: e.pointerId,
            mode: "marker-follow",
            startX: e.clientX,
            origCursorMs: jumpedMs,
            moved: true,
          };
          drawOffsetAll();
        }
        return;
      }

      mainCanvas.setPointerCapture(e.pointerId);
      dragState = {
        pointerId: e.pointerId,
        mode: "pan",
        button: e.button,
        startX: e.clientX,
        startY: e.clientY,
        origStartMs: offState.viewStartMs,
        origEndMs: offState.viewEndMs,
        origYPanNorm: offState.yPanNorm,
        moved: false,
      };
    });

    mainCanvas.addEventListener("pointermove", (e) => {
      if (!offState.diffF32) return;
      const rect = mainCanvas.getBoundingClientRect();
      const padL = 16, padR = 16, padT = 26, padB = 30;
      const plotW = Math.max(10, rect.width - padL - padR);
      const plotH = Math.max(10, rect.height - padT - padB);

      if (dragState && dragState.pointerId === e.pointerId) {
        if (dragState.mode === "marker-fine" || dragState.mode === "marker-follow") {
          const span = offState.viewEndMs - offState.viewStartMs;
          let nextMs;
          if (dragState.mode === "marker-fine") {
            const dx = e.clientX - dragState.startX;
            const deltaMs = (dx / plotW) * span * 0.2;
            nextMs = Math.max(0, Math.min(TOTAL_MS - 1, Math.round(dragState.origCursorMs + deltaMs)));
          } else {
            const relX = Math.max(0, Math.min(plotW, e.clientX - rect.left - padL));
            nextMs = Math.max(0, Math.min(TOTAL_MS - 1, Math.round(offState.viewStartMs + (relX / plotW) * span)));
          }
          offState.cursorMs = nextMs;
          if (!offState.isPlaying) offState.livePlayheadMs = nextMs;
          updateCursorReadout();
          updateHoverReadoutAtMs(nextMs, e.clientX, rect.width);
          drawOffsetAll();
          return;
        }

        const dx = e.clientX - dragState.startX;
        const dy = e.clientY - dragState.startY;
        if (Math.hypot(dx, dy) > 4 || dragState.button === 1) {
          dragState.moved = true;
          if (stickyStage) stickyStage.classList.add("is-dragging");
        }
        if (dragState.moved) {
          const span = dragState.origEndMs - dragState.origStartMs;
          const deltaMs = -(dx / plotW) * span;
          const clampedStart = Math.max(0, Math.min(TOTAL_MS - span, dragState.origStartMs + deltaMs));
          offState.viewStartMs = clampedStart;
          offState.viewEndMs = clampedStart + span;

          if (offState.yGain > 1.0001) {
            const virtualPlotH = plotH * offState.yGain;
            const maxPanPx = Math.max(1e-6, virtualPlotH - plotH);
            const origPanPx = dragState.origYPanNorm * maxPanPx;
            const nextPanPx = Math.max(0, Math.min(maxPanPx, origPanPx - dy));
            offState.yPanNorm = nextPanPx / maxPanPx;
            if (Math.abs(dy) > 2) {
              offState.yPannedByUser = true;
            }
          }

          updateViewportSpanReadout();
          syncScrollHostFromState();
          drawOffsetAll();
          return;
        }
      }

      if (stickyStage) {
        const span = offState.viewEndMs - offState.viewStartMs;
        const cursorX = padL + ((offState.cursorMs - offState.viewStartMs) / span) * plotW;
        const nearWithMod = (
          (e.ctrlKey || e.metaKey) &&
          offState.cursorMs >= offState.viewStartMs &&
          offState.cursorMs <= offState.viewEndMs &&
          Math.abs((e.clientX - rect.left) - cursorX) <= 12
        );
        stickyStage.classList.toggle("is-adjusting-marker", nearWithMod);
      }

      const relX = e.clientX - rect.left - padL;
      if (relX < 0 || relX > plotW) {
        offState.hoverMs = null;
        if (hoverMsValEl) hoverMsValEl.textContent = "-";
        if (hoverTooltip) hoverTooltip.style.display = "none";
        drawMainEnvelope();
        return;
      }

      const span = offState.viewEndMs - offState.viewStartMs;
      const hm = Math.max(0, Math.min(TOTAL_MS - 1, Math.round(offState.viewStartMs + (relX / plotW) * span)));
      updateHoverReadoutAtMs(hm, e.clientX, rect.width);
      drawMainEnvelope();
    });

    const finishPointerInteraction = (e, cancelled = false) => {
      if (!dragState || dragState.pointerId !== e.pointerId) return;
      const finished = dragState;
      dragState = null;
      if (stickyStage) {
        stickyStage.classList.remove("is-dragging");
        stickyStage.classList.remove("is-adjusting-marker");
      }
      if (cancelled || !offState.diffF32) return;

      if (finished.mode === "marker-fine" || finished.mode === "marker-follow") {
        if (offState.isPlaying) {
          startPlaybackFrom(offState.cursorMs);
        } else {
          offState.livePlayheadMs = offState.cursorMs;
          drawOffsetAll();
        }
        return;
      }

      const wasClick = (!finished.moved && finished.button === 0);
      if (!wasClick) return;

      const rect = mainCanvas.getBoundingClientRect();
      const padL = 16, padR = 16;
      const plotW = Math.max(10, rect.width - padL - padR);
      const relX = Math.max(0, Math.min(plotW, e.clientX - rect.left - padL));
      const span = offState.viewEndMs - offState.viewStartMs;
      const clickedMs = Math.max(0, Math.min(TOTAL_MS - 1, Math.round(offState.viewStartMs + (relX / plotW) * span)));

      offState.cursorMs = clickedMs;
      offState.livePlayheadMs = clickedMs;
      updateCursorReadout();
      if (offState.isPlaying) {
        startPlaybackFrom(clickedMs);
      } else {
        drawOffsetAll();
      }
    };

    mainCanvas.addEventListener("pointerup", (e) => finishPointerInteraction(e, false));
    mainCanvas.addEventListener("pointercancel", (e) => finishPointerInteraction(e, true));

    mainCanvas.addEventListener("pointerleave", () => {
      if (stickyStage && !dragState) {
        stickyStage.classList.remove("is-adjusting-marker");
      }
      offState.hoverMs = null;
      if (hoverMsValEl) hoverMsValEl.textContent = "-";
      if (hoverTooltip) hoverTooltip.style.display = "none";
      if (offState.diffF32) drawMainEnvelope();
    });

    mainCanvas.addEventListener("contextmenu", (e) => {
      if (!offState.diffF32) return;
      e.preventDefault();
      if (suppressNextContextMenu || e.ctrlKey || e.metaKey) {
        suppressNextContextMenu = false;
        return;
      }
      if (offState.hoverMs !== null) {
        copyPositiveMs(offState.hoverMs);
      }
    });
  }

  // 缩略导航条拖拽平移与点击跳转
  if (minimapCanvas) {
    let miniDrag = null;
    const jumpMinimapAt = (clientX) => {
      const rect = minimapCanvas.getBoundingClientRect();
      const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / Math.max(1, rect.width)));
      const centerMs = ratio * TOTAL_MS;
      const span = offState.viewEndMs - offState.viewStartMs;
      setViewport(centerMs - span * 0.5, centerMs + span * 0.5);
    };
    minimapCanvas.addEventListener("pointerdown", (e) => {
      if (!offState.diffF32 || e.button !== 0) return;
      minimapCanvas.setPointerCapture(e.pointerId);
      miniDrag = e.pointerId;
      jumpMinimapAt(e.clientX);
    });
    minimapCanvas.addEventListener("pointermove", (e) => {
      if (miniDrag !== e.pointerId || !offState.diffF32) return;
      jumpMinimapAt(e.clientX);
    });
    minimapCanvas.addEventListener("pointerup", (e) => {
      if (miniDrag === e.pointerId) miniDrag = null;
    });
  }

  // 工具栏按钮绑定
  if (playPauseBtn) playPauseBtn.addEventListener("click", togglePlayPause);
  if (rewindCursorBtn) {
    rewindCursorBtn.addEventListener("click", () => {
      stopPlayback(true);
    });
  }
  if (copyCursorMsBtn) {
    copyCursorMsBtn.addEventListener("click", () => copyPositiveMs(offState.cursorMs));
  }

  function updatePitchWarnVisibility() {
    if (!pitchWarnEl) return;
    const shouldShow = !!offState.preservePitch && offState.playbackRate < 0.99;
    pitchWarnEl.classList.toggle("show", shouldShow);
  }

  document.querySelectorAll("#offsetSpeedGroup .seg-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const nextRate = parseFloat(btn.dataset.rate || "1");
      if (!nextRate || nextRate === offState.playbackRate) return;
      document.querySelectorAll("#offsetSpeedGroup .seg-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      const wasPlaying = offState.isPlaying;
      const resumeMs = wasPlaying ? (getLivePlayheadSec() * 1000.0) : offState.livePlayheadMs;
      offState.playbackRate = nextRate;
      updatePitchWarnVisibility();
      if (wasPlaying) {
        startPlaybackFrom(resumeMs);
      }
    });
  });

  if (preservePitchChk) {
    preservePitchChk.addEventListener("change", () => {
      const wasPlaying = offState.isPlaying;
      const resumeMs = wasPlaying ? (getLivePlayheadSec() * 1000.0) : offState.livePlayheadMs;
      if (wasPlaying) stopPlayback(false);
      offState.preservePitch = preservePitchChk.checked;
      updatePitchWarnVisibility();
      if (wasPlaying) {
        startPlaybackFrom(resumeMs);
      }
    });
  }

  document.querySelectorAll("#offsetPolarityGroup .seg-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#offsetPolarityGroup .seg-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      offState.polarity = btn.dataset.pol || "pos";
      offState.yPanNorm = getDefaultYPanNorm(offState.polarity);
      offState.yPannedByUser = false;
      syncScrollHostFromState();
      drawOffsetAll();
    });
  });

  document.querySelectorAll("#offsetGainGroup .seg-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const targetGain = parseFloat(btn.dataset.gain || "1") || 1;
      applyVerticalGain(targetGain, null);
    });
  });

  if (zoomInBtn) {
    zoomInBtn.addEventListener("click", () => {
      const center = (offState.viewStartMs + offState.viewEndMs) * 0.5;
      zoomAroundMs(center, 0.55);
    });
  }
  if (zoomOutBtn) {
    zoomOutBtn.addEventListener("click", () => {
      const center = (offState.viewStartMs + offState.viewEndMs) * 0.5;
      zoomAroundMs(center, 1.8);
    });
  }
  if (zoomInitBtn) {
    zoomInitBtn.addEventListener("click", () => {
      setViewport(0, getOffsetInitViewSec() * 1000);
    });
  }
  if (zoomResetBtn) {
    zoomResetBtn.addEventListener("click", () => setViewport(0, TOTAL_MS));
  }

  syncOffsetInitSecHints();

  async function openOffsetSecondaryView() {
    if (state.activeIndex < 0 || state.activeIndex >= state.items.length) return;
    const item = state.items[state.activeIndex];
    if (!item || item.status !== "done" || !item.file) return;

    const knownDur = getKnownItemDurationSec(item);
    if (knownDur !== null && knownDur < 60.0) {
      showShortAudioModal(`当前音频「${item.name}」有效时长为 ${knownDur.toFixed(2)} 秒，不足 60 秒，不予分析前 1 分钟起峰包络图。`);
      return;
    }

    syncOffsetInitSecHints();
    const initViewSec = getOffsetInitViewSec();
    const initViewMs = initViewSec * 1000;

    releaseAudioAndBuffers();
    offState.isOpen = true;
    offState.cursorMs = 0;
    offState.livePlayheadMs = 0;
    offState.hoverMs = null;
    offState.viewStartMs = 0;
    offState.viewEndMs = initViewMs;
    offState.yPanNorm = getDefaultYPanNorm(offState.polarity);
    offState.yPannedByUser = false;
    syncGainButtonsUI();
    updateCursorReadout();
    if (hoverMsValEl) hoverMsValEl.textContent = "-";

    const chMode = getCommittedChannelMode();
    const chLabel = OFFSET_CHANNEL_LABELS[chMode] || OFFSET_CHANNEL_LABELS.auto;

    if (songTitleEl) songTitleEl.textContent = item.name;
    if (songSubEl) {
      songSubEl.textContent = `正在读取前 60.000 秒 32-bit Float PCM 与 diff_1000Hz 包络 · ${chLabel}…`;
    }
    if (playPauseBtn) playPauseBtn.disabled = true;
    if (rewindCursorBtn) rewindCursorBtn.disabled = true;

    if (headerEl) headerEl.style.display = "none";
    if (mainLayoutEl) mainLayoutEl.style.display = "none";
    secondaryView.classList.add("active");
    loadingPanel.style.display = "flex";
    workspaceEl.style.display = "none";
    if (loadingTextEl) {
      loadingTextEl.textContent = "正在解码前 60 秒 32 位浮点 PCM 并提取 diff_1000Hz 起峰包络…";
    }

    const ctrl = new AbortController();
    offState.abortCtrl = ctrl;

    try {
      const fd = new FormData();
      fd.append("file", item.file);
      fd.append("channel", chMode);
      const resp = await fetch("/api/offset_envelope_pcm", {
        method: "POST",
        body: fd,
        signal: ctrl.signal,
      });

      if (!resp.ok) {
        let errMsg = `HTTP ${resp.status}`;
        let errCode = "";
        try {
          const j = await resp.json();
          if (j && j.error) errMsg = j.error;
          if (j && j.code) errCode = j.code;
        } catch (_) {}
        closeOffsetSecondaryView();
        if (errCode === "duration_under_60s" || errMsg.includes("不足 60 秒")) {
          showShortAudioModal(errMsg);
        } else {
          window.alert(errMsg);
        }
        return;
      }

      const rawBuf = await resp.arrayBuffer();
      if (!offState.isOpen) return;

      if (rawBuf.byteLength < 20) {
        throw new Error("起峰包络数据包长度异常");
      }
      const dv = new DataView(rawBuf);
      const magic = String.fromCharCode(dv.getUint8(0), dv.getUint8(1), dv.getUint8(2), dv.getUint8(3));
      if (magic !== "MOFS") {
        throw new Error("未知的包络数据头标识");
      }
      const sr = dv.getUint32(4, true);
      const pcmChannels = Math.max(1, dv.getUint32(8, true));
      const nDiff = dv.getUint32(12, true);
      const nPcm = dv.getUint32(16, true);
      const expectedBytes = 20 + (nDiff + nPcm) * 4;
      if (rawBuf.byteLength < expectedBytes || nDiff < TOTAL_MS || nPcm < pcmChannels) {
        throw new Error("起峰包络二进制帧不完整");
      }

      const diffArr = new Float32Array(rawBuf, 20, nDiff);
      const pcmArr = new Float32Array(rawBuf, 20 + nDiff * 4, nPcm);

      let maxPos = 1e-6;
      let maxAbs = 1e-6;
      for (let i = 0; i < nDiff; i++) {
        const v = diffArr[i];
        if (v > maxPos) maxPos = v;
        const av = Math.abs(v);
        if (av > maxAbs) maxAbs = av;
      }

      offState.sampleRate = sr;
      offState.pcmChannels = pcmChannels;
      offState.diffF32 = diffArr;
      offState.pcmF32 = pcmArr;
      offState.maxPosPeak = maxPos;
      offState.maxAbsPeak = maxAbs;

      if (songSubEl) {
        songSubEl.textContent = `已截取前 60.000 秒（初始显示前 ${initViewSec} 秒） · 解码采样率 ${sr} Hz (32-bit Float PCM) · diff_1000Hz · ${chLabel}`;
      }
      loadingPanel.style.display = "none";
      workspaceEl.style.display = "flex";
      if (playPauseBtn) playPauseBtn.disabled = false;
      if (rewindCursorBtn) rewindCursorBtn.disabled = false;

      setViewport(0, initViewMs);
    } catch (err) {
      if (!offState.isOpen || (err && err.name === "AbortError")) return;
      closeOffsetSecondaryView();
      window.alert(`加载前 1 分钟起峰包络失败：${err.message || err}`);
    } finally {
      if (offState.abortCtrl === ctrl) {
        offState.abortCtrl = null;
      }
    }
  }

  function closeOffsetSecondaryView() {
    if (!offState.isOpen) return;
    offState.isOpen = false;
    releaseAudioAndBuffers();
    secondaryView.classList.remove("active");
    if (headerEl) headerEl.style.display = "";
    if (mainLayoutEl) mainLayoutEl.style.display = "";
    if (typeof syncActiveMarquee === "function") syncActiveMarquee(false);
    if (typeof drawAcfChart === "function") drawAcfChart();
  }

  function handleOffsetEntryClick() {
    if (state.activeIndex < 0 || state.activeIndex >= state.items.length) return;
    const item = state.items[state.activeIndex];
    if (!item || item.status !== "done" || !item.file) return;

    const knownDur = getKnownItemDurationSec(item);
    if (knownDur !== null && knownDur < 60.0) {
      showShortAudioModal(`当前音频「${item.name}」有效时长为 ${knownDur.toFixed(2)} 秒，不足 60 秒，不予分析前 1 分钟起峰包络图。`);
      return;
    }

    if (offState.skipPreNoticeInSession || !preNoticeModal) {
      openOffsetSecondaryView();
      return;
    }

    showPreNoticeModal();
  }

  if (openOffsetViewLink) {
    openOffsetViewLink.addEventListener("click", handleOffsetEntryClick);
  }
  if (backBtn) {
    backBtn.addEventListener("click", closeOffsetSecondaryView);
  }

  // 颜色调色盘 + 8位 #RRGGBBAA 输入框 + 0~255 Alpha 整数框三方双向同步
  function syncColorControlsFromDraft(skipElem = null) {
    const p = getDraftCurveColorParts();
    if (colorPickerInput && skipElem !== colorPickerInput) {
      colorPickerInput.value = p.hex6;
    }
    if (colorSwatchEl) {
      colorSwatchEl.style.background = formatCssRgba(p.r, p.g, p.b, p.a);
    }
    if (colorHexInput && skipElem !== colorHexInput) {
      colorHexInput.value = p.hex8.toUpperCase();
    }
    if (colorAlphaInput && skipElem !== colorAlphaInput) {
      colorAlphaInput.value = String(p.a);
    }
  }

  function commitHexInputText() {
    if (!colorHexInput) return;
    const curA = getDraftCurveColorParts().a;
    const parsed = parseHex8Color(colorHexInput.value, curA);
    if (parsed) {
      state.draftSettings.offsetCurveColor = parsed.hex8;
    }
    syncColorControlsFromDraft(null);
  }

  function commitAlphaInputText() {
    if (!colorAlphaInput) return;
    const raw = String(colorAlphaInput.value ?? "").trim();
    if (raw !== "" && Number.isFinite(Number(raw))) {
      const clampedA = Math.max(0, Math.min(255, Math.round(Number(raw))));
      const p = getDraftCurveColorParts();
      state.draftSettings.offsetCurveColor = `${p.hex6}${clampedA.toString(16).padStart(2, "0")}`;
    }
    syncColorControlsFromDraft(null);
  }

  if (colorPickerInput) {
    const onPickerUpdate = () => {
      const p = getDraftCurveColorParts();
      const parsed = parseHex8Color(colorPickerInput.value, p.a);
      if (parsed) {
        state.draftSettings.offsetCurveColor = parsed.hex8;
        syncColorControlsFromDraft(colorPickerInput);
      }
    };
    colorPickerInput.addEventListener("input", onPickerUpdate);
    colorPickerInput.addEventListener("change", onPickerUpdate);
  }

  if (colorHexInput) {
    colorHexInput.addEventListener("input", () => {
      let s = String(colorHexInput.value ?? "").trim();
      if (s.startsWith("#")) s = s.slice(1);
      // 键入满 8 位或满 6 位合法十六进制字符时即时联动预览，不改写当前正在键入的文本框
      if (/^[0-9a-fA-F]{8}$/.test(s) || /^[0-9a-fA-F]{6}$/.test(s)) {
        const curA = getDraftCurveColorParts().a;
        const parsed = parseHex8Color(s, curA);
        if (parsed) {
          state.draftSettings.offsetCurveColor = parsed.hex8;
          syncColorControlsFromDraft(colorHexInput);
        }
      }
    });
    colorHexInput.addEventListener("blur", commitHexInputText);
    colorHexInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        commitHexInputText();
      }
    });
  }

  if (colorAlphaInput) {
    colorAlphaInput.addEventListener("input", () => {
      const raw = String(colorAlphaInput.value ?? "").trim();
      if (/^\d+$/.test(raw)) {
        const clampedA = Math.max(0, Math.min(255, parseInt(raw, 10)));
        const p = getDraftCurveColorParts();
        state.draftSettings.offsetCurveColor = `${p.hex6}${clampedA.toString(16).padStart(2, "0")}`;
        syncColorControlsFromDraft(colorAlphaInput);
      }
    });
    colorAlphaInput.addEventListener("blur", commitAlphaInputText);
    colorAlphaInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        commitAlphaInputText();
      }
    });
  }

  const prevRenderSettingsDraft = window.renderSettingsDraft;
  if (typeof prevRenderSettingsDraft === "function") {
    window.renderSettingsDraft = function () {
      const r = prevRenderSettingsDraft();
      const active = document.activeElement || null;
      syncColorControlsFromDraft(
        (active === colorHexInput || active === colorAlphaInput) ? active : null
      );
      return r;
    };
  }

  const saveSettingsBtnEl = document.getElementById("saveSettingsBtn");
  if (saveSettingsBtnEl) {
    saveSettingsBtnEl.addEventListener("click", () => {
      if (document.activeElement === colorHexInput) {
        commitHexInputText();
      } else if (document.activeElement === colorAlphaInput) {
        commitAlphaInputText();
      }
    }, true);
  }

  syncColorControlsFromDraft(null);

  // 同步控制入口条显隐与初始视野秒数文案（设置保存时 commitDraftSettings 会调用 renderActiveDetail）
  const prevRenderActiveDetail = window.renderActiveDetail;
  if (typeof prevRenderActiveDetail === "function") {
    window.renderActiveDetail = function () {
      const r = prevRenderActiveDetail();
      syncOffsetInitSecHints();
      if (offsetEntryBar) {
        const activeItem = (state.activeIndex >= 0 && state.activeIndex < state.items.length)
          ? state.items[state.activeIndex]
          : null;
        offsetEntryBar.style.display = (activeItem && activeItem.status === "done") ? "flex" : "none";
      }
      return r;
    };
  }

  // 二级页面开启时接管键盘事件，防止误触发后台隐藏的队列上下切歌，并支持 Space 播放/暂停与 C 键复制毫秒
  window.addEventListener("keydown", (e) => {
    if (shortModal && shortModal.classList.contains("open")) {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopImmediatePropagation();
        closeShortAudioModal();
        return;
      }
      if (e.key === "ArrowUp" || e.key === "ArrowDown") {
        e.stopImmediatePropagation();
        return;
      }
      return;
    }
    if (preNoticeModal && preNoticeModal.classList.contains("open")) {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopImmediatePropagation();
        closePreNoticeModal();
        return;
      }
      if (e.key === "ArrowUp" || e.key === "ArrowDown") {
        e.stopImmediatePropagation();
        return;
      }
      return;
    }
    if (!offState.isOpen) return;

    if (e.key === "Escape") {
      e.preventDefault();
      e.stopImmediatePropagation();
      closeOffsetSecondaryView();
      return;
    }

    if (e.key === "ArrowUp" || e.key === "ArrowDown") {
      e.stopImmediatePropagation();
      return;
    }
    if (e.code === "Space" && !e.metaKey && !e.ctrlKey && !e.altKey) {
      const tag = e.target && e.target.tagName ? e.target.tagName.toUpperCase() : "";
      if (tag !== "INPUT" && tag !== "TEXTAREA") {
        e.preventDefault();
        e.stopImmediatePropagation();
        togglePlayPause();
      }
      return;
    }
    if ((e.key === "c" || e.key === "C") && !e.metaKey && !e.ctrlKey && !e.altKey) {
      if (offState.diffF32) {
        e.preventDefault();
        e.stopImmediatePropagation();
        const targetMs = offState.hoverMs !== null ? offState.hoverMs : offState.cursorMs;
        copyPositiveMs(targetMs);
      }
    }
  }, true);

  window.addEventListener("keyup", (e) => {
    if (offState.isOpen && stickyStage && (e.key === "Control" || e.key === "Meta")) {
      stickyStage.classList.remove("is-adjusting-marker");
    }
  });

  window.addEventListener("resize", () => {
    if (offState.isOpen && offState.diffF32) {
      syncScrollHostFromState();
      drawOffsetAll();
    }
  });
})();
</script>
"""


def inject_offset_ui(index_html: str) -> str:
    """将底部二级页面入口链接、设置弹窗分组、全宽二级页面视图及仿 AU 起峰包络控制器注入首页模板。"""
    html = index_html
    html = html.replace("</style>", _OFFSET_CSS + "\n</style>", 1)

    # 在 #chartCard 闭合后（#detailView 内部最下方）插入二级页面入口链接条
    target_anchor = """        <div class="canvas-wrap">
          <canvas id="acfCanvas"></canvas>
          <div class="tooltip" id="chartTooltip"></div>
        </div>
      </div>"""
    if target_anchor not in html:
        raise RuntimeError("inject_offset_ui 未找到 #chartCard 锚点，拒绝静默注入失败")
    html = html.replace(target_anchor, target_anchor + "\n" + _OFFSET_ENTRY_HTML, 1)

    # 在 #settingsModal 的 .modal-body 最底部注入「起峰包络图辅助定位（Offset）」设置分组
    settings_anchor = """              <div class="stepper" data-stepper="subTrunkMinSec">
                <button type="button" class="stepper-btn" data-dir="-1" aria-label="减小">−</button>
                <span class="stepper-val" id="val_subTrunkMinSec">15 s</span>
                <button type="button" class="stepper-btn" data-dir="1" aria-label="增大">+</button>
              </div>
            </div>
          </div>
        </div>
      </section>"""
    if settings_anchor not in html:
        raise RuntimeError("inject_offset_ui 未找到 #settingsModal 分组锚点，拒绝静默注入失败")
    html = html.replace(settings_anchor, settings_anchor + "\n" + _OFFSET_SETTINGS_GROUP_HTML, 1)

    # 在主布局之后插入封闭二级页面与不足60秒提示框
    html = html.replace(
        "<!-- 设置模态弹窗 -->",
        _OFFSET_SECONDARY_HTML + "\n<!-- 设置模态弹窗 -->",
        1,
    )

    # 在 </body> 前追加二级页面控制脚本
    html = html.replace("</body>", _OFFSET_JS + "\n</body>", 1)
    return html
