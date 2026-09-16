"""Full-track, sequential subtitle selection with owned per-event image groups."""
from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple

from .ass import AlphaRewritePlan, Event, SourceDocument, build_analysis
from .config import (AppConfig, ConfigError, MarkerOverrides, load_sidecar, merge_overrides,
                     parse_marker, resolve_event_config)
from .contracts import BackendCapabilities, FrameRequest, FrameSelection, ImageGroup, ResolvedMaskConfig


class SelectionError(ValueError):
    pass


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def mask_values(config: ResolvedMaskConfig) -> dict:
    return {key: value for key, value in dataclasses.asdict(config).items() if key != "sources"}


@dataclass(frozen=True)
class SelectedEvent:
    event: Event
    overrides: MarkerOverrides
    config: ResolvedMaskConfig

    @property
    def index(self):
        return self.event.index

    @property
    def group_name(self):
        return self.overrides.values.get("group")

    @property
    def group_id(self):
        return "named:" + self.group_name if self.group_name is not None else "event:" + str(self.index)


@dataclass(frozen=True)
class SelectionPlan:
    source_sha256: str
    targets: Tuple[SelectedEvent, ...]
    default_config: ResolvedMaskConfig
    app_config: AppConfig
    digest: str

    @property
    def by_index(self):
        return {target.index: target for target in self.targets}

    @property
    def target_indices(self):
        return tuple(target.index for target in self.targets)

    def manifest(self):
        return {"selection_digest": self.digest, "marker_field": "Actor/Name", "marker_prefix": self.app_config.marker_prefix,
                "marker_semantics": "raw, case-sensitive startswith", "backend": self.app_config.selection["backend"],
                "grouping": self.app_config.selection["grouping"],
                "targets": [{"event_index": target.index, "event_key": target.event.key,
                    "line_number": target.event.line_number, "start_ms": target.event.start_ms,
                    "end_ms": target.event.end_ms, "actor": target.event.actor,
                    "group_name": target.group_name, "group_id": target.group_id,
                    "explicit_overrides": dict(target.overrides.values),
                    "resolved_mask_config": dataclasses.asdict(target.config)} for target in self.targets]}


def build_selection_plan(source: SourceDocument, config: AppConfig, sidecar=None) -> SelectionPlan:
    sidecar_rows = load_sidecar(sidecar, source)
    targets = []
    for event in source.events:
        try:
            marker = parse_marker(event.actor, config.marker_prefix)
            overrides = merge_overrides(marker, sidecar_rows.get(event.index))
            if overrides is not None:
                targets.append(SelectedEvent(event, overrides, resolve_event_config(config, overrides)))
        except ConfigError as error:
            raise ConfigError("Dialogue {} (line {}): {}".format(event.index, event.line_number, error))
    digest = _digest({"source": source.sha256, "backend": config.selection["backend"],
        "grouping": config.selection["grouping"], "targets": [(target.index, target.group_id, mask_values(target.config)) for target in targets]})
    return SelectionPlan(source.sha256, tuple(targets), resolve_event_config(config, MarkerOverrides({}, {})), config, digest)


def _preflight_overlaps(plan: SelectionPlan, independent=False):
    # End boundaries sort before starts: ASS activity is [Start, End).
    boundaries = []
    for target in plan.targets:
        if target.event.start_ms < target.event.end_ms:
            boundaries.append((target.event.start_ms, 1, target.index, target))
            boundaries.append((target.event.end_ms, 0, target.index, target))
    active = {}
    for at, action, index, target in sorted(boundaries, key=lambda item: item[:3]):
        if action == 0:
            active.pop(index, None)
            continue
        peers = [previous for previous in active.values()
                 if not independent or previous.group_id == target.group_id]
        if peers:
            # All previous active configs have already passed equality, so one is sufficient.
            previous = peers[0]
            end = min(previous.event.end_ms, target.event.end_ms)
            if previous.config != target.config:
                left, right = mask_values(previous.config), mask_values(target.config)
                differences = [key for key in left if left[key] != right[key]]
                raise SelectionError("Targets in the same background group, Dialogue {} (line {}) and {} (line {}), overlap [{}..{}) ms with different mask parameters: {}".format(
                    previous.index, previous.event.line_number, index, target.event.line_number, at, end, ", ".join(differences)))
            if not independent and target.config.mode == "box" and not plan.app_config.selection["allow_merged_box"]:
                raise SelectionError("Multiple Box targets Dialogue {} and {} overlap [{}..{}) ms. Alpha can produce only a shared box; explicitly enable selection.allow_merged_box/--allow-merged-box".format(previous.index, index, at, end))
        active[index] = target


