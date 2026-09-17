"""Exact final-mask reuse must preserve transport bytes and owner lifetimes."""
from fractions import Fraction
from types import SimpleNamespace

import pytest

from assglass.contracts import FrameRequest, MaskContext, RasterMask
from assglass.masks import merge_masks
from assglass.native import NativeBudget, NativeMask
from assglass.weights import WeightFrame, YUV420PLeftWeightEncoder


def plan(size=(4, 4), **extra):
    return SimpleNamespace(frame_size=size, pix_fmt='yuv420p',
                           sampler_id='left-tent2-v1', profile_id='test-v1', **extra)


def frame(index=0, size=(4, 4)):
    return FrameRequest(index, index * 1001, Fraction(1, 60000), size)


def test_exact_native_comparison_uses_roi_and_float_bits():
    budget = NativeBudget(1024)
    first = NativeMask.from_values((1, 1, 3, 2), [.5, 0], budget)
    same = NativeMask.from_values((1, 1, 3, 2), [.5, 0], budget)
    moved = NativeMask.from_values((0, 1, 2, 2), [.5, 0], budget)
    changed = NativeMask.from_values((1, 1, 3, 2), [.50001, 0], budget)
    signed_zero = NativeMask.from_values((1, 1, 3, 2), [.5, -0.0], budget)
    retained = first.retain()
    try:
        assert first.equals(first) and first.equals(retained) and first.equals(same)
        assert not first.equals(moved) and not first.equals(changed)
        assert not first.equals(signed_zero) and not first.equals(None)
    finally:
        for mask in (first, same, moved, changed, signed_zero, retained):
            mask.release()
    assert budget.used == 0


def test_rebuilt_merged_mask_reuses_weights_with_independent_frame_metadata():
    budget = NativeBudget(1024)
    encoder = YUV420PLeftWeightEncoder(budget, limit_bytes=128)
    processing = plan()
    context = MaskContext((4, 4), budget)
    masks = [RasterMask((0, 0, 1, 1), [1]), RasterMask((1, 0, 2, 1), [.5])]
    first_mask = merge_masks(masks, context)
    first = encoder.encode(first_mask, frame(0), processing)
    first_mask.release()
    second_mask = merge_masks(list(reversed(masks)), context)
    second = encoder.encode(second_mask, frame(7), processing)
    second_mask.release()
    assert first.owner is not second.owner
    assert first.content_token is second.content_token
    assert bytes(first.buffer) == bytes(second.buffer)
    assert (first.frame_index, first.pts, first.time_base) == (0, 0, Fraction(1, 60000))
    assert (second.frame_index, second.pts, second.time_base) == (7, 7007, Fraction(1, 60000))
    assert encoder.metrics['hits'] == 1 and encoder.metrics['encodes'] == 1
    assert encoder.metrics['bytes_used'] == 8 + 24
    assert budget.used == 8 + 24
    first.release()
    encoder.close()
    encoder.close()
    assert budget.used == 24
    assert second.buffer.readonly
    assert bytes(second.buffer)[:2] == b'\xff\x80'
    second.release()
    assert budget.used == 0


def test_changed_mask_roi_profile_plan_and_dimensions_invalidate_cache():
    budget = NativeBudget(4096)
    encoder = YUV420PLeftWeightEncoder(budget)
    processing = plan()
    tokens = []
    sources = [RasterMask((0, 0, 1, 1), [.5]),
               RasterMask((0, 0, 1, 1), [.50001]),
               RasterMask((1, 0, 2, 1), [.50001])]
    for index, source in enumerate(sources):
        result = encoder.encode(source, frame(index), processing)
        tokens.append(result.content_token)
        result.release()
    processing.profile_id = 'test-v2'
    result = encoder.encode(sources[-1], frame(3), processing)
    tokens.append(result.content_token)
    result.release()
    processing = plan()
    result = encoder.encode(sources[-1], frame(4), processing)
    tokens.append(result.content_token)
    result.release()
    processing.frame_size = (8, 6)
    result = encoder.encode(sources[-1], frame(5, (8, 6)), processing)
    tokens.append(result.content_token)
    result.release()
    encoder.quantizer_id = 'new-quantizer-version'
    result = encoder.encode(sources[-1], frame(6, (8, 6)), processing)
    tokens.append(result.content_token)
    result.release()
    assert len({id(token) for token in tokens}) == len(tokens)
    assert encoder.metrics['hits'] == 0
    encoder.close()
    assert budget.used == 0


def test_mutable_imported_source_is_snapshotted_before_comparison():
    budget = NativeBudget(1024)
    encoder = YUV420PLeftWeightEncoder(budget)
    processing = plan()
    values = [1]
    source = RasterMask((0, 0, 1, 1), values)
    first = encoder.encode(source, frame(0), processing)
    same = encoder.encode(source, frame(1), processing)
    assert first.content_token is same.content_token
    values[0] = .25
    changed = encoder.encode(source, frame(2), processing)
    assert changed.content_token is not first.content_token
    assert bytes(first.buffer)[0] == 255
    assert bytes(changed.buffer)[0] == 64
    for result in (first, same, changed):
        result.release()
    encoder.close()
    assert budget.used == 0


