"""Strict video profiles and streaming, exact rational frame validation."""
from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Iterator

PROFILE_ID = "sdr-bt709-yuv420p8-left-1080p60-v1"
PROFILE_5994_ID = "sdr-bt709-yuv420p8-left-1080p5994-v1"
FRAME_FIELDS = ("pts", "best_effort_timestamp", "pkt_duration", "width", "height",
                "pix_fmt", "sample_aspect_ratio", "interlaced_frame", "color_range",
                "color_space", "color_primaries", "color_transfer", "chroma_location")


def run_json(argv):
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode:
        raise ValueError("视频探测失败：" + result.stderr[-6000:])
    return json.loads(result.stdout)


@dataclass(frozen=True)
class VideoSpec:
    path: Path
    stream: dict
    format: dict
    audio: tuple

    @property
    def width(self):
        return self.stream["width"]

    @property
    def height(self):
        return self.stream["height"]

    @property
    def time_base(self):
        return Fraction(self.stream["time_base"])


@dataclass(frozen=True)
class ProcessingPlan:
    spec: VideoSpec
    profile_id: str = PROFILE_ID
    sampler_id: str = "left-tent2-v1"
    pix_fmt: str = "yuv420p"
    frame_rate: Fraction = Fraction(60)

    @property
    def frame_size(self):
        return self.spec.width, self.spec.height

    @property
    def ticks_per_frame(self):
        value = 1 / self.frame_rate / self.spec.time_base
        if value.denominator != 1:
            raise ValueError(f"输入 time_base 不能精确表示 {self.frame_rate} fps")
        return value.numerator

    @property
    def frame_bytes(self):
        return self.spec.width * self.spec.height * 3 // 2

    def make_weight_encoder(self, budget=None):
        from .weights import YUV420PLeftWeightEncoder
        return YUV420PLeftWeightEncoder(budget)


class VideoProbe:
    def __init__(self, ffprobe="ffprobe"):
        self.ffprobe = ffprobe

    def inspect(self, path):
        path = Path(path).resolve(strict=True)
        data = run_json([self.ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)])
        videos = [s for s in data["streams"] if s["codec_type"] == "video"
                  and not s.get("disposition", {}).get("attached_pic")]
        if not videos:
            raise ValueError("输入没有视频流")
        return VideoSpec(path, videos[0], data.get("format", {}),
                         tuple(s for s in data["streams"] if s["codec_type"] == "audio"))


class ProcessingProfileRegistry:
    """Explicit registry; adding a format never changes subtitle geometry."""
    def __init__(self):
        self.profiles = {PROFILE_ID: self._initial, PROFILE_5994_ID: self._5994}

    def register(self, name, resolver):
        if name in self.profiles:
            raise ValueError("重复 video profile: " + name)
        self.profiles[name] = resolver

    def resolve(self, spec, requested="auto"):
        name = requested
        if requested == "auto":
            try:
                rate = Fraction(spec.stream.get("avg_frame_rate", "0/1"))
            except (ValueError, ZeroDivisionError):
                rate = None
            name = PROFILE_5994_ID if rate == Fraction(60000, 1001) else PROFILE_ID
        if name not in self.profiles:
            raise ValueError("未支持 video profile: " + name)
        return self.profiles[name](spec)

    @staticmethod
    def _initial(spec):
        return ProcessingProfileRegistry._resolve_sdr(spec, PROFILE_ID, Fraction(60))

    @staticmethod
    def _5994(spec):
        return ProcessingProfileRegistry._resolve_sdr(spec, PROFILE_5994_ID, Fraction(60000, 1001))

    @staticmethod
    def _resolve_sdr(spec, profile_id, frame_rate):
        expected = {"codec_name": "h264", "width": 1920, "height": 1080,
                    "pix_fmt": "yuv420p", "sample_aspect_ratio": "1:1",
                    "field_order": "progressive", "color_range": "tv",
                    "color_space": "bt709", "color_primaries": "bt709",
                    "color_transfer": "bt709", "chroma_location": "left", "start_pts": 0}
        differences = [f"{k}: 期望 {v!r}，实际 {spec.stream.get(k, 'unknown')!r}"
                       for k, v in expected.items() if spec.stream.get(k) != v]
        for key in ("r_frame_rate", "avg_frame_rate"):
            try:
                valid = Fraction(spec.stream.get(key, "0/1")) == frame_rate
            except (ValueError, ZeroDivisionError):
                valid = False
            if not valid:
                differences.append(f"{key}: 期望 {frame_rate}，实际 {spec.stream.get(key, 'unknown')}")
        if spec.stream.get("tags", {}).get("rotate") not in (None, "0") or any(
                s.get("side_data_type") == "Display Matrix" for s in spec.stream.get("side_data_list", [])):
            differences.append("首版不支持 rotation/display matrix")
        if differences:
            raise ValueError("视频不符合首版 profile：\n" + "\n".join(differences))
        plan = ProcessingPlan(spec, profile_id=profile_id, frame_rate=frame_rate)
        plan.ticks_per_frame
        return plan


