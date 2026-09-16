#!/bin/sh
# Build the optional, project-private EventImages extension; never install globally.
set -eu
assglass_script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
assglass_project_dir=$(CDPATH= cd -- "$assglass_script_dir/.." && pwd)
assglass_destination="$assglass_project_dir/.tools/libass-event-images"
assglass_cache="$assglass_project_dir/.tools/libass-event-build"
assglass_archive="$assglass_cache/libass-0.16.0.tar.xz"
assglass_build_dir=$(mktemp -d "${TMPDIR:-/tmp}/assglass-libass.XXXXXX")
trap 'rm -rf -- "$assglass_build_dir"' EXIT HUP INT TERM
mkdir -p "$assglass_cache"
pkg-config --exists freetype2 fribidi harfbuzz
if [ ! -f "$assglass_archive" ]; then
  curl --fail --location --output "$assglass_archive" \
    https://github.com/libass/libass/releases/download/0.16.0/libass-0.16.0.tar.xz
fi
assglass_expected_sha=5dbde9e22339119cf8eed59eea6c623a0746ef5a90b689e68a090109078e3c08
if command -v sha256sum >/dev/null 2>&1; then
  assglass_actual_sha=$(sha256sum "$assglass_archive")
else
  assglass_actual_sha=$(shasum -a 256 "$assglass_archive")
fi
assglass_actual_sha=${assglass_actual_sha%% *}
if [ "$assglass_actual_sha" != "$assglass_expected_sha" ]; then
  echo "libass source checksum mismatch; refusing to build." >&2
  exit 1
fi
tar -xf "$assglass_archive" -C "$assglass_build_dir" --strip-components=1
cd "$assglass_build_dir"
patch -p1 < "$assglass_project_dir/native/libass-0.16.0-event-images.patch"
./configure --prefix="$assglass_destination" --libdir="$assglass_destination/lib" \
  --enable-shared --disable-static --disable-asm
make -j "${ASSGLASS_BUILD_JOBS:-4}"
make install
cp COPYING "$assglass_destination/COPYING"
cp "$assglass_project_dir/native/libass-0.16.0-event-images.patch" "$assglass_destination/"
printf '%s\n' "libass 0.16.0 source SHA256 $assglass_expected_sha; Assglass EventImages ABI 1" \
  > "$assglass_destination/build-version.txt"
echo "Built private libass at $assglass_destination"
echo "Next: sh scripts/build_ffmpeg.sh; python3 native/build.py"
