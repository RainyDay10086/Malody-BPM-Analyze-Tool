import os
import sys
import json
import math
import ctypes
import shutil
import hashlib
import tempfile
import platform
import sysconfig
import subprocess
import numpy as np


def _detect_process_arch() -> str:
    """精确识别当前 Python 进程自身的指令集架构 ('arm64' 或 'x86_64')。"""
    if sys.platform == "win32":
        # Windows 下优先读取编译进 Python 解释器的目标平台，避免 x64 进程在 WoW64/ARM64 仿真下被机器硬件名误导
        plat = sysconfig.get_platform().lower()
        if "arm64" in plat or "aarch64" in plat:
            return "arm64"
        if "amd64" in plat or "x86_64" in plat:
            return "x86_64"
        ver = sys.version.lower()
        if "arm64" in ver or "aarch64" in ver:
            return "arm64"
        if "amd64" in ver or "x86_64" in ver:
            return "x86_64"

    # macOS universal2 Python 下 sysconfig.get_platform() 为 universal2，uname/machine 才反映当前实际运行切片
    mach = platform.machine().lower()
    if mach in ("arm64", "aarch64"):
        return "arm64"
    if mach in ("x86_64", "amd64"):
        return "x86_64"
    return mach or "x86_64"


def _bind_c_dsp_prototypes(lib: ctypes.CDLL) -> ctypes.CDLL:
    """绑定 C 动态库的全部函数原型并校验导出符号完整性。"""
    lib.cascade_biquad_inplace_f32.argtypes = [
        ctypes.POINTER(ctypes.c_float),
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_double),
        ctypes.c_int,
    ]
    lib.cascade_biquad_inplace_f32.restype = None

    lib.cascade_biquad_inplace.argtypes = [
        ctypes.POINTER(ctypes.c_float),
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_double),
        ctypes.c_int,
    ]
    lib.cascade_biquad_inplace.restype = None

    lib.compute_median_float_histogram.argtypes = [
        ctypes.POINTER(ctypes.c_float),
        ctypes.c_int,
    ]
    lib.compute_median_float_histogram.restype = ctypes.c_float

    lib.accumulate_band_log_novelty.argtypes = [
        ctypes.POINTER(ctypes.c_float),
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_float,
        ctypes.c_float,
        ctypes.POINTER(ctypes.c_float),
        ctypes.c_int,
    ]
    lib.accumulate_band_log_novelty.restype = None

    lib.resample_to_1000hz.argtypes = [
        ctypes.POINTER(ctypes.c_float),
        ctypes.c_int,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_float),
        ctypes.c_int,
    ]
    lib.resample_to_1000hz.restype = None

    lib.apply_zero_phase_diff_exact.argtypes = [
        ctypes.POINTER(ctypes.c_float),
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_float),
        ctypes.POINTER(ctypes.c_double),
    ]
    lib.apply_zero_phase_diff_exact.restype = None

    lib.apply_zero_phase_diff.argtypes = [
        ctypes.POINTER(ctypes.c_float),
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_float),
    ]
    lib.apply_zero_phase_diff.restype = None

    lib.downmix_s16le_exact.argtypes = [
        ctypes.POINTER(ctypes.c_int16),
        ctypes.c_int,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_float),
    ]
    lib.downmix_s16le_exact.restype = None

    lib.downmix_s24le_exact.argtypes = [
        ctypes.POINTER(ctypes.c_uint8),
        ctypes.c_int,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_float),
    ]
    lib.downmix_s24le_exact.restype = None

    lib.downmix_f32le_exact.argtypes = [
        ctypes.POINTER(ctypes.c_float),
        ctypes.c_int,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_float),
    ]
    lib.downmix_f32le_exact.restype = None

    lib.autocorr_fft_exact_c.argtypes = [
        ctypes.POINTER(ctypes.c_float),
        ctypes.c_int,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_float),
    ]
    lib.autocorr_fft_exact_c.restype = None

    lib.find_acf_local_peaks_c.argtypes = [
        ctypes.POINTER(ctypes.c_float),
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_int),
    ]
    lib.find_acf_local_peaks_c.restype = ctypes.c_int

    lib.track_full_song_exact_c.argtypes = [
        ctypes.POINTER(ctypes.c_float),
        ctypes.c_int,
        ctypes.c_double,
        ctypes.POINTER(ctypes.c_double),
    ]
    lib.track_full_song_exact_c.restype = ctypes.c_int

    lib.track_full_song_relaxed_c.argtypes = [
        ctypes.POINTER(ctypes.c_float),
        ctypes.c_int,
        ctypes.c_double,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_double),
    ]
    lib.track_full_song_relaxed_c.restype = ctypes.c_int

    lib.scan_full_song_harmonics_c.argtypes = [
        ctypes.POINTER(ctypes.c_float),
        ctypes.c_int,
        ctypes.c_double,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_double,
        ctypes.POINTER(ctypes.c_double),
    ]
    lib.scan_full_song_harmonics_c.restype = ctypes.c_int

    lib.scan_full_song_harmonics_v2_c.argtypes = [
        ctypes.POINTER(ctypes.c_float),
        ctypes.c_int,
        ctypes.c_double,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_double,
        ctypes.POINTER(ctypes.c_double),
    ]
    lib.scan_full_song_harmonics_v2_c.restype = ctypes.c_int

    lib.scan_batch_harmonics_v2_c.argtypes = [
        ctypes.POINTER(ctypes.c_float),
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_double),
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_double,
        ctypes.POINTER(ctypes.c_double),
        ctypes.POINTER(ctypes.c_int),
    ]
    lib.scan_batch_harmonics_v2_c.restype = None

    lib.extract_onset_envelope_c.argtypes = [
        ctypes.POINTER(ctypes.c_float),
        ctypes.c_int,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_double),
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_float),
        ctypes.POINTER(ctypes.c_float),
        ctypes.c_int,
    ]
    lib.extract_onset_envelope_c.restype = None

    if hasattr(lib, "extract_onset_envelope_multi_c"):
        lib.extract_onset_envelope_multi_c.argtypes = [
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_double),
            ctypes.POINTER(ctypes.c_float),
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_int,
        ]
        lib.extract_onset_envelope_multi_c.restype = None
    return lib


