import os
import sys
import shutil
import subprocess
import json
import ctypes
import numpy as np
from .filters import _c_dsp, require_c_dsp

_FF_BIN_CACHE: dict[str, str] = {}


def _resolve_ff_binary(name: str) -> str:
    """跨平台解析 ffmpeg / ffprobe 可执行文件路径 (兼容 macOS Apple Silicon /opt/homebrew/bin 与 Windows 常见安装位置)。"""
    cached = _FF_BIN_CACHE.get(name)
    if cached and os.path.isfile(cached):
        return cached

    env_keys = (f"MALODY_{name.upper()}_BIN", f"{name.upper()}_BINARY")
    for key in env_keys:
        val = os.environ.get(key, "").strip()
        if val and os.path.isfile(val):
            _FF_BIN_CACHE[name] = val
            return val

    found = shutil.which(name)
    if found:
        _FF_BIN_CACHE[name] = found
        return found

    exe_name = f"{name}.exe" if sys.platform == "win32" else name
    pkg_dir = os.path.dirname(os.path.abspath(__file__))
    proj_dir = os.path.dirname(pkg_dir)
    search_dirs: list[str] = [
        pkg_dir,
        proj_dir,
        os.path.join(proj_dir, "bin"),
    ]

    # 适配 PyInstaller / Auto-py-to-exe 打包环境 (支持 onefile 解压与 onedir / _internal 模式)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        meipass_abs = os.path.abspath(meipass)
        search_dirs.extend([
            os.path.join(meipass_abs, "malody_bpm"),
            meipass_abs,
            os.path.join(meipass_abs, "bin"),
        ])

    if getattr(sys, "frozen", False) and sys.executable:
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        search_dirs.extend([
            os.path.join(exe_dir, "malody_bpm"),
            exe_dir,
            os.path.join(exe_dir, "bin"),
            os.path.join(exe_dir, "_internal", "malody_bpm"),
            os.path.join(exe_dir, "_internal"),
            os.path.join(exe_dir, "_internal", "bin"),
        ])

    if sys.platform == "darwin":
        # Apple Silicon Homebrew 默认位于 /opt/homebrew/bin，GUI 启动时常不在默认 PATH 中
        search_dirs.extend([
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/opt/local/bin",
        ])
    elif sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA", "")
        user_profile = os.environ.get("USERPROFILE", "")
        prog_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        prog_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        if local_app:
            search_dirs.append(os.path.join(local_app, "Microsoft", "WinGet", "Links"))
        if user_profile:
            search_dirs.append(os.path.join(user_profile, "scoop", "shims"))
        search_dirs.extend([
            r"C:\ProgramData\chocolatey\bin",
            r"C:\ffmpeg\bin",
            os.path.join(prog_files, "ffmpeg", "bin"),
            os.path.join(prog_x86, "ffmpeg", "bin"),
        ])

    for d in search_dirs:
        cand = os.path.join(d, exe_name)
        if os.path.isfile(cand) and (sys.platform == "win32" or os.access(cand, os.X_OK)):
            _FF_BIN_CACHE[name] = cand
            return cand

    if sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA", "")
        if local_app:
            import glob
            pkg_pattern = os.path.join(local_app, "Microsoft", "WinGet", "Packages", "*FFmpeg*", "**", exe_name)
            for cand in glob.glob(pkg_pattern, recursive=True):
                if os.path.isfile(cand):
                    _FF_BIN_CACHE[name] = cand
                    return cand

    return name


def _subproc_extra_kwargs() -> dict:
    kwargs: dict = {"stdin": subprocess.DEVNULL}
    if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


MIN_PCM_DURATION_SEC = 45.0
MAX_PCM_DURATION_SEC = 600.0


def _validate_pcm_frames(
    n_frames: int,
    sr: int,
    capped_overflow: bool = False,
    allow_any_duration: bool = False,
) -> None:
    """全模式统一校验解码后的实际单声道 PCM 帧数是否在 [45s, 600s] 闭区间内（allow_any_duration=True 时仅校验非空）。"""
    if sr <= 0 or n_frames <= 0:
        raise ValueError("音频实际 PCM 时长为 0.00s，不在允许范围 [45s, 600s] 内")
    if allow_any_duration:
        return
    max_frames = int(round(MAX_PCM_DURATION_SEC * float(sr)))
    min_frames = int(round(MIN_PCM_DURATION_SEC * float(sr)))
    if capped_overflow or n_frames > max_frames:
        if capped_overflow:
            raise ValueError(f"音频实际 PCM 时长超过上限 (>{MAX_PCM_DURATION_SEC:.0f}s)，不在允许范围 [45s, 600s] 内")
        dur = n_frames / float(sr)
        raise ValueError(f"音频实际 PCM 时长 ({dur:.2f}s) 超出允许范围 [45s, 600s]")
    if n_frames < min_frames:
        dur = n_frames / float(sr)
        raise ValueError(f"音频实际 PCM 时长 ({dur:.2f}s) 短于允许范围 [45s, 600s]")


