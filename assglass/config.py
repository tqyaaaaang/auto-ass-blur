"""Configuration scopes, Actor prefix markers, and source-bound sidecars."""
from __future__ import annotations

import dataclasses
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .ass import PARSER_VERSION, SourceDocument
from .contracts import ResolvedMaskConfig


class ConfigError(ValueError):
    pass


ALIASES = {"feather": "feather_sigma", "radius": "corner_radius", "include": "include_types",
           "threshold": "opacity_threshold",
           "sigma": "blur_sigma", "blur": "blur_sigma"}
MASK_KEYS = {field.name for field in dataclasses.fields(ResolvedMaskConfig)} - {"sources", "schema_version", "algorithm_version"}
INTEGER_KEYS = {"padding_x", "padding_y", "corner_radius", "expand_x", "expand_y", "close"}
FLOAT_KEYS = {"feather_sigma", "strength", "blur_sigma", "opacity_threshold"}
_NUMBER = re.compile(r"-?(?:\d+(?:\.\d*)?|\.\d+)\Z")
_SECTION_DEFAULTS = {
    "selection": {"backend": "auto", "alpha_unsafe": "error", "allow_merged_box": False, "grouping": "per-event"},
    "render": {"fonts_dir": None, "target_width": 1920, "target_height": 1080,
               "adapter_profile": "ffmpeg-ass-verified", "manifest_output": None},
    "video": {"profile": "auto", "unsupported": "error"},
    "transport": {"mode": "pipe", "max_in_flight_frames": 4, "max_in_flight_bytes": 67108864,
                  "mask_cache_bytes": 0, "debug_mask_max_bytes": 1073741824},
    "runtime": {"native_workers": 1, "ffmpeg_threads": "auto", "ffmpeg_filter_threads": "auto"},
    # The FFmpeg adapter resolves encoder aliases/defaults in one scope, so a
    # user `vb` cannot accidentally conflict with an already-filled `b:v`.
    "encoder": {},
}


@dataclass(frozen=True)
class VideoBlurConfig:
    blur_sigma: float = 40.0
    source: str = "builtin"


@dataclass(frozen=True)
class SubtitleOutputConfig:
    burn_subtitles: bool = True
    source: str = "builtin"


@dataclass(frozen=True)
class MarkerOverrides:
    values: Mapping[str, Any]
    sources: Mapping[str, str]
    raw_marker: Optional[str] = None


@dataclass(frozen=True)
class AppConfig:
    marker_prefix: str
    mask_defaults: Mapping[str, Any]
    mask_sources: Mapping[str, str]
    video_blur: VideoBlurConfig
    output: SubtitleOutputConfig
    selection: Mapping[str, Any]
    render: Mapping[str, Any]
    video: Mapping[str, Any]
    transport: Mapping[str, Any]
    runtime: Mapping[str, Any]
    encoder: Mapping[str, Any]

    def manifest(self) -> dict:
        return dataclasses.asdict(self)


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError("Duplicate configuration key {!r}".format(key))
        result[key] = value
    return result


def load_project(path) -> dict:
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig")
    try:
        if path.suffix.lower() in (".yaml", ".yml"):
            try:
                import yaml
            except ImportError:
                raise ConfigError("YAML configuration requires PyYAML; install it or use JSON")
            class UniqueLoader(yaml.SafeLoader):
                pass
            def mapping(loader, node, deep=False):
                return _unique_pairs((loader.construct_object(key, deep=deep), loader.construct_object(value, deep=deep)) for key, value in node.value)
            UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
            try:
                result = yaml.load(text, Loader=UniqueLoader)
            except yaml.YAMLError as error:
                raise ConfigError("Invalid YAML configuration {}: {}".format(path, error))
        else:
            result = json.loads(text, object_pairs_hook=_unique_pairs)
    except (ValueError, TypeError) as error:
        raise ConfigError("Invalid configuration {}: {}".format(path, error))
    if not isinstance(result, dict):
        raise ConfigError("Configuration root must be an object")
    return result


