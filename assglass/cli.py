"""User-facing command line and one-job orchestration."""
from __future__ import annotations

import argparse
import errno
from dataclasses import asdict, is_dataclass
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from types import SimpleNamespace

from . import __version__
from .ffmpeg import (PipePipeline, build_command, build_graph, capabilities,
                     normalize_encoder_options, resolve_encoder, write_all)
from .runtime import file_hash, verify_ass_runtime
from .video import ProcessingProfileRegistry, VideoProbe, verify_output, verify_timeline


def parser():
    p = argparse.ArgumentParser(description="为 ASS Actor/Name 字段带前缀标记的字幕生成背景高斯模糊，并同次压制字幕。")
    p.add_argument("video", type=Path)
    p.add_argument("subtitle", type=Path)
    p.add_argument("-o", "--output", required=True, type=Path)
    p.add_argument("--config", type=Path, help="YAML 或 JSON 项目配置")
    p.add_argument("--marker-prefix", help="Actor 字段前缀，默认 bgblur；大小写敏感")
    p.add_argument("--default", action="append", default=[], metavar="KEY=VALUE")
    p.add_argument("--blur-sigma", type=float)
    group = p.add_mutually_exclusive_group()
    group.add_argument("--burn-subtitles", dest="burn_subtitles", action="store_true")
    group.add_argument("--no-burn-subtitles", dest="burn_subtitles", action="store_false")
    p.set_defaults(burn_subtitles=None)
    p.add_argument("--fonts-dir", type=Path)
    p.add_argument("--sidecar", type=Path)
    p.add_argument("--allow-merged-box", action="store_true", default=None)
    p.add_argument("--backend", choices=["auto", "alpha", "event-images"])
    p.add_argument("--crf", type=float)
    p.add_argument("--preset")
    p.add_argument("--maxrate")
    p.add_argument("--bufsize")
    p.add_argument("--encoder", action="append", default=[], metavar="KEY=VALUE")
    p.add_argument("--audio-codec", choices=["copy", "aac", "none"], default=None)
    bundled = Path(__file__).resolve().parent.parent / ".tools" / "ffmpeg-6.1.1"
    p.add_argument("--ffmpeg", default=str(bundled / "ffmpeg") if (bundled / "ffmpeg").is_file() else "ffmpeg")
    p.add_argument("--ffprobe", default=str(bundled / "ffprobe") if (bundled / "ffprobe").is_file() else "ffprobe")
    p.add_argument("--manifest", type=Path)
    p.add_argument("--debug-mask", type=Path, help="显式保存实际三 plane 权重 raw 文件（默认不保存）")
    p.add_argument("--check-only", action="store_true", help="完成输入、时间轴与运行环境预检，不编码")
    p.add_argument("--overwrite", action="store_true", help="成功验收后替换既有输出")
    p.add_argument("--version", action="version", version=__version__)
    return p


def log(message):
    print(message, file=sys.stderr, flush=True)


def json_default(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, (Path, Fraction)):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(type(value).__name__)


def publish(source, destination, overwrite):
    source, destination = Path(source), Path(destination)
    def atomic(staged):
        if overwrite:
            os.replace(str(staged), str(destination))
        else:
            # A hard link makes no-clobber publication atomic.
            os.link(str(staged), str(destination))
            staged.unlink()
    try:
        atomic(source)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        # Custom diagnostics/debug paths can live on a different filesystem.
        # Stage there first so publishing that file remains atomic as well.
        fd, name = tempfile.mkstemp(prefix=".assglass-publish-", dir=str(destination.parent))
        staged = Path(name)
        try:
            with os.fdopen(fd, "wb") as sink, source.open("rb") as original:
                shutil.copyfileobj(original, sink)
                sink.flush()
                os.fsync(sink.fileno())
            atomic(staged)
            source.unlink()
        finally:
            if staged.exists():
                staged.unlink()


def _cli_encoder(args):
    result = {}
    for item in args.encoder:
        if "=" not in item:
            raise ValueError("--encoder 需要 KEY=VALUE")
        key, value = item.split("=", 1)
        normalized = normalize_encoder_options({key: None if value == "null" else value})
        if set(result) & set(normalized):
            raise ValueError("重复的 --encoder 参数: " + key)
        result.update(normalized)
    for key in ("crf", "preset", "maxrate", "bufsize"):
        value = getattr(args, key)
        if value is not None:
            if key in result:
                raise ValueError("重复的 CLI 编码参数: " + key)
            result[key] = value
    return result