def _candidate_resource_dirs(primary_dir: str | None = None) -> list[str]:
    """返回源码运行与 PyInstaller 冻结运行 (.app / onedir / onefile) 下的候选资源目录列表。"""
    raw_dirs: list[str] = []
    pkg_dir = os.path.dirname(os.path.abspath(__file__))
    if primary_dir:
        raw_dirs.append(os.path.abspath(primary_dir))
    raw_dirs.append(pkg_dir)

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        meipass_abs = os.path.abspath(meipass)
        raw_dirs.extend([
            os.path.join(meipass_abs, "malody_bpm"),
            meipass_abs,
            os.path.normpath(os.path.join(meipass_abs, "..", "Resources", "malody_bpm")),
            os.path.normpath(os.path.join(meipass_abs, "..", "Resources")),
        ])

    if getattr(sys, "frozen", False) and sys.executable:
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        raw_dirs.extend([
            os.path.join(exe_dir, "malody_bpm"),
            exe_dir,
            os.path.normpath(os.path.join(exe_dir, "..", "Frameworks", "malody_bpm")),
            os.path.normpath(os.path.join(exe_dir, "..", "Frameworks")),
            os.path.normpath(os.path.join(exe_dir, "..", "Resources", "malody_bpm")),
            os.path.normpath(os.path.join(exe_dir, "..", "Resources")),
        ])

    seen: set[str] = set()
    result: list[str] = []
    for d in raw_dirs:
        norm_d = os.path.normpath(d)
        if norm_d not in seen:
            seen.add(norm_d)
            result.append(norm_d)
    return result


def _resolve_resource_file(filename: str, primary_dir: str | None = None) -> tuple[str | None, list[str]]:
    """在候选资源目录中按优先级定位数据文件。"""
    searched: list[str] = []
    for d in _candidate_resource_dirs(primary_dir):
        cand = os.path.join(d, filename)
        searched.append(cand)
        if os.path.isfile(cand):
            return cand, searched
    return None, searched


def _resolve_macos_clang(run_kwargs: dict) -> str | None:
    """定位可用的 macOS clang 编译器，避免在未安装或半残缺 CLT 的用户机器上触发 libxcselect 弹窗。"""
    direct_candidates = [
        "/Library/Developer/CommandLineTools/usr/bin/clang",
        "/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/clang",
    ]
    for cand in direct_candidates:
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand

    which_clang = shutil.which("clang")
    if which_clang and os.path.realpath(which_clang) != "/usr/bin/clang":
        if os.path.isfile(which_clang) and os.access(which_clang, os.X_OK):
            return which_clang

    xcselect = "/usr/bin/xcode-select"
    if os.path.isfile(xcselect):
        try:
            probe = subprocess.run([xcselect, "-p"], **run_kwargs)
            dev_dir = (probe.stdout or "").strip()
            if probe.returncode != 0 or not dev_dir or not os.path.isdir(dev_dir):
                return None
            for rel_clang in (
                os.path.join("usr", "bin", "clang"),
                os.path.join("Toolchains", "XcodeDefault.xctoolchain", "usr", "bin", "clang"),
            ):
                real_clang = os.path.join(dev_dir, rel_clang)
                if os.path.isfile(real_clang) and os.access(real_clang, os.X_OK):
                    return real_clang
            return None
        except Exception:
            return None

    return None


