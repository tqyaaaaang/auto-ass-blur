"""Organic geometry regressions against an independent full-canvas reference.

The reference uses scalar Python and explicit two-dimensional neighborhoods,
without calling the native geometry helpers or reproducing their ROI strategy.
"""
from dataclasses import replace
import gc
import math

import pytest

from assglass.contracts import ImageGroup, MaskContext, ResolvedMaskConfig
from assglass.masks import create_builder, geometry_manifest
from assglass.native import NativeBudget, NativeImages, box_mask, organic_mask


def config(**changes):
    defaults = dict(mode='organic', expand_x=0, expand_y=0, close=0,
                    feather_sigma=0, strength=1, opacity_threshold=0)
    defaults.update(changes)
    return replace(ResolvedMaskConfig(), **defaults)


def plane(x, y, width, pixels, *, alpha=0, image_type='character'):
    assert len(pixels) % width == 0
    return dict(x=x, y=y, width=width, height=len(pixels) // width,
                pixels=pixels, alpha=alpha, image_type=image_type)


def native_images(planes, budget=None):
    images = NativeImages.empty(budget)
    for item in planes:
        images.append(item['x'], item['y'], item['width'], item['height'],
                      item['pixels'], color=0x12345600 | item['alpha'],
                      image_type=item['image_type'])
    return images


def full_frame(mask, size):
    width, height = size
    pixels = [0.0] * (width * height)
    if mask.roi is not None:
        left, top, right, bottom = mask.roi
        values = mask.weights
        for y in range(top, bottom):
            for x in range(left, right):
                pixels[y * width + x] = values[(y - top) * (right - left) + x - left]
    return pixels


def ellipse(rx, ry):
    # The public kernel definition is an integer lattice ellipse, including its
    # boundary. Degenerate ellipses have explicitly defined line support.
    if not rx:
        return [(0, y) for y in range(-ry, ry + 1)]
    if not ry:
        return [(x, 0) for x in range(-rx, rx + 1)]
    return [(x, y) for y in range(-ry, ry + 1) for x in range(-rx, rx + 1)
            if x * x * ry * ry + y * y * rx * rx <= rx * rx * ry * ry]


