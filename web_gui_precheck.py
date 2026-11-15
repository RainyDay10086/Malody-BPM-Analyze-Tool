#!/usr/bin/env python3
"""
Malody BPM 测算工作台 —— 独立音频预检、精简解码器白名单与全流程状态推送模块

设计目标：
1. 独立承载所有 UI 音频导入前置校验、临时会话管理、NDJSON 流式阶段推送及弹窗/状态前端组件，
   避免继续向 web_gui.py 堆砌业务与前端模板逻辑；
2. 仅放行无需任何外部第三方库即可由 FFmpeg 源码纯 C 原生编译的容器与音频解码器；
   不做文件名后缀拦截，统一按 ffprobe 探测到的真实内部封装与编码判定；
3. 严格要求音频流（codec_type == "audio"）数量等于 1（忽略封面与视频流），拦截多音轨与无音轨文件；
4. 原生采样率 >= 96000 Hz 或声道数 > 2 时触发风险确认弹窗（重新选择音频 / 仍要继续计算）。
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import secrets
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable

from flask import Flask, Response, jsonify, request, stream_with_context

from malody_bpm.decoder import _resolve_ff_binary, _subproc_extra_kwargs, normalize_channel_mode
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

# ------------------------------------------------------------------------------
# 精简自编译 FFmpeg (零外部第三方依赖库) 白名单定义与编译配置备查
# ------------------------------------------------------------------------------
UI_ACCEPT_EXTENSIONS = ".ogg,.mp3,.flac,.wav,.m4a,.aac,.opus"

# 对应 FFmpeg --enable-demuxer=... 的内置纯 C 解复用器集合
# 注：.m4a 在 ffprobe 中返回的 format_name 为 "mov,mp4,m4a,3gp,3g2,mj2"，包含 "mov"
ALLOWED_NATIVE_DEMUXERS: frozenset[str] = frozenset({
    "ogg",
    "mp3",
    "flac",
    "wav",
    "mov",
    "aac",
})

# 对应 FFmpeg --enable-decoder=... 的内置纯 C 音频解码器集合（无需 libvorbis/libopus/libflac/fdk-aac/lame）
ALLOWED_NATIVE_AUDIO_CODECS: frozenset[str] = frozenset({
    # OGG / OPUS
    "vorbis",
    "opus",
    # MP3
    "mp3",
    "mp3float",
    # FLAC
    "flac",
    # M4A / AAC
    "aac",
    "aac_latm",
    "alac",
    # WAV 标准无压缩 PCM 家族
    "pcm_s16le",
    "pcm_s16be",
    "pcm_s24le",
    "pcm_s24be",
    "pcm_s32le",
    "pcm_s32be",
    "pcm_f32le",
    "pcm_f32be",
    "pcm_f64le",
    "pcm_f64be",
    "pcm_u8",
    "pcm_s8",
    "pcm_alaw",
    "pcm_mulaw",
})

# 供后续自编译精简 FFmpeg 时直接参考的 ./configure 组件清单
FFMPEG_MINIMAL_BUILD_PROFILE: dict[str, tuple[str, ...]] = {
    "protocols": ("file", "pipe"),
    "demuxers": ("ogg", "mp3", "flac", "wav", "mov", "aac"),
    "parsers": ("vorbis", "opus", "mpegaudio", "flac", "aac", "aac_latm"),
    "decoders": tuple(sorted(ALLOWED_NATIVE_AUDIO_CODECS)),
    # decoder.py 输出裸 PCM 管道所需的内置编码器、复用器与重采样/混音滤镜
    "encoders": ("pcm_s16le", "pcm_s24le", "pcm_f32le"),
    "muxers": ("s16le", "s24le", "f32le"),
    "filters": ("aresample", "aformat", "pan"),
}

CONTAINER_DISPLAY_LABELS: dict[str, str] = {
    "ogg": "OGG",
    "mp3": "MP3",
    "flac": "FLAC",
    "wav": "WAV",
    "mov": "M4A/MP4",
    "aac": "AAC",
}

QUALITY_WARNING_PARAGRAPHS: tuple[str, str, str] = (
    "检测到您可能使用了国产音乐APP的“超高音质格式”或“沉浸声格式”。",
    "所谓“超高音质”很多只是强加了超低振幅高频底噪混水摸鱼的，不但不是真正意义上的音质提升，还有可能导致计算结果出现偏差。沉浸声音频（6声道）由于振幅的复杂性，更可能出现偏差。",
    "建议使用SQ及以下音质的音频。",
)


class _ClientStreamCancelled(Exception):
    """前端主动断开流式请求或移除队列项时用于提前中止后续计算阶段的内部信号。"""


def _format_channel_label(channels: int) -> str:
    if channels == 1:
        return "单声道"
    if channels == 2:
        return "双声道"
    return f"{channels} 声道"


def _match_container_label(format_name: str) -> str | None:
    tokens = [t.strip().lower() for t in format_name.split(",") if t.strip()]
    for tok in tokens:
        if tok in ALLOWED_NATIVE_DEMUXERS:
            return CONTAINER_DISPLAY_LABELS.get(tok, tok.upper())
    return None


def probe_and_validate_audio(file_path: str, original_filename: str) -> dict[str, Any]:
    """
    使用 ffprobe 探测完整容器与全部流信息（不按文件后缀拦截，完全依据真实内部结构判定）：
    1. 校验内部容器与音频编码是否属于精简 FFmpeg 原生解码白名单；
    2. 统计 codec_type == "audio" 的流数量（忽略封面与视频流），拦截 0 音轨与 >=2 多音轨；
    3. 提取未经折算的原生采样率与原生声道数，判定是否需要弹出超高音质/沉浸声确认框。
    """
    try:
        ffprobe_bin = _resolve_ff_binary("ffprobe")
        cmd = [
            ffprobe_bin,
            "-v",
            "error",
            "-show_entries",
            "format=format_name,duration:stream=index,codec_type,codec_name,sample_rate,channels,bits_per_raw_sample,bits_per_sample",
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
    except FileNotFoundError:
        return {
            "ok": False,
            "fail_step": "format",
            "error_code": "ffprobe_missing",
            "title": "缺少音频探测组件 (ffprobe)",
            "message": (
                "未找到 ffprobe 可执行文件，无法执行音频编码与音轨预检。\n"
                "请确认系统或运行环境中已包含 ffprobe。"
            ),
        }
    except Exception as exc:
        return {
            "ok": False,
            "fail_step": "format",
            "error_code": "probe_exception",
            "title": "音频预检失败",
            "message": f"读取文件「{original_filename}」元数据时发生异常：{exc}",
        }

    data: dict[str, Any] | None = None
    if res.returncode == 0 and res.stdout.strip():
        try:
            parsed = json.loads(res.stdout)
            if isinstance(parsed, dict):
                data = parsed
        except ValueError:
            data = None

    if not data:
        return {
            "ok": False,
            "fail_step": "format",
            "error_code": "unparseable_media",
            "title": "无法解析音频格式",
            "message": (
                f"无法识别文件「{original_filename}」的内部音频封装与编码结构"
                "（当前仅支持 OGG / MP3 / FLAC / WAV / M4A / AAC / OPUS 原生音频）。\n\n"
                "补充说明：该文件可能是平台加密格式、非音频文件或已损坏文件；"
                "也有可能是原本属于其他格式，但被直接重命名修改了文件后缀（内部真实格式并未改变）。"
            ),
        }

    fmt_info = data.get("format") or {}
    format_name = str(fmt_info.get("format_name") or "").strip().lower()
    streams = data.get("streams") or []
    audio_streams = [
        s for s in streams
        if isinstance(s, dict) and str(s.get("codec_type") or "").strip().lower() == "audio"
    ]

    container_label = _match_container_label(format_name)
    if container_label is None:
        first_codec = str(audio_streams[0].get("codec_name") or "").strip().lower() if audio_streams else ""
        desc_parts = []
        if format_name:
            desc_parts.append(f"封装: {format_name}")
        if first_codec:
            desc_parts.append(f"编码: {first_codec}")
        detected_desc = "，".join(desc_parts) if desc_parts else "未知封装格式"
        return {
            "ok": False,
            "fail_step": "format",
            "error_code": "unsupported_container",
            "title": "不支持的音频格式",
            "message": (
                f"文件「{original_filename}」的实际内部格式（{detected_desc}）不在精简解码器支持范围内。\n"
                "当前仅支持 OGG (Vorbis/Opus)、MP3、FLAC、WAV (标准 PCM)、M4A (AAC/ALAC)、AAC、OPUS。\n\n"
                "补充说明：若该文件的后缀名看似属于受支持格式，可能是文件曾被直接修改过后缀名（其内部真实封装与编码并未转换）。"
            ),
        }

    if len(audio_streams) == 0:
        return {
            "ok": False,
            "fail_step": "tracks",
            "error_code": "no_audio_track",
            "title": "未检测到有效音轨",
            "meta": {
                "container_label": container_label,
                "codec_name": "-",
                "audio_streams": 0,
            },
            "message": (
                f"在文件「{original_filename}」中未检测到任何音频轨道，无法进行 BPM 测算。\n\n"
                "补充说明：该文件可能是不含音轨的纯视频或空容器，且可能曾被直接修改过文件后缀名。"
            ),
        }

    primary_audio = audio_streams[0]
    codec_name = str(primary_audio.get("codec_name") or "").strip().lower()
    if codec_name not in ALLOWED_NATIVE_AUDIO_CODECS:
        return {
            "ok": False,
            "fail_step": "format",
            "error_code": "unsupported_codec",
            "title": "不支持的音频解码器",
            "message": (
                f"文件「{original_filename}」的内部音频编码为「{codec_name or '未知'}」，不在精简原生解码器支持范围内。\n"
                "当前仅支持 Vorbis、Opus、MP3、FLAC、AAC、ALAC 及标准 WAV PCM 编码。\n\n"
                "补充说明：若该文件的后缀名看似属于受支持格式，可能是文件曾被直接重命名修改了后缀（其内部真实编码并未转换）。"
            ),
        }

    if len(audio_streams) > 1:
        return {
            "ok": False,
            "fail_step": "tracks",
            "error_code": "multi_audio_track",
            "title": "不支持多音轨音频",
            "meta": {
                "container_label": container_label,
                "codec_name": codec_name,
                "audio_streams": len(audio_streams),
            },
            "message": (
                f"检测到文件「{original_filename}」包含 {len(audio_streams)} 条独立音轨"
                "（例如原唱/伴奏双音轨封装或多轨工程导出）。\n\n"
                "当前仅支持单音轨音频，请提取或混缩为单条音轨后再导入。"
            ),
        }

    try:
        raw_sr = int(primary_audio.get("sample_rate") or 0)
    except (TypeError, ValueError):
        raw_sr = 0

    try:
        channels = int(primary_audio.get("channels") or 0)
    except (TypeError, ValueError):
        channels = 0

    if raw_sr <= 0 or channels <= 0:
        return {
            "ok": False,
            "fail_step": "format",
            "error_code": "invalid_stream_params",
            "title": "无法解析有效音频数据",
            "message": (
                f"无法从文件「{original_filename}」中读取到有效的采样率或声道参数，无法解码。\n\n"
                "补充说明：该文件可能是平台加密格式、非音频数据或头部已损坏；"
                "也有可能是原本属于其他格式，但被直接重命名修改了文件后缀（内部真实格式并未改变）。"
            ),
        }

    raw_bits = primary_audio.get("bits_per_raw_sample") or primary_audio.get("bits_per_sample") or 0
    try:
        bits = int(raw_bits)
    except (TypeError, ValueError):
        bits = 0

    needs_quality_warning = (raw_sr >= 96000) or (channels > 2)

    return {
        "ok": True,
        "needs_quality_warning": needs_quality_warning,
        "meta": {
            "container_label": container_label,
            "codec_name": codec_name,
            "audio_streams": 1,
            "raw_sr": raw_sr,
            "channels": channels,
            "channel_label": _format_channel_label(channels),
            "bits": bits,
        },
    }


class PrecheckSessionStore:
    """线程安全的预检临时文件托管池，避免大体积无损音频在预检与计算阶段重复上传两次。"""

    def __init__(self, ttl_seconds: float = 900.0) -> None:
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._items: dict[str, tuple[str, str, dict[str, Any] | None, float]] = {}
        atexit.register(self.clear_all)

    @staticmethod
    def _safe_remove(path: str) -> None:
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    def _sweep_expired_locked(self, now: float) -> list[str]:
        expired_paths: list[str] = []
        expired_tokens = [
            tok for tok, (_, _, _, ts) in self._items.items()
            if (now - ts) > self._ttl
        ]
        for tok in expired_tokens:
            path, _, _, _ = self._items.pop(tok)
            expired_paths.append(path)
        return expired_paths

    def put(self, tmp_path: str, filename: str, meta: dict[str, Any] | None = None) -> str:
        now = time.monotonic()
        token = secrets.token_urlsafe(18)
        meta_copy = dict(meta) if isinstance(meta, dict) else None
        with self._lock:
            expired = self._sweep_expired_locked(now)
            self._items[token] = (tmp_path, filename, meta_copy, now)
        for p in expired:
            self._safe_remove(p)
        return token

    def pop_entry(self, token: str) -> tuple[str, str, dict[str, Any] | None] | None:
        if not token:
            return None
        now = time.monotonic()
        with self._lock:
            expired = self._sweep_expired_locked(now)
            entry = self._items.pop(token, None)
        for p in expired:
            self._safe_remove(p)
        if entry is None:
            return None
        return entry[0], entry[1], entry[2]

    def pop(self, token: str) -> tuple[str, str] | None:
        entry = self.pop_entry(token)
        if entry is None:
            return None
        return entry[0], entry[1]

    def discard(self, token: str) -> bool:
        popped = self.pop_entry(token)
        if popped is None:
            return False
        self._safe_remove(popped[0])
        return True

    def clear_all(self) -> None:
        with self._lock:
            paths = [p for p, _, _, _ in self._items.values()]
            self._items.clear()
        for p in paths:
            self._safe_remove(p)


_SESSION_STORE = PrecheckSessionStore()


def register_precheck_routes(
    app: Flask,
    analyze_audio_fn: Callable[..., dict[str, Any]],
    parse_bool_fn: Callable[[str | None, bool], bool],
    parse_int_fn: Callable[[str | None, int, int, int, str], int],
    parse_float_fn: Callable[[str | None, float, float, float, str], float],
) -> None:
    """在 Flask 应用上注册 /api/precheck、/api/precheck/discard 与 /api/analyze_stream 路由。"""

    @app.route("/api/precheck", methods=["POST"])
    def api_precheck():
        if "file" not in request.files:
            return jsonify({"ok": False, "fail_step": "format", "title": "上传失败", "message": "未收到上传的音频文件"}), 400

        up_file = request.files["file"]
        if not up_file or not up_file.filename:
            return jsonify({"ok": False, "fail_step": "format", "title": "上传失败", "message": "上传文件名为空"}), 400

        _, ext = os.path.splitext(up_file.filename)
        safe_ext = ext if (ext and len(ext) <= 12 and ext.replace(".", "").isalnum()) else ".tmp"
        fd, tmp_path = tempfile.mkstemp(prefix="malody_pre_", suffix=safe_ext)
        os.close(fd)

        try:
            up_file.save(tmp_path)
            probe_res = probe_and_validate_audio(tmp_path, up_file.filename)
        except Exception as exc:
            PrecheckSessionStore._safe_remove(tmp_path)
            return jsonify({
                "ok": False,
                "fail_step": "format",
                "title": "音频预检异常",
                "message": f"预检文件「{up_file.filename}」失败：{exc}",
            }), 500

        if not probe_res.get("ok"):
            PrecheckSessionStore._safe_remove(tmp_path)
            return jsonify(probe_res)

        token = _SESSION_STORE.put(tmp_path, up_file.filename, probe_res.get("meta"))
        probe_res["token"] = token
        return jsonify(probe_res)

    @app.route("/api/precheck/discard", methods=["POST"])
    def api_precheck_discard():
        token = ""
        if request.is_json:
            body = request.get_json(silent=True) or {}
            token = str(body.get("token") or "").strip()
        if not token:
            token = (request.form.get("token") or "").strip()
        if not token and request.data:
            try:
                body = json.loads(request.data.decode("utf-8", errors="ignore"))
                if isinstance(body, dict):
                    token = str(body.get("token") or "").strip()
            except ValueError:
                pass
        removed = _SESSION_STORE.discard(token)
        return jsonify({"ok": True, "discarded": removed})

    @app.route("/api/analyze_stream", methods=["POST"])
    def api_analyze_stream():
        token = (request.form.get("token") or "").strip()
        session_entry = _SESSION_STORE.pop_entry(token)
        if session_entry is None:
            return jsonify({"error": "预检临时会话已过期或不存在，请重新导入该音频文件"}), 400

        tmp_path, orig_filename, precheck_meta = session_entry

        mode = request.form.get("mode", "all").strip().lower()
        if mode not in ("exact", "enhanced_v3", "force_v3", "all", "enhanced", "enhanced_v2", "both"):
            mode = "all"

        octave_norm = parse_bool_fn(request.form.get("octave_norm"), False)
        sr_raw = request.form.get("target_sr", "").strip()
        target_sr = int(sr_raw) if sr_raw.isdigit() else None

        try:
            channel = normalize_channel_mode(request.form.get("channel", "auto"))
            trim_silence = parse_bool_fn(request.form.get("trim_silence"), False)
            silence_threshold_db = parse_float_fn(
                request.form.get("silence_threshold_db"),
                DEFAULT_SILENCE_THRESHOLD_DB,
                MIN_SILENCE_THRESHOLD_DB,
                MAX_SILENCE_THRESHOLD_DB,
                "silence_threshold_db",
            )
            trim_short_bursts = parse_bool_fn(request.form.get("trim_short_bursts"), False)
            short_burst_max_sec = parse_float_fn(
                request.form.get("short_burst_max_sec"),
                DEFAULT_SHORT_BURST_MAX_SEC,
                MIN_SHORT_BURST_MAX_SEC,
                MAX_SHORT_BURST_MAX_SEC,
                "short_burst_max_sec",
            )
            short_burst_min_gap_sec = parse_float_fn(
                request.form.get("short_burst_min_gap_sec"),
                DEFAULT_SHORT_BURST_MIN_GAP_SEC,
                MIN_SHORT_BURST_MIN_GAP_SEC,
                MAX_SHORT_BURST_MIN_GAP_SEC,
                "short_burst_min_gap_sec",
            )
            allow_any_duration = parse_bool_fn(request.form.get("allow_any_duration"), False)
            acf_decay_ratio = parse_float_fn(
                request.form.get("acf_decay_ratio"), 0.7, 0.1, 1.0, "acf_decay_ratio"
            )
            cand_update_ratio = parse_float_fn(
                request.form.get("cand_update_ratio"), 1.25, 1.0, 2.0, "cand_update_ratio"
            )
            # [新增 2026-11-14] 240~300 BPM 折半对比度门限
            halve_contrast_ratio = parse_float_fn(
                request.form.get("halve_contrast_ratio"), 1.10, 1.0, 2.0, "halve_contrast_ratio"
            )
            track_tol = parse_int_fn(request.form.get("track_tol"), 8, 5, 25, "track_tol")
            comb_tol = parse_int_fn(request.form.get("comb_tol"), 35, 10, 50, "comb_tol")
            apply_comb_tol_to_force = parse_bool_fn(
                request.form.get("apply_comb_tol_to_force"), False
            )
            transition_miss_beats = parse_int_fn(
                request.form.get("transition_miss_beats"), 4, 2, 10, "transition_miss_beats"
            )
            transition_resume_beats = parse_int_fn(
                request.form.get("transition_resume_beats"), 3, 2, 10, "transition_resume_beats"
            )
            transition_guard = parse_float_fn(
                request.form.get("transition_guard"), 1.0, 0.0, 3.0, "transition_guard"
            )
            trim_recalc = parse_bool_fn(
                request.form.get("trim_recalc"), False
            )
            sub_trunk_min_sec = parse_int_fn(
                request.form.get("sub_trunk_min_sec"), 15, 8, 15, "sub_trunk_min_sec"
            )
        except ValueError as val_err:
            PrecheckSessionStore._safe_remove(tmp_path)
            return jsonify({"error": str(val_err)}), 400

        evt_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        cancel_event = threading.Event()

        def _progress_cb(stage_evt: str | dict[str, Any]) -> None:
            if cancel_event.is_set():
                raise _ClientStreamCancelled()
            if isinstance(stage_evt, dict):
                evt_out: dict[str, Any] = {"type": "stage"}
                if "text" in stage_evt:
                    evt_out["text"] = stage_evt["text"]
                if "phase" in stage_evt:
                    evt_out["phase"] = stage_evt["phase"]
                if stage_evt.get("dwell"):
                    evt_out["dwell"] = True
                if "pill" in stage_evt and isinstance(stage_evt["pill"], dict):
                    evt_out["pill"] = stage_evt["pill"]
                evt_queue.put(evt_out)
            else:
                evt_queue.put({"type": "stage", "text": str(stage_evt)})

        _progress_cb._accepts_event_dict = True  # type: ignore[attr-defined]

        def _worker() -> None:
            try:
                payload = analyze_audio_fn(
                    tmp_path,
                    mode=mode,
                    octave_norm=octave_norm,
                    target_sr=target_sr,
                    trim_silence=trim_silence,
                    allow_any_duration=allow_any_duration,
                    acf_decay_ratio=acf_decay_ratio,
                    cand_update_ratio=cand_update_ratio,
                    halve_contrast_ratio=halve_contrast_ratio,
                    track_tol=track_tol,
                    comb_tol=comb_tol,
                    apply_comb_tol_to_force=apply_comb_tol_to_force,
                    transition_miss_beats=transition_miss_beats,
                    transition_resume_beats=transition_resume_beats,
                    transition_guard=transition_guard,
                    trim_recalc=trim_recalc,
                    sub_trunk_min_sec=sub_trunk_min_sec,
                    silence_threshold_db=silence_threshold_db,
                    trim_short_bursts=trim_short_bursts,
                    short_burst_max_sec=short_burst_max_sec,
                    short_burst_min_gap_sec=short_burst_min_gap_sec,
                    channel=channel,
                    _audio_meta=precheck_meta,
                    progress_cb=_progress_cb,
                )
                payload["filename"] = orig_filename
                evt_queue.put({"type": "result", "payload": payload})
            except _ClientStreamCancelled:
                pass
            except Exception as exc:
                if isinstance(exc, ValueError) and "[45s, 600s]" in str(exc):
                    evt_queue.put({
                        "type": "intercept",
                        "title": "音频时长超出允许范围",
                        "message": (
                            f"文件「{orig_filename}」未通过时长校验：{exc}。\n\n"
                            "当前默认仅处理 45 秒 ~ 600 秒（10 分钟）的有效音频；"
                            "若仍需测算，可在右上角「设置 → 音频输入与时长限制」中开启"
                            "「我愿意承担精确度降低和死机的风险，以测量超短或超长音频」后重新导入。"
                        ),
                    })
                else:
                    evt_queue.put({"type": "error", "error": f"音频分析失败: {exc}"})
            finally:
                PrecheckSessionStore._safe_remove(tmp_path)
                evt_queue.put(None)

        worker_thread = threading.Thread(
            target=_worker,
            name="MalodyAnalyzeStreamWorker",
            daemon=True,
        )
        worker_thread.start()

        def _generate():
            try:
                while True:
                    item = evt_queue.get()
                    if item is None:
                        break
                    yield (json.dumps(item, ensure_ascii=False) + "\n").encode("utf-8")
            except GeneratorExit:
                cancel_event.set()
                raise
            finally:
                cancel_event.set()

        headers = {
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        }
        return Response(
            stream_with_context(_generate()),
            mimetype="application/x-ndjson",
            headers=headers,
        )


# ------------------------------------------------------------------------------
# 前端样式、模态框与状态流控制器注入片段
# ------------------------------------------------------------------------------
_PRECHECK_CSS = r"""
  /* --- 独立预检与状态展示扩展样式 (web_gui_precheck.py) --- */
  .file-badge.confirm-wait {
    color: #f0b72f;
    background: rgba(210, 153, 34, 0.16);
    border: 1px solid rgba(210, 153, 34, 0.38);
  }
  .precheck-status-card {
    display: flex;
    flex-direction: column;
    gap: 14px;
    padding: 20px 22px;
  }
  .precheck-head-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    min-width: 0;
  }
  .precheck-head-row .pill {
    flex-shrink: 0;
  }
  .precheck-file-label {
    font-size: 12.5px;
    color: var(--muted);
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .precheck-main-row {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .precheck-spinner {
    width: 18px;
    height: 18px;
    border-radius: 50%;
    border: 2.2px solid rgba(88, 166, 255, 0.22);
    border-top-color: var(--accent);
    animation: malodyPreSpin 0.78s linear infinite;
    flex-shrink: 0;
  }
  .precheck-spinner.is-paused {
    animation: none;
    border-color: rgba(210, 153, 34, 0.85);
    background: rgba(210, 153, 34, 0.18);
  }
  @keyframes malodyPreSpin {
    to { transform: rotate(360deg); }
  }
  .precheck-stage-text {
    font-size: 16px;
    font-weight: 600;
    color: var(--text);
    letter-spacing: 0.2px;
  }
  .precheck-pills-row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    font-size: 12px;
  }
  .modal-backdrop.quality-warn-layer {
    z-index: 1120;
  }
  .modal-backdrop.intercept-alert-layer {
    z-index: 1150;
  }
  .modal-dialog.quality-dialog {
    background: #161618; /* 二级模态弹窗统一深空灰调 */
    width: min(500px, 100%);
    border-radius: 24px;
  }
  .modal-dialog.quality-dialog .modal-header {
    border-bottom: none;
    padding: 22px 24px 8px 24px;
  }
  .modal-dialog.quality-dialog .modal-body {
    padding: 6px 24px 20px 24px;
    gap: 12px;
  }
  .modal-dialog.quality-dialog .modal-footer {
    border-top: none;
    background: transparent;
    padding: 0 24px 20px 24px;
    justify-content: flex-end;
  }
  .quality-file-chip {
    background: rgba(210, 153, 34, 0.12);
    border: 1px solid rgba(210, 153, 34, 0.36);
    color: #f2cc60;
    border-radius: 10px;
    padding: 8px 12px;
    font-size: 12.5px;
    line-height: 1.45;
    word-break: break-all;
  }
  .quality-copy-p {
    font-size: 14px;
    line-height: 1.68;
    color: #FFFFFF;
    margin: 0;
  }
  .intercept-item-list {
    display: flex;
    flex-direction: column;
    gap: 10px;
    max-height: 46vh;
    overflow-y: auto;
  }
  .intercept-item-box {
    background: rgba(248, 81, 73, 0.08);
    border: 1px solid rgba(248, 81, 73, 0.3);
    border-radius: 12px;
    padding: 11px 13px;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .intercept-item-title {
    font-size: 13px;
    font-weight: 600;
    color: #ffa198;
  }
  .intercept-item-msg {
    font-size: 13px;
    line-height: 1.62;
    color: #FFFFFF;
    white-space: pre-wrap;
    word-break: break-word;
  }
"""

_PRECHECK_MODALS_HTML = f"""
<!-- 超高采样率 / 多声道风险提示弹窗 (web_gui_precheck.py) -->
<div class="modal-backdrop quality-warn-layer" id="qualityWarnModal" role="dialog" aria-modal="true" aria-labelledby="qualityWarnModalTitle">
  <div class="modal-dialog quality-dialog">
    <div class="modal-header">
      <h2 id="qualityWarnModalTitle">音质与声道格式提示</h2>
    </div>
    <div class="modal-body">
      <div class="quality-file-chip" id="qualityWarnFileChip"></div>
      <p class="quality-copy-p">{QUALITY_WARNING_PARAGRAPHS[0]}</p>
      <p class="quality-copy-p">{QUALITY_WARNING_PARAGRAPHS[1]}</p>
      <p class="quality-copy-p">{QUALITY_WARNING_PARAGRAPHS[2]}</p>
    </div>
    <div class="modal-footer">
      <div class="modal-footer-right">
        <button type="button" class="m3-btn m3-btn-tonal" id="qualityContinueBtn">仍要继续计算</button>
        <button type="button" class="m3-btn m3-btn-filled" id="qualityReselectBtn">重新选择音频</button>
      </div>
    </div>
  </div>
</div>

<!-- 不支持格式 / 多音轨硬性拦截提示弹窗 (web_gui_precheck.py) -->
<div class="modal-backdrop intercept-alert-layer" id="interceptAlertModal" role="dialog" aria-modal="true" aria-labelledby="interceptAlertModalTitle">
  <div class="modal-dialog quality-dialog">
    <div class="modal-header">
      <h2 id="interceptAlertModalTitle">音频导入已拦截</h2>
    </div>
    <div class="modal-body">
      <div class="intercept-item-list" id="interceptAlertList"></div>
    </div>
    <div class="modal-footer">
      <div class="modal-footer-right">
        <button type="button" class="m3-btn m3-btn-filled" id="closeInterceptAlertBtn">我知道了</button>
      </div>
    </div>
  </div>
</div>
"""

_PRECHECK_JS = r"""
<script>
(() => {
  const PRECHECK_STEP_DWELL_MS = 160;

  const qualityWarnModal = document.getElementById("qualityWarnModal");
  const qualityWarnFileChip = document.getElementById("qualityWarnFileChip");
  const qualityContinueBtn = document.getElementById("qualityContinueBtn");
  const qualityReselectBtn = document.getElementById("qualityReselectBtn");

  const interceptAlertModal = document.getElementById("interceptAlertModal");
  const interceptAlertList = document.getElementById("interceptAlertList");
  const closeInterceptAlertBtn = document.getElementById("closeInterceptAlertBtn");

  let pendingQualityResolver = null;
  let pendingQualityItem = null;

  function sleepMs(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  async function waitAtLeast(startTs, minMs) {
    const elapsed = performance.now() - startTs;
    if (elapsed < minMs) {
      await sleepMs(minMs - elapsed);
    }
  }

  function discardServerToken(token) {
    if (!token) return;
    const payload = JSON.stringify({ token });
    try {
      if (navigator.sendBeacon) {
        const blob = new Blob([payload], { type: "application/json" });
        if (navigator.sendBeacon("/api/precheck/discard", blob)) {
          return;
        }
      }
    } catch (_) {}
    fetch("/api/precheck/discard", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: payload,
      keepalive: true,
    }).catch(() => {});
  }

  function pushInterceptNotice(title, message) {
    if (!interceptAlertModal || !interceptAlertList) return;
    if (!interceptAlertModal.classList.contains("open")) {
      interceptAlertList.innerHTML = "";
    }
    const box = document.createElement("div");
    box.className = "intercept-item-box";

    const titleEl = document.createElement("div");
    titleEl.className = "intercept-item-title";
    titleEl.textContent = title || "不支持的音频";

    const msgEl = document.createElement("div");
    msgEl.className = "intercept-item-msg";
    msgEl.textContent = message || "";

    box.appendChild(titleEl);
    box.appendChild(msgEl);
    interceptAlertList.appendChild(box);
    interceptAlertModal.classList.add("open");
  }

  function closeInterceptAlertModal() {
    if (!interceptAlertModal) return;
    interceptAlertModal.classList.remove("open");
    if (interceptAlertList) interceptAlertList.innerHTML = "";
  }

  if (closeInterceptAlertBtn) {
    closeInterceptAlertBtn.addEventListener("click", closeInterceptAlertModal);
  }
  if (interceptAlertModal) {
    interceptAlertModal.addEventListener("click", (e) => {
      if (e.target === interceptAlertModal) closeInterceptAlertModal();
    });
  }

  function resolveQualityModal(choice) {
    if (qualityWarnModal) {
      qualityWarnModal.classList.remove("open");
    }
    const resolver = pendingQualityResolver;
    pendingQualityResolver = null;
    pendingQualityItem = null;
    if (resolver) resolver(choice);
  }

  function askQualityWarning(item, meta) {
    if (!qualityWarnModal || !qualityWarnFileChip) {
      return Promise.resolve("continue");
    }
    const specParts = [`${meta.raw_sr} Hz`, meta.channel_label || `${meta.channels} 声道`];
    if (meta.bits > 0) specParts.push(`${meta.bits}-bit`);
    qualityWarnFileChip.textContent = `当前音频：${item.name}（检出规格：${specParts.join(" · ")}）`;

    pendingQualityItem = item;
    qualityWarnModal.classList.add("open");
    return new Promise((resolve) => {
      pendingQualityResolver = resolve;
    });
  }

  if (qualityContinueBtn) {
    qualityContinueBtn.addEventListener("click", () => resolveQualityModal("continue"));
  }
  if (qualityReselectBtn) {
    qualityReselectBtn.addEventListener("click", () => resolveQualityModal("reselect"));
  }

  function isItemAlive(item) {
    return !!item && !item._cancelled && state.items.includes(item);
  }

  function refreshItemViews(item) {
    renderFileList(false);
    if (state.items[state.activeIndex] === item) {
      renderActiveDetail();
    }
  }

  function cancelItemWork(item) {
    if (!item) return;
    item._cancelled = true;
    if (item._abortCtrl) {
      try { item._abortCtrl.abort(); } catch (_) {}
      item._abortCtrl = null;
    }
    if (item._precheckToken) {
      const tok = item._precheckToken;
      item._precheckToken = null;
      discardServerToken(tok);
    }
    if (pendingQualityItem === item) {
      resolveQualityModal("reselect");
    }
  }

  function cancelAllItems(items) {
    if (!Array.isArray(items)) return;
    for (const it of items) {
      cancelItemWork(it);
    }
  }

  function handleEscapeKey() {
    if (interceptAlertModal && interceptAlertModal.classList.contains("open")) {
      closeInterceptAlertModal();
      return true;
    }
    if (qualityWarnModal && qualityWarnModal.classList.contains("open")) {
      resolveQualityModal("reselect");
      return true;
    }
    return false;
  }

  function hasBlockingModalOpen() {
    return (
      (interceptAlertModal && interceptAlertModal.classList.contains("open"))
      || (qualityWarnModal && qualityWarnModal.classList.contains("open"))
    );
  }

  function applyRowBadge(badgeEl, item) {
    if (item.status === "pending") {
      badgeEl.classList.add("pending");
      badgeEl.textContent = "排队中";
      return true;
    }
    if (item.status === "prechecking") {
      badgeEl.classList.add("pending");
      badgeEl.textContent = "检测中...";
      return true;
    }
    if (item.status === "awaiting_confirm") {
      badgeEl.classList.add("confirm-wait");
      badgeEl.textContent = "待确认";
      return true;
    }
    if (item.status === "running") {
      badgeEl.classList.add("pending");
      badgeEl.textContent = "测算中...";
      return true;
    }
    return false;
  }

  function buildInProgressDetailHtml(item) {
    const isQueued = item.status === "pending";
    const isPaused = item.status === "awaiting_confirm";
    const phaseTag = isQueued
      ? "等待队列调度"
      : (isPaused
        ? "等待风险确认"
        : (item.status === "prechecking"
          ? "导入前置检测"
          : (item.phaseTag || "音频解码与预处理")));
    const stageText = item.stageText || (isQueued ? "排队等待检测…" : "正在准备…");

    const pills = Array.isArray(item.revealedPills) ? item.revealedPills : [];
    const pillsHtml = pills.length > 0
      ? `<div class="precheck-pills-row">${pills.map(p => `<span class="pill ${p.cls || ""}">${escapeHtml(p.text)}</span>`).join("")}</div>`
      : "";

    return `
      <div class="res-card precheck-status-card">
        <div class="precheck-head-row">
          <span class="pill">${escapeHtml(phaseTag)}</span>
          <span class="precheck-file-label" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</span>
        </div>
        <div class="precheck-main-row">
          <span class="precheck-spinner ${isPaused ? "is-paused" : ""}"></span>
          <span class="precheck-stage-text">${escapeHtml(stageText)}</span>
        </div>
        ${pillsHtml}
      </div>
    `;
  }

  function buildAnalyzeFormData(token, settingsObj) {
    const s = settingsObj;
    const fd = new FormData();
    fd.append("token", token);
    fd.append("mode", s.mode);
    fd.append("channel", s.channel || "auto");
    fd.append("octave_norm", "false");
    fd.append("target_sr", "");
    fd.append("trim_silence", s.trimSilence ? "true" : "false");
    fd.append("silence_threshold_db", String(Math.round(s.silenceThresholdDb ?? -50)));
    fd.append("trim_short_bursts", (s.trimSilence && s.trimShortBursts) ? "true" : "false");
    fd.append("short_burst_max_sec", Number(s.shortBurstMaxSec ?? 1.5).toFixed(1));
    fd.append("short_burst_min_gap_sec", Number(s.shortBurstMinGapSec ?? 0.8).toFixed(1));
    fd.append("allow_any_duration", s.allowAnyDuration ? "true" : "false");
    fd.append("acf_decay_ratio", s.acfDecayRatio.toFixed(2));
    fd.append("cand_update_ratio", s.candUpdateRatio.toFixed(2));
    // [新增 2026-11-14] 透传 240~300 BPM 折半对比度门限
    fd.append("halve_contrast_ratio", Number(s.halveContrastRatio ?? 1.10).toFixed(2));
    fd.append("track_tol", String(Math.round(s.trackTol)));
    fd.append("comb_tol", String(Math.round(s.combTol)));
    fd.append("apply_comb_tol_to_force", s.applyCombTolToForce ? "true" : "false");
    fd.append("transition_miss_beats", String(Math.round(s.transitionMissBeats)));
    fd.append("transition_resume_beats", String(Math.round(s.transitionResumeBeats)));
    fd.append("transition_guard", s.transitionGuard.toFixed(1));
    fd.append("trim_recalc", s.trimRecalc ? "true" : "false");
    fd.append("sub_trunk_min_sec", String(Math.round(s.subTrunkMinSec ?? 15)));
    return fd;
  }

  async function runItemPipeline(item) {
    if (!isItemAlive(item)) return;

    item.revealedPills = [];
    item.phaseTag = "导入前置检测";
    item.status = "prechecking";
    item.stageText = "检测音频格式与解码器…";
    refreshItemViews(item);

    // 1. 上传并执行 ffprobe 原生封装/编码与音轨预检
    const step1Start = performance.now();
    const preForm = new FormData();
    preForm.append("file", item.file);
    const preAbort = new AbortController();
    item._abortCtrl = preAbort;

    let preJson = null;
    try {
      const resp = await fetch("/api/precheck", {
        method: "POST",
        body: preForm,
        signal: preAbort.signal,
      });
      preJson = await resp.json();
    } catch (err) {
      if (!isItemAlive(item)) return;
      item.status = "error";
      item.error = err.message || String(err);
      refreshItemViews(item);
      return;
    } finally {
      if (item._abortCtrl === preAbort) {
        item._abortCtrl = null;
      }
    }

    if (preJson && preJson.token) {
      item._precheckToken = preJson.token;
    }
    if (!isItemAlive(item)) {
      if (item._precheckToken) {
        const tok = item._precheckToken;
        item._precheckToken = null;
        discardServerToken(tok);
      }
      return;
    }

    await waitAtLeast(step1Start, PRECHECK_STEP_DWELL_MS);
    if (!isItemAlive(item)) return;

    // 若在格式/解码器阶段被拦截，直接弹窗并自动移出队列
    if (!preJson.ok && preJson.fail_step === "format") {
      pushInterceptNotice(preJson.title || "不支持的音频格式", preJson.message || "音频格式不受支持");
      removeQueueItem(item);
      return;
    }

    const meta = preJson.meta || {};
    if (meta.container_label) {
      const fmtText = meta.codec_name && meta.codec_name !== "-"
        ? `${meta.container_label} · ${meta.codec_name}`
        : meta.container_label;
      item.revealedPills.push({ text: fmtText, cls: "highlight" });
    }

    // 2. 检测音轨数量
    const step2Start = performance.now();
    item.stageText = "检测音轨数量…";
    refreshItemViews(item);
    await waitAtLeast(step2Start, PRECHECK_STEP_DWELL_MS);
    if (!isItemAlive(item)) return;

    if (!preJson.ok) {
      pushInterceptNotice(preJson.title || "音轨检测未通过", preJson.message || "仅支持单音轨音频");
      removeQueueItem(item);
      return;
    }

    item.revealedPills.push({ text: "单音轨", cls: "highlight" });

    // 3. 检测采样率
    const step3Start = performance.now();
    item.stageText = "检测采样率…";
    refreshItemViews(item);
    await waitAtLeast(step3Start, PRECHECK_STEP_DWELL_MS);
    if (!isItemAlive(item)) return;

    const highSr = Number(meta.raw_sr) >= 96000;
    item.revealedPills.push({
      text: `${meta.raw_sr} Hz`,
      cls: highSr ? "amber" : "highlight",
    });

    // 4. 检测声道数与位深
    const step4Start = performance.now();
    item.stageText = "检测声道数…";
    refreshItemViews(item);
    await waitAtLeast(step4Start, PRECHECK_STEP_DWELL_MS);
    if (!isItemAlive(item)) return;

    const multiCh = Number(meta.channels) > 2;
    item.revealedPills.push({
      text: meta.channel_label || `${meta.channels} 声道`,
      cls: multiCh ? "amber" : "highlight",
    });
    if (Number(meta.bits) > 0) {
      item.revealedPills.push({ text: `${meta.bits}-bit`, cls: "" });
    }
    refreshItemViews(item);

    // 5. 若采样率 >= 96000 或声道数 > 2，挂起等待用户选择
    if (preJson.needs_quality_warning) {
      item.status = "awaiting_confirm";
      item.stageText = "等待音质 / 声道风险确认…";
      refreshItemViews(item);

      const userDecision = await askQualityWarning(item, meta);
      if (!isItemAlive(item) || userDecision !== "continue") {
        if (item._precheckToken) {
          const tok = item._precheckToken;
          item._precheckToken = null;
          discardServerToken(tok);
        }
        if (state.items.includes(item)) {
          removeQueueItem(item);
        }
        return;
      }
    }

    // 6. 进入流式解码、预处理与 BPM 测算阶段（消费一次性 token，无需二次上传文件）
    const token = item._precheckToken;
    item._precheckToken = null;
    item.status = "running";
    item.phaseTag = "音频解码与预处理";
    item.stageText = "解码音频数据…";
    refreshItemViews(item);

    const streamAbort = new AbortController();
    item._abortCtrl = streamAbort;
    let interceptedInfo = null;

    try {
      const fd = buildAnalyzeFormData(token, state.settings);
      const resp = await fetch("/api/analyze_stream", {
        method: "POST",
        body: fd,
        signal: streamAbort.signal,
      });

      if (!resp.ok || !resp.body) {
        let errDetail = `HTTP ${resp.status}`;
        try {
          const errJson = await resp.json();
          if (errJson && errJson.error) errDetail = errJson.error;
        } catch (_) {}
        throw new Error(errDetail);
      }

      const reader = resp.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buf = "";
      let finalPayload = null;
      let pendingDwellStart = null;

      const handleStreamEvent = async (evt) => {
        if (!evt || !isItemAlive(item)) return;
        if (pendingDwellStart !== null) {
          await waitAtLeast(pendingDwellStart, PRECHECK_STEP_DWELL_MS);
          pendingDwellStart = null;
          if (!isItemAlive(item)) return;
        }
        if (evt.type === "stage") {
          let changed = false;
          if (evt.phase && item.phaseTag !== evt.phase) {
            item.phaseTag = evt.phase;
            changed = true;
          }
          if (evt.text && item.stageText !== evt.text) {
            item.stageText = evt.text;
            changed = true;
          }
          if (evt.pill && evt.pill.text) {
            item.revealedPills.push({
              text: String(evt.pill.text),
              cls: evt.pill.cls ? String(evt.pill.cls) : "",
            });
            changed = true;
          }
          if (changed && state.items[state.activeIndex] === item) {
            renderActiveDetail();
          }
          if (evt.dwell) {
            pendingDwellStart = performance.now();
          }
        } else if (evt.type === "intercept") {
          interceptedInfo = {
            title: evt.title || "音频时长超出允许范围",
            message: evt.message || "音频时长不在允许范围内",
          };
        } else if (evt.type === "result") {
          finalPayload = evt.payload;
        } else if (evt.type === "error") {
          throw new Error(evt.error || "音频分析失败");
        }
      };

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let nlIdx;
        while ((nlIdx = buf.indexOf("\n")) !== -1) {
          const rawLine = buf.slice(0, nlIdx).trim();
          buf = buf.slice(nlIdx + 1);
          if (!rawLine) continue;
          const evt = JSON.parse(rawLine);
          await handleStreamEvent(evt);
          if (!isItemAlive(item)) return;
        }
      }

      const tail = (buf + decoder.decode()).trim();
      if (tail) {
        const evt = JSON.parse(tail);
        await handleStreamEvent(evt);
      }

      if (!isItemAlive(item)) return;
      if (!interceptedInfo) {
        if (!finalPayload) {
          throw new Error("未收到测算结果数据");
        }
        item.status = "done";
        item.data = finalPayload;
        item.error = null;
      }
    } catch (err) {
      if (!isItemAlive(item)) return;
      item.status = "error";
      item.error = err.message || String(err);
    } finally {
      if (item._abortCtrl === streamAbort) {
        item._abortCtrl = null;
      }
    }

    if (!isItemAlive(item)) return;
    if (interceptedInfo) {
      pushInterceptNotice(interceptedInfo.title, interceptedInfo.message);
      removeQueueItem(item);
      return;
    }

    refreshItemViews(item);
  }

  // 自动装饰并接管 web_gui.py 原生队列、徽章与详情卡片渲染钩子，保持原主文件零侵入
  const origRemoveQueueItem = window.removeQueueItem;
  if (typeof origRemoveQueueItem === "function") {
    window.removeQueueItem = function (item) {
      cancelItemWork(item);
      return origRemoveQueueItem(item);
    };
  }

  if (typeof window.analyzeSingleItem === "function") {
    window.analyzeSingleItem = function (item) {
      return runItemPipeline(item);
    };
  }

  const origUpdateFileRowElement = window.updateFileRowElement;
  if (typeof origUpdateFileRowElement === "function") {
    window.updateFileRowElement = function (li, item, isActive) {
      origUpdateFileRowElement(li, item, isActive);
      const badge = li ? li.querySelector(".file-badge") : null;
      if (badge && (item.status === "pending" || item.status === "prechecking" || item.status === "awaiting_confirm" || item.status === "running")) {
        badge.className = "file-badge";
        applyRowBadge(badge, item);
      }
    };
  }

  const origRenderActiveDetail = window.renderActiveDetail;
  if (typeof origRenderActiveDetail === "function") {
    window.renderActiveDetail = function () {
      if (state.activeIndex >= 0 && state.activeIndex < state.items.length) {
        const item = state.items[state.activeIndex];
        if (item && (item.status === "pending" || item.status === "prechecking" || item.status === "awaiting_confirm" || item.status === "running")) {
          if (typeof hideMetricNotePopover === "function") hideMetricNotePopover();
          emptyState.style.display = "none";
          detailView.style.display = "flex";
          const infoBarEl = document.getElementById("audioInfoBar");
          if (infoBarEl) infoBarEl.style.display = "none";
          chartCard.style.display = "none";
          chartTooltip.style.display = "none";
          cardsGrid.innerHTML = buildInProgressDetailHtml(item);
          return;
        }
      }
      return origRenderActiveDetail();
    };
  }

  // 捕获阶段监听队列清空与全局快捷键，确保及时释放中止信号与服务端临时会话
  const clearBtnEl = document.getElementById("clearQueueBtn");
  if (clearBtnEl) {
    clearBtnEl.addEventListener("click", () => {
      cancelAllItems(state.items);
    }, true);
  }
  const confirmClearBtnEl = document.getElementById("confirmClearQueueBtn");
  if (confirmClearBtnEl) {
    confirmClearBtnEl.addEventListener("click", () => {
      cancelAllItems(state.items);
    }, true);
  }

  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (handleEscapeKey()) {
        e.preventDefault();
        e.stopImmediatePropagation();
      }
      return;
    }
    if ((e.key === "ArrowUp" || e.key === "ArrowDown") && hasBlockingModalOpen()) {
      e.stopImmediatePropagation();
    }
  }, true);

  window.MalodyPrecheckUI = Object.freeze({
    runItemPipeline,
    cancelItemWork,
    cancelAllItems,
    handleEscapeKey,
    hasBlockingModalOpen,
    applyRowBadge,
    buildInProgressDetailHtml,
  });
})();
</script>
"""


def inject_precheck_ui(index_html: str) -> str:
    """将精简格式提示、新增弹窗、样式及前端控制器无侵入注入到 INDEX_HTML。"""
    html = index_html

    # 1. 收紧文件选择框默认展示格式为精简 FFmpeg 原生解码器格式集合（不阻拦拖拽任意后缀文件）
    html = html.replace(
        'accept="audio/*,.ogg,.flac,.mp3,.wav,.m4a,.opus,.aac"',
        f'accept="{UI_ACCEPT_EXTENSIONS}"',
        1,
    )
    html = html.replace(
        '<div class="dropzone-sub">支持 .ogg / .flac / .mp3 / .wav / .m4a (可多选批量)</div>',
        '<div class="dropzone-sub">支持 .ogg / .mp3 / .flac / .wav / .m4a / .aac / .opus 格式的单音轨音频（不支持多轨会话，支持批量选择）</div>',
        1,
    )

    # 2. 注入扩展样式与弹窗 DOM
    html = html.replace("</style>", _PRECHECK_CSS + "\n</style>", 1)
    html = html.replace(
        "<!-- 节拍匹配与占总时长比例 · 0延迟轻量计算说明浮窗 -->",
        _PRECHECK_MODALS_HTML + "\n<!-- 节拍匹配与占总时长比例 · 0延迟轻量计算说明浮窗 -->",
        1,
    )

    # 3. 在主脚本之后注入独立控制器脚本
    html = html.replace("</body>", _PRECHECK_JS + "\n</body>", 1)
    return html