def _run_ffmpeg_decode(
    cmd: list[str],
    file_path: str,
    max_bytes: int | None = None,
) -> tuple[bytes, bytes, int, bool]:
    import threading

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **_subproc_extra_kwargs(),
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "未找到 FFmpeg 可执行文件 (ffmpeg)。请先安装 FFmpeg "
            "(macOS: brew install ffmpeg；Windows: winget install Gyan.FFmpeg) 并确保其在 PATH 中。"
        ) from exc

    if max_bytes is None or max_bytes <= 0:
        stdout, stderr = proc.communicate()
        return stdout, stderr, proc.returncode, False

    err_chunks: list[bytes] = []

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        data = proc.stderr.read()
        if data:
            err_chunks.append(data)

    err_thread = threading.Thread(target=_drain_stderr, daemon=True)
    err_thread.start()

    out_chunks: list[bytes] = []
    total_read = 0
    capped_overflow = False
    assert proc.stdout is not None
    try:
        while True:
            chunk = proc.stdout.read(min(262144, max_bytes - total_read + 4096))
            if not chunk:
                break
            out_chunks.append(chunk)
            total_read += len(chunk)
            if total_read >= max_bytes:
                capped_overflow = True
                try:
                    proc.kill()
                except OSError:
                    pass
                break
    finally:
        try:
            proc.stdout.close()
        except OSError:
            pass
        proc.wait()
        err_thread.join(timeout=2.0)

    return b"".join(out_chunks), b"".join(err_chunks), proc.returncode, capped_overflow


def match_supported_sample_rate(sr: int) -> int:
    """
    针对 32000, 44100, 48000 以外的非常规采样率：
    不统一暴力归一到 44100，而是乘（或除）2^n 倍频，直到精确匹配到 32k、44.1k、48k 之一。
    例如：
      96000 / 2 = 48000 Hz, 88200 / 2 = 44100 Hz,
      24000 * 2 = 48000 Hz, 22050 * 2 = 44100 Hz, 16000 * 2 = 32000 Hz
    避免非整数重采样的分数相位畸变，保持最佳音频瞬态精度。
    """
    TARGET_RATES = (48000, 44100, 32000)
    if sr in TARGET_RATES:
        return sr

    candidates = []
    for target in TARGET_RATES:
        if sr < target:
            if target % sr == 0:
                ratio = target // sr
                if (ratio & (ratio - 1)) == 0:
                    n = ratio.bit_length() - 1
                    candidates.append((n, target))
        else:
            if sr % target == 0:
                ratio = sr // target
                if (ratio & (ratio - 1)) == 0:
                    n = ratio.bit_length() - 1
                    candidates.append((n, target))

    if candidates:
        candidates.sort(key=lambda x: (x[0], -x[1]))
        return candidates[0][1]

    return min(TARGET_RATES, key=lambda t: abs(t - sr))


_STANDARD_FL_FR_LAYOUTS = frozenset({
    "stereo", "2.1", "3.0", "3.0(back)", "4.0", "quad", "quad(side)",
    "3.1", "5.0", "5.0(side)", "4.1", "5.1", "5.1(side)", "6.0", "6.0(front)",
    "3.1.2", "hexagonal", "6.1", "6.1(back)", "6.1(front)", "7.0", "7.0(front)",
    "7.1", "7.1(wide)", "7.1(wide-side)", "5.1.2", "5.1.2(back)", "octagonal",
    "cube", "5.1.4", "7.1.2", "7.1.4", "7.2.3", "9.1.4", "9.1.6",
    "hexadecagonal", "22.2",
})


