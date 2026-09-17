"""Independent mask-builder registry and full-resolution max aggregation."""
import math
import struct
from .contracts import MASK_ALGORITHMS, MaskContext, RasterMask
from .native import NativeMask, box_mask, organic_mask, union_native


class _NativeMaskBuilder:

    def validate_config(self, cfg):
        if cfg.mode != self.name:
            raise ValueError('%s requires mode=%s' % (type(self).__name__, self.name))
        if cfg.alpha_policy != 'geometry-only':
            raise ValueError('follow-visual-alpha is not supported; use geometry-only')
        if cfg.clip_policy != 'expand-after-clip' or cfg.bbox_policy != 'ink':
            raise ValueError('first release requires clip_policy=expand-after-clip and bbox_policy=ink')
        for key in self.pixel_keys:
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
        owner = self.native_build(group.images, cfg, context.frame_size, context.budget)
        return RasterMask(owner.roi, owner)


class BoxMaskBuilder(_NativeMaskBuilder):
    name = 'box'
    algorithm_version = MASK_ALGORITHMS[name]
    pixel_keys = ('padding_x', 'padding_y', 'corner_radius')
    native_build = staticmethod(box_mask)


class OrganicMaskBuilder(_NativeMaskBuilder):
    name = 'organic'
    algorithm_version = MASK_ALGORITHMS[name]
    pixel_keys = ('expand_x', 'expand_y', 'close')
    native_build = staticmethod(organic_mask)


def geometry_manifest(cfg):
    """Resolved numeric conventions, independent of selection and video layout."""
    # The existing Box ABI accepts float32 sigma; Organic accepts double.
    # Report the actual finite support used by native code, including values
    # just above/below integer Gaussian-radius boundaries.
    sigma = struct.unpack('f', struct.pack('f', cfg.feather_sigma))[0] if cfg.mode == 'box' else cfg.feather_sigma
    radius = math.ceil(3 * sigma)
    result = {'algorithm_version': MASK_ALGORITHMS[cfg.mode], 'dtype': 'float32',
              'boundary': 'constant-zero; crop to frame after all operators',
              'requested_feather_sigma': cfg.feather_sigma,
              'feather_sigma': sigma, 'feather_radius': radius,
              'feather_kernel_size': 2 * radius + 1,
              'opacity_threshold': cfg.opacity_threshold, 'union': 'max'}
    if cfg.mode == 'organic':
        result.update(ellipse='integer pixel centers inside ellipse, including boundary; zero axis is a line',
                      dilation_kernel_shape='ellipse', closing_kernel_shape='rectangle',
                      dilation_kernel_size=[2 * cfg.expand_x + 1, 2 * cfg.expand_y + 1],
                      closing_kernel_size=[2 * cfg.close + 1, 2 * cfg.close + 1],
                      closing_iterations=1,
                      halo=[cfg.expand_x + 2 * cfg.close + radius, cfg.expand_y + 2 * cfg.close + radius],
                      source='max coverage/255 for pixels whose coverage*opacity/65025 > threshold')
    return result


_BUILDERS = {'box': BoxMaskBuilder, 'organic': OrganicMaskBuilder}


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