def reference(planes, cfg, size):
    """Zero-extended full world canvas; only the final result is frame-clipped."""
    width, height = size
    radius = math.ceil(3 * cfg.feather_sigma)
    # This canvas includes the entire frame and every source plane, with much
    # more padding than the operator chain needs. It does not derive an ink ROI.
    margin = 12 + 3 * (max(cfg.expand_x, cfg.expand_y) + cfg.close + radius)
    left = min([0] + [item['x'] for item in planes]) - margin
    top = min([0] + [item['y'] for item in planes]) - margin
    right = max([width] + [item['x'] + item['width'] for item in planes]) + margin
    bottom = max([height] + [item['y'] + item['height'] for item in planes]) + margin
    coordinates = [(x, y) for y in range(top, bottom) for x in range(left, right)]
    source = dict.fromkeys(coordinates, 0.0)
    for item in planes:
        if item['image_type'] not in cfg.include_types:
            continue
        opacity = 255 - item['alpha']
        for index, coverage in enumerate(item['pixels']):
            if coverage * opacity / 65025.0 > cfg.opacity_threshold:
                point = (item['x'] + index % item['width'],
                         item['y'] + index // item['width'])
                source[point] = max(source[point], coverage / 255.0)

    def morphology(values, offsets, operation):
        return {(x, y): operation(values.get((x + dx, y + dy), 0.0)
                                   for dx, dy in offsets)
                for x, y in coordinates}

    source = morphology(source, ellipse(cfg.expand_x, cfg.expand_y), max)
    closing_kernel = [(x, y) for y in range(-cfg.close, cfg.close + 1)
                      for x in range(-cfg.close, cfg.close + 1)]
    source = morphology(source, closing_kernel, max)
    source = morphology(source, closing_kernel, min)
    if radius:
        kernel = [math.exp(-offset * offset / (2 * cfg.feather_sigma ** 2))
                  for offset in range(-radius, radius + 1)]
        total = sum(kernel)
        kernel = [value / total for value in kernel]
        # Direct 2D convolution at each final pixel, independent of separable
        # filtering and any native workspace or intermediate edge treatment.
        return [sum(source.get((x + dx, y + dy), 0.0) * kernel[dx + radius] * kernel[dy + radius]
                    for dy in range(-radius, radius + 1) for dx in range(-radius, radius + 1))
                * cfg.strength for y in range(height) for x in range(width)]
    return [source[(x, y)] * cfg.strength for y in range(height) for x in range(width)]


def build(images, cfg, size):
    group = ImageGroup('organic-test', None, ('event-1',), cfg, images)
    return create_builder('organic').build(group, cfg, MaskContext(size, images.budget))


def test_zero_operators_preserve_grayscale_and_leave_input_untouched():
    planes = [plane(2, 3, 3, [0, 1, 64, 128, 200, 255])]
    with native_images(planes) as images:
        original_digest = images.digest
        original_pixels = bytes(images[0].coverage)
        mask = build(images, config(), (8, 8))
        try:
            expected = [0.0] * 64
            expected[3 * 8 + 2:3 * 8 + 5] = [0, 1 / 255, 64 / 255]
            expected[4 * 8 + 2:4 * 8 + 5] = [128 / 255, 200 / 255, 1]
            assert full_frame(mask, (8, 8)) == pytest.approx(expected, abs=6e-8)
            assert images.digest == original_digest
            assert bytes(images[0].coverage) == original_pixels
        finally:
            mask.release()


@pytest.mark.parametrize('coverage,opacity', [(255, 128), (128, 255), (200, 200), (128, 128), (1, 1)])
def test_threshold_is_strict_visual_product_but_kept_weight_is_geometry(coverage, opacity):
    boundary = coverage * opacity / 65025.0
    planes = [plane(2, 3, 1, [coverage], alpha=255 - opacity)]
    with native_images(planes) as images:
        for threshold, keep in ((boundary - boundary * 1e-10, True),
                                (boundary, False), (boundary + boundary * 1e-10, False)):
            with organic_mask(images, config(opacity_threshold=threshold), (6, 6)) as mask:
                expected = [0.0] * 36
                if keep:
                    expected[3 * 6 + 2] = coverage / 255.0
                else:
                    assert mask.roi is None
                assert full_frame(mask, (6, 6)) == pytest.approx(expected, abs=6e-8)


def test_source_union_uses_max_filters_types_and_never_adds_opacity():
    planes = [plane(2, 2, 1, [180]),
              plane(2, 2, 1, [200], alpha=80, image_type='outline'),
              plane(2, 2, 1, [255], alpha=200),  # Below the .5 threshold.
              plane(2, 2, 1, [255], image_type='shadow'),
              plane(5, 5, 1, [255], alpha=128),
              plane(5, 5, 1, [255], alpha=128),  # Two weak layers do not add.
              plane(6, 1, 1, [255], alpha=255)]
    with native_images(planes) as images:
        with organic_mask(images, config(opacity_threshold=.5), (8, 8)) as mask:
            expected = [0.0] * 64
            expected[2 * 8 + 2] = 200 / 255
            assert full_frame(mask, (8, 8)) == pytest.approx(expected, abs=6e-8)
        with organic_mask(images, config(opacity_threshold=.5, include_types=('shadow',)), (8, 8)) as mask:
            expected[2 * 8 + 2] = 1
            assert full_frame(mask, (8, 8)) == pytest.approx(expected, abs=6e-8)


@pytest.mark.parametrize('radii', [(0, 0), (0, 3), (3, 0), (3, 2), (2, 3)])
def test_dilation_uses_radius_and_exact_ellipse_including_degenerate_axes(radii):
    rx, ry = radii
    planes = [plane(5, 5, 1, [170])]
    with native_images(planes) as images:
        with organic_mask(images, config(expand_x=rx, expand_y=ry), (12, 12)) as mask:
            actual = full_frame(mask, (12, 12))
            support = {(i % 12 - 5, i // 12 - 5) for i, value in enumerate(actual) if value}
            assert support == set(ellipse(rx, ry))
            assert [value for value in actual if value] == pytest.approx([170 / 255] * len(support))


def test_closing_fills_a_small_hole_without_binarizing_or_implicit_hole_fill():
    pixels = [160] * 49
    pixels[3 * 7 + 3] = 0
    planes = [plane(3, 3, 7, pixels)]
    with native_images(planes) as images:
        with organic_mask(images, config(), (14, 14)) as unclosed:
            assert full_frame(unclosed, (14, 14))[6 * 14 + 6] == 0
        cfg = config(close=1)
        with organic_mask(images, cfg, (14, 14)) as closed:
            actual = full_frame(closed, (14, 14))
            assert actual[6 * 14 + 6] == pytest.approx(160 / 255)
            assert actual[0] == 0
            assert actual == pytest.approx(reference(planes, cfg, (14, 14)), abs=6e-8)


def test_closing_uses_square_corners_instead_of_a_disk():
    # Four separated pixels form a filled 3x3 square under a square radius-1
    # closing. A radius-1 disk cannot fill the missing center pixel.
    planes = [plane(3, 3, 3, [160, 0, 160, 0, 0, 0, 160, 0, 160])]
    with native_images(planes) as images:
        with organic_mask(images, config(close=1), (9, 9)) as mask:
            actual = full_frame(mask, (9, 9))
            expected = [160 / 255 if 3 <= x <= 5 and 3 <= y <= 5 else 0
                        for y in range(9) for x in range(9)]
            assert actual == pytest.approx(expected, abs=6e-8)


@pytest.mark.parametrize('radius', [1, 2, 4])
def test_rectangle_closing_matches_2d_grayscale_oracle_on_nonsquare_canvas(radius):
    planes = [plane(-1, 1, 5, [0, 64, 0, 255, 128,
                              200, 0, 150, 0, 100,
                              0, 64, 255, 0, 0]),
              plane(5, -1, 2, [90, 220, 200, 0, 0, 128, 255, 80])]
    cfg = config(close=radius)
    size = (9, 5)
    with native_images(planes) as images:
        with organic_mask(images, cfg, size) as mask:
            assert full_frame(mask, size) == pytest.approx(reference(planes, cfg, size), abs=6e-8)


@pytest.mark.parametrize('position', [(-1, -1), (0, 0), (4, 5)])
def test_close24_preserves_solid_rectangle_without_uniform_expansion(position):
    # Even a closing radius larger than the frame must preserve a solid
    # rectangle, including its visible portion when the source is offscreen.
    x0, y0 = position
    size = (8, 8)
    with native_images([plane(x0, y0, 3, [180] * 6)]) as images:
        with organic_mask(images, config(close=24), size) as mask:
            expected = [180 / 255 if x0 <= x < x0 + 3 and y0 <= y < y0 + 2 else 0
                        for y in range(size[1]) for x in range(size[0])]
            assert full_frame(mask, size) == pytest.approx(expected, abs=6e-8)


def test_equal_bounding_boxes_keep_distinct_shapes_through_public_builder():
    size = (8, 8)
    patterns = ([255, 0, 0, 0, 128, 0, 0, 0, 255], [255] * 9)
    organic_outputs, box_outputs = [], []
    for pixels in patterns:
        with native_images([plane(2, 2, 3, pixels)]) as images:
            cfg = config()
            mask = build(images, cfg, size)
            try:
                organic_outputs.append(full_frame(mask, size))
            finally:
                mask.release()
            box_cfg = replace(cfg, mode='box', padding_x=0, padding_y=0, corner_radius=0)
            with box_mask(images, box_cfg, size) as box:
                box_outputs.append(full_frame(box, size))
    assert organic_outputs[0] != organic_outputs[1]
    assert organic_outputs[0][2 * 8 + 3] == 0
    assert box_outputs[0] == box_outputs[1]


@pytest.mark.parametrize('position', [(0, 0), (12, 0), (0, 10), (12, 10),
                                      (-2, -1), (-4, 4), (15, 4), (6, 5)])
def test_morphology_and_feather_match_zero_extended_full_canvas_at_frame_edges(position):
    x, y = position
    planes = [plane(x, y, 3, [0, 255, 64, 200, 0, 128, 32, 180, 255]),
              plane(x + 2, y - 1, 2, [100, 0, 255, 220], alpha=80, image_type='outline')]
    cfg = config(expand_x=2, expand_y=1, close=2, feather_sigma=.7,
                 opacity_threshold=.2, strength=.65)
    size = (14, 12)
    with native_images(planes) as images:
        with organic_mask(images, cfg, size) as mask:
            actual = full_frame(mask, size)
            # Input and intermediate arrays are float32 in production; the
            # reference retains Python doubles through its direct convolution.
            assert actual == pytest.approx(reference(planes, cfg, size), abs=3e-7)
            assert any(value > 0 for value in actual)
            if mask.roi is not None:
                left, top, right, bottom = mask.roi
                assert 0 <= left < right <= size[0]
                assert 0 <= top < bottom <= size[1]


def test_gaussian_has_explicit_finite_support_and_strength_is_applied_once():
    size = (16, 16)
    cfg = config(feather_sigma=.6, strength=.4)
    planes = [plane(7, 7, 1, [180])]
    with native_images(planes) as images:
        with organic_mask(images, cfg, size) as mask:
            actual = full_frame(mask, size)
            assert actual == pytest.approx(reference(planes, cfg, size), abs=1e-7)
            assert actual[7 * 16 + 9] > 0
            assert actual[7 * 16 + 10] == 0
            assert sum(actual) == pytest.approx(180 / 255 * .4, abs=1e-7)


@pytest.mark.parametrize('sigma,radius', [(1 / 3, 1),
    (float.fromhex('0x1.5555555555556p-2'), 1),
    (float.fromhex('0x1.5555555555557p-2'), 2)])
def test_gaussian_sigma_preserves_double_precision_at_support_boundary(sigma, radius):
    # The hexadecimal values are the next two doubles above 1/3 (Python 3.8
    # has no math.nextafter). The first one's product still rounds to 1.0;
    # the second one's 3*sigma exceeds 1. All three collapse to one float32.
    # No ABI conversion may change the double-precision ceil decision.
    cfg = config(feather_sigma=sigma)
    planes = [plane(4, 4, 1, [255])]
    with native_images(planes) as images:
        with organic_mask(images, cfg, (10, 10)) as mask:
            actual = full_frame(mask, (10, 10))
            assert geometry_manifest(cfg)['feather_radius'] == radius
            assert actual[4 * 10 + 4 + radius] > 0
            assert actual[4 * 10 + 5 + radius] == 0
            assert actual == pytest.approx(reference(planes, cfg, (10, 10)), abs=1e-7)


def test_extremely_small_positive_sigma_keeps_finite_delta_weights():
    cfg = config(feather_sigma=1e-300, strength=.5)
    with native_images([plane(4, 4, 1, [200])]) as images:
        with organic_mask(images, cfg, (10, 10)) as mask:
            actual = full_frame(mask, (10, 10))
            expected = [0.0] * 100
            expected[4 * 10 + 4] = .5 * 200 / 255
            assert actual == pytest.approx(expected, abs=1e-7)
            assert all(math.isfinite(value) for value in actual)
            assert geometry_manifest(cfg)['feather_radius'] == 1
            assert mask.roi == (3, 3, 6, 6)


@pytest.mark.parametrize('planes,changes', [([], {}),
    ([plane(1, 1, 1, [127])], {'opacity_threshold': .5}),
    ([plane(1, 1, 1, [255])], {'opacity_threshold': 1}),
    ([plane(1, 1, 1, [255])], {'strength': 0}),
    ([plane(-50, -50, 1, [255])], {})])
def test_empty_sources_and_zero_strength_stay_empty(planes, changes):
    cfg = config(expand_x=2, expand_y=2, close=1, feather_sigma=1, **changes)
    with native_images(planes) as images:
        with organic_mask(images, cfg, (8, 8)) as mask:
            assert mask.roi is None
            assert list(mask.weights) == []


@pytest.mark.parametrize('limit', [4096, 20000, 35000])
def test_budget_failure_releases_workspace_and_does_not_damage_reusable_images(limit):
    # These limits fail at three different allocation stages: initial canvas,
    # second image buffer, and morphology scratch after both canvases exist.
    budget = NativeBudget(limit)
    images = native_images([plane(2, 2, 2, [255, 0, 0, 128])], budget)
    baseline = budget.used
    digest = images.digest
    for _ in range(3):
        with pytest.raises(RuntimeError, match='max_in_flight_bytes'):
            organic_mask(images, config(expand_x=20, expand_y=20, close=3, feather_sigma=2), (48, 48))
        assert budget.used == baseline
        assert budget.peak <= budget.limit
        assert images.digest == digest
    with organic_mask(images, config(), (8, 8)) as mask:
        assert full_frame(mask, (8, 8))[2 * 8 + 2] == 1
    assert budget.used == baseline
    images.release()
    assert budget.used == 0


@pytest.mark.parametrize('scratch_allowance', [0, 9 * 4])
def test_rectangle_line_scratch_obeys_budget_and_releases_on_failure(scratch_allowance):
    # A 1x3 source plus closing's halo creates a 9x11 canvas. The first
    # allowance rejects the horizontal queue; the second fits it but rejects
    # the longer vertical queue. Neither may leave either canvas allocated.
    budget = NativeBudget(2 * 9 * 11 * 4 + scratch_allowance)
    with native_images([plane(3, 3, 1, [100, 0, 200])]) as images:
        digest = images.digest
        for _ in range(2):
            with pytest.raises(RuntimeError, match='max_in_flight_bytes'):
                organic_mask(images, config(close=2), (12, 12), budget)
            assert budget.used == 0
            assert budget.peak <= budget.limit
            assert images.digest == digest


def test_output_view_owns_storage_after_mask_and_images_are_released():
    budget = NativeBudget(16384)
    images = native_images([plane(2, 2, 1, [128])], budget)
    mask = organic_mask(images, config(expand_x=1, expand_y=1, close=1), (8, 8))
    view = mask.weights
    expected = list(view)
    mask.release()
    images.release()
    assert list(view) == expected
    assert budget.used > 0
    view.release()
    del view
    gc.collect()
    assert budget.used == 0


@pytest.mark.parametrize('changes', [
    {'expand_x': -1}, {'expand_y': -1}, {'close': -1},
    {'feather_sigma': -1}, {'feather_sigma': float('nan')},
    {'strength': 1.01}, {'strength': float('inf')},
    {'opacity_threshold': -.01}, {'opacity_threshold': 1.01},
    {'opacity_threshold': float('nan')},
])
def test_native_entry_point_rejects_invalid_numeric_configuration(changes):
    with native_images([plane(1, 1, 1, [255])]) as images:
        baseline = images.budget.used
        with pytest.raises(RuntimeError):
            organic_mask(images, config(**changes), (8, 8))
        assert images.budget.used == baseline


@pytest.mark.parametrize('changes', [
    {'expand_x': 1.5}, {'expand_y': True}, {'close': 1000001},
    {'mode': 'box'}, {'alpha_policy': 'follow-visual-alpha'},
    {'bbox_policy': 'layout'}, {'clip_policy': 'respect-clip'},
    {'opacity_threshold': True},
])
def test_public_builder_rejects_unresolved_or_unsupported_configuration(changes):
    with native_images([plane(1, 1, 1, [255])]) as images:
        with pytest.raises(ValueError):
            build(images, config(**changes), (8, 8))


def test_public_builder_checks_clip_context():
    cfg = config()
    with native_images([plane(1, 1, 1, [255])]) as images:
        group = ImageGroup('organic-test', None, ('event-1',), cfg, images)
        with pytest.raises(ValueError, match='clip'):
            create_builder('organic').build(group, cfg, MaskContext((8, 8), images.budget, 'respect-clip'))
