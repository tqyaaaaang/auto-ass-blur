"""Independent native numeric references and owned-buffer regression tests."""
from dataclasses import replace
from fractions import Fraction
import gc
import math
import pytest

from assglass.contracts import ImageGroup, MaskContext, RasterMask, ResolvedMaskConfig
from assglass.masks import create_builder, merge_masks, register_builder
from assglass.native import NativeBudget, NativeImages, NativeMask, NativeSession, box_mask, ffmpeg_time_ms
from assglass.weights import encode_yuv420p_left


def config(**kwargs):
    return replace(ResolvedMaskConfig(), padding_x=kwargs.pop('padding_x', 0), padding_y=kwargs.pop('padding_y', 0),
                   corner_radius=kwargs.pop('corner_radius', 0), feather_sigma=kwargs.pop('feather_sigma', 0),
                   strength=kwargs.pop('strength', 1), opacity_threshold=kwargs.pop('opacity_threshold', 0), **kwargs)


def build(images, cfg, size=(12, 10)):
    return create_builder('box').build(ImageGroup('synthetic', None, ('event-1',), cfg, images), cfg,
                                       MaskContext(size, images.budget))


def dense(mask, size):
    width, height = size
    result = [[0.0] * width for _ in range(height)]
    if mask.empty:
        return result
    x0, y0, x1, y1 = mask.roi
    values = mask.weights
    for y in range(y0, y1):
        for x in range(x0, x1):
            result[y][x] = values[(y - y0) * (x1 - x0) + x - x0]
    return result


def reference_box(ink, cfg, size):
    """Direct full padded world-canvas reference, independent of native ROI loops."""
    x0, y0, x1, y1 = ink
    x0 -= cfg.padding_x
    y0 -= cfg.padding_y
    x1 += cfg.padding_x
    y1 += cfg.padding_y
    r = min(cfg.corner_radius, (x1 - x0) / 2, (y1 - y0) / 2)
    L, R, T, B = x0 - .5, x1 - .5, y0 - .5, y1 - .5
    def coverage(x, y):
        count = 0
        for dy in (-.375, -.125, .125, .375):
            for dx in (-.375, -.125, .125, .375):
                sx, sy = x + dx, y + dy
                if not (L <= sx < R and T <= sy < B):
                    continue
                qx, qy = max(L + r - sx, 0, sx - (R - r)), max(T + r - sy, 0, sy - (B - r))
                count += qx * qx + qy * qy <= r * r
        return count / 16
    halo = math.ceil(3 * cfg.feather_sigma)
    kernel = [math.exp(-i * i / (2 * cfg.feather_sigma ** 2)) for i in range(-halo, halo + 1)] if halo else [1]
    norm = sum(kernel)
    kernel = [x / norm for x in kernel]
    return [[sum(coverage(x + dx, y + dy) * kernel[dx + halo] * kernel[dy + halo]
                 for dy in range(-halo, halo + 1) for dx in range(-halo, halo + 1)) * cfg.strength
             for x in range(size[0])] for y in range(size[1])]


def test_copy_rows_with_short_final_stride_and_owned_views():
    budget = NativeBudget(4096)
    images = NativeImages.empty(budget).append(3, 4, 2, 2, [1, 2, 99, 3, 4], stride=3, color=0xABCDEF80)
    plane = images[0]
    assert (plane.stride, plane.color, plane.type) == (2, 0xABCDEF80, 'character')
    assert plane.coverage.readonly
    assert list(plane.coverage) == [1, 2, 3, 4]
    images.release()
    assert list(plane.coverage) == [1, 2, 3, 4]
    assert budget.used > 0
    del plane
    gc.collect()
    assert budget.used == 0


def test_invalid_stride_and_short_final_buffer_rejected():
    images = NativeImages.empty()
    with pytest.raises(RuntimeError, match='stride'):
        images.append(0, 0, 2, 2, [1, 2, 3], stride=1)
    with pytest.raises(RuntimeError, match='final row'):
        images.append(0, 0, 2, 2, [1, 2, 3, 4], stride=3)


