#!/bin/sh
# Build a private FFmpeg 6.1.1 for the supported MP4/MOV workflow.
# Requires curl, tar, make, a C compiler, pkg-config, libass and libx264 dev files.
# No sudo, system installation, or shell profile edits are performed.
set -eu

assglass_script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
assglass_project_dir=$(CDPATH= cd -- "$assglass_script_dir/.." && pwd)
assglass_destination="$assglass_project_dir/.tools/ffmpeg-6.1.1"
assglass_event_prefix="$assglass_project_dir/.tools/libass-event-images"
if [ -f "$assglass_event_prefix/lib/pkgconfig/libass.pc" ]; then
  PKG_CONFIG_PATH="$assglass_event_prefix/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
  export PKG_CONFIG_PATH
  # The macOS install name is absolute. Linux requires an explicit runtime path.
  LDFLAGS="${LDFLAGS:-} -Wl,-rpath,$assglass_event_prefix/lib"
  export LDFLAGS
fi
assglass_build_dir=$(mktemp -d "${TMPDIR:-/tmp}/assglass-ffmpeg.XXXXXX")
trap 'rm -rf -- "$assglass_build_dir"' EXIT HUP INT TERM

pkg-config --exists libass x264
curl --fail --location --output "$assglass_build_dir/source.tar.xz" \
  https://ffmpeg.org/releases/ffmpeg-6.1.1.tar.xz

assglass_expected_sha=8684f4b00f94b85461884c3719382f1261f0d9eb3d59640a1f4ac0873616f968
if command -v sha256sum >/dev/null 2>&1; then
  assglass_actual_sha=$(sha256sum "$assglass_build_dir/source.tar.xz")
else
  assglass_actual_sha=$(shasum -a 256 "$assglass_build_dir/source.tar.xz")
fi
assglass_actual_sha=${assglass_actual_sha%% *}
if [ "$assglass_actual_sha" != "$assglass_expected_sha" ]; then
  echo "FFmpeg source checksum mismatch; refusing to build." >&2
  exit 1
fi

mkdir "$assglass_build_dir/source"
tar -xf "$assglass_build_dir/source.tar.xz" -C "$assglass_build_dir/source" --strip-components=1
cd "$assglass_build_dir/source"
./configure \
  --prefix="$assglass_destination" \
  --disable-everything --disable-doc --disable-debug --disable-x86asm --disable-ffplay \
  --enable-gpl --enable-libx264 --enable-libass --enable-ffmpeg --enable-ffprobe \
  --enable-avfilter --enable-swscale --enable-swresample \
  --enable-protocol=file,pipe \
  --enable-demuxer=mov,rawvideo \
  --enable-muxer=mp4,mov,rawvideo,null \
  --enable-decoder=h264,rawvideo,aac,pcm_s16le,wrapped_avframe \
  --enable-encoder=libx264,rawvideo,wrapped_avframe,aac \
  --enable-parser=h264,aac \
  --enable-filter=ass,gblur,maskedmerge,split,format,setparams,settb,setpts,showinfo,select,color,testsrc2,crop,scale,null,anull \
  --enable-indev=lavfi
make -j "${ASSGLASS_BUILD_JOBS:-4}"

# Publish only after a complete successful build. Re-running explicitly replaces
# this project's private executables, never the user's system FFmpeg.
mkdir -p "$assglass_destination"
cp ffmpeg "$assglass_destination/ffmpeg.new"
cp ffprobe "$assglass_destination/ffprobe.new"
mv -f "$assglass_destination/ffmpeg.new" "$assglass_destination/ffmpeg"
mv -f "$assglass_destination/ffprobe.new" "$assglass_destination/ffprobe"
"$assglass_destination/ffmpeg" -version > "$assglass_destination/build-version.txt"
cp COPYING.GPLv2 "$assglass_destination/COPYING.GPLv2"
echo "Built private tools at $assglass_destination"
echo "Run: sh scripts/test_matrix.sh .tools/ffmpeg-6.1.1"