def _value(key, value, from_text=False):
    if value is None:
        raise ConfigError("{} cannot be null; remove the key to inherit".format(key))
    if key == "group":
        if not isinstance(value, str) or not re.fullmatch(r"[\w.-]{1,128}", value):
            raise ConfigError("group must contain 1-128 letters, digits, underscores, dots or hyphens")
        return value
    if key in INTEGER_KEYS | FLOAT_KEYS:
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ConfigError("{} must be a finite number".format(key))
        if isinstance(value, str) and not _NUMBER.fullmatch(value):
            raise ConfigError("{} must be a finite decimal literal".format(key))
        try:
            number = float(value)
        except (ValueError, OverflowError):
            raise ConfigError("{} must be a finite number".format(key))
        if not math.isfinite(number) or number < 0:
            raise ConfigError("{} must be finite and nonnegative".format(key))
        if key in ("strength", "opacity_threshold") and number > 1:
            raise ConfigError("{} must be in [0, 1]".format(key))
        if key == "blur_sigma" and number > 1024:
            raise ConfigError("blur_sigma exceeds FFmpeg gblur's supported maximum 1024")
        if key == "feather_sigma" and number > 100000:
            raise ConfigError("feather_sigma exceeds the native builder's supported maximum 100000")
        if key in INTEGER_KEYS:
            if number > 1000000:
                raise ConfigError("{} must be a nonnegative pixel value <= 1000000".format(key))
            return number
        return number
    if key == "include_types":
        if isinstance(value, str) and from_text:
            value = value.split("+")
        if not isinstance(value, (list, tuple)) or not value:
            raise ConfigError("include_types must be a nonempty array (tag syntax: character+outline)")
        if any(item not in ("character", "outline", "shadow") for item in value) or len(set(value)) != len(value):
            raise ConfigError("include_types accepts unique character/outline/shadow values")
        return tuple(item for item in ("character", "outline", "shadow") if item in value)
    enums = {"mode": ("box", "organic"), "alpha_policy": ("geometry-only",),
             "bbox_policy": ("ink",), "clip_policy": ("expand-after-clip",)}
    if key in enums:
        if value not in enums[key]:
            raise ConfigError("Unsupported {}={!r}; supported values: {}".format(key, value, ", ".join(enums[key])))
        return value
    raise ConfigError("Unknown parameter {!r}".format(key))


def normalize_overrides(items, *, scope="row", from_text=False) -> dict:
    if isinstance(items, Mapping):
        items = list(items.items())
    result = {}
    for key, value in items:
        if not isinstance(key, str):
            raise ConfigError("Parameter names must be strings")
        canonical = ALIASES.get(key, key)
        if canonical == "blur_sigma" and scope != "defaults":
            raise ConfigError("{} is task-global only and cannot be overridden per row".format(key))
        if canonical not in MASK_KEYS | ({"blur_sigma"} if scope == "defaults" else {"group"}):
            raise ConfigError("Unknown {} parameter {!r}".format(scope, key))
        if canonical in result:
            raise ConfigError("Duplicate canonical key {!r} (including aliases)".format(canonical))
        result[canonical] = _value(canonical, value, from_text)
    return result


def _parse_pairs(text: str):
    if not text:
        return []
    output = []
    for part in text.split(";"):
        if not part or part.count("=") != 1:
            raise ConfigError("Each marker parameter must be a nonempty key=value separated by semicolons")
        key, value = (item.strip() for item in part.split("=", 1))
        if not key or not value:
            raise ConfigError("Marker keys and values must be nonempty")
        output.append((key, value))
    return output


def validate_prefix(prefix: str):
    if not isinstance(prefix, str) or not prefix or any(char in prefix for char in ",{};\r\n\x00"):
        raise ConfigError("marker_prefix must be a nonempty string without commas, braces, semicolons, or newlines")


