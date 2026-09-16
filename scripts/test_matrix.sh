#!/bin/sh
# Run the real pipeline checks against explicitly supplied FFmpeg installations.
# Example: sh scripts/test_matrix.sh /opt/ffmpeg-6.1/bin /opt/ffmpeg-8/bin
# This does not download/install software or claim untested versions supported.
set -eu

if [ "$#" -eq 0 ]; then
  echo "Usage: sh scripts/test_matrix.sh /path/to/ffmpeg-and-ffprobe-directory [...]" >&2
  exit 2
fi

assglass_test_python=${ASSGLASS_TEST_PYTHON:-python3}
assglass_script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$assglass_script_dir/.."

for assglass_tool_dir in "$@"; do
  assglass_tool_dir=$(CDPATH= cd -- "$assglass_tool_dir" && pwd)
  test -x "$assglass_tool_dir/ffmpeg"
  test -x "$assglass_tool_dir/ffprobe"
  "$assglass_tool_dir/ffmpeg" -version
  "$assglass_tool_dir/ffprobe" -version
  ASSGLASS_TEST_FFMPEG="$assglass_tool_dir/ffmpeg" \
  ASSGLASS_TEST_FFPROBE="$assglass_tool_dir/ffprobe" \
    "$assglass_test_python" -m pytest -q tests/test_pipeline.py
done