def _auto_build_c_dsp(lib_dir: str, arch: str) -> tuple[ctypes.CDLL | None, str | None, str | None]:
    """当预编译库缺失或被系统策略拦截时，尝试调用本机编译器按当前架构即时编译并签名。"""
    default_pkg_dir = os.path.dirname(os.path.abspath(__file__))
    if getattr(sys, "frozen", False) and os.path.abspath(lib_dir) == default_pkg_dir:
        src_path, _ = _resolve_resource_file("c_dsp.c", lib_dir)
    else:
        cand_src = os.path.join(lib_dir, "c_dsp.c")
        src_path = cand_src if os.path.isfile(cand_src) else None

    if not src_path or not os.path.isfile(src_path):
        return None, None, None

    try:
        with open(src_path, "rb") as f:
            src_hash = hashlib.sha256(f.read()).hexdigest()[:12]
    except OSError:
        return None, None, None

    ext = ".dylib" if sys.platform == "darwin" else (".dll" if sys.platform == "win32" else ".so")
    cache_path = os.path.join(
        tempfile.gettempdir(),
        f"malody_bpm_c_dsp_{sys.platform}_{arch}_{src_hash}{ext}",
    )

    if os.path.isfile(cache_path):
        try:
            return _bind_c_dsp_prototypes(ctypes.CDLL(cache_path)), cache_path, None
        except Exception:
            pass

    tmp_out = f"{cache_path}.{os.getpid()}.tmp{ext}"
    run_kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "capture_output": True,
        "text": True,
        "timeout": 45,
    }
    if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    last_err = None
    try:
        if sys.platform == "darwin":
            clang_bin = _resolve_macos_clang(run_kwargs)
            if not clang_bin:
                return None, None, "未找到可用的 clang 编译器 (未安装 Command Line Tools)"
            macos_arch = "arm64" if arch == "arm64" else "x86_64"
            minos = "11.0" if macos_arch == "arm64" else "10.13"
            cmd = [
                clang_bin,
                "-arch", macos_arch,
                f"-mmacosx-version-min={minos}",
                "-O3", "-ffp-contract=off", "-fno-fast-math",
                "-dynamiclib",
                "-install_name", "@rpath/c_dsp.dylib",
                src_path,
                "-o", tmp_out,
                "-lm",
            ]
            res = subprocess.run(cmd, **run_kwargs)
            if res.returncode != 0:
                return None, None, (res.stderr or res.stdout or "clang 编译失败").strip()
            codesign_bin = "/usr/bin/codesign" if os.path.isfile("/usr/bin/codesign") else shutil.which("codesign")
            if codesign_bin:
                subprocess.run([codesign_bin, "--force", "--sign", "-", tmp_out], **run_kwargs)
            os.replace(tmp_out, cache_path)
            return _bind_c_dsp_prototypes(ctypes.CDLL(cache_path)), cache_path, None

        if sys.platform == "win32":
            win_target_gnu = "aarch64-w64-mingw32" if arch == "arm64" else "x86_64-w64-mingw32"
            win_target_msvc = "aarch64-pc-windows-msvc" if arch == "arm64" else "x86_64-pc-windows-msvc"
            build_cmds: list[list[str]] = []
            clang_bin = shutil.which("clang")
            if clang_bin:
                build_cmds.append([
                    clang_bin, f"--target={win_target_msvc}",
                    "-O3", "-ffp-contract=off", "-fno-fast-math",
                    "-shared", src_path, "-o", tmp_out,
                ])
                build_cmds.append([
                    clang_bin, f"--target={win_target_gnu}",
                    "-O3", "-ffp-contract=off", "-fno-fast-math",
                    "-shared", src_path, "-o", tmp_out,
                ])
            gcc_bin = shutil.which("gcc")
            if gcc_bin:
                build_cmds.append([
                    gcc_bin, "-O3", "-ffp-contract=off", "-fno-fast-math",
                    "-shared", src_path, "-o", tmp_out,
                ])
            cl_bin = shutil.which("cl")
            if cl_bin:
                build_cmds.append([
                    cl_bin, "/nologo", "/O2", "/fp:strict", "/LD",
                    src_path, f"/Fe:{tmp_out}",
                ])
            for cmd in build_cmds:
                res = subprocess.run(cmd, **run_kwargs)
                if res.returncode == 0 and os.path.isfile(tmp_out):
                    os.replace(tmp_out, cache_path)
                    try:
                        return _bind_c_dsp_prototypes(ctypes.CDLL(cache_path)), cache_path, None
                    except Exception as exc:
                        last_err = str(exc)
                else:
                    last_err = (res.stderr or res.stdout or "").strip()
    except Exception as exc:
        last_err = str(exc)
    finally:
        if os.path.exists(tmp_out):
            try:
                os.remove(tmp_out)
            except OSError:
                pass

    return None, None, last_err


