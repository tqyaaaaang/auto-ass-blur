"""Replaceable selection backend and full-track, sequential Alpha implementation."""
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
        "grouping": config.selection["grouping"], "targets": [(target.index, mask_values(target.config)) for target in targets]})
    return SelectionPlan(source.sha256, tuple(targets), resolve_event_config(config, MarkerOverrides({}, {})), config, digest)


def _preflight_overlaps(plan: SelectionPlan):
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
        if active:
            # All previous active configs have already passed equality, so one is sufficient.
            previous = next(iter(active.values()))
            end = min(previous.event.end_ms, target.event.end_ms)
            if previous.config != target.config:
                left, right = mask_values(previous.config), mask_values(target.config)
                differences = [key for key in left if left[key] != right[key]]
                raise SelectionError("Alpha targets Dialogue {} (line {}) and {} (line {}) overlap [{}..{}) ms with different mask parameters: {}".format(
                    previous.index, previous.event.line_number, index, target.event.line_number, at, end, ", ".join(differences)))
            if target.config.mode == "box" and not plan.app_config.selection["allow_merged_box"]:
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
            raise SelectionError("Alpha does not support per-event grouping; event-images is not implemented")
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


_BACKENDS = {"alpha": AlphaTrackBackend}


def register_backend(name, factory):
    if name in _BACKENDS:
        raise SelectionError("Selection backend {!r} already registered".format(name))
    if not callable(factory):
        raise TypeError("Selection backend factory must be callable")
    _BACKENDS[name] = factory


def create_backend(name="alpha"):
    if name == "event-images" and name not in _BACKENDS:
        raise SelectionError("event-images is not implemented; it requires a verified libass EventImages extension")
    if name not in _BACKENDS:
        raise SelectionError("Unknown selection backend {!r}".format(name))
    return _BACKENDS[name]()