def parse_marker(actor: str, prefix: str = "bgblur") -> Optional[MarkerOverrides]:
    """Case-sensitive *raw Actor* startswith; arbitrary suffixes are selected.

    Only a brace/semicolon immediately after the prefix starts a parameter list.
    Thus bgblurSpeaker selects defaults, but bgblur{strength=0} supplies an override.
    Effect never participates in marker detection. Whitespace is not stripped.
    """
    validate_prefix(prefix)
    if not actor.startswith(prefix):
        return None
    suffix = actor[len(prefix):]
    if suffix.startswith("{"):
        if not suffix.endswith("}") or "{" in suffix[1:] or "}" in suffix[:-1]:
            raise ConfigError("Malformed Actor marker parameter braces")
        content = suffix[1:-1]
    elif suffix.startswith(";"):
        content = suffix[1:]
        if not content:
            raise ConfigError("Empty legacy marker parameter list")
    else:
        content = ""
    values = normalize_overrides(_parse_pairs(content), from_text=True)
    return MarkerOverrides(values, {key: "actor" for key in values}, actor)


def _merge_dict(base, updates):
    result = dict(base)
    for key, value in updates.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _merge_dict(result[key], value)
        else:
            result[key] = value
    return result


def resolve_config(project=None, cli_defaults: Sequence[str] = (), blur_sigma=None,
                   burn_subtitles=None, marker_prefix=None, section_overrides=None) -> AppConfig:
    project_path = Path(project).resolve() if isinstance(project, (str, Path)) else None
    project = load_project(project_path) if project_path else dict(project or {})
    if project_path and isinstance(project.get("render"), Mapping):
        project["render"] = dict(project["render"])
        fonts_dir = project["render"].get("fonts_dir")
        if fonts_dir is not None and isinstance(fonts_dir, str) and not Path(fonts_dir).is_absolute():
            project["render"]["fonts_dir"] = str((project_path.parent / fonts_dir).resolve())
        manifest_path = project["render"].get("manifest_output")
        if manifest_path is not None and isinstance(manifest_path, str) and not Path(manifest_path).is_absolute():
            project["render"]["manifest_output"] = str((project_path.parent / manifest_path).resolve())
    allowed = set(_SECTION_DEFAULTS) | {"defaults", "output", "marker_prefix"}
    if set(project) - allowed:
        raise ConfigError("Unknown configuration sections: {}".format(", ".join(sorted(set(project) - allowed))))
    if section_overrides:
        if set(section_overrides) - allowed:
            raise ConfigError("Unknown CLI configuration sections")
        project = _merge_dict(project, section_overrides)
    defaults_input = project.get("defaults", {})
    if not isinstance(defaults_input, Mapping):
        raise ConfigError("defaults must be an object")
    layers = [("project", normalize_overrides(defaults_input, scope="defaults"))]
    cli_pairs = []
    for item in cli_defaults:
        if not isinstance(item, str) or item.count("=") != 1:
            raise ConfigError("--default must be KEY=VALUE")
        key, value = (part.strip() for part in item.split("=", 1))
        cli_pairs.append((key, value))
    if blur_sigma is not None:
        cli_pairs.append(("blur_sigma", blur_sigma))
    layers.append(("cli", normalize_overrides(cli_pairs, scope="defaults", from_text=True)))
    mask_defaults, sources = {}, {}
    builtin_blur = VideoBlurConfig()
    sigma, sigma_source = builtin_blur.blur_sigma, builtin_blur.source
    for source, values in layers:
        for key, value in values.items():
            if key == "blur_sigma":
                sigma, sigma_source = value, source
            else:
                mask_defaults[key], sources[key] = value, source
    output = project.get("output", {})
    if not isinstance(output, Mapping) or set(output) - {"burn_subtitles"}:
        raise ConfigError("output accepts only burn_subtitles")
    burn, burn_source = True, "builtin"
    if "burn_subtitles" in output:
        burn, burn_source = output["burn_subtitles"], "project"
    if burn_subtitles is not None:
        burn, burn_source = burn_subtitles, "cli"
    if type(burn) is not bool:
        raise ConfigError("output.burn_subtitles must be a boolean, not a string or null")
    prefix = marker_prefix if marker_prefix is not None else project.get("marker_prefix", "bgblur")
    validate_prefix(prefix)
    sections = {}
    for name, builtin in _SECTION_DEFAULTS.items():
        configured = project.get(name, {})
        if not isinstance(configured, Mapping):
            raise ConfigError("{} must be an object".format(name))
        # Encoder-specific keys/values are validated by the FFmpeg adapter.
        if name != "encoder" and set(configured) - set(builtin):
            raise ConfigError("Unknown {} keys: {}".format(name, ", ".join(sorted(set(configured) - set(builtin)))))
        sections[name] = _merge_dict(builtin, configured)
    selection = sections["selection"]
    if selection["backend"] not in ("auto", "alpha", "event-images"):
        raise ConfigError("Unknown selection backend {!r}".format(selection["backend"]))
    if selection["alpha_unsafe"] != "error":
        raise ConfigError("selection.alpha_unsafe only permits 'error'; no unsafe bypass exists")
    if type(selection["allow_merged_box"]) is not bool:
        raise ConfigError("selection.allow_merged_box must be boolean")
    # The existing opt-in flag remains an explicit request for the legacy shared box.
    # Reject contradictory input instead of silently overriding independent grouping.
    if selection["allow_merged_box"]:
        if project.get("selection", {}).get("grouping") == "per-event":
            raise ConfigError("allow_merged_box conflicts with grouping=per-event")
        selection["grouping"] = "merged"
    if selection["grouping"] not in ("merged", "per-event"):
        raise ConfigError("selection.grouping must be merged or per-event")
    if sections["video"]["unsupported"] != "error":
        raise ConfigError("video.unsupported only permits 'error'")
    transport = sections["transport"]
    if transport["mode"] != "pipe":
        raise ConfigError("Only bounded pipe transport is implemented")
    for key in ("max_in_flight_frames", "max_in_flight_bytes", "debug_mask_max_bytes"):
        if type(transport[key]) is not int or transport[key] <= 0:
            raise ConfigError("transport.{} must be a positive integer".format(key))
    if transport["mask_cache_bytes"] != 0 or type(transport["mask_cache_bytes"]) is not int:
        raise ConfigError("Cross-frame caching is not enabled; mask_cache_bytes must be 0")
    runtime = sections["runtime"]
    if runtime["native_workers"] != 1 or type(runtime["native_workers"]) is not int:
        raise ConfigError("The sequential renderer requires native_workers=1")
    for key in ("ffmpeg_threads", "ffmpeg_filter_threads"):
        if runtime[key] != "auto" and (type(runtime[key]) is not int or runtime[key] <= 0):
            raise ConfigError("runtime.{} must be 'auto' or a positive integer".format(key))
    for key in ("target_width", "target_height"):
        if type(sections["render"][key]) is not int or sections["render"][key] <= 0:
            raise ConfigError("render.{} must be a positive integer".format(key))
    if sections["render"]["fonts_dir"] is not None and not isinstance(sections["render"]["fonts_dir"], str):
        raise ConfigError("render.fonts_dir must be a string or null")
    cfg = AppConfig(prefix, mask_defaults, sources, VideoBlurConfig(sigma, sigma_source),
                    SubtitleOutputConfig(burn, burn_source), **sections)
    resolve_event_config(cfg, MarkerOverrides({}, {}))
    return cfg