def snapshot_fonts(fonts_dir, work):
    if not fonts_dir:
        return None, []
    root = Path(fonts_dir).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("fonts_dir 不是目录")
    destination = work / "fonts"
    destination.mkdir()
    records = []
    for source in sorted(root.iterdir()):
        if source.is_file():
            shutil.copyfile(str(source), str(destination / source.name))
            records.append({"name": source.name, "sha256": file_hash(destination / source.name)})
    return destination, records


def run(args):
    from .ass import SourceDocument
    from .config import resolve_config
    from .selection import build_selection_plan, create_backend
    from .contracts import MaskContext, RasterMask
    from .masks import create_builder, merge_masks
    from .native import NativeBudget, libass_info

    started = time.monotonic()
    output = args.output.expanduser().resolve()
    video = args.video.expanduser().resolve(strict=True)
    subtitle = args.subtitle.expanduser().resolve(strict=True)
    protected_inputs = {video, subtitle}
    for item in (args.config, args.sidecar):
        if item is not None:
            protected_inputs.add(item.expanduser().resolve(strict=True))
    if output in protected_inputs:
        raise ValueError("输出不能覆盖输入视频、原 ASS、配置或 sidecar")
    if output.suffix.lower() not in (".mp4", ".mov"):
        raise ValueError("输出使用 .mp4/.mov 以保留精确 CFR 时间戳；暂不支持 MKV")
    if not output.parent.is_dir():
        raise ValueError("输出目录不存在: " + str(output.parent))
    manifest_path = (args.manifest or Path(str(output) + ".manifest.json")).resolve()
    ledger_path = Path(str(output) + ".ledger.jsonl")
    log_path = Path(str(output) + ".ffmpeg.log")
    destinations = [output, manifest_path, ledger_path, log_path]
    if args.debug_mask:
        args.debug_mask = args.debug_mask.resolve()
        destinations.append(args.debug_mask)
    if len(set(destinations)) != len(destinations) or any(p in protected_inputs for p in destinations):
        raise ValueError("输出、manifest、debug mask 与输入路径必须互不相同")
    for path in destinations:
        if not path.parent.is_dir():
            raise ValueError("输出目录不存在: " + str(path.parent))
        if path.exists() and not args.overwrite:
            raise ValueError("目标已存在；如需替换请使用 --overwrite: " + str(path))

    overrides = {}
    if args.fonts_dir is not None:
        overrides["render"] = {"fonts_dir": str(args.fonts_dir.resolve())}
    if args.allow_merged_box is not None or args.backend is not None:
        overrides["selection"] = {}
        if args.allow_merged_box is not None:
            overrides["selection"]["allow_merged_box"] = args.allow_merged_box
        if args.backend is not None:
            overrides["selection"]["backend"] = args.backend
    cfg = resolve_config(args.config, cli_defaults=args.default, blur_sigma=args.blur_sigma,
                         burn_subtitles=args.burn_subtitles, marker_prefix=args.marker_prefix,
                         section_overrides=overrides)
    if args.manifest is None and cfg.render.get("manifest_output"):
        new_manifest = Path(cfg.render["manifest_output"]).resolve()
        if new_manifest in protected_inputs or new_manifest in (output, ledger_path, log_path, args.debug_mask):
            raise ValueError("manifest_output 与另一个输入/输出冲突")
        if new_manifest.exists() and not args.overwrite:
            raise ValueError("manifest_output 已存在: " + str(new_manifest))
        if not new_manifest.parent.is_dir():
            raise ValueError("manifest_output 目录不存在")
        manifest_path = new_manifest
    source = SourceDocument.read(subtitle)
    bottom_styles = [s.name for s in source.styles.values() if s.fields.get("borderstyle") == "3"]
    if bottom_styles:
        log("提示：ASS 含 BorderStyle=3 底板，原样保留：" + ", ".join(bottom_styles))
    selection_plan = build_selection_plan(source, cfg, args.sidecar)
    encoder = resolve_encoder(cfg.encoder, _cli_encoder(args))
    audio_codec = args.audio_codec or cfg.encoder.get("audio_codec", "copy")
    if audio_codec not in ("copy", "aac", "none"):
        raise ValueError("audio_codec 必须为 copy/aac/none")
    caps = capabilities(args.ffmpeg, args.ffprobe)
    log("FFmpeg：" + caps["ffmpeg"])
    log("正在验证视频格式与完整 PTS 时间轴…")
    spec = VideoProbe(caps["ffprobe"]).inspect(video)
    plan = ProcessingProfileRegistry().resolve(spec, cfg.video.get("profile", "auto"))
    if (cfg.render["target_width"], cfg.render["target_height"]) != plan.frame_size:
        raise ValueError("render.target_width/target_height 必须与视频显示尺寸相同；首版不支持 resize")
    source_stat = video.stat()
    source_identity = (source_stat.st_dev, source_stat.st_ino, source_stat.st_size, source_stat.st_mtime_ns)
    manifest = {"schema_version": "assglass-manifest-v1", "version": __version__, "status": "preflight",
                "input": {"video": str(video), "video_size": source_stat.st_size,
                          "video_mtime_ns": source_stat.st_mtime_ns, "stream": spec.stream,
                          "ass": str(subtitle), "ass_sha256": source.sha256, "ass_encoding": "utf-8", "bom": source.bom},
                "tools": caps, "profile": plan.profile_id, "sampler": plan.sampler_id,
                "config": cfg, "encoder": encoder, "audio_codec": audio_codec,
                "output": {"path": str(output), "burn_subtitles": cfg.output.burn_subtitles,
                           "source": cfg.output.source, "burn_status": "enabled" if cfg.output.burn_subtitles else "disabled"}}
    with tempfile.TemporaryDirectory(prefix=".assglass-", dir=str(output.parent)) as directory:
        work = Path(directory)
        (work / "original.ass").write_bytes(source.raw)
        fonts_dir, fonts = snapshot_fonts(cfg.render.get("fonts_dir"), work)
        manifest["fonts"] = {"directory_files": fonts, "system_fonts": "system provider; see renderer observations"}
        ledger = verify_timeline(plan, caps["ffprobe"], work / "ledger.jsonl",
                                 progress=lambda n: log(f"时间轴已检查 {n} 帧"))
        manifest["ledger"] = {"sha256": ledger.sha256, "frames": ledger.count,
                              "time_base": str(ledger.time_base), "history_epoch": 0,
                              "time_conversion": "int64(double(pts) * double(num)/double(den) * 1000), no accumulation"}
        preflight_seconds = time.monotonic() - started
        budget = NativeBudget(cfg.transport["max_in_flight_bytes"])
        weight_encoder = plan.make_weight_encoder(budget)
        profile = SimpleNamespace(frame_size=plan.frame_size, width=spec.width, height=spec.height,
                                  fonts_dir=fonts_dir, native_budget=budget, max_bytes=budget.limit,
                                  profile_id="ffmpeg-ass-mirrored-v1")
        backend = create_backend(cfg.selection["backend"])
        prepared = backend.preflight(source, selection_plan, profile)
        builders = {selection_plan.default_config.mode: create_builder(selection_plan.default_config.mode)}
        builders.update({target.config.mode: create_builder(target.config.mode) for target in selection_plan.targets})
        for target in selection_plan.targets:
            builders[target.config.mode].validate_config(target.config)
        native_info = libass_info()
        manifest["runtime"] = verify_ass_runtime(caps, work, native_info["path"], int(native_info["version_hex"], 16), bool(fonts_dir))
        manifest["native"] = native_info
        manifest["prepared_selection"] = prepared.manifest()
        manifest["analysis_sha256"] = hashlib.sha256(prepared.analysis_data).hexdigest()
        manifest["selection"] = selection_plan.manifest()
        if args.check_only:
            manifest["status"] = "checked"
            manifest["timings"] = {"total_seconds": time.monotonic() - started}
            staged = work / "manifest.json"
            staged.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")
            publish(staged, manifest_path, args.overwrite)
            publish(ledger.path, ledger_path, args.overwrite)
            log(f"预检通过：{ledger.count} 帧；未编码。诊断：{manifest_path}")
            return 0

        if args.debug_mask and ledger.count * plan.frame_bytes > cfg.transport["debug_mask_max_bytes"]:
            raise ValueError("debug mask 超出 debug_mask_max_bytes；提高配置上限或取消 --debug-mask")
        graph = build_graph(plan, cfg.video_blur.blur_sigma, cfg.output.burn_subtitles, bool(fonts_dir))
        temporary_output = work / ("output" + output.suffix.lower())
        argv = build_command(caps, plan, encoder, graph, temporary_output, cfg.runtime,
                             cfg.transport["max_in_flight_frames"], audio_codec)
        manifest["ffmpeg_argv"] = argv
        manifest["filtergraph"] = graph
        session = backend.open(prepared)
        pipeline = None
        debug = None
        mask_digest = hashlib.sha256()
        stage_times = {"render": 0.0, "mask": 0.0, "weights": 0.0, "pipe_wait": 0.0}
        context = MaskContext(plan.frame_size, budget)
        last_report = time.monotonic()
        try:
            pipeline = PipePipeline(argv, work, work / "ffmpeg.log", ledger.count, plan.ticks_per_frame)
            if args.debug_mask:
                debug = (work / "mask.raw").open("wb", buffering=0)
            log(f"开始处理 {ledger.count} 帧；字幕烧录{'开启' if cfg.output.burn_subtitles else '关闭'}。")
            for frame in ledger.frames():
                selection = mask = weight = None
                masks = []
                try:
                    before = time.monotonic()
                    selection = session.render(frame)
                    stage_times["render"] += time.monotonic() - before
                    before = time.monotonic()
                    for group in selection.groups:
                        masks.append(builders[group.effect_config.mode].build(group, group.effect_config, context))
                    if len(masks) == 1:
                        mask = masks.pop()
                    elif masks:
                        mask = merge_masks(masks, context)
                    else:
                        mask = RasterMask()
                    stage_times["mask"] += time.monotonic() - before
                    before = time.monotonic()
                    weight = weight_encoder.encode(mask, frame, plan)
                    stage_times["weights"] += time.monotonic() - before
                    view = weight.buffer
                    try:
                        mask_digest.update(view)
                        if debug:
                            write_all(debug, view)
                        before = time.monotonic()
                        pipeline.write_frame(view, plan.frame_bytes)
                        stage_times["pipe_wait"] += time.monotonic() - before
                    finally:
                        del view
                finally:
                    if weight is not None:
                        weight.release()
                    if mask is not None:
                        mask.release()
                    for item in masks:
                        item.release()
                    if selection is not None:
                        selection.release()
                now = time.monotonic()
                if now - last_report >= 2:
                    log(f"已发送 {frame.frame_index + 1}/{ledger.count} 帧；应用缓冲峰值 {budget.peak / 1048576:.1f} MiB")
                    last_report = now
            manifest["transport"] = pipeline.finish()
            if debug:
                debug.close()
                debug = None
            current = video.stat()
            if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != source_identity:
                raise ValueError("处理期间输入视频被修改，拒绝发布")
            manifest["verification"] = verify_output(temporary_output, plan, ledger.count, caps["ffprobe"], encoder["options"]["level"])
            manifest["mask_sha256"] = mask_digest.hexdigest()
            manifest["helper_render_log"] = session.logs
            manifest["memory"] = {"max_in_flight_bytes": budget.limit, "native_peak_bytes": budget.peak,
                                  "application_frames_in_flight": 1, "cache_bytes": 0,
                                  "excludes": "FFmpeg internal queues/encoder, OS pipe, libass/font caches, ASS metadata"}
            manifest["timings"] = {"preflight_seconds": preflight_seconds, "total_seconds": time.monotonic() - started,
                                   "stages_seconds": stage_times}
            manifest["status"] = "complete"
            staged = work / "manifest.json"
            staged.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")
            # No video is published until all count/PTS/pixel-format checks pass.
            publish(ledger.path, ledger_path, args.overwrite)
            publish(work / "ffmpeg.log", log_path, args.overwrite)
            if args.debug_mask:
                publish(work / "mask.raw", args.debug_mask, args.overwrite)
            publish(staged, manifest_path, args.overwrite)
            publish(temporary_output, output, args.overwrite)
        finally:
            if debug:
                debug.close()
            if pipeline is not None:
                pipeline.close()
            session.close()
    log(f"完成：{output}\n诊断：{manifest_path}")
    return 0


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        log("已取消；未发布未完成的视频。")
        return 130
    except (ValueError, RuntimeError, OSError) as exc:
        log("错误：" + str(exc))
        return 1
