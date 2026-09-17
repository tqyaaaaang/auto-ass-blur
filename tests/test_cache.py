"""Correctness and ownership of last-input native subtitle-mask reuse."""
from dataclasses import replace
from fractions import Fraction

import pytest

from assglass.cache import MaskCache
from assglass.contracts import FrameRequest, FrameSelection, ImageGroup, MaskContext, ResolvedMaskConfig
from assglass.masks import create_builder
from assglass.native import NativeBudget, NativeImages, NativeMask


SIZE = (32, 24)
CFG = replace(ResolvedMaskConfig(), padding_x=1, padding_y=1,
              corner_radius=1, feather_sigma=0, opacity_threshold=.5)


def image(budget, x=5, y=7, pixels=(255, 192, 128, 0), color=0xFFFFFF00, kind='character'):
    return NativeImages.empty(budget).append(x, y, 2, 2, pixels, color=color, image_type=kind)


def group(images, name='first', event='event-a', cfg=CFG, keys=None):
    return ImageGroup(name, event, keys or (event,), cfg, images)


def selection(groups, index=0, size=SIZE, backend='event-images', epoch=0, digest='selection'):
    return FrameSelection(FrameRequest(index, index, Fraction(1, 60), size),
                          index * 1000 // 60, tuple(groups), 0, backend, epoch,
                          selection_digest=digest)


def cached(cache, selected, item=None, profile='profile-a', builder=None):
    cache.begin_frame(selected, profile)
    item = item or selected.groups[0]
    return cache.build(item, item.effect_config, MaskContext(selected.frame.frame_size, cache.budget),
                       builder or create_builder(item.effect_config.mode))


def snapshot(mask):
    return mask.roi, bytes(mask.weights) if not mask.empty else b''


@pytest.mark.parametrize('change', ['x', 'y', 'shape', 'pixels', 'color', 'type', 'count'])
def test_native_equality_compares_every_descriptor_and_pixel(change):
    budget = NativeBudget()
    with image(budget) as first, image(budget) as second:
        assert first.equals(second)
        assert first.allocation_bytes > 4
        with first.retain() as alias:
            assert first.equals(alias)
            assert alias.allocation_bytes == first.allocation_bytes
        second.release()
        kwargs = {}
        if change == 'x':
            kwargs['x'] = 6
        elif change == 'y':
            kwargs['y'] = 8
        elif change == 'pixels':
            kwargs['pixels'] = (255, 191, 128, 0)
        elif change == 'color':
            kwargs['color'] = 0xFFFFFE00
        elif change == 'type':
            kwargs['kind'] = 'outline'
        second = image(budget, **kwargs)
        try:
            if change == 'shape':
                second.release()
                second = NativeImages.empty(budget).append(5, 7, 4, 1, (255, 192, 128, 0))
            elif change == 'count':
                second.append(9, 7, 1, 1, [255])
            assert not first.equals(second)
        finally:
            second.release()
    assert budget.used == 0


def test_static_group_reuses_mask_and_returned_owners_survive_eviction():
    budget = NativeBudget()
    with MaskCache(4096, budget) as cache:
        with selection([group(image(budget))]) as first:
            result = cached(cache, first)
            expected = snapshot(result)
            result.release()
        held_bytes = budget.used
        assert held_bytes == cache.bytes_used > 0
        with selection([group(image(budget))], index=1) as second:
            reused = cached(cache, second)
            assert snapshot(reused) == expected
            assert cache.metrics['hits'] == 1
            assert cache.metrics['builds'] == 1
            cache.evict_all()
            assert snapshot(reused) == expected
            reused.release()
    assert budget.used == 0


def test_other_active_group_changes_do_not_invalidate_static_group():
    budget = NativeBudget()
    with MaskCache(4096, budget) as cache:
        for index, two in enumerate([False, True, False]):
            items = [group(image(budget))]
            if two:
                items.append(group(image(budget, x=15), name='second', event='event-b'))
            with selection(items, index=index, digest='different-%s' % index) as selected:
                cache.begin_frame(selected, 'profile-a')
                for item in items:
                    mask = cache.build(item, item.effect_config, MaskContext(SIZE, budget), create_builder('box'))
                    mask.release()
        assert cache.metrics['builds'] == 2
        assert cache.metrics['hits'] == 2
        assert cache.metrics['entries'] == 1
        cache.begin_frame(selection([], index=3), 'profile-a')
        assert cache.metrics['entries'] == 0
        assert budget.used == 0