def _profile_values(profile):
    size = getattr(profile, "frame_size", (getattr(profile, "width", None), getattr(profile, "height", None)))
    if len(size) != 2 or any(type(value) is not int or value <= 0 for value in size):
        raise SelectionError("Render profile must supply a positive integer frame_size")
    return {"frame_size": tuple(size), "fonts_dir": getattr(profile, "fonts_dir", None),
            "profile_id": getattr(profile, "profile_id", "ffmpeg-ass-mirrored-v1")}


@dataclass(frozen=True)
class PreparedSelection:
    backend: str
    source_sha256: str
    selection_digest: str
    profile_digest: str
    plan: SelectionPlan
    profile: Any
    rewrite_plan: AlphaRewritePlan

    @property
    def analysis_data(self):
        return self.rewrite_plan.analysis_data

    def manifest(self):
        return dict(self.rewrite_plan.manifest(), backend=self.backend, profile_digest=self.profile_digest,
                    selection_digest=self.selection_digest)


class AlphaTrackBackend:
    name = "alpha"
    capabilities = BackendCapabilities(supports_event_groups=False, transparent_coverage="visible-only")

    def preflight(self, source: SourceDocument, plan: SelectionPlan, profile) -> PreparedSelection:
        if source.sha256 != plan.source_sha256:
            raise SelectionError("Selection plan belongs to a different source ASS")
        if plan.app_config.selection["grouping"] != "merged":
            raise SelectionError("Alpha does not support per-event grouping. Build EventImages with sh scripts/build_libass.sh and rebuild FFmpeg with sh scripts/build_ffmpeg.sh; or explicitly use --backend alpha --grouping merged for the legacy box")
        if any(target.group_name is not None for target in plan.targets):
            raise SelectionError("Named group markers require --backend event-images --grouping per-event; merged mode cannot preserve separate groups")
        _preflight_overlaps(plan)
        profile_digest = _digest(_profile_values(profile))
        rewrite = build_analysis(source, plan.target_indices)
        return PreparedSelection(self.name, source.sha256, plan.digest, profile_digest, plan, profile, rewrite)

    def open(self, prepared: PreparedSelection):
        if prepared.backend != self.name or _digest(_profile_values(prepared.profile)) != prepared.profile_digest:
            raise SelectionError("Prepared selection backend/render profile mismatch")
        if prepared.source_sha256 != prepared.rewrite_plan.source_sha256 or prepared.selection_digest != prepared.plan.digest:
            raise SelectionError("Prepared selection source/plan identity mismatch")
        return AlphaRenderSession(prepared, self.capabilities)


