"""Weight sampling references use the declared continuous chroma grid."""
import math
import pytest
from assglass.contracts import MaskContext, RasterMask
from assglass.masks import merge_masks
from assglass.native import NativeMask
from assglass.weights import encode_yuv420p_left


def quantize(v):
    return math.floor(max(0, min(1, v)) * 255 + .5)


def reference_uv(values, width, height):
    out = []
    for v in range(height // 2):
        for u in range(width // 2):
            cx, cy = 2 * u, 2 * v + .5
            total = 0
            norm = 0
            for y in range(math.floor(cy) - 2, math.ceil(cy) + 3):
                for x in range(math.floor(cx) - 2, math.ceil(cx) + 3):
                    weight = max(0, 1 - abs(cx - x) / 2) * max(0, 1 - abs(cy - y) / 2)
                    total += values[max(0, min(height - 1, y)) * width + max(0, min(width - 1, x))] * weight
                    norm += weight
            out.append(quantize(total / norm))
    return out


@pytest.mark.parametrize('level,expected', [(0, 0), (1, 255), (.5, 128)])
def test_constant_weight_all_three_planes(level, expected):
    mask = NativeMask.from_values((0, 0, 8, 6), [level] * 48)
    data = bytes(encode_yuv420p_left(RasterMask(mask.roi, mask), (8, 6)).buffer)
    assert len(data) == 72
    assert set(data) == {expected}


@pytest.mark.parametrize('x,y', [(0, 0), (1, 1), (2, 1), (3, 4), (7, 5)])
def test_impulse_preserves_left_phase_at_odd_roi_and_edges(x, y):
    width, height = 8, 6
    values = [0.0] * (width * height)
    values[y * width + x] = 1.0
    mask = NativeMask.from_values((x, y, x + 1, y + 1), [1])
    data = bytes(encode_yuv420p_left(RasterMask(mask.roi, mask), (width, height)).buffer)
    ybytes, uvbytes = width * height, width * height // 4
    assert list(data[:ybytes]) == [quantize(value) for value in values]
    assert list(data[ybytes:ybytes + uvbytes]) == reference_uv(values, width, height)
    assert data[ybytes:ybytes + uvbytes] == data[ybytes + uvbytes:]


def test_union_is_done_before_downsampling():
    a = RasterMask((0, 0, 1, 1), [1])
    b = RasterMask((1, 0, 2, 1), [1])
    combined = merge_masks([a, b], MaskContext((4, 4)))
    merged = bytes(encode_yuv420p_left(combined, (4, 4)).buffer)
    separate = [bytes(encode_yuv420p_left(mask, (4, 4)).buffer) for mask in (a, b)]
    assert merged[16] > max(separate[0][16], separate[1][16])


def test_yuv420p_rejects_odd_dimensions():
    with pytest.raises(RuntimeError, match='positive even'):
        encode_yuv420p_left(RasterMask(), (3, 4))


def test_weight_encoder_substitution_changes_layout_without_changing_mask():
    from fractions import Fraction
    from types import SimpleNamespace
    import struct
    from assglass.contracts import FrameRequest
    from assglass.weights import PlaneSpec, WeightFrame, YUV420PLeftWeightEncoder

    mask = RasterMask((1, 0, 3, 2), [1, 0, .5, 1])
    frame = FrameRequest(7, 14, Fraction(1, 120), (4, 4))
    plan = SimpleNamespace(frame_size=(4, 4), pix_fmt='yuv420p', sampler_id='left-tent2-v1')
    standard = YUV420PLeftWeightEncoder().encode(mask, frame, plan)
    assert [plane.width for plane in standard.planes] == [4, 2, 2]
    assert (standard.frame_index, standard.pts, standard.time_base) == (7, 14, Fraction(1, 120))
    assert standard.size == len(standard.buffer) == 24

    class TestSinglePlane16:
        sampler_id = 'test-16bit-full-resolution'
        def encode(self, source, request, processing):
            values = [0.0] * (request.frame_size[0] * request.frame_size[1])
            x0, y0, x1, y1 = source.roi
            for y in range(y0, y1):
                for x in range(x0, x1):
                    values[y * request.frame_size[0] + x] = source.weights[(y - y0) * (x1 - x0) + x - x0]
            data = struct.pack('<' + 'H' * len(values), *(math.floor(v * 65535 + .5) for v in values))
            plane = PlaneSpec('test', 4, 4, 8, 2, 16, 0)
            return WeightFrame(data, (plane,), request.frame_index, request.pts, request.time_base, self.sampler_id)

    alternate = TestSinglePlane16().encode(mask, frame, plan)
    assert alternate.size == len(alternate.buffer) == 32
    values = struct.unpack('<16H', alternate.buffer)
    assert (values[1], values[2], values[5], values[6]) == (65535, 0, 32768, 65535)
    assert alternate.planes[0].bit_depth == 16
    assert mask.weights == [1, 0, .5, 1]
