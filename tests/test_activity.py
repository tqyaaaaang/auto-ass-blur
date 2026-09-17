"""Safe activity detection, exact timing, and streaming ownership regressions."""
from dataclasses import replace
from fractions import Fraction
import hashlib
from types import SimpleNamespace

import pytest

from assglass.activity import group_may_need_blur, scan_activity
from assglass.contracts import FrameRequest, FrameSelection, ImageGroup, ResolvedMaskConfig
from assglass.native import NativeImages


SIZE = (100, 100)
BASE = replace(ResolvedMaskConfig(), padding_x=0, padding_y=0, feather_sigma=0,
               expand_x=0, expand_y=0, close=0)


def group(images, **changes):
    return ImageGroup('test', None, ('event',), replace(BASE, **changes), images)


@pytest.mark.parametrize('coverage,alpha,threshold,expected', [
    (255, 0, .5, True), (0, 0, 0, False), (255, 255, 0, False),
    (127, 0, .5, False), (128, 0, .5, True),
    (128, 0, 128 / 255, False), (200, 100, .5, False),
])
def test_native_threshold_and_opacity(coverage, alpha, threshold, expected):
    with NativeImages.empty() as images:
        images.append(1, 1, 1, 1, [coverage], color=0xFFFFFF00 | alpha)
        assert group_may_need_blur(group(images, opacity_threshold=threshold), SIZE) is expected


def test_empty_images_and_excluded_types():
    with NativeImages.empty() as images:
        assert not group_may_need_blur(group(images), SIZE)
        images.append(1, 1, 1, 1, [255], image_type='shadow')
        assert not group_may_need_blur(group(images), SIZE)
        assert group_may_need_blur(group(images, include_types=('shadow',)), SIZE)
    assert not group_may_need_blur(group(None), SIZE)


def test_strength_zero_short_circuits_statistics():
    def unexpected(*_):
        pytest.fail('strength=0 must not inspect source images')
    assert not group_may_need_blur(group(SimpleNamespace(source_stats=unexpected), strength=0), SIZE)


@pytest.mark.parametrize('mode,changes,x,y,expected', [
    ('box', {}, -1, 10, False), ('box', {}, 100, 10, False),
    ('box', {}, 10, -1, False), ('box', {}, 10, 100, False),
    ('box', {'padding_x': 2}, -2, 10, True),
    ('box', {'padding_y': 2}, 10, -2, True),
    ('box', {'feather_sigma': 1 / 3}, -2, 10, True),
    ('organic', {'expand_x': 2}, -2, 10, True),
    ('organic', {'expand_y': 2}, 10, -2, True),
    ('organic', {'close': 2}, -4, 10, True),
    ('organic', {'close': 2}, -5, 10, False),
    ('organic', {'feather_sigma': 1}, -3, 10, True),
    ('organic', {'feather_sigma': 1}, -4, 10, False),
])
def test_frame_intersection_uses_operator_halo(mode, changes, x, y, expected):
    with NativeImages.empty() as images:
        images.append(x, y, 1, 1, [255])
        assert group_may_need_blur(group(images, mode=mode, **changes), SIZE) is expected


def test_unknown_mode_and_non_native_images_are_conservative():
    assert group_may_need_blur(group(object()), SIZE)
    images = SimpleNamespace(source_stats=lambda *_: (None, 0))
    assert group_may_need_blur(group(images, mode='extension'), SIZE)


class SyntheticBackend:
    """A history-sensitive session which refuses retained frame resources."""
    def __init__(self, active, fail_at=None):
        self.active, self.fail_at = active, fail_at
        self.rendered = self.live = self.max_live = self.released = 0
        self.closed = False

    def open(self, prepared):
        assert prepared == 'prepared'
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def render(self, frame):
        assert frame.frame_index == self.rendered
        assert self.live == 0, 'previous frame image retained by activity scan'
        if self.fail_at == frame.frame_index:
            raise RuntimeError('render failed')
        self.rendered += 1
        self.live += 1
        self.max_live = max(self.max_live, self.live)
        bbox = (10, 10, 11, 11) if self.active(frame.frame_index) else None

        def release():
            self.live -= 1
            self.released += 1
        images = SimpleNamespace(source_stats=lambda *_: (bbox, int(bbox is not None)), release=release)
        return FrameSelection(frame, 0, (group(images),))