@pytest.mark.parametrize('changed', ['event', 'keys', 'cfg', 'size', 'backend', 'profile', 'epoch'])
def test_identity_changes_invalidate_even_when_images_match(changed):
    budget = NativeBudget()
    with MaskCache(4096, budget) as cache:
        with selection([group(image(budget))]) as selected:
            cached(cache, selected).release()
        item = group(image(budget))
        args, profile = dict(index=1), 'profile-a'
        if changed == 'event':
            item = replace(item, event_key='event-b')
        elif changed == 'keys':
            item = replace(item, target_event_keys=('event-a', 'event-b'))
        elif changed == 'cfg':
            item = replace(item, effect_config=replace(CFG, padding_x=2))
        elif changed == 'size':
            args['size'] = (34, 24)
        elif changed == 'backend':
            args['backend'] = 'alpha'
        elif changed == 'profile':
            profile = 'profile-b'
        elif changed == 'epoch':
            args['epoch'] = 1
        with selection([item], **args) as selected:
            cached(cache, selected, profile=profile).release()
        assert cache.metrics['hits'] == 0
        assert cache.metrics['builds'] == 2
    assert budget.used == 0


def test_digest_collision_still_requires_exact_pixel_comparison(monkeypatch):
    monkeypatch.setattr(NativeImages, 'digest', property(lambda self: 'forced-collision'))
    budget = NativeBudget()
    with MaskCache(4096, budget) as cache:
        snapshots = []
        for index, x in enumerate([5, 8, 8]):
            with selection([group(image(budget, x=x))], index=index) as selected:
                mask = cached(cache, selected)
                snapshots.append(snapshot(mask))
                mask.release()
        assert snapshots[0] != snapshots[1] == snapshots[2]
        assert cache.metrics['hits'] == 1
        assert cache.metrics['builds'] == 2
    assert budget.used == 0


@pytest.mark.parametrize('limit', [0, 1])
def test_disabled_or_too_small_cache_preserves_output_and_budget(limit):
    budget = NativeBudget()
    with MaskCache(limit, budget) as cache:
        for index in range(2):
            with selection([group(image(budget))], index=index) as selected:
                mask = cached(cache, selected)
                reference = create_builder('box').build(selected.groups[0], CFG, MaskContext(SIZE, budget))
                assert snapshot(mask) == snapshot(reference)
                mask.release()
                reference.release()
            assert budget.used == 0
        assert cache.metrics['hits'] == 0
        assert cache.metrics['entries'] == 0


def test_budget_pressure_reclaims_optional_storage_and_retries_allocation():
    budget = NativeBudget(1200)
    with MaskCache(1000, budget) as cache:
        with selection([group(image(budget))]) as selected:
            cached(cache, selected).release()
        assert 0 < budget.used < 1200
        # The new allocation fits alone but not while the old cache is retained.
        count = budget.limit // 4
        result = cache.run_with_reclaim(lambda: NativeMask.from_values((0, 0, count, 1), [0] * count, budget))
        assert result.allocation_bytes == budget.limit
        assert cache.metrics['budget_retries'] == 1
        assert cache.bytes_used == 0
        assert budget.peak <= budget.limit
        result.release()
    assert budget.used == 0


def test_budget_failure_without_cache_and_other_errors_are_not_retried():
    budget = NativeBudget(128)
    calls = []
    with MaskCache(128, budget) as cache:
        def operation():
            calls.append(1)
            return NativeMask.from_values((0, 0, 100, 1), [0] * 100, budget)
        with pytest.raises(RuntimeError, match='max_in_flight_bytes'):
            cache.run_with_reclaim(operation)
        assert len(calls) == 1
        with selection([group(image(budget), cfg=replace(CFG, padding_x=0, padding_y=0))]) as selected:
            cached(cache, selected).release()
        def unrelated():
            calls.append(1)
            raise RuntimeError('unrelated failure')
        with pytest.raises(RuntimeError, match='unrelated failure'):
            cache.run_with_reclaim(unrelated)
        assert len(calls) == 2
        assert cache.metrics['entries'] == 1
    assert budget.used == 0


def test_mask_build_reclaims_another_groups_old_result_under_pressure():
    budget = NativeBudget(4200)
    small = replace(CFG, padding_x=5, padding_y=5, corner_radius=0)
    large = replace(CFG, padding_x=10, padding_y=10, corner_radius=0)
    with MaskCache(4000, budget) as cache:
        with selection([group(image(budget), cfg=small)]) as selected:
            cached(cache, selected).release()
        items = [group(image(budget, x=10, y=10), name='second', event='b', cfg=large),
                 group(image(budget), cfg=small)]
        with selection(items, index=1) as selected:
            cache.begin_frame(selected, 'profile-a')
            masks = []
            try:
                for item in items:
                    masks.append(cache.build(item, item.effect_config, MaskContext(SIZE, budget), create_builder('box')))
                assert [mask.roi for mask in masks] == [(0, 0, 22, 22), (0, 2, 12, 14)]
                assert cache.metrics['budget_retries'] == 1
                assert budget.peak <= budget.limit
            finally:
                for mask in masks:
                    mask.release()
    assert budget.used == 0


