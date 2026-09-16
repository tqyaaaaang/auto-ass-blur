import sys
import threading
import time

import pytest

from assglass.ffmpeg import PipePipeline


def test_stderr_reader_failure_unblocks_writer(tmp_path):
    script = "import os; os.write(2, b'x'*2000000); data=os.read(0, 2000000)"
    pipeline = PipePipeline([sys.executable, "-c", script], tmp_path,
                            tmp_path / "nonexistent" / "log", 1, 1)
    finished = threading.Event()
    errors = []
    def writer():
        try:
            pipeline.write_frame(b"x" * 2000000, 2000000)
        except (ValueError, OSError) as error:
            errors.append(error)
        finally:
            finished.set()
    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    try:
        assert finished.wait(5), "log failure left stdin writer blocked"
        assert errors
    finally:
        pipeline.close()
        thread.join(timeout=2)


def test_success_exit_does_not_hide_missing_frames(tmp_path):
    script = "import sys; sys.stdin.buffer.read(); sys.stderr.write('[Parsed_showinfo_0] n: 0 pts: 0\\n')"
    pipeline = PipePipeline([sys.executable, "-c", script], tmp_path, tmp_path / "log", 2, 1500)
    try:
        pipeline.write_frame(b"x", 1)
        with pytest.raises(ValueError, match="帧完整性"):
            pipeline.finish()
    finally:
        pipeline.close()


def test_wrong_ass_entry_pts_fails_even_when_count_matches(tmp_path):
    script = "import sys; sys.stdin.buffer.read(); sys.stderr.write('[Parsed_showinfo_0] n: 0 pts: 1\\n')"
    pipeline = PipePipeline([sys.executable, "-c", script], tmp_path, tmp_path / "log", 1, 1500)
    try:
        pipeline.write_frame(b"x", 1)
        with pytest.raises(ValueError, match="实际字幕入口帧"):
            pipeline.finish()
    finally:
        pipeline.close()