def test_nonzero_ink_half_open_types_and_transparency():
    images = NativeImages.empty()
    images.append(2, 3, 4, 3, [0, 0, 0, 0, 0, 1, 2, 0, 0, 0, 0, 0])
    images.append(9, 9, 1, 1, [255], image_type='shadow')
    images.append(0, 0, 1, 1, [255], color=0xFFFFFFff)
    images.append(0, 0, 1, 1, [0])
    assert images.image_count == 2
    assert images.source_stats()[0] == (3, 4, 5, 5)
    assert images.source_stats(('shadow',))[0] == (9, 9, 10, 10)
    assert build(images, config()).roi == (3, 4, 5, 5)


@pytest.mark.parametrize('coverage,opacity', [(255, 255), (255, 128), (255, 0), (1, 255), (1, 1)])
def test_visual_source_product_never_prematurely_quantized(coverage, opacity):
    images = NativeImages.empty().append(1, 1, 1, 1, [coverage], color=0x12345600 | (255 - opacity))
    assert images.source_stats()[1] == pytest.approx(coverage * opacity / 65025)
    native = box_mask(images, config(alpha_policy='follow-visual-alpha'), (4, 4), allow_visual=True)
    values = list(native.weights)
    assert values == pytest.approx([coverage * opacity / 65025] if opacity else [])
    with pytest.raises(ValueError, match='first release'):
        build(images, config(alpha_policy='follow-visual-alpha'), (4, 4))


@pytest.mark.parametrize('coverage,opacity', [(255, 128), (128, 255), (200, 200), (128, 128), (1, 1)])
def test_opacity_threshold_strict_double_precision_boundary(coverage, opacity):
    images = NativeImages.empty().append(2, 3, 1, 1, [coverage], color=0xFFFFFF00 | (255 - opacity))
    boundary = coverage * opacity / 65025.0
    delta = boundary * 1e-10
    # All three values collapse to the same float32. The C ABI must preserve
    # double precision, and the product must never be quantized to an 8-bit mask.
    for threshold, expected in ((boundary - delta, (2, 3, 3, 4)),
                                (boundary, None), (boundary + delta, None)):
        bbox, peak = images.source_stats(opacity_threshold=threshold)
        assert bbox == expected
        assert peak == pytest.approx(boundary)
        mask = box_mask(images, config(opacity_threshold=threshold), (8, 8))
        assert mask.roi == expected
        assert list(mask.weights) == ([1.0] if expected else [])


def test_half_opacity_threshold_trims_faint_outer_ink_without_changing_images():
    pixels = [1] * 25
    pixels[11:14] = [127, 255, 128]
    images = NativeImages.empty().append(3, 2, 5, 5, pixels)
    before_digest = images.digest
    assert images.source_stats()[0] == (3, 2, 8, 7)
    assert images.source_stats(opacity_threshold=.5)[0] == (5, 4, 7, 5)
    cfg = config(opacity_threshold=.5, padding_x=1, padding_y=1, feather_sigma=.6)
    actual = dense(build(images, cfg), (12, 10))
    expected = reference_box((5, 4, 7, 5), cfg, (12, 10))
    for actual_row, expected_row in zip(actual, expected):
        assert actual_row == pytest.approx(expected_row, abs=2e-7)
    assert any(0 < value < 1 for row in actual for value in row)
    assert images.digest == before_digest
    assert bytes(images[0].coverage) == bytes(pixels)


def test_opacity_threshold_includes_color_alpha_and_uses_max_across_layers():
    images = NativeImages.empty()
    images.append(1, 1, 1, 1, [255], color=0xFFFFFF80)  # opacity 127/255
    images.append(1, 1, 1, 1, [255], color=0xFFFFFF80)  # overlapping alpha must not add
    images.append(4, 4, 1, 1, [255], color=0xFFFFFF7F, image_type='outline')
    images.append(8, 8, 1, 1, [255], image_type='shadow')
    assert images.source_stats(('character',), opacity_threshold=.5)[0] is None
    assert images.source_stats(opacity_threshold=.5)[0] == (4, 4, 5, 5)
    assert build(images, config(opacity_threshold=.5)).roi == (4, 4, 5, 5)
    assert build(images, config(opacity_threshold=.5, include_types=('shadow',))).roi == (8, 8, 9, 9)
    assert build(images, config(opacity_threshold=.5, include_types=('character',))).empty


