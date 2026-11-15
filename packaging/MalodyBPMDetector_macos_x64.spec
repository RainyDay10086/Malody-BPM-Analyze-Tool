# -*- mode: python ; coding: utf-8 -*-
"""
MalodyBPMDetector macOS x64 (x86_64) PyInstaller 打包配置文件。

说明：
1. 对齐 packaging/auto_py_to_exe_windows_x64.json 的应用名、无终端窗口及模块集合；
2. 通过 packaging/hooks/hook-malody_bpm.py 自动收集 c_dsp.dylib、精简版 ffmpeg/ffprobe、
   filter_coeffs.json 与 c_dsp.c 兜底源码；
3. 生成 onedir 形态的 MalodyBPMDetector.app，目标架构严格锁定 x86_64。
"""

import os
import sys

# SPECPATH 指向 packaging/ 目录，项目根目录为其上一级目录
PACKAGING_DIR = os.path.abspath(SPECPATH)
ROOT_DIR = os.path.dirname(PACKAGING_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

ICON_ICNS = os.path.join(PACKAGING_DIR, "app_icon.icns")
if not os.path.isfile(ICON_ICNS):
    ICON_ICNS = None

HIDDEN_IMPORTS = [
    "web_gui",
    "malody_bpm",
    "malody_bpm.autocorr",
    "malody_bpm.decoder",
    "malody_bpm.detector",
    "malody_bpm.enhanced",
    "malody_bpm.enhanced_v2",
    "malody_bpm.enhanced_v3",
    "malody_bpm.force_v3",
    "malody_bpm.v3_post_refine",
    "malody_bpm.envelope",
    "malody_bpm.filters",
    "malody_bpm.harmonic",
    "flask",
    "werkzeug.serving",
    "web_gui_algo_doc",
    "web_gui_lifecycle",
    "web_gui_offset",
    "web_gui_precheck",
    "web_gui_settings",
    "web_gui_silence",
    "web_gui_tempo_regions",
    "web_gui_unmatched",
]

EXCLUDES = [
    "auto_py_to_exe",
    "eel",
    "gevent",
    "greenlet",
    "zope",
    "pytest",
    "delocate",
    "setuptools",
    "pip",
    "PySide6",
    "shiboken6",
    "PyQt5",
    "PyQt6",
    "tkinter",
    "matplotlib",
    "scipy",
    "PIL",
    "capstone",
    "lxml",
    "docx",
    "sounddevice",
    "requests",
    "httpx",
    "qrcode",
    "uncompyle6",
    "decompyle3",
    "xdis",
    "spark_parser",
]

a = Analysis(
    [os.path.join(ROOT_DIR, "web_gui.py")],
    pathex=[ROOT_DIR],
    binaries=[],
    datas=[],
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[os.path.join(PACKAGING_DIR, "hooks")],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MalodyBPMDetector",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="x86_64",
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON_ICNS,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="MalodyBPMDetector",
)

app = BUNDLE(
    coll,
    name="MalodyBPMDetector.app",
    icon=ICON_ICNS,
    bundle_identifier="com.malody.bpmdetector",
    info_plist={
        "CFBundleName": "MalodyBPMDetector",
        "CFBundleDisplayName": "Malody BPM Detector",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1.0.0",
        "LSMinimumSystemVersion": "14.0",
        "NSHighResolutionCapable": True,
    },
)
