"""Independent mask-builder registry and full-resolution max aggregation."""
import math
from .contracts import MaskContext, RasterMask
from .native import NativeMask, box_mask, union_native


class BoxMaskBuilder:
    name = 'box'
    algorithm_version = 'roundrect-ss4-opacity-threshold-v2'

    def validate_config(self, cfg):
        if cfg.mode != self.name:
            raise ValueError('BoxMaskBuilder requires mode=box')
        if cfg.alpha_policy != 'geometry-only':
            raise ValueError('follow-visual-alpha is not supported in the first release')
        if cfg.clip_policy != 'expand-after-clip' or cfg.bbox_policy != 'ink':
            raise ValueError('first release requires clip_policy=expand-after-clip and bbox_policy=ink')
        for key in ('padding_x', 'padding_y', 'corner_radius'):
            value = getattr(cfg, key)
            if type(value) is not int or not 0 <= value <= 1000000:
                raise ValueError('%s must be a resolved pixel integer in [0,1000000]' % key)
        if not math.isfinite(cfg.feather_sigma) or not 0 <= cfg.feather_sigma <= 100000:
            raise ValueError('feather_sigma must be finite in [0,100000]')
        if not math.isfinite(cfg.strength) or not 0 <= cfg.strength <= 1:
            raise ValueError('strength must be finite in [0,1]')
        if isinstance(cfg.opacity_threshold, bool) or not math.isfinite(cfg.opacity_threshold) or not 0 <= cfg.opacity_threshold <= 1:
            raise ValueError('opacity_threshold must be finite in [0,1]')

    def build(self, group, cfg, context):
        self.validate_config(cfg)
        if context.clip_policy != cfg.clip_policy:
            raise ValueError('mask context/config clip policy mismatch')
        owner = box_mask(group.images, cfg, context.frame_size, context.budget)
        return RasterMask(owner.roi, owner)


_BUILDERS = {'box': BoxMaskBuilder}


def register_builder(name, factory):
    if name in _BUILDERS:
        raise ValueError('mask builder already registered: %s' % name)
    _BUILDERS[name] = factory


def create_builder(name):
    if name not in _BUILDERS:
        raise ValueError('mask mode %r is not supported; available: %s' % (name, ', '.join(sorted(_BUILDERS))))
    return _BUILDERS[name]()


def as_native(mask, budget=None):
    if isinstance(mask.data, NativeMask):
        return mask.data, False
    return NativeMask.from_values(None if mask.empty else mask.roi, () if mask.empty else mask.weights, budget), True


def merge_masks(masks, context):
    owners, temporary = [], []
    try:
        for mask in masks:
            owner, created = as_native(mask, context.budget)
            owners.append(owner)
            if created:
                temporary.append(owner)
        result = union_native(owners, context.frame_size, context.budget)
        return RasterMask(result.roi, result)
    finally:
        for owner in temporary:
            owner.release()