class AlphaRenderSession:
    def __init__(self, prepared: PreparedSelection, capabilities: BackendCapabilities):
        from .native import NativeSession
        values = _profile_values(prepared.profile)
        self._native = NativeSession(prepared.analysis_data, values["frame_size"][0], values["frame_size"][1],
            fonts_dir=values["fonts_dir"], budget=getattr(prepared.profile, "native_budget", None))
        self.prepared = prepared
        self.capabilities = capabilities
        self._size = values["frame_size"]
        self._last_index = -1
        self._last_time = None
        self._closed = False
        self._active = {}
        self._starts = sorted(prepared.plan.targets, key=lambda target: (target.event.start_ms, target.index))
        self._cursor = 0

    @property
    def logs(self):
        return self._native.logs

    def render(self, frame: FrameRequest) -> FrameSelection:
        from .native import ffmpeg_time_ms
        if self._closed:
            raise SelectionError("Render session is closed")
        if frame.frame_index != self._last_index + 1:
            raise SelectionError("Render requests must be consecutive from frame 0; random access requires history replay")
        if tuple(frame.frame_size) != self._size:
            raise SelectionError("Render frame size differs from prepared profile")
        time_ms = ffmpeg_time_ms(frame.pts, frame.time_base)
        if self._last_time is not None and time_ms < self._last_time:
            raise SelectionError("Render timestamps must be nondecreasing")
        self._last_index, self._last_time = frame.frame_index, time_ms
        while self._cursor < len(self._starts) and self._starts[self._cursor].event.start_ms <= time_ms:
            target = self._starts[self._cursor]
            self._active[target.index] = target
            self._cursor += 1
        self._active = {index: target for index, target in self._active.items() if time_ms < target.event.end_ms}
        targets = tuple(self._active[index] for index in sorted(self._active))
        # Render every frame, including an empty activity set, to preserve collision history.
        images = self._native.render(time_ms)
        keys = tuple(target.event.key for target in targets)
        config = targets[0].config if targets else self.prepared.plan.default_config
        if not targets and images.image_count:
            images.release()
            raise SelectionError("Alpha invariant violated: visible images with no active targets")
        selection_digest = _digest((self.prepared.selection_digest, keys, mask_values(config)))
        image_digest = str(images.digest)
        group = ImageGroup("alpha-merged", None, keys, config, images)
        return FrameSelection(frame, time_ms, (group,), images.changed, "alpha", 0, self.capabilities,
            selection_digest, image_digest, _digest((selection_digest, image_digest)))

    def close(self):
        if not self._closed:
            self._native.close()
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


@dataclass(frozen=True)
class PreparedEventSelection:
    backend: str
    source_sha256: str
    selection_digest: str
    profile_digest: str
    plan: SelectionPlan
    profile: Any
    source: SourceDocument

    @property
    def analysis_data(self):
        return self.source.raw

    def manifest(self):
        return {"backend": self.backend, "source_sha256": self.source_sha256,
                "selection_digest": self.selection_digest, "profile_digest": self.profile_digest,
                "analysis_rewrite": "none", "event_mapping": "validated-frozen-track-indices-v1",
                "grouping": self.plan.app_config.selection["grouping"],
                "group_identity": "explicit-group-or-dialogue-index-v1"}


def _event_session(prepared):
    from .native import NativeEventSession
    values = _profile_values(prepared.profile)
    session = NativeEventSession(prepared.analysis_data, *values["frame_size"],
                                 fonts_dir=values["fonts_dir"],
                                 budget=getattr(prepared.profile, "native_budget", None),
                                 selected_indices=prepared.plan.target_indices)
    try:
        # libass sorts rendered events by layer, but the frozen track event array
        # remains in parsed order. Never infer identity from bitmap/callback order.
        metadata = session.event_metadata
        if len(metadata) != len(prepared.source.events):
            raise SelectionError("libass/ASS parser Dialogue count mismatch; cannot assign event images safely")
        for event, actual in zip(prepared.source.events, metadata):
            expected = {"start_ms": event.start_ms, "duration_ms": event.duration_ms,
                        "layer": int(event.fields.get("layer", "0")), "text": event.text}
            if any(actual.get(key) != value for key, value in expected.items()):
                raise SelectionError("libass/ASS parser event identity mismatch at Dialogue {} (line {})".format(
                    event.index, event.line_number))
        return session
    except BaseException:
        session.close()
        raise


class EventImagesBackend:
    name = "event-images"
    capabilities = BackendCapabilities(supports_event_groups=True, transparent_coverage="visible-only")

    def preflight(self, source, plan, profile):
        from .native import event_export_available
        if not event_export_available():
            raise SelectionError("EventImages extension unavailable. Run sh scripts/build_libass.sh and sh scripts/build_ffmpeg.sh; helper and FFmpeg must use the same extended libass")
        if source.sha256 != plan.source_sha256:
            raise SelectionError("Selection plan belongs to a different source ASS")
        independent = plan.app_config.selection["grouping"] == "per-event"
        if not independent and any(target.group_name is not None for target in plan.targets):
            raise SelectionError("Named group markers require grouping=per-event; merged mode cannot preserve separate groups")
        _preflight_overlaps(plan, independent=independent)
        prepared = PreparedEventSelection(self.name, source.sha256, plan.digest,
                                         _digest(_profile_values(profile)), plan, profile, source)
        # Also validate under --check-only, before starting the encoder.
        _event_session(prepared).close()
        return prepared

    def open(self, prepared):
        if prepared.backend != self.name or _digest(_profile_values(prepared.profile)) != prepared.profile_digest:
            raise SelectionError("Prepared selection backend/render profile mismatch")
        if prepared.source_sha256 != prepared.source.sha256 or prepared.selection_digest != prepared.plan.digest:
            raise SelectionError("Prepared selection source/plan identity mismatch")
        return EventRenderSession(prepared, self.capabilities)


