"""Bounded last-input mask reuse, with exact native image comparison.

Only mask geometry is cached: the source video still changes and must be decoded,
blurred, composited and encoded for every affected frame. Retained native storage
stays charged to the pipeline's shared NativeBudget. A failed native allocation
can reclaim the cache before retrying the same operation.
"""
from dataclasses import dataclass

from .contracts import RasterMask
from .native import NativeImages, NativeMask


_BUDGET_ERROR = 'native frame allocation exceeds max_in_flight_bytes'


@dataclass
class _Entry:
    identity: tuple
    digest: str
    images: NativeImages
    mask: RasterMask
    size: int

    def release(self):
        self.images.release()
        self.mask.release()


class MaskCache:
    """At most one retained input/result pair per currently active group.

    ``begin_frame`` establishes the renderer/profile/history identity and drops
    inactive groups. ``build`` returns an independently owned mask. Cache limits
    describe retained native allocations, not additional copies: inputs/results
    remain shared with their original owners until those owners release them.
    """

    def __init__(self, limit_bytes, budget):
        if type(limit_bytes) is not int or not 0 <= limit_bytes <= budget.limit:
            raise ValueError('mask_cache_bytes must be an integer in [0,max_in_flight_bytes]')
        self.limit_bytes = limit_bytes
        self.budget = budget
        self._entries = {}
        self._frame_identity = None
        self._active = set()
        self.bytes_used = 0
        self._metrics = dict(lookups=0, hits=0, builds=0, evictions=0,
                             budget_retries=0, uncacheable=0, peak_bytes=0)

    @property
    def metrics(self):
        return dict(self._metrics, entries=len(self._entries), bytes_used=self.bytes_used,
                    limit_bytes=self.limit_bytes)

    def _evict(self, group_id):
        entry = self._entries.pop(group_id, None)
        if entry is not None:
            self.bytes_used -= entry.size
            entry.release()
            self._metrics['evictions'] += 1

    def evict_all(self):
        for group_id in tuple(self._entries):
            self._evict(group_id)

    close = evict_all

    def begin_frame(self, selection, profile_id):
        identity = (selection.backend, profile_id, selection.history_epoch,
                    tuple(selection.frame.frame_size))
        if identity != self._frame_identity:
            self.evict_all()
        self._frame_identity = identity
        self._active = {group.group_id for group in selection.groups}
        for group_id in tuple(self._entries):
            if group_id not in self._active:
                self._evict(group_id)

    def run_with_reclaim(self, operation):
        """Retry a native allocation once after releasing all optional storage.

        The operation must be safely repeatable after a failed allocation. Other
        failures are propagated unchanged. Callers can use this around rendering,
        mask union and weight encoding as well as the automatic use in ``build``.
        """
        try:
            return operation()
        except RuntimeError as exc:
            if not str(exc).startswith(_BUDGET_ERROR) or not self._entries:
                raise
            self.evict_all()
            self._metrics['budget_retries'] += 1
        return operation()

    def build(self, group, cfg, context, builder):
        if self._frame_identity is None or group.group_id not in self._active:
            raise ValueError('call begin_frame with this active group before cache.build')
        if tuple(context.frame_size) != self._frame_identity[-1]:
            raise ValueError('mask context and current frame size disagree')
        if context.budget is not self.budget or group.images.budget is not self.budget:
            raise ValueError('cache, images and context must share one NativeBudget')
        self._metrics['lookups'] += 1
        identity = (self._frame_identity, group.group_id, group.event_key,
                    tuple(group.target_event_keys), cfg, context.clip_policy,
                    type(builder), builder.algorithm_version)
        digest = group.images.digest if self.limit_bytes else None
        entry = self._entries.get(group.group_id)
        if (entry is not None and entry.identity == identity and entry.digest == digest
                and entry.images.equals(group.images)):
            self._metrics['hits'] += 1
            return RasterMask(entry.mask.roi, entry.mask.data.retain())

        # A changed group must release its previous result before allocating a
        # new one. Other groups retain their useful previous-frame entries.
        self._evict(group.group_id)

        def build_mask():
            self._metrics['builds'] += 1
            return builder.build(group, cfg, context)

        mask = self.run_with_reclaim(build_mask)
        if (not self.limit_bytes or not isinstance(mask.data, NativeMask)
                or mask.data.budget is not self.budget):
            self._metrics['uncacheable'] += 1
            return mask
        size = group.images.allocation_bytes + mask.data.allocation_bytes
        if size > self.limit_bytes - self.bytes_used:
            # Do not evict another active group's result merely to admit this
            # one. Optional cache storage must not add a mandatory allocation.
            self._metrics['uncacheable'] += 1
            return mask
        retained_images = group.images.retain()
        try:
            retained_mask = RasterMask(mask.roi, mask.data.retain())
        except BaseException:
            retained_images.release()
            mask.release()
            raise
        self._entries[group.group_id] = _Entry(identity, digest, retained_images, retained_mask, size)
        self.bytes_used += size
        self._metrics['peak_bytes'] = max(self._metrics['peak_bytes'], self.bytes_used)
        return mask

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def __del__(self):
        if hasattr(self, '_entries'):
            self.close()