@pytest.mark.parametrize('limit', [0, 27])
def test_disabled_or_too_small_cache_does_not_retain_active_weights(limit):
    budget = NativeBudget(1024)
    encoder = YUV420PLeftWeightEncoder(budget, limit_bytes=limit)
    processing = plan()
    tokens = []
    for index in range(3):
        result = encoder.encode(RasterMask((0, 0, 1, 1), [1]), frame(index), processing)
        tokens.append(result.content_token)
        assert encoder.metrics['bytes_used'] == 0
        result.release()
        assert budget.used == 0
    assert encoder.metrics['hits'] == 0 and encoder.metrics['encodes'] == 3
    assert encoder.metrics['uncacheable'] == 3
    assert len({id(token) for token in tokens}) == 3
    encoder.close()


def test_zero_limit_also_disables_empty_cache_and_external_frames_have_no_token():
    budget = NativeBudget(1024)
    encoder = YUV420PLeftWeightEncoder(budget, limit_bytes=0)
    processing = plan()
    first = encoder.encode(RasterMask(), frame(0), processing)
    first_token = first.content_token
    first.release()
    assert budget.used == 0
    second = encoder.encode(RasterMask(), frame(1), processing)
    assert second.content_token is not first_token
    second.release()
    assert budget.used == 0
    assert encoder.empty_frames == encoder.empty_allocations == 2
    assert encoder.metrics['entries'] == 0
    a = WeightFrame(bytes(24), (), 0, 0, Fraction(1, 60), 'test')
    b = WeightFrame(a.owner, (), 1, 1, Fraction(1, 60), 'test')
    assert a.content_token is b.content_token is None


def test_empty_active_empty_retains_only_consecutive_content():
    budget = NativeBudget(1024)
    encoder = YUV420PLeftWeightEncoder(budget)
    processing = plan()
    sources = [RasterMask(), RasterMask(), RasterMask((0, 0, 1, 1), [1]), RasterMask()]
    tokens = []
    for index, source in enumerate(sources):
        result = encoder.encode(source, frame(index), processing)
        tokens.append(result.content_token)
        result.release()
    assert tokens[0] is tokens[1] and tokens[0] is not tokens[3]
    assert encoder.empty_frames == 3 and encoder.empty_allocations == 2
    assert encoder.metrics['hits'] == 1 and encoder.metrics['encodes'] == 3
    encoder.close()
    assert budget.used == 0


def test_import_oom_reclaims_previous_cache_and_retries_without_leak():
    # First cache uses 16 mask + 24 weights bytes. Importing the following
    # 64-byte mask cannot fit until it is evicted; final new content needs 88.
    budget = NativeBudget(100)
    encoder = YUV420PLeftWeightEncoder(budget, limit_bytes=100)
    processing = plan()
    first = encoder.encode(RasterMask((0, 0, 2, 2), [1] * 4), frame(0), processing)
    first.release()
    assert budget.used == 40
    second = encoder.encode(RasterMask((0, 0, 4, 4), [.5] * 16), frame(1), processing)
    assert bytes(second.buffer) == bytes([128] * 24)
    assert encoder.metrics['budget_retries'] == 1
    assert encoder.metrics['bytes_used'] == budget.used == 88
    assert budget.peak <= budget.limit
    second.release()
    encoder.close()
    assert budget.used == 0


def test_global_reclaim_and_unrecoverable_oom_do_not_leak():
    budget = NativeBudget(100)
    encoder = YUV420PLeftWeightEncoder(budget)
    processing = plan()
    result = encoder.encode(RasterMask(), frame(0), processing)
    result.release()
    imported = encoder.run_with_reclaim(lambda: NativeMask.from_values((0, 0, 5, 4), [1] * 20, budget))
    assert encoder.metrics['budget_retries'] == 1
    imported.release()
    with pytest.raises(RuntimeError, match='max_in_flight_bytes'):
        encoder.encode(RasterMask((0, 0, 5, 5), [1] * 25), frame(1), processing)
    encoder.close()
    assert budget.used == 0


def test_foreign_budget_mask_is_not_retained_by_cache():
    budget, foreign = NativeBudget(1024), NativeBudget(1024)
    encoder = YUV420PLeftWeightEncoder(budget)
    native = NativeMask.from_values((0, 0, 1, 1), [1], foreign)
    result = encoder.encode(RasterMask(native.roi, native), frame(), plan())
    assert encoder.metrics['uncacheable'] == 1 and encoder.metrics['bytes_used'] == 0
    assert budget.used == 24 and foreign.used == 4
    result.release()
    native.release()
    encoder.close()
    assert budget.used == foreign.used == 0


@pytest.mark.parametrize('limit', [-1, True, 1.5, 1025])
def test_invalid_cache_limits(limit):
    with pytest.raises(ValueError, match='weight_cache_bytes'):
        YUV420PLeftWeightEncoder(NativeBudget(1024), limit_bytes=limit)


def test_invalid_profile_does_not_return_cached_weights():
    budget = NativeBudget(1024)
    encoder = YUV420PLeftWeightEncoder(budget)
    processing = plan()
    result = encoder.encode(RasterMask(), frame(0), processing)
    result.release()
    processing.sampler_id = 'wrong-phase'
    with pytest.raises(ValueError, match='processing profile'):
        encoder.encode(RasterMask(), frame(1), processing)
    encoder.close()
    assert budget.used == 0
