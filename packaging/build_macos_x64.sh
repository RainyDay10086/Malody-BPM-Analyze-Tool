#!/usr/bin/env bash
# ==============================================================================
# MalodyBPMDetector macOS x64 (x86_64) 一键打包脚本
#
# 功能流程：
#   1. 检查并按需编译 x86_64 原生 C-DSP 动态库 (malody_bpm/c_dsp.dylib)
#   2. 检查精简版 macOS x64 FFmpeg / FFprobe (malody_bpm/ffmpeg, malody_bpm/ffprobe)
#   3. 检查并按需从 app_icon.png 生成 macOS 图标 (packaging/app_icon.icns)
#   4. 调用 PyInstaller 6.x 构建 x86_64 MalodyBPMDetector.app（中间产物置于临时目录）
#   5. 对 .app 及其内嵌可执行/动态库执行完整 Ad-hoc 代码签名与严格校验
#   6. 制作带 Applications 快捷方式的压缩镜像 output/MalodyBPMDetector-macos-x64.dmg
# ==============================================================================

set -euo pipefail

PACKAGING_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$PACKAGING_DIR/.." && pwd)"
MALODY_DIR="$ROOT_DIR/malody_bpm"
OUTPUT_DIR="$ROOT_DIR/output"
SPEC_FILE="$PACKAGING_DIR/MalodyBPMDetector_macos_x64.spec"
APP_NAME="MalodyBPMDetector.app"
APP_PATH="$OUTPUT_DIR/$APP_NAME"
DMG_PATH="$OUTPUT_DIR/MalodyBPMDetector-macos-x64.dmg"

# 临时工作目录：承载 PyInstaller workpath、iconset 与 DMG 暂存区，退出时自动清理
TMP_WORK="$(mktemp -d -t malody_pkg_XXXXXX)"
cleanup() {
    rm -rf "$TMP_WORK"
}
trap cleanup EXIT

echo "[1/6] 检查原生组件 (c_dsp.dylib / ffmpeg / ffprobe)..."

# 若尚未存在 c_dsp.dylib 或架构不包含 x86_64，则按 filters.py 一致参数编译
if [[ ! -f "$MALODY_DIR/c_dsp.dylib" ]] || ! lipo -archs "$MALODY_DIR/c_dsp.dylib" 2>/dev/null | grep -qw "x86_64"; then
    echo "  -> 编译 x86_64 c_dsp.dylib..."
    clang -arch x86_64 -mmacosx-version-min=10.13 -O3 -ffp-contract=off -fno-fast-math \
        -dynamiclib -install_name "@rpath/c_dsp.dylib" \
        "$MALODY_DIR/c_dsp.c" -o "$MALODY_DIR/c_dsp.dylib" -lm
    codesign --force --sign - "$MALODY_DIR/c_dsp.dylib"
fi

for ff_bin in ffmpeg ffprobe; do
    if [[ ! -x "$MALODY_DIR/$ff_bin" ]]; then
        echo "错误: 缺少可执行的 $MALODY_DIR/$ff_bin，请先编译精简版 macOS x64 FFmpeg/FFprobe。" >&2
        exit 1
    fi
done

