"""Backend-independent ownership and geometry contracts.

Coordinates are display-pixel coordinates and all rectangles are half open.
Native owners deliberately remain opaque to the scheduling and selection layers.
"""
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Mapping, Optional, Tuple


MASK_ALGORITHMS = {'box': 'roundrect-ss4-opacity-threshold-v2',
                   'organic': 'organic-ellipse-expand-rect-close-v2'}


@dataclass(frozen=True)
class ResolvedMaskConfig:
    mode: str = 'box'
    padding_x: int = 28
    padding_y: int = 16
    corner_radius: int = 24
    feather_sigma: float = 12.0
    strength: float = 1.0
    opacity_threshold: float = 0.5
    include_types: Tuple[str, ...] = ('character', 'outline')
    alpha_policy: str = 'geometry-only'
    bbox_policy: str = 'ink'
    clip_policy: str = 'expand-after-clip'
    expand_x: int = 48
    expand_y: int = 36
    close: int = 48
    sources: Mapping[str, str] = field(default_factory=dict, compare=False, hash=False)
    schema_version: str = 'mask-v1'
    algorithm_version: str = MASK_ALGORITHMS['box']


@dataclass(frozen=True)
class FrameRequest:
    frame_index: int
    pts: int
    time_base: Fraction
    frame_size: Tuple[int, int]


@dataclass(frozen=True)
class BackendCapabilities:
    supports_event_groups: bool = False
    transparent_coverage: str = 'visible-only'


@dataclass(frozen=True)
class ImageGroup:
    group_id: str
    event_key: Optional[str]
    target_event_keys: Tuple[str, ...]
    effect_config: ResolvedMaskConfig
    images: Any


@dataclass
class FrameSelection:
    frame: FrameRequest
    time_ms: int
    groups: Tuple[ImageGroup, ...]
    changed: int = 0
    backend: str = 'alpha'
    history_epoch: int = 0
    capabilities: Any = field(default_factory=BackendCapabilities)
    selection_digest: str = ''
    image_digest: str = ''
    export_digest: str = ''

    def release(self):
        seen = set()
        for group in self.groups:
            if id(group.images) not in seen:
                seen.add(id(group.images))
                release = getattr(group.images, 'release', None)
                if release:
                    release()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.release()


@dataclass(frozen=True)
class MaskContext:
    frame_size: Tuple[int, int]
    budget: Any = None
    clip_policy: str = 'expand-after-clip'


@dataclass
class RasterMask:
    """An empty mask, or a final clipped ROI with owned float32 samples.

    ``data`` can be a native owner or a flat float buffer for third-party/test
    builders. Consumers must not infer video-plane layout from this type.
    """
    roi: Optional[Tuple[int, int, int, int]] = None
    data: Any = None

    @property
    def empty(self):
        return self.roi is None or self.roi[0] >= self.roi[2] or self.roi[1] >= self.roi[3]

    @property
    def weights(self):
        return self.data.weights if hasattr(self.data, 'weights') else self.data

    def release(self):
        release = getattr(self.data, 'release', None)
        if release:
            release()
        self.data = None
        self.roi = None
