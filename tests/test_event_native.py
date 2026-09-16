"""Ownership/error boundaries of the real optional EventImages native ABI."""
import gc

import pytest

from assglass.native import (NativeBudget, NativeEventSession, NativeImages,
                             NativeSession, event_export_available, libass_info)
from test_ass import HEADER


pytestmark = pytest.mark.skipif(not event_export_available(), reason='optional EventImages libass is not installed')


def drawing(x=40, width=80, height=30):
    return r'{\an7\pos(%d,80)\bord0\shad0\p1}m 0 0 l %d 0 %d %d 0 %d' % (x, width, width, height, height)


def row(text, start='0:00:00.00', end='0:00:01.00', actor='bgblur', layer=0):
    return 'Dialogue: %d,%s,%s,Default,%s,0,0,0,,%s\n' % (layer, start, end, actor, text)


def source(*rows):
    return (HEADER + ''.join(rows)).encode()


def test_callback_copies_survive_later_frames_and_session_destruction():
    budget = NativeBudget(1024 * 1024)
    data = source(row(drawing()), row(drawing(400), start='0:00:01.00', end='0:00:02.00'))
    session = NativeEventSession(data, 640, 360, budget=budget, selected_indices=(0, 1))
    with session.render(0) as frame:
        first = frame.take(0)
        empty = frame.take(1)
        assert len(empty) == 0
        empty.release()
        with pytest.raises(RuntimeError, match='already transferred'):
            frame.take(0)
    digest = first.digest
    with session.render(1100) as frame:
        absent, next_images = frame.take(0), frame.take(1)
        assert len(absent) == 0
        assert next_images.source_stats(opacity_threshold=.5)[0] == (400, 80, 480, 110)
        absent.release()
        next_images.release()
    session.close()
    assert first.digest == digest
    view = first[0].coverage
    first.release()
    assert max(view) == 255
    del view
    gc.collect()
    assert budget.used == 0


def test_budget_error_is_reported_after_render_and_does_not_poison_next_frame():
    budget = NativeBudget(1024)
    session = NativeEventSession(source(row(drawing(width=300, height=200))), 640, 360,
                                 budget=budget, selected_indices=(0,))
    with pytest.raises(RuntimeError, match='max_in_flight_bytes'):
        session.render(0)
    with session.render(1100) as frame, frame.take(0) as empty:
        assert len(empty) == 0
    session.close()
    assert budget.used == 0


def test_export_combination_matches_the_unmodified_public_chain():
    data = source(row(drawing()), row(drawing(400), layer=2))
    with NativeEventSession(data, 640, 360, selected_indices=(0, 1)) as exported, NativeSession(data, 640, 360) as public:
        for timestamp in (0, 10, 500, 1000, 1010):
            with exported.render(timestamp) as frame, public.render(timestamp) as ordinary:
                with frame.take(0) as left, frame.take(1) as right:
                    with NativeImages.combine((left, right)) as combined:
                        assert combined.digest == ordinary.digest
                        assert frame.changed == ordinary.changed
    assert libass_info()['event_export_abi'] == 1


def test_metadata_has_dialogue_order_including_zero_duration_and_comments():
    data = source('Comment: 9,0:00:00.00,0:00:01.00,Default,ignored,0,0,0,,comment\n',
                  row(drawing(), end='0:00:00.00', actor='zero', layer=8),
                  'Format: Start, End, Layer, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n',
                  'Dialogue: 0:00:00.10,0:00:00.50,2,Default,bgblurSpeaker,1,2,3,fx,text,commas\n')
    with NativeEventSession(data, 640, 360, selected_indices=(1, 0)) as session:
        metadata = session.event_metadata
        assert len(metadata) == 2
        assert metadata[0]['name'] == 'zero'
        assert metadata[0]['duration_ms'] == 0
        assert metadata[1] == dict(start_ms=100, duration_ms=400, layer=2, style='Default',
                                   name='bgblurSpeaker', margin_l=1, margin_r=2, margin_v=3,
                                   effect='fx', text='text,commas')
        with session.render(100) as frame, frame.take(0) as zero:
            assert len(zero) == 0


@pytest.mark.parametrize('indices', [(-1,), (2,), (0, 0)])
def test_invalid_selection_closes_partially_created_session(indices):
    budget = NativeBudget(1024)
    with pytest.raises((ValueError, RuntimeError)):
        NativeEventSession(source(row(drawing())), 640, 360, budget=budget, selected_indices=indices)
    gc.collect()
    assert budget.used == 0