def resolve_event_config(config: AppConfig, overrides: MarkerOverrides) -> ResolvedMaskConfig:
    values = dict(config.mask_defaults)
    values.update(overrides.values)
    values.pop("group", None)  # Selection metadata, never a mask parameter or pixel dimension.
    mode = values.get("mode", "box")
    if mode != "box":
        raise ConfigError("Mask mode {!r} is not implemented; only box is available".format(mode))
    invalid = {"expand_x", "expand_y", "close"} & set(overrides.values)
    if invalid:
        raise ConfigError("Box row does not accept Organic parameters: {}".format(", ".join(sorted(invalid))))
    # Future Organic presets may live in global defaults; they cannot affect Box identity.
    for key in ("expand_x", "expand_y", "close"):
        values.pop(key, None)
    sources = {key: "builtin" for key in MASK_KEYS}
    sources.update(config.mask_sources)
    sources.update({key: source for key, source in overrides.sources.items() if key != "group"})
    unscaled = ResolvedMaskConfig(**values, sources=sources)
    scale = config.render["target_height"] / 1080.0
    actual = {key: int(math.floor(getattr(unscaled, key) * scale + 0.5)) for key in INTEGER_KEYS}
    actual["feather_sigma"] = unscaled.feather_sigma * scale
    return dataclasses.replace(unscaled, **actual)


