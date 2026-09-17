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


@pytest.mark.parametrize('reply', ['Success', 'Undefined error: 0'])
def test_activity_enable_command_success_formats(tmp_path, reply):
    line = ('[Parsed_sendcmd_1] Command reply for command #0: ret:' + reply + ' res:\n'
            '[Parsed_showinfo_0] n: 0 pts: 0\n')
    script = 'import sys; sys.stdin.buffer.read(); sys.stderr.write(%r)' % line
    pipeline = PipePipeline([sys.executable, '-c', script], tmp_path, tmp_path / 'log', 1, 1500)
    try:
        pipeline.write_frame(b'x', 1)
        assert pipeline.finish()['observed_frames'] == 1
    finally:
        pipeline.close()


def test_activity_command_failure_is_not_hidden_by_ffmpeg_success_exit(tmp_path):
    line = ('[Parsed_sendcmd_1] Command reply for command #0: ret:Function not implemented res:\n'
            '[Parsed_showinfo_0] n: 0 pts: 0\n')
    script = 'import sys; sys.stdin.buffer.read(); sys.stderr.write(%r)' % line
    pipeline = PipePipeline([sys.executable, '-c', script], tmp_path, tmp_path / 'log', 1, 1500)
    try:
        pipeline.write_frame(b'x', 1)
        with pytest.raises(ValueError, match='活动区间命令失败'):
            pipeline.finish()
    finally:
        pipeline.close()


def _sendcmd_ffmpeg():
    import os
    from pathlib import Path
    import shutil
    import subprocess
    local = Path(__file__).resolve().parents[1] / '.tools' / 'ffmpeg-6.1.1' / 'ffmpeg'
    requested = os.environ.get('ASSGLASS_TEST_FFMPEG', str(local) if local.is_file() else 'ffmpeg')
    binary = shutil.which(requested)
    if not binary:
        pytest.skip('real sendcmd regression needs FFmpeg')
    filters = subprocess.run([binary, '-hide_banner', '-filters'], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, check=True).stdout
    import re
    if not re.search(r'\s+sendcmd\s+', filters):
        pytest.skip('rebuild private FFmpeg with sendcmd for activity regression')
    return binary


def _activity_command_text(states, time_base, ticks, merge=True):
    from fractions import Fraction
    from assglass.ffmpeg import ACTIVITY_BLUR_TARGET, ACTIVITY_MERGE_TARGET
    changes = [0] + [i for i in range(1, len(states)) if states[i] != states[i - 1]]
    edges = [Fraction(0)] + [(Fraction(i) - Fraction(1, 2)) * ticks * time_base for i in changes[1:]]
    edges.append((Fraction(len(states)) - Fraction(1, 2)) * ticks * time_base)
    lines = []
    for j, index in enumerate(changes):
        commands = '[enter] %s enable %d' % (ACTIVITY_BLUR_TARGET, states[index])
        if merge:
            commands += ', [enter] %s enable %d' % (ACTIVITY_MERGE_TARGET, states[index])
        lines.append('%.9f-%.9f %s;\n' % (float(edges[j]), float(edges[j + 1]), commands))
    return ''.join(lines)


def _run_activity_graph(binary, directory, graph, mask_data, frame_bytes, rate, filter_threads, delivery):
    import re
    import subprocess
    from assglass.ffmpeg import write_all
    argv = [binary, '-hide_banner', '-nostdin', '-loglevel', 'verbose', '-nostats',
            '-filter_complex_threads', str(filter_threads), '-thread_queue_size', '64',
            '-f', 'rawvideo', '-pixel_format', 'yuv420p', '-video_size', '64x48',
            '-framerate', str(rate), '-i', 'base.raw', '-thread_queue_size', '64',
            '-f', 'rawvideo', '-pixel_format', 'yuv420p', '-video_size', '64x48',
            '-framerate', str(rate), '-i', 'pipe:0', '-filter_complex', graph,
            '-map', '[outv]', '-vsync', '0', '-f', 'rawvideo', 'pipe:1']
    if delivery == 'early':
        result = subprocess.run(argv, cwd=directory, input=mask_data, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=30)
        code, output, error = result.returncode, result.stdout, result.stderr
    else:
        process = subprocess.Popen(argv, cwd=directory, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, bufsize=0)
        sink = process.stdin
        process.stdin = None  # communicate drains stdout/stderr; dedicated writer owns stdin.
        failures = []
        def writer():
            try:
                time.sleep(.08)  # Let base frames queue before the first mask arrives.
                for start in range(0, len(mask_data), frame_bytes):
                    write_all(sink, mask_data[start:start + frame_bytes])
                    time.sleep(.002)
            except BaseException as exc:
                failures.append(exc)
            finally:
                sink.close()
        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        try:
            output, error = process.communicate(timeout=30)
            code = process.returncode
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            thread.join(timeout=5)
        assert not thread.is_alive() and not failures
    log = error.decode('utf-8', 'replace')
    assert code == 0, log[-6000:]
    frames = [(int(n), int(pts)) for n, pts in re.findall(r'Parsed_showinfo.*?\bn:\s*(\d+)\s+pts:\s*(-?\d+)', log)]
    replies = re.findall(r'Command reply for command #\d+: ret:(.*?) res:', log)
    assert all(reply in ('Success', 'Undefined error: 0') for reply in replies)
    return output, frames, replies