def ledger(count, step=Fraction(1, 60)):
    def frames():
        for index in range(count):
            yield FrameRequest(index, index, step, SIZE)
    return SimpleNamespace(count=count, frames=frames)


def run_scan(tmp_path, backend, frame_ledger, progress=None):
    commands, flags = tmp_path / 'activity.cmd', tmp_path / 'activity.flags'
    metrics = scan_activity(backend, 'prepared', frame_ledger, commands, flags, progress)
    return metrics, commands.read_text(), flags.read_bytes()


@pytest.mark.parametrize('step', [Fraction(1, 60), Fraction(1001, 60000)])
def test_scan_preserves_history_and_uses_half_frame_transitions(tmp_path, step):
    backend = SyntheticBackend(lambda index: index in (1, 2, 4))
    progress = []
    metrics, commands, flags = run_scan(tmp_path, backend, ledger(5, step), progress.append)
    assert flags == b'\0\1\1\0\1'
    lines = commands.splitlines()
    spans = [line.split()[0] for line in lines]
    intervals = [tuple(Fraction(value) for value in span.split('-')) for span in spans]
    assert len(lines) == 4
    assert intervals[0][0] == 0
    for index, (start, end) in enumerate(intervals):
        assert start < end
        if index:
            assert start == intervals[index - 1][1]
    for index in range(5):
        stamp = index * step
        line = next(line for line, (start, end) in zip(lines, intervals) if start <= stamp < end)
        expected = int(index in (1, 2, 4))
        assert 'gblur@assglass_blur enable %d,' % expected in line
        assert 'maskedmerge@assglass_merge enable %d;' % expected in line
    if step == Fraction(1001, 60000):
        assert spans == ['0.000000-0.008342', '0.008342-0.041708',
                         '0.041708-0.058392', '0.058392-0.075075']
    assert metrics['frames'] == 5
    assert metrics['active_frames'] == 3
    assert metrics['inactive_frames'] == 2
    assert metrics['active_intervals'] == 2
    assert metrics['transitions'] == 3
    assert metrics['flags_sha256'] == hashlib.sha256(flags).hexdigest()
    assert metrics['command_sha256'] == hashlib.sha256(commands.encode('ascii')).hexdigest()
    assert progress[-1] == 5
    assert backend.closed and backend.live == 0
    assert backend.rendered == backend.released == 5


@pytest.mark.parametrize('active', [False, True])
def test_frame_zero_and_constant_state_always_receive_initial_command(tmp_path, active):
    metrics, commands, flags = run_scan(tmp_path, SyntheticBackend(lambda _: active), ledger(1))
    assert flags == bytes([int(active)])
    assert len(commands.splitlines()) == 1
    assert commands.startswith('0.000000-0.008333 [enter]')
    assert 'enable %d;' % int(active) in commands
    assert metrics['transitions'] == 0
    assert metrics['active_intervals'] == int(active)


def test_empty_ledger_has_finite_disabled_initial_command(tmp_path):
    metrics, commands, flags = run_scan(tmp_path, SyntheticBackend(lambda _: False), ledger(0))
    assert flags == b'' and metrics['frames'] == 0
    assert commands == ('0.000000-0.008333 [enter] gblur@assglass_blur enable 0, '
                        '[enter] maskedmerge@assglass_merge enable 0;\n')


def test_flags_stream_without_retaining_image_owners(tmp_path):
    count = 10000
    backend = SyntheticBackend(lambda index: index % 1000 == 0)
    metrics, _, flags = run_scan(tmp_path, backend, ledger(count))
    assert len(flags) == count
    assert sum(flags) == metrics['active_frames'] == 10
    assert backend.max_live == 1 and backend.live == 0
    assert backend.rendered == backend.released == count


def test_scan_closes_session_after_render_failure(tmp_path):
    backend = SyntheticBackend(lambda _: True, fail_at=2)
    with pytest.raises(RuntimeError, match='render failed'):
        run_scan(tmp_path, backend, ledger(5))
    assert backend.closed and backend.live == 0 and backend.released == 2


def test_scan_rejects_inconsistent_ledger_and_aliased_outputs(tmp_path):
    bad = ledger(2)
    bad.count = 3
    with pytest.raises(ValueError, match='frame count'):
        run_scan(tmp_path, SyntheticBackend(lambda _: False), bad)
    path = tmp_path / 'both'
    with pytest.raises(ValueError, match='must be different'):
        scan_activity(SyntheticBackend(lambda _: False), 'prepared', ledger(1), path, path)