def _load_c_dsp() -> tuple[ctypes.CDLL | None, str | None, str, str | None]:
    lib_dir = os.path.dirname(os.path.abspath(__file__))
    arch = _detect_process_arch()

    if sys.platform == "darwin":
        expected_rel = "malody_bpm/c_dsp.dylib"
        rel_names = ["c_dsp.dylib", f"c_dsp_{arch}.dylib"]
    elif sys.platform == "win32":
        win_tag = "arm64" if arch == "arm64" else "x64"
        other_tag = "x64" if win_tag == "arm64" else "arm64"
        expected_rel = f"malody_bpm/c_dsp_{win_tag}.dll"
        rel_names = [f"c_dsp_{win_tag}.dll", "c_dsp.dll", f"c_dsp_{other_tag}.dll"]
    else:
        expected_rel = "malody_bpm/c_dsp.so"
        rel_names = [f"c_dsp_{arch}.so", "c_dsp.so"]

    candidates: list[str] = []
    seen_cands: set[str] = set()

    def _add_candidate(path: str) -> None:
        norm = os.path.normpath(path)
        if norm not in seen_cands:
            seen_cands.add(norm)
            candidates.append(path)

    env_override = os.environ.get("MALODY_C_DSP_PATH", "").strip()
    if env_override:
        _add_candidate(env_override)
    for search_dir in _candidate_resource_dirs(lib_dir):
        for name in rel_names:
            _add_candidate(os.path.join(search_dir, name))
    global _c_dsp_candidates
    _c_dsp_candidates = list(candidates)

    errors: list[str] = []
    for cand in candidates:
        if not os.path.isfile(cand):
            continue
        try:
            lib = _bind_c_dsp_prototypes(ctypes.CDLL(cand))
            return lib, cand, expected_rel, None
        except Exception as exc:
            errors.append(f"{os.path.basename(cand)}: {exc}")

    # 若预编译动态库缺失或因 Gatekeeper 隔离/架构不匹配加载失败，自动尝试即时编译当前架构动态库
    built_lib, built_path, build_err = _auto_build_c_dsp(lib_dir, arch)
    if built_lib is not None:
        return built_lib, built_path, expected_rel, None
    if build_err:
        errors.append(f"auto-build: {build_err}")

    err_msg = "; ".join(errors) if errors else f"未找到 {expected_rel}"
    return None, None, expected_rel, err_msg


_c_dsp_candidates: list[str] = []
_c_dsp, _lib_path, _c_dsp_lib_name, _c_dsp_error = _load_c_dsp()


_DEFAULT_LIB = object()


def require_c_dsp(lib: ctypes.CDLL | None = _DEFAULT_LIB) -> ctypes.CDLL:
    """
    断言 C 原生动态库已成功加载；若缺失或加载失败，抛出标准化 RuntimeError。
    支持传入指定模块内的 lib 引用以支持针对性隔离校验。
    """
    active_lib = _c_dsp if lib is _DEFAULT_LIB else lib
    if active_lib is None:
        arch = _detect_process_arch()
        detail_error = _c_dsp_error if _c_dsp_error else "未找到匹配架构的原生动态库文件"
        raise RuntimeError(
            f"Malody-BPM-Detector 严格依赖 C 原生高性能 DSP 动态库 ({_c_dsp_lib_name})，"
            f"但加载与即时编译均失败。\n"
            f"系统平台: {sys.platform} ({arch})\n"
            f"检索路径: {_c_dsp_candidates}\n"
            f"失败原因: {detail_error}\n"
            f"解决方案: 请参考 README.md 编译原生动态库，或在运行环境中安装 clang / gcc / msvc 编译器。"
        )
    return active_lib