class EventRenderSession:
    def __init__(self, prepared, capabilities):
        self._native = _event_session(prepared)
        self.prepared = prepared
        self.capabilities = capabilities
        self._size = _profile_values(prepared.profile)["frame_size"]
        self._last_index = -1
        self._last_time = None
        self._closed = False
        self._active = {}
        self._starts = sorted(prepared.plan.targets, key=lambda target: (target.event.start_ms, target.index))
        self._cursor = 0

    @property
    def logs(self):
        return self._native.logs

    def render(self, frame):
        from .native import NativeImages, ffmpeg_time_ms
        if self._closed:
            raise SelectionError("Render session is closed")
        if frame.frame_index != self._last_index + 1:
            raise SelectionError("Render requests must be consecutive from frame 0; random access requires history replay")
        if tuple(frame.frame_size) != self._size:
            raise SelectionError("Render frame size differs from prepared profile")
        time_ms = ffmpeg_time_ms(frame.pts, frame.time_base)
        if self._last_time is not None and time_ms < self._last_time:
            raise SelectionError("Render timestamps must be nondecreasing")
        self._last_index, self._last_time = frame.frame_index, time_ms
        while self._cursor < len(self._starts) and self._starts[self._cursor].event.start_ms <= time_ms:
            target = self._starts[self._cursor]
            self._active[target.index] = target
            self._cursor += 1
        self._active = {index: target for index, target in self._active.items() if time_ms < target.event.end_ms}
        grouped = {}
        merged = self.prepared.plan.app_config.selection["grouping"] == "merged"
        for index in sorted(self._active):
            target = self._active[index]
            grouped.setdefault("event-merged" if merged else target.group_id, []).append(target)
        # Render ALL original events on EVERY frame, including empty target sets.
        # Only the callback collector selects images; collision/history are untouched.
        exported = self._native.render(time_ms)
        groups = []
        try:
            for group_id, targets in grouped.items():
                images = []
                try:
                    for target in targets:
                        images.append(exported.take(target.index))
                    owner = images.pop() if len(images) == 1 else NativeImages.combine(images)
                finally:
                    for item in images:
                        item.release()
                keys = tuple(target.event.key for target in targets)
                groups.append(ImageGroup(group_id, keys[0] if len(keys) == 1 else None,
                                         keys, targets[0].config, owner))
            identity = [(group.group_id, group.target_event_keys, mask_values(group.effect_config)) for group in groups]
            selection_digest = _digest((self.prepared.selection_digest, identity))
            image_digest = _digest([(group.group_id, str(group.images.digest)) for group in groups])
            return FrameSelection(frame, time_ms, tuple(groups), exported.changed, "event-images", 0,
                                  self.capabilities, selection_digest, image_digest,
                                  _digest((selection_digest, image_digest)))
        except BaseException:
            for group in groups:
                group.images.release()
            raise
        finally:
            exported.release()

    def close(self):
        if not self._closed:
            self._native.close()
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


_BACKENDS = {"alpha": AlphaTrackBackend, "event-images": EventImagesBackend}


def register_backend(name, factory):
    if name in _BACKENDS:
        raise SelectionError("Selection backend {!r} already registered".format(name))
    if not callable(factory):
        raise TypeError("Selection backend factory must be callable")
    _BACKENDS[name] = factory


def create_backend(name="auto"):
    if name == "auto":
        from .native import event_export_available
        name = "event-images" if event_export_available() else "alpha"
    if name not in _BACKENDS:
        raise SelectionError("Unknown selection backend {!r}".format(name))
    return _BACKENDS[name]()
