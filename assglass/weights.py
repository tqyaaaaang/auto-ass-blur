"""YUV transport conversion, deliberately outside shape/selection modules."""
from .masks import as_native
from .native import NativeBudget, encode_native

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


from dataclasses import dataclass, field
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
    # Only immutable internal native storage receives a reuse identity. A
    # third-party frame may wrap mutable bytes, including a reused bytearray.
    content_token: Any = field(default=None, repr=False, compare=False)

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
    """Convert final merged masks, retaining at most one exact input/result.

    The optional cache retains immutable native owners, never complete frames:
    timestamps and buffer lifetimes belong to each independently returned frame.
    Its byte limit includes both the source mask and the planar weight storage,
    all charged to ``budget``. Zero disables active and empty-frame reuse alike.
    """

    sampler_id = SAMPLER_VERSION
    quantizer_id = QUANTIZER_VERSION

    def __init__(self, budget=None, limit_bytes=None):
        self.budget = budget or NativeBudget()
        if limit_bytes is None:
            limit_bytes = min(16 * 1024 * 1024, self.budget.limit)
        if type(limit_bytes) is not int or not 0 <= limit_bytes <= self.budget.limit:
            raise ValueError('weight_cache_bytes must be an integer in [0,max_in_flight_bytes]')
        self.limit_bytes = limit_bytes
        self._cached_mask = None
        self._cached_owner = None
        self._cached_plan = None
        self._cached_identity = None
        self._cached_token = None
        self.bytes_used = 0
        self._metrics = dict(lookups=0, hits=0, encodes=0, evictions=0,
                             budget_retries=0, uncacheable=0, peak_bytes=0)
        self.empty_frames = 0
        self.empty_allocations = 0

    @property
    def metrics(self):
        return dict(self._metrics, entries=int(self._cached_owner is not None),
                    bytes_used=self.bytes_used, limit_bytes=self.limit_bytes)

    @property
    def _empty_owner(self):
        """Compatibility view of the consecutive empty-frame cache."""
        if self._cached_mask is not None and self._cached_mask.roi is None:
            return self._cached_owner
        return None

    def close(self):
        """Release optional retained storage; returned frames stay valid."""
        if self._cached_owner is not None:
            self._cached_owner.release()
            self._cached_mask.release()
            self._metrics['evictions'] += 1
        self._cached_owner = self._cached_mask = None
        self._cached_plan = self._cached_identity = self._cached_token = None
        self.bytes_used = 0

    evict_all = close

    def run_with_reclaim(self, operation):
        """Retry a failed allocation after releasing the optional cache."""
        try:
            return operation()
        except RuntimeError as exc:
            if (not str(exc).startswith('native frame allocation exceeds max_in_flight_bytes')
                    or self._cached_owner is None):
                raise
            self.close()
            self._metrics['budget_retries'] += 1
        return operation()

    def __del__(self):
        if hasattr(self, '_cached_owner'):
            self.close()

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
        self._metrics['lookups'] += 1
        identity = (tuple(frame.frame_size), plan.pix_fmt, plan.sampler_id,
                    getattr(plan, 'profile_id', None), self.quantizer_id)
        # Keep the plan itself alive so object-id reuse cannot produce a hit.
        # Native conversion snapshots mutable third-party float buffers before
        # exact comparison and keeps every retained byte on the shared budget.
        native, temporary = self.run_with_reclaim(lambda: as_native(mask, self.budget))
        try:
            if (self._cached_owner is not None and self._cached_plan is plan
                    and self._cached_identity == identity and self._cached_mask.equals(native)):
                owner, token = self._cached_owner.retain(), self._cached_token
                self._metrics['hits'] += 1
            else:
                # Only the preceding content is useful: release it before a
                # new full-frame output is allocated, including zero buffers.
                self.close()
                owner = encode_native(native, frame.frame_size, self.budget)
                self._metrics['encodes'] += 1
                token = object()
                if mask.empty:
                    self.empty_allocations += 1
                size = native.allocation_bytes + owner.allocation_bytes
                if self.limit_bytes and size <= self.limit_bytes and native.budget is self.budget:
                    cached_mask = native.retain()
                    try:
                        cached_owner = owner.retain()
                    except BaseException:
                        cached_mask.release()
                        owner.release()
                        raise
                    self._cached_mask, self._cached_owner = cached_mask, cached_owner
                    self._cached_plan, self._cached_identity = plan, identity
                    self._cached_token = token
                    self.bytes_used = size
                    self._metrics['peak_bytes'] = max(self._metrics['peak_bytes'], size)
                else:
                    self._metrics['uncacheable'] += 1
            if mask.empty:
                self.empty_frames += 1
            return WeightFrame(owner, planes, frame.frame_index, frame.pts, frame.time_base,
                               self.sampler_id, token)
        finally:
            if temporary:
                native.release()