echo "[2/6] 检查应用图标 (app_icon.icns)..."
if [[ ! -f "$PACKAGING_DIR/app_icon.icns" ]]; then
    if [[ ! -f "$ROOT_DIR/app_icon.png" ]]; then
        echo "错误: 缺少 app_icon.png 与 app_icon.icns" >&2
        exit 1
    fi
    ICONSET="$TMP_WORK/app_icon.iconset"
    mkdir -p "$ICONSET"
    sips -z 16 16     "$ROOT_DIR/app_icon.png" --out "$ICONSET/icon_16x16.png"      >/dev/null
    sips -z 32 32     "$ROOT_DIR/app_icon.png" --out "$ICONSET/icon_16x16@2x.png"   >/dev/null
    sips -z 32 32     "$ROOT_DIR/app_icon.png" --out "$ICONSET/icon_32x32.png"      >/dev/null
    sips -z 64 64     "$ROOT_DIR/app_icon.png" --out "$ICONSET/icon_32x32@2x.png"   >/dev/null
    sips -z 128 128   "$ROOT_DIR/app_icon.png" --out "$ICONSET/icon_128x128.png"    >/dev/null
    sips -z 256 256   "$ROOT_DIR/app_icon.png" --out "$ICONSET/icon_128x128@2x.png" >/dev/null
    sips -z 256 256   "$ROOT_DIR/app_icon.png" --out "$ICONSET/icon_256x256.png"    >/dev/null
    sips -z 512 512   "$ROOT_DIR/app_icon.png" --out "$ICONSET/icon_256x256@2x.png" >/dev/null
    sips -z 512 512   "$ROOT_DIR/app_icon.png" --out "$ICONSET/icon_512x512.png"    >/dev/null
    sips -z 1024 1024 "$ROOT_DIR/app_icon.png" --out "$ICONSET/icon_512x512@2x.png" >/dev/null
    iconutil -c icns "$ICONSET" -o "$PACKAGING_DIR/app_icon.icns"
fi

echo "[3/6] 运行 PyInstaller 构建 x86_64 .app（于临时目录避开 iCloud File Provider 扩展属性干扰）..."
TMP_DIST="$TMP_WORK/dist"
TMP_APP="$TMP_DIST/$APP_NAME"
mkdir -p "$TMP_DIST" "$OUTPUT_DIR"
rm -rf "$APP_PATH" "$OUTPUT_DIR/MalodyBPMDetector" "$DMG_PATH"

PYTHON_BIN="${PYTHON_BIN:-python3}"
"$PYTHON_BIN" -m PyInstaller \
    --noconfirm \
    --clean \
    --log-level WARN \
    --distpath "$TMP_DIST" \
    --workpath "$TMP_WORK/pyi_build" \
    "$SPEC_FILE"

if [[ ! -d "$TMP_APP" ]]; then
    echo "错误: PyInstaller 未生成 $TMP_APP" >&2
    exit 1
fi

echo "[4/6] 执行完整 Ad-hoc 代码签名..."
xattr -cr "$TMP_APP" 2>/dev/null || true

# 先对内部独立的原生二进制/动态库逐一重签，再对整个 .app 做 --deep 签名
for inner_bin in \
    "$TMP_APP/Contents/Frameworks/malody_bpm/ffmpeg" \
    "$TMP_APP/Contents/Frameworks/malody_bpm/ffprobe" \
    "$TMP_APP/Contents/Frameworks/malody_bpm/c_dsp.dylib"; do
    if [[ -f "$inner_bin" ]]; then
        chmod +x "$inner_bin"
        codesign --force --sign - "$inner_bin"
    fi
done

codesign --force --deep --sign - "$TMP_APP"
codesign --verify --deep --strict "$TMP_APP"

echo "[5/6] 制作分发镜像 $DMG_PATH 并同步输出 .app..."
DMG_STAGE="$TMP_WORK/dmg_stage"
TMP_DMG="$TMP_WORK/MalodyBPMDetector-macos-x64.dmg"
mkdir -p "$DMG_STAGE"
ditto --norsrc "$TMP_APP" "$DMG_STAGE/$APP_NAME"
ln -s /Applications "$DMG_STAGE/Applications"

hdiutil create \
    -volname "MalodyBPMDetector" \
    -srcfolder "$DMG_STAGE" \
    -ov \
    -format UDZO \
    "$TMP_DMG" >/dev/null

# 将签名完整的 DMG 与 .app 复制到 output/（注意 iCloud 目录内可能会附带同步元数据，分发优先使用 DMG）
cp -f "$TMP_DMG" "$DMG_PATH"
ditto "$TMP_APP" "$APP_PATH"

echo "[6/6] 构建完成:"
echo "  - App: $APP_PATH"
echo "  - DMG: $DMG_PATH"
