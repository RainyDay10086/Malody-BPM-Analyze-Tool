#!/usr/bin/env bash
# ==============================================================================
# 精简版 macOS x64 (x86_64) FFmpeg & FFprobe 一键编译脚本
#
# 严格对齐 web_gui_precheck.py 的 FFMPEG_MINIMAL_BUILD_PROFILE 白名单：
#   - 基于 FFmpeg 9.0.2 官方源码静态编译，零 Homebrew/第三方动态库依赖
#   - 仅保留 file/pipe 协议、ogg/mp3/flac/wav/mov/aac 容器、22 种纯 C 原生音频解码器、
#     pcm_s16le/s24le/f32le 编码与复用器，以及 aresample/aformat/pan/anull 滤镜
#   - 仅依赖 macOS 系统内建 libSystem.B.dylib / libz.1.dylib / Apple 系统框架
#   - 中间下载与编译文件全部置于临时目录，完成后自动清除，产物写入 malody_bpm/
# ==============================================================================

set -euo pipefail

PACKAGING_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$PACKAGING_DIR/.." && pwd)"
MALODY_DIR="$ROOT_DIR/malody_bpm"
FFMPEG_VERSION="${FFMPEG_VERSION:-9.0.2}"
FFMPEG_URL="https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.xz"

TMP_BUILD="$(mktemp -d -t malody_ffmpeg_XXXXXX)"
cleanup() {
    rm -rf "$TMP_BUILD"
}
trap cleanup EXIT

echo "[1/4] 下载并解压 FFmpeg ${FFMPEG_VERSION}..."
curl -fsSL -o "$TMP_BUILD/ffmpeg.tar.xz" "$FFMPEG_URL"
tar -xf "$TMP_BUILD/ffmpeg.tar.xz" -C "$TMP_BUILD"

SRC_DIR="$TMP_BUILD/ffmpeg-${FFMPEG_VERSION}"
BUILD_DIR="$TMP_BUILD/build"
OUT_DIR="$TMP_BUILD/out"
mkdir -p "$BUILD_DIR" "$OUT_DIR"

echo "[2/4] 配置精简组件 (x86_64, minos 10.15, --disable-autodetect)..."
cd "$BUILD_DIR"
"$SRC_DIR/configure" \
    --prefix="$OUT_DIR" \
    --arch=x86_64 \
    --cc="clang" \
    --extra-cflags="-arch x86_64 -mmacosx-version-min=10.15 -O3" \
    --extra-ldflags="-arch x86_64 -mmacosx-version-min=10.15" \
    --disable-shared \
    --enable-static \
    --disable-debug \
    --disable-doc \
    --disable-htmlpages \
    --disable-manpages \
    --disable-podpages \
    --disable-txtpages \
    --disable-autodetect \
    --enable-pthreads \
    --enable-zlib \
    --disable-network \
    --disable-everything \
    --disable-avdevice \
    --disable-programs \
    --enable-ffmpeg \
    --enable-ffprobe \
    --enable-protocol=file,pipe \
    --enable-demuxer=ogg,mp3,flac,wav,mov,aac \
    --enable-parser=vorbis,opus,mpegaudio,flac,aac,aac_latm \
    --enable-decoder=vorbis,opus,mp3,mp3float,flac,aac,aac_latm,alac,pcm_s16le,pcm_s16be,pcm_s24le,pcm_s24be,pcm_s32le,pcm_s32be,pcm_f32le,pcm_f32be,pcm_f64le,pcm_f64be,pcm_u8,pcm_s8,pcm_alaw,pcm_mulaw \
    --enable-encoder=pcm_s16le,pcm_s24le,pcm_f32le \
    --enable-muxer=pcm_s16le,pcm_s24le,pcm_f32le \
    --enable-filter=aresample,aformat,pan,anull \
    --enable-bsf=null,aac_adtstoasc \
    >/dev/null

echo "[3/4] 编译并 strip 瘦身..."
NCPU="$(sysctl -n hw.ncpu 2>/dev/null || echo 4)"
make -j"$NCPU" >/dev/null
make install >/dev/null
strip -x "$OUT_DIR/bin/ffmpeg" "$OUT_DIR/bin/ffprobe"

echo "[4/4] 安装到 malody_bpm/ 并执行 Ad-hoc 签名..."
cp "$OUT_DIR/bin/ffmpeg" "$MALODY_DIR/ffmpeg"
cp "$OUT_DIR/bin/ffprobe" "$MALODY_DIR/ffprobe"
chmod +x "$MALODY_DIR/ffmpeg" "$MALODY_DIR/ffprobe"
codesign --force --sign - "$MALODY_DIR/ffmpeg"
codesign --force --sign - "$MALODY_DIR/ffprobe"

echo "完成: $MALODY_DIR/ffmpeg ($(du -h "$MALODY_DIR/ffmpeg" | awk '{print $1}')), $MALODY_DIR/ffprobe ($(du -h "$MALODY_DIR/ffprobe" | awk '{print $1}'))"