@pytest.mark.parametrize('coverage,threshold', [(127, .5), (255, 1.0)])
def test_threshold_can_produce_empty_mask_even_with_padding_and_feather(coverage, threshold):
    images = NativeImages.empty().append(4, 4, 1, 1, [coverage])
    assert images.source_stats(opacity_threshold=threshold)[0] is None
    assert build(images, config(opacity_threshold=threshold, padding_x=4, padding_y=4,
                                feather_sigma=2)).empty


@pytest.mark.parametrize('threshold', [-.01, 1.01, float('nan'), float('inf'), -float('inf')])
def test_native_entry_points_reject_invalid_opacity_threshold(threshold):
    images = NativeImages.empty().append(1, 1, 1, 1, [255])
    with pytest.raises(RuntimeError, match='opacity_threshold.*finite.*\\[0,1\\]'):
        images.source_stats(opacity_threshold=threshold)
    # Call the native bridge directly, bypassing the public builder validation.
    with pytest.raises(RuntimeError, match='opacity_threshold.*finite.*\\[0,1\\]'):
        box_mask(images, config(opacity_threshold=threshold), (4, 4))


@pytest.mark.parametrize('position,size,kwargs', [
    ((0, 0), (8, 8), {'padding_x': 2, 'padding_y': 1, 'corner_radius': 3, 'feather_sigma': .7}),
    ((7, 0), (8, 8), {'padding_x': 1, 'padding_y': 3, 'corner_radius': 9, 'feather_sigma': 1}),
    ((0, 7), (8, 8), {'padding_x': 0, 'padding_y': 0, 'corner_radius': 1, 'feather_sigma': 1.2}),
    ((7, 7), (8, 8), {'padding_x': 2, 'padding_y': 2, 'corner_radius': 2, 'feather_sigma': .4, 'strength': .8}),
    ((3, 3), (8, 8), {'padding_x': 1, 'padding_y': 2, 'corner_radius': 3, 'feather_sigma': 0}),
    ((3, 3), (8, 8), {'padding_x': 0, 'padding_y': 0, 'corner_radius': 0, 'feather_sigma': 0}),
])
def test_roundrect_and_finite_gaussian_match_world_reference(position, size, kwargs):
    x, y = position
    images = NativeImages.empty().append(x, y, 1, 1, [255])
    cfg = config(**kwargs)
    actual = dense(build(images, cfg, size), size)
    expected = reference_box((x, y, x + 1, y + 1), cfg, size)
    for actual_row, expected_row in zip(actual, expected):
        assert actual_row == pytest.approx(expected_row, abs=2e-7)


def test_max_union_preserves_nonrectangular_input_and_factory_boundary():
    class CoverageBuilder:
        name = 'test-coverage'
        algorithm_version = 'test-only'
        def build(self, group, cfg, context):
            plane = group.images[0]
            return RasterMask((plane.dst_x, plane.dst_y, plane.dst_x + plane.w, plane.dst_y + plane.h),
                              [coverage / 255 for coverage in plane.coverage])
    register_builder('test-coverage-native', CoverageBuilder)
    outputs = []
    for pixels in ([255, 0, 0, 255], [255, 255, 255, 255]):
        images = NativeImages.empty().append(1, 1, 2, 2, pixels)
        group = ImageGroup('g', None, ('key',), config(), images)
        raw = create_builder('test-coverage-native').build(group, config(), MaskContext((4, 4)))
        merged = merge_masks([raw], MaskContext((4, 4), images.budget))
        outputs.append(bytes(encode_yuv420p_left(merged, (4, 4)).buffer))
    assert outputs[0] != outputs[1]
    first, second = RasterMask((0, 0, 2, 1), [.4, .8]), RasterMask((1, 0, 3, 1), [.5, .3])
    union = merge_masks([first, second], MaskContext((4, 4)))
    assert list(union.weights) == pytest.approx([.4, .8, .3])
    with pytest.raises(ValueError, match='not supported'):
        create_builder('organic')


def test_allocations_enforce_budget_before_growing_and_release_on_failure():
    budget = NativeBudget(2048)
    images = NativeImages.empty(budget).append(1, 1, 1, 1, [255])
    initial = budget.used
    with pytest.raises(RuntimeError, match='max_in_flight_bytes'):
        build(images, config(padding_x=20, padding_y=20, feather_sigma=1))
    assert budget.used == initial
    assert budget.peak <= budget.limit
    images.release()
    assert budget.used == 0