# 加载系数表 (逆向提取自 Malody 0x10042f100 / 0x78EBF0)
_coeff_path, _coeff_searched = _resolve_resource_file("filter_coeffs.json")
if _coeff_path is None:
    raise FileNotFoundError(
        f"未找到滤波器系数文件 filter_coeffs.json，已检索路径: {_coeff_searched}"
    )
with open(_coeff_path, "r", encoding="utf-8") as f:
    _FILTER_COEFFS = json.load(f)

# 各频段在滤波器表中的切片配置 (start_stage, stage_count)
BAND_CONFIGS = [
    (0, 2),   # Band 0: 2 级双二阶低通 (< 600 Hz)
    (2, 4),   # Band 1: 4 级双二阶带通 (600 ~ 1200 Hz)
    (6, 4),   # Band 2: 4 级双二阶带通 (1200 ~ 2400 Hz)
    (10, 2),  # Band 3: 2 级双二阶高通 (> 2400 Hz)
]

# 能量平滑滤波配置 (start_stage, stage_count)
SMOOTH_CONFIGS = [
    (12, 1),  # Band 0 专用能量平滑
    (0, 0),   # Band 1 无单独平滑
    (0, 0),   # Band 2 无单独平滑
    (0, 0),   # Band 3 无单独平滑
]

# 群延迟补偿量 (采样点) 与 融合权重
BAND_DELAYS = [-320, -64, -32, 0]
BAND_WEIGHTS = [0.30, 0.20, 0.20, 0.30]

# 主融合包络的末端平滑滤波 (2 级双二阶低通)
MASTER_SMOOTH_CONFIG = (13, 2)


def get_filter_stages(sample_rate: int, start_idx: int, count: int) -> np.ndarray:
    """获取指定采样率下的滤波器阶段参数数组 (shape: count * 5)"""
    sr_key = str(sample_rate)
    if sr_key not in _FILTER_COEFFS:
        sr_key = "44100"  # 默认回退 44100
    all_stages = _FILTER_COEFFS[sr_key]
    stages = all_stages[start_idx : start_idx + count]
    return np.array(stages, dtype=np.float64)


def apply_biquad_cascade(data: np.ndarray, stages: np.ndarray, exact: bool = True) -> np.ndarray:
    """
    执行级联双二阶滤波 (Direct Form II)：
    - exact=True:  100% 还原 Malody 0x100191e84 的单精度 float32 滤波器状态机
    - exact=False: 使用双精度 float64 滤波器状态机，抑制长音频递归舍入误差
    """
    if len(stages) == 0:
        return data.copy()

    c_dsp = require_c_dsp(_c_dsp)
    out = np.ascontiguousarray(data.copy(), dtype=np.float32)
    num_stages = len(stages)
    stages_flat = np.ascontiguousarray(stages.flatten(), dtype=np.float64)

    if exact:
        c_dsp.cascade_biquad_inplace_f32(
            out.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
            len(out),
            stages_flat.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
            num_stages
        )
    else:
        c_dsp.cascade_biquad_inplace(
            out.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
            len(out),
            stages_flat.ctypes.data_as(ctypes.POINTER(ctypes.c_double)),
            num_stages
        )
    return out


def compute_median(data: np.ndarray) -> float:
    """计算 4096-bin 浮点直方图中值 (Malody 0x10017e3b2，包络管线以 esi=-4 调用)"""
    c_dsp = require_c_dsp(_c_dsp)
    arr = np.ascontiguousarray(data, dtype=np.float32)
    return float(c_dsp.compute_median_float_histogram(
        arr.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
        len(arr)
    ))


def resample_1000hz(src: np.ndarray, sample_rate: int) -> np.ndarray:
    """
    将信号重采样至 1000 Hz (1 ms/采样点，严格对齐 Malody 0x100192871 - 0x10019295a)：
    目标长度 n_dst = ceil(n_src * (1000.0 / sample_rate))，采样下标 idx = lrint(m / factor)，
    当末端 idx >= n_src 时置 0.0f (对齐 0x10019294c)。
    """
    c_dsp = require_c_dsp(_c_dsp)
    factor = 1000.0 / float(sample_rate)
    total_ms = int(math.ceil(len(src) * factor))
    if total_ms <= 0:
        return np.zeros(0, dtype=np.float32)
    dst = np.zeros(total_ms, dtype=np.float32)
    src_c = np.ascontiguousarray(src, dtype=np.float32)

    c_dsp.resample_to_1000hz(
        src_c.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
        len(src_c),
        sample_rate,
        dst.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
        total_ms
    )
    return dst