def merge_overrides(first: Optional[MarkerOverrides], second: Optional[MarkerOverrides]) -> Optional[MarkerOverrides]:
    if first is None:
        return second
    if second is None:
        return first
    values, sources = dict(first.values), dict(first.sources)
    for key, value in second.values.items():
        if key in values and values[key] != value:
            raise ConfigError("Actor/sidecar conflict for {!r}: {!r} versus {!r}".format(key, values[key], value))
        values[key] = value
        sources[key] = "actor+sidecar" if key in first.values else second.sources[key]
    return MarkerOverrides(values, sources, first.raw_marker)


SIDECAR_SCHEMA_VERSION = 1


def create_sidecar(document: SourceDocument, indices=None) -> dict:
    indices = tuple(range(len(document.events)) if indices is None else indices)
    if any(type(index) is not int or not 0 <= index < len(document.events) for index in indices):
        raise ConfigError("Sidecar indices must be valid zero-based Dialogue indices")
    if len(indices) != len(set(indices)):
        raise ConfigError("Duplicate sidecar Dialogue index")
    return {"schema_version": SIDECAR_SCHEMA_VERSION, "parser_version": PARSER_VERSION,
            "ass_sha256": document.sha256, "events": [
                {"index": index, "event_sha256": document.events[index].event_sha256,
                 "line_number": document.events[index].line_number, "overrides": {}}
                for index in indices]}


def load_sidecar(sidecar, document: SourceDocument) -> Dict[int, MarkerOverrides]:
    if sidecar is None:
        return {}
    data = load_project(sidecar) if isinstance(sidecar, (str, Path)) else sidecar
    if not isinstance(data, Mapping) or set(data) - {"schema_version", "parser_version", "ass_sha256", "events"}:
        raise ConfigError("Invalid sidecar object/schema fields")
    if type(data.get("schema_version")) is not int or data["schema_version"] != SIDECAR_SCHEMA_VERSION or data.get("parser_version") != PARSER_VERSION:
        raise ConfigError("Sidecar schema/parser version mismatch; regenerate the sidecar")
    if data.get("ass_sha256") != document.sha256:
        raise ConfigError("Sidecar ASS SHA-256 mismatch; regenerate/rebind after any source change")
    if not isinstance(data.get("events"), list):
        raise ConfigError("Sidecar events must be an array")
    result = {}
    for record in data["events"]:
        if not isinstance(record, Mapping) or set(record) - {"index", "event_sha256", "line_number", "overrides"}:
            raise ConfigError("Invalid sidecar event record")
        index = record.get("index")
        if type(index) is not int or not 0 <= index < len(document.events):
            raise ConfigError("Sidecar Dialogue index out of bounds: {!r}".format(index))
        if index in result:
            raise ConfigError("Duplicate sidecar Dialogue index {}".format(index))
        event = document.events[index]
        if record.get("event_sha256") != event.event_sha256 or ("line_number" in record and record["line_number"] != event.line_number):
            raise ConfigError("Sidecar event digest/line mismatch at Dialogue {}".format(index))
        overrides = record.get("overrides", {})
        if not isinstance(overrides, Mapping):
            raise ConfigError("Sidecar overrides must be an object")
        values = normalize_overrides(overrides)
        result[index] = MarkerOverrides(values, {key: "sidecar" for key in values})
    return result
