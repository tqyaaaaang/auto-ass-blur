"""YUV transport conversion, deliberately outside shape/selection modules."""
from .masks import as_native
from .native import encode_native

SAMPLER_VERSION = 'left-tent2-v1'
QUANTIZER_VERSION = 'clamp-round-half-up-u8-v1'


def encode_yuv420p_left(mask, frame_size, budget=None):
    """Return an owned raw Y/U/V weight buffer (all planes span 0..255)."""
    owner, temporary = as_native(mask, budget)
    try:
        return encode_native(owner, frame_size, budget)
    finally:
        if temporary:
            owner.release()


from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Protocol, Tuple


@dataclass(frozen=True)
class PlaneSpec:
    name: str
    width: int
    height: int
    stride: int
    bytes_per_sample: int
    bit_depth: int
    offset: int

    @property
    def size(self):
        return self.stride * self.height


@dataclass
class WeightFrame:
    owner: Any
    planes: Tuple[PlaneSpec, ...]
    frame_index: int
    pts: int
    time_base: Fraction
    sampler_id: str

    @property
    def buffer(self):
        if hasattr(self.owner, 'buffer'):
            return self.owner.buffer
        return memoryview(self.owner).toreadonly()

    @property
    def size(self):
        return sum(plane.size for plane in self.planes)

    def release(self):
        release = getattr(self.owner, 'release', None)
        if release:
            release()
        self.owner = None


class WeightFrameEncoder(Protocol):
    sampler_id: str

    def encode(self, mask, frame, plan) -> WeightFrame:
        ...


class YUV420PLeftWeightEncoder:
    sampler_id = SAMPLER_VERSION
    quantizer_id = QUANTIZER_VERSION

    def __init__(self, budget=None):
        self.budget = budget

    @staticmethod
    def plane_specs(frame_size):
        width, height = frame_size
        if width <= 0 or height <= 0 or width % 2 or height % 2:
            raise ValueError('yuv420p weights require positive even dimensions')
        ysize = width * height
        uvsize = ysize // 4
        return (PlaneSpec('Y', width, height, width, 1, 8, 0),
                PlaneSpec('U', width // 2, height // 2, width // 2, 1, 8, ysize),
                PlaneSpec('V', width // 2, height // 2, width // 2, 1, 8, ysize + uvsize))

    def encode(self, mask, frame, plan):
        if frame.frame_size != plan.frame_size:
            raise ValueError('frame and processing profile dimensions disagree')
        if plan.pix_fmt != 'yuv420p' or plan.sampler_id != self.sampler_id:
            raise ValueError('processing profile does not match this weight encoder')
        planes = self.plane_specs(frame.frame_size)
        owner = encode_yuv420p_left(mask, frame.frame_size, self.budget)
        return WeightFrame(owner, planes, frame.frame_index, frame.pts, frame.time_base, self.sampler_id)