def normalize_channel_mode(channel: str) -> str:
    """校验并归一化声道选择模式 ('auto' | 'left' | 'right' | 'downmix')。"""
    if not isinstance(channel, str):
        raise ValueError(
            f"channel 必须为字符串 ('auto' / 'left' / 'right' / 'downmix')，当前传入: {channel!r}"
        )
    norm = channel.strip().lower()
    if norm == "auto":
        return "auto"
    if norm in ("left", "l"):
        return "left"
    if norm in ("right", "r"):
        return "right"
    if norm == "downmix":
        return "downmix"
    raise ValueError(
        f"channel 必须为 'auto'、'left' ('l')、'right' ('r') 或 'downmix'，当前传入: {channel!r}"
    )


def _single_channel_pan_filter(side: str, channels: int, channel_layout: str) -> str:
    """构造单边声道提取的 FFmpeg pan 滤镜表达式（2ch 取 c0/c1；>2ch 优先 FL/FR 否则回退 c0/c1）。"""
    is_left = (side == "left")
    fallback_src = "c0" if is_left else "c1"
    if channels <= 2:
        return f"pan=mono|c0={fallback_src}"

    layout = (channel_layout or "").strip().lower()
    target_tok = "fl" if is_left else "fr"
    tokens = {tok.strip() for tok in layout.replace("|", "+").split("+") if tok.strip()}
    if layout in _STANDARD_FL_FR_LAYOUTS or target_tok in tokens:
        named_src = "FL" if is_left else "FR"
        return f"pan=mono|c0={named_src}"
    return f"pan=mono|c0={fallback_src}"


def _probe_audio_stream_full(file_path: str) -> tuple[int, int, str, int, str]:
    """
    使用 ffprobe 探测音频的原生采样率、声道数、编码格式、原始位深与声道布局。
    返回 (matched_sr, channels, codec_name, bits_per_raw_sample, channel_layout)。
    """
    try:
        cmd = [
            _resolve_ff_binary("ffprobe"),
            "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=sample_rate,channels,channel_layout,codec_name,bits_per_raw_sample,bits_per_sample",
            "-of", "json",
            file_path
        ]
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            **_subproc_extra_kwargs(),
        )
        data = json.loads(res.stdout)
        stream = data["streams"][0]
        raw_sr = int(stream.get("sample_rate", 44100))
        channels = int(stream.get("channels", 2))
        codec_name = str(stream.get("codec_name", ""))
        channel_layout = str(stream.get("channel_layout", "") or "").strip().lower()
        raw_bits = stream.get("bits_per_raw_sample") or stream.get("bits_per_sample") or 0
        try:
            bits = int(raw_bits)
        except (ValueError, TypeError):
            bits = 0
        return match_supported_sample_rate(raw_sr), max(1, channels), codec_name, bits, channel_layout
    except Exception:
        return 44100, 2, "", 0, ""


def probe_audio_stream(file_path: str) -> tuple[int, int, str, int]:
    """
    使用 ffprobe 探测音频的原生采样率、声道数、编码格式与原始位深。
    返回 (matched_sr, channels, codec_name, bits_per_raw_sample)。
    """
    sr, channels, codec_name, bits, _ = _probe_audio_stream_full(file_path)
    return sr, channels, codec_name, bits


def get_native_sample_rate(file_path: str) -> int:
    """
    使用 ffprobe 探测音频的原生采样率，并映射至最匹配的 Malody 原生滤波采样率 (48k / 44.1k / 32k)。
    """
    sr, _, _, _ = probe_audio_stream(file_path)
    return sr