@pytest.mark.integration
@pytest.mark.parametrize('filter_threads,delivery', [(1, 'early'), (1, 'late'), (4, 'early'), (4, 'late')])
@pytest.mark.parametrize('initial_active', [False, True])
@pytest.mark.parametrize('ntsc', [False, True])
def test_real_activity_switch_matches_reference_at_every_frame(tmp_path, filter_threads, delivery, initial_active, ntsc):
    from fractions import Fraction
    from types import SimpleNamespace
    from assglass.ffmpeg import build_graph
    binary = _sendcmd_ffmpeg()
    count, frame_bytes = 24, 64 * 48 * 3 // 2
    time_base, ticks = (Fraction(1, 60000), 1001) if ntsc else (Fraction(1, 90000), 1500)
    rate = 1 / (time_base * ticks)
    plan = SimpleNamespace(spec=SimpleNamespace(time_base=time_base, stream={'index': 0}), ticks_per_frame=ticks)
    states = [int((index + int(initial_active)) % 2) for index in range(count)]
    source = b''.join(bytes((index * 37 + x * 13 + x // 64 * 11) % 219 + 16 for x in range(frame_bytes))
                      for index in range(count))
    # Active masks include gradients, so this checks all plane values rather
    # than only the 0/255 endpoints. Empty frames must be byte-identical to base.
    mask = b''.join(bytes((x * 17 + index * 3) % 256 for x in range(frame_bytes)) if active else bytes(frame_bytes)
                    for index, active in enumerate(states))
    (tmp_path / 'base.raw').write_bytes(source)
    (tmp_path / 'activity.cmd').write_text(_activity_command_text(states, time_base, ticks))
    outputs = []
    for controlled in (False, True):
        graph = build_graph(plan, 3, False, activity_commands='activity.cmd' if controlled else None)
        output, frames, replies = _run_activity_graph(binary, tmp_path, graph, mask, frame_bytes, rate,
                                                       filter_threads, delivery)
        assert len(output) == count * frame_bytes
        assert frames == [(index, index * ticks) for index in range(count)]
        assert len(replies) == (2 * count if controlled else 0)
        outputs.append(output)
    assert outputs[0] == outputs[1]
    for index, active in enumerate(states):
        start, end = index * frame_bytes, (index + 1) * frame_bytes
        if not active:
            assert outputs[1][start:end] == source[start:end]
        else:
            assert outputs[1][start:end] != source[start:end]


@pytest.mark.integration
@pytest.mark.parametrize('active', [False, True])
def test_real_activity_constant_state_and_zero_masks(tmp_path, active):
    from fractions import Fraction
    from types import SimpleNamespace
    from assglass.ffmpeg import build_graph
    binary = _sendcmd_ffmpeg()
    count, frame_bytes = 12, 64 * 48 * 3 // 2
    plan = SimpleNamespace(spec=SimpleNamespace(time_base=Fraction(1, 60), stream={'index': 0}), ticks_per_frame=1)
    source = bytes((index * 13) % 219 + 16 for index in range(count * frame_bytes))
    (tmp_path / 'base.raw').write_bytes(source)
    (tmp_path / 'activity.cmd').write_text(_activity_command_text([active] * count, Fraction(1, 60), 1))
    graph = build_graph(plan, 3, False, activity_commands='activity.cmd')
    output, frames, replies = _run_activity_graph(binary, tmp_path, graph, bytes(count * frame_bytes),
                                                  frame_bytes, 60, 4, 'late')
    assert output == source  # Conservative active=true still preserves a zero mask exactly.
    assert frames == [(index, index) for index in range(count)]
    assert len(replies) == 2


@pytest.mark.integration
def test_real_sendcmd_wrong_target_cannot_publish_silent_unblurred_success(tmp_path):
    from fractions import Fraction
    from types import SimpleNamespace
    from assglass.ffmpeg import build_graph
    binary = _sendcmd_ffmpeg()
    size = 64 * 48 * 3 // 2
    (tmp_path / 'base.raw').write_bytes(bytes((index * 13) % 219 + 16 for index in range(size)))
    (tmp_path / 'activity.cmd').write_text('0-1 [enter] gblur@missing_target enable 1;\n')
    plan = SimpleNamespace(spec=SimpleNamespace(time_base=Fraction(1, 60), stream={'index': 0}), ticks_per_frame=1)
    graph = build_graph(plan, 3, False, activity_commands='activity.cmd')
    argv = [binary, '-hide_banner', '-nostdin', '-loglevel', 'verbose', '-filter_complex_threads', '1',
            '-f', 'rawvideo', '-pixel_format', 'yuv420p', '-video_size', '64x48', '-framerate', '60',
            '-i', 'base.raw', '-f', 'rawvideo', '-pixel_format', 'yuv420p', '-video_size', '64x48',
            '-framerate', '60', '-i', 'pipe:0', '-filter_complex', graph, '-map', '[outv]', '-f', 'null', '-']
    pipeline = PipePipeline(argv, tmp_path, tmp_path / 'ffmpeg.log', 1, 1)
    try:
        pipeline.write_frame(bytes([255]) * size, size)
        with pytest.raises(ValueError, match='活动区间命令失败'):
            pipeline.finish()
        assert pipeline.process.returncode == 0  # sendcmd failures alone do not fail FFmpeg.
    finally:
        pipeline.close()