def test_encode_buffer_view_survives_explicit_release():
    budget = NativeBudget(2048)
    mask = NativeMask.from_values((0, 0, 2, 2), [1] * 4, budget)
    weights = encode_yuv420p_left(RasterMask(mask.roi, mask), (2, 2), budget)
    view = weights.buffer
    weights.release()
    mask.release()
    assert list(view) == [255] * 6
    view.release()
    gc.collect()
    assert budget.used == 0


ASS = b'''[Script Info]
ScriptType: v4.00+
PlayResX: 384
PlayResY: 288
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,30,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,2,10,10,10,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,{\\move(40,120,300,120)}Moving
Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,{\\alpha&HFF&}Hidden
Dialogue: 0,0:00:02.00,0:00:03.00,Default,,0,0,0,,Visible
'''


def test_real_libass_blur_faint_outer_pixels_do_not_enlarge_thresholded_box():
    # A vector rectangle makes this check independent of installed font metrics.
    header = ASS.split(b'Dialogue:', 1)[0]
    dialogue = (b'Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,'
                b'{\\an7\\pos(100,100)\\bord0\\shad0\\blur12\\p1}m 0 0 l 80 0 80 40 0 40\n')
    with NativeSession(header + dialogue, 384, 288) as session:
        images = session.render(0)
        old_bbox, peak = images.source_stats()
        cropped_bbox, cropped_peak = images.source_stats(opacity_threshold=.5)
        assert peak > .5
        assert cropped_peak == peak
        assert old_bbox[0] < cropped_bbox[0] < cropped_bbox[2] < old_bbox[2]
        assert old_bbox[1] < cropped_bbox[1] < cropped_bbox[3] < old_bbox[3]
        assert build(images, config(opacity_threshold=.5), (384, 288)).roi == cropped_bbox
        assert build(images, config(opacity_threshold=0), (384, 288)).roi == old_bbox


def test_real_render_owned_images_survive_next_frame_and_close():
    budget = NativeBudget(4 * 1024 * 1024)
    session = NativeSession(ASS, 384, 288, budget=budget)
    first = session.render(0)
    digest = first.digest
    descriptor = first[0]
    bitmap = bytes(descriptor.coverage)
    later = session.render(500)
    assert first.source_stats()[0] != later.source_stats()[0]
    later.release()
    assert session.render(1000).image_count == 0
    assert session.render(2000).image_count > 0
    session.close()
    session.close()
    assert first.digest == digest
    assert bytes(descriptor.coverage) == bitmap
    assert 'libass' in session.logs.lower() or 'fontselect' in session.logs.lower()
    first.release()
    del descriptor
    gc.collect()
    assert budget.used == 0


def test_timestamp_uses_ffmpeg_double_multiply_then_truncates():
    for pts in (0, 1, 29, 30, 59, 60, 125999, -1):
        assert ffmpeg_time_ms(pts, Fraction(1, 60)) == int(float(pts) * (1.0 / 60.0) * 1000.0)


def test_sequential_frame_lifetimes_return_budget_to_zero():
    from assglass.contracts import FrameRequest
    from assglass.weights import YUV420PLeftWeightEncoder
    from types import SimpleNamespace
    budget = NativeBudget(4 * 1024 * 1024)
    encoder = YUV420PLeftWeightEncoder(budget)
    plan = SimpleNamespace(frame_size=(384, 288), pix_fmt='yuv420p', sampler_id='left-tent2-v1')
    with NativeSession(ASS, 384, 288, budget=budget) as session:
        for index in range(12):
            images = session.render(index * 50)
            mask = build(images, config(feather_sigma=1.5), (384, 288))
            frame = FrameRequest(index, index, Fraction(1, 20), (384, 288))
            weights = encoder.encode(mask, frame, plan)
            view = weights.buffer
            assert len(view) == weights.size
            view.release()
            weights.release()
            mask.release()
            images.release()
            assert budget.used == 0
    assert 0 < budget.peak < budget.limit


def test_box_native_integer_configuration_cannot_wrap():
    images = NativeImages.empty().append(1, 1, 1, 1, [255])
    with pytest.raises(ValueError, match='pixel integer'):
        build(images, config(padding_x=2**32))