def decode_audio_to_pcm(
    file_path: str,
    target_sr: int | None = None,
    exact: bool = True,
    allow_any_duration: bool = False,
    channel: str = "downmix",
) -> tuple[np.ndarray, int]:
    """
    将输入音频解码为指定采样率的 32 位浮点 PCM 数组（默认强制校验实际 PCM 时长属于 [45s, 600s]；allow_any_duration=True 时解除该限制）：
    - exact=True:  100% 还原 Malody 0x1000bf16e & 0x10017e57d 的解码与混音管线（固定返回一维单声道数组，忽略 channel）：
                   1) 常规压缩音频 (OGG/MP3/16-bit PCM, FMT=2) 解码为 s16le，每声道乘 1/32768 (3.0517578125e-05f)
                      在 float32 累加后再乘 (1.0f / channels)；
                   2) 24-bit 无损音频 (24-bit FLAC/WAV, FMT=3, 0x10017e68e) 解码为 s24le，每声道乘 1/8388608
                      (1.1920928955078125e-07f) 在 float32 累加后再乘 (1.0f / channels)；
                   3) 32-bit 浮点 WAV (pcm_f32le, FMT=5) 直接按 float32 累加混音。
    - exact=False: 增强模式以 32-bit 浮点 (f32le) 解码，按 channel 执行对应声道策略：
                   - "downmix" (独立调用默认): FFmpeg -ac 1 线性混音返回一维 (n_frames,) 数组；
                   - "auto": 单声道返回一维 (n_frames,)，多声道原样解码返回二维 (n_frames, channels) 供频段能量域融合；
                   - "left" / "right": 1ch 回退唯一单声道，>=2ch 提取左 (c0/FL) 或右 (c1/FR) 返回一维 (n_frames,)。
    """
    norm_channel = normalize_channel_mode(channel)
    probed_sr, channels, codec_name, bits, channel_layout = _probe_audio_stream_full(file_path)
    sr = probed_sr if target_sr is None else target_sr
    if sr <= 0:
        raise ValueError(f"无效的目标采样率: {sr}")
    ffmpeg_bin = _resolve_ff_binary("ffmpeg")
    max_cap_frames = None if allow_any_duration else (int(round(MAX_PCM_DURATION_SEC * float(sr))) + 1)

    is_24bit = (bits == 24) or codec_name.startswith("pcm_s24")

    if exact and is_24bit:
        cmd = [
            ffmpeg_bin,
            "-v", "error",
            "-i", file_path,
            "-f", "s24le",
            "-acodec", "pcm_s24le",
            "-ar", str(sr),
            "-"
        ]
        stdout, stderr, returncode, capped = _run_ffmpeg_decode(
            cmd,
            file_path,
            max_bytes=None if max_cap_frames is None else max_cap_frames * channels * 3,
        )

        if not capped and returncode != 0 and len(stdout) == 0:
            raise RuntimeError(f"FFmpeg 解码音频失败 ({file_path}): {stderr.decode('utf-8', errors='ignore')}")

        u8 = np.frombuffer(stdout, dtype=np.uint8)
        n_frames = len(u8) // (channels * 3)
        _validate_pcm_frames(n_frames, sr, capped_overflow=capped, allow_any_duration=allow_any_duration)

        u8 = np.ascontiguousarray(u8[: n_frames * channels * 3], dtype=np.uint8)
        out = np.zeros(n_frames, dtype=np.float32)

        require_c_dsp(_c_dsp).downmix_s24le_exact(
            u8.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)),
            n_frames,
            channels,
            out.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        )
        return out, sr

    if exact and codec_name != "pcm_f32le":
        cmd = [
            ffmpeg_bin,
            "-v", "error",
            "-i", file_path,
            "-f", "s16le",
            "-acodec", "pcm_s16le",
            "-ar", str(sr),
            "-"
        ]
        stdout, stderr, returncode, capped = _run_ffmpeg_decode(
            cmd,
            file_path,
            max_bytes=None if max_cap_frames is None else max_cap_frames * channels * 2,
        )

        if not capped and returncode != 0 and len(stdout) == 0:
            raise RuntimeError(f"FFmpeg 解码音频失败 ({file_path}): {stderr.decode('utf-8', errors='ignore')}")

        frame_bytes = channels * 2
        usable_bytes = (len(stdout) // frame_bytes) * frame_bytes
        s16 = np.frombuffer(stdout[:usable_bytes], dtype=np.int16)
        n_frames = len(s16) // channels
        _validate_pcm_frames(n_frames, sr, capped_overflow=capped, allow_any_duration=allow_any_duration)

        s16 = np.ascontiguousarray(s16[: n_frames * channels], dtype=np.int16)
        out = np.zeros(n_frames, dtype=np.float32)

        require_c_dsp(_c_dsp).downmix_s16le_exact(
            s16.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
            n_frames,
            channels,
            out.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        )
        return out, sr

    if exact and codec_name == "pcm_f32le" and channels >= 2:
        cmd = [
            ffmpeg_bin,
            "-v", "error",
            "-i", file_path,
            "-f", "f32le",
            "-acodec", "pcm_f32le",
            "-ar", str(sr),
            "-"
        ]
        stdout, stderr, returncode, capped = _run_ffmpeg_decode(
            cmd,
            file_path,
            max_bytes=None if max_cap_frames is None else max_cap_frames * channels * 4,
        )

        if not capped and returncode != 0 and len(stdout) == 0:
            raise RuntimeError(f"FFmpeg 解码音频失败 ({file_path}): {stderr.decode('utf-8', errors='ignore')}")

        frame_bytes = channels * 4
        usable_bytes = (len(stdout) // frame_bytes) * frame_bytes
        f32 = np.frombuffer(stdout[:usable_bytes], dtype=np.float32)
        n_frames = len(f32) // channels
        _validate_pcm_frames(n_frames, sr, capped_overflow=capped, allow_any_duration=allow_any_duration)

        f32 = np.ascontiguousarray(f32[: n_frames * channels], dtype=np.float32)
        out = np.zeros(n_frames, dtype=np.float32)

        require_c_dsp(_c_dsp).downmix_f32le_exact(
            f32.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
            n_frames,
            channels,
            out.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        )
        return out, sr

    eff_channel = "downmix" if exact else norm_channel

    if eff_channel == "auto" and channels >= 2:
        cmd = [
            ffmpeg_bin,
            "-v", "error",
            "-i", file_path,
            "-f", "f32le",
            "-acodec", "pcm_f32le",
            "-ar", str(sr),
            "-"
        ]
        stdout, stderr, returncode, capped = _run_ffmpeg_decode(
            cmd,
            file_path,
            max_bytes=None if max_cap_frames is None else max_cap_frames * channels * 4,
        )

        if not capped and returncode != 0 and len(stdout) == 0:
            raise RuntimeError(f"FFmpeg 解码音频失败 ({file_path}): {stderr.decode('utf-8', errors='ignore')}")

        frame_bytes = channels * 4
        usable_bytes = (len(stdout) // frame_bytes) * frame_bytes
        f32 = np.frombuffer(stdout[:usable_bytes], dtype=np.float32)
        n_frames = len(f32) // channels
        _validate_pcm_frames(n_frames, sr, capped_overflow=capped, allow_any_duration=allow_any_duration)

        pcm_multi = np.ascontiguousarray(
            f32[: n_frames * channels].reshape(n_frames, channels),
            dtype=np.float32,
        )
        return pcm_multi, sr

    if eff_channel in ("left", "right") and channels >= 2:
        pan_expr = _single_channel_pan_filter(eff_channel, channels, channel_layout)
        cmd = [
            ffmpeg_bin,
            "-v", "error",
            "-i", file_path,
            "-af", pan_expr,
            "-f", "f32le",
            "-acodec", "pcm_f32le",
            "-ar", str(sr),
            "-"
        ]
        stdout, stderr, returncode, capped = _run_ffmpeg_decode(
            cmd,
            file_path,
            max_bytes=None if max_cap_frames is None else max_cap_frames * 4,
        )

        if not capped and returncode != 0 and len(stdout) == 0:
            raise RuntimeError(f"FFmpeg 解码音频失败 ({file_path}): {stderr.decode('utf-8', errors='ignore')}")

        usable_bytes = (len(stdout) // 4) * 4
        pcm = np.frombuffer(stdout[:usable_bytes], dtype=np.float32)
        _validate_pcm_frames(len(pcm), sr, capped_overflow=capped, allow_any_duration=allow_any_duration)
        return pcm, sr

    # 增强模式 downmix (或单声道 f32le / 1ch 音频): 直接使用 FFmpeg 单声道浮点解码
    cmd = [
        ffmpeg_bin,
        "-v", "error",
        "-i", file_path,
        "-f", "f32le",
        "-acodec", "pcm_f32le",
        "-ac", "1",
        "-ar", str(sr),
        "-"
    ]

    stdout, stderr, returncode, capped = _run_ffmpeg_decode(
        cmd,
        file_path,
        max_bytes=None if max_cap_frames is None else max_cap_frames * 4,
    )

    if not capped and returncode != 0 and len(stdout) == 0:
        raise RuntimeError(f"FFmpeg 解码音频失败 ({file_path}): {stderr.decode('utf-8', errors='ignore')}")

    usable_bytes = (len(stdout) // 4) * 4
    pcm = np.frombuffer(stdout[:usable_bytes], dtype=np.float32)
    _validate_pcm_frames(len(pcm), sr, capped_overflow=capped, allow_any_duration=allow_any_duration)
    return pcm, sr