def iter_frames(ffprobe, path, stream_index=0) -> Iterator[dict]:
    argv = [ffprobe, "-v", "error", "-select_streams", str(stream_index), "-show_frames",
            "-show_entries", "frame=" + ",".join(FRAME_FIELDS), "-of", "compact=p=0:nk=0", str(path)]
    # stderr is file-backed, so decode failure cannot block stdout draining.
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=errors, text=True)
        try:
            for line in process.stdout:
                values = dict(item.split("=", 1) for item in line.rstrip().split("|") if "=" in item)
                if "width" in values:
                    yield values
            if process.wait():
                errors.seek(0)
                raise ValueError("逐帧探测失败：" + errors.read()[-6000:].decode("utf-8", "replace"))
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


@dataclass(frozen=True)
class Ledger:
    path: Path
    count: int
    sha256: str
    time_base: Fraction
    frame_size: tuple

    def frames(self):
        from .contracts import FrameRequest
        with self.path.open() as source:
            for line in source:
                row = json.loads(line)
                yield FrameRequest(row["frame_index"], row["pts"], self.time_base, self.frame_size)


def verify_timeline(plan, ffprobe, ledger_path, progress=None):
    spec = plan.spec
    digest = hashlib.sha256()
    count = 0
    expected = {"width": str(spec.width), "height": str(spec.height), "pix_fmt": "yuv420p",
                "sample_aspect_ratio": "1:1", "interlaced_frame": "0", "color_range": "tv",
                "color_space": "bt709", "color_primaries": "bt709", "color_transfer": "bt709",
                "chroma_location": "left"}
    with Path(ledger_path).open("wb") as sink:
        for index, frame in enumerate(iter_frames(ffprobe, spec.path, spec.stream["index"])):
            for key, value in expected.items():
                if frame.get(key) != value:
                    raise ValueError(f"第 {index} 帧 {key} 不符合 profile: {frame.get(key, 'unknown')!r}")
            try:
                pts = int(frame["pts"])
            except (KeyError, ValueError):
                raise ValueError(f"第 {index} 帧缺少有效 PTS") from None
            if pts != index * plan.ticks_per_frame:
                raise ValueError(f"第 {index} 帧 PTS={pts}，期望 {index * plan.ticks_per_frame}；拒绝 VFR/非零起点/缺帧")
            duration = frame.get("pkt_duration")
            if duration not in (None, "N/A", "0") and int(duration) != plan.ticks_per_frame:
                raise ValueError(f"第 {index} 帧 duration={duration} 不符合 {plan.frame_rate} fps")
            row = (json.dumps({"frame_index": index, "pts": pts, "duration": plan.ticks_per_frame}, separators=(",", ":")) + "\n").encode()
            sink.write(row)
            digest.update(row)
            count += 1
            if progress and count % 600 == 0:
                progress(count)
    if not count:
        raise ValueError("视频没有可解码帧")
    return Ledger(Path(ledger_path), count, digest.hexdigest(), spec.time_base, plan.frame_size)


def verify_output(path, plan, expected_count, ffprobe, expected_level=None):
    spec = VideoProbe(ffprobe).inspect(path)
    for key in ("width", "height", "pix_fmt", "sample_aspect_ratio", "color_range", "color_space",
                "color_primaries", "color_transfer", "chroma_location"):
        if spec.stream.get(key) != plan.spec.stream.get(key):
            raise ValueError(f"输出 {key} 改变: {spec.stream.get(key)}")
    if spec.stream.get("codec_name") != "h264":
        raise ValueError("输出编码不是 h264")
    for key in ("r_frame_rate", "avg_frame_rate"):
        try:
            valid = Fraction(spec.stream.get(key, "0/1")) == plan.frame_rate
        except (ValueError, ZeroDivisionError):
            valid = False
        if not valid:
            raise ValueError(f"输出 {key} 改变: {spec.stream.get(key)}")
    if expected_level is not None and spec.stream.get("level") != int(float(expected_level) * 10):
        raise ValueError(f"输出 H.264 level 不符: {spec.stream.get('level')}")
    count = 0
    for index, frame in enumerate(iter_frames(ffprobe, path, spec.stream["index"])):
        if "pts" not in frame or int(frame["pts"]) * spec.time_base != index / plan.frame_rate:
            raise ValueError(f"输出第 {index} 帧时间戳不匹配")
        count += 1
    if count != expected_count:
        raise ValueError(f"输出帧数 {count} 与 mask/ledger {expected_count} 不符")
    return {"frames": count, "stream": spec.stream}