@pytest.mark.parametrize('mode', ['box', 'organic'])
def test_dynamic_and_static_frames_match_uncached_pixels_exactly(mode):
    budget = NativeBudget()
    cfg = replace(CFG, mode=mode, expand_x=2, expand_y=1, close=2, feather_sigma=1)
    with MaskCache(32000, budget) as cache:
        for index, (x, opacity) in enumerate([(5, 0), (5, 0), (7, 0), (7, 128), (7, 0), (7, 0)]):
            with selection([group(image(budget, x=x, color=0xFFFFFF00 | opacity), cfg=cfg)], index=index) as selected:
                builder = create_builder(mode)
                mask = cached(cache, selected, builder=builder)
                reference = builder.build(selected.groups[0], cfg, MaskContext(SIZE, budget))
                assert snapshot(mask) == snapshot(reference)
                mask.release()
                reference.release()
        assert cache.metrics['hits'] == 2
        assert cache.metrics['builds'] == 4
    assert budget.used == 0


def test_empty_thresholded_result_can_be_reused_without_pixel_allocation():
    budget = NativeBudget()
    with MaskCache(4096, budget) as cache:
        for index in range(2):
            with selection([group(image(budget, pixels=(100, 100, 100, 100)))], index=index) as selected:
                mask = cached(cache, selected)
                assert mask.empty
                assert mask.data.allocation_bytes == 0
                mask.release()
        assert cache.metrics['hits'] == 1
    assert budget.used == 0


def test_cache_limit_and_shared_budget_are_validated():
    budget = NativeBudget(4096)
    for limit in (-1, True, 4097):
        with pytest.raises(ValueError, match='mask_cache_bytes'):
            MaskCache(limit, budget)
    with MaskCache(4096, budget) as cache, selection([group(image(budget))]) as selected:
        item = selected.groups[0]
        with pytest.raises(ValueError, match='begin_frame'):
            cache.build(item, CFG, MaskContext(SIZE, budget), create_builder('box'))
        cache.begin_frame(selected, 'profile')
        with pytest.raises(ValueError, match='share one NativeBudget'):
            cache.build(item, CFG, MaskContext(SIZE, NativeBudget()), create_builder('box'))


def assert_render_budget_recovery(backend_name):
    """Shared actual-libass fixture for the two backend regression suites."""
    from types import SimpleNamespace
    from assglass.ass import SourceDocument
    from assglass.config import resolve_config
    from assglass.selection import SelectionError, build_selection_plan, create_backend
    from test_ass import HEADER

    size = (640, 360)
    drawing = r'{\an7\pos(0,0)\bord0\shad0\p1}m 0 0 l 640 0 640 360 0 360'
    rows = ['Dialogue: 0,0:00:00.00,0:00:00.02,Default,bgblur,0,0,0,,' + drawing + '\n']
    rows += ['Dialogue: %d,0:00:00.02,0:00:00.10,Default,bgblur,0,0,0,,' % layer
             + drawing + '\n' for layer in range(5)]
    source = SourceDocument.from_bytes((HEADER + ''.join(rows)).encode())
    selection_values = {'backend': backend_name, 'grouping': 'per-event'}
    if backend_name == 'alpha':
        selection_values.update(grouping='merged', allow_merged_box=True)
    cfg = resolve_config({'selection': selection_values,
                          'defaults': {'padding_x': 0, 'padding_y': 0, 'corner_radius': 0, 'feather_sigma': 0}})
    budget = NativeBudget(2100000)
    profile = SimpleNamespace(frame_size=size, fonts_dir=None, native_budget=budget, profile_id='reclaim-test')
    backend = create_backend(backend_name)
    prepared = backend.preflight(source, build_selection_plan(source, cfg), profile)

    def request(index):
        return FrameRequest(index, index, Fraction(1, 50), size)

    # The first full-frame mask fits, as do the five next-frame image planes.
    # Keeping the former cached while allocating the latter does not fit.
    observed = []
    with MaskCache(1800000, budget) as cache, backend.open(prepared) as session:
        with session.render(request(0)) as selected:
            cache.begin_frame(selected, profile.profile_id)
            item = selected.groups[0]
            cache.build(item, item.effect_config, MaskContext(size, budget), create_builder('box')).release()
        assert cache.bytes_used > 1000000
        for index in (1, 2):
            with cache.run_with_reclaim(lambda: session.render(request(index))) as selected:
                observed.append([(item.target_event_keys, item.images.digest) for item in selected.groups])
        assert cache.metrics['budget_retries'] == 1
        assert cache.metrics['entries'] == 0
        assert budget.peak <= budget.limit
        with pytest.raises(SelectionError, match='consecutive'):
            session.render(request(2))
    assert budget.used == 0
    # A clean sequential run must export identical descriptors and coverage.
    with backend.open(prepared) as session:
        for index in range(3):
            with session.render(request(index)) as selected:
                if index:
                    assert [(item.target_event_keys, item.images.digest) for item in selected.groups] == observed[index - 1]
    assert budget.used == 0
