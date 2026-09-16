from copy import deepcopy
from fractions import Fraction
from pathlib import Path

import pytest

from assglass.video import (PROFILE_ID, PROFILE_5994_ID, ProcessingProfileRegistry, VideoSpec,
                           verify_output, verify_timeline)


def spec(**changes):
    values = {"index": 0, "codec_name": "h264", "width": 1920, "height": 1080,
              "coded_height": 1088, "pix_fmt": "yuv420p", "sample_aspect_ratio": "1:1",
              "field_order": "progressive", "color_range": "tv", "color_space": "bt709",
              "color_primaries": "bt709", "color_transfer": "bt709", "chroma_location": "left",
              "start_pts": 0, "time_base": "1/90000", "r_frame_rate": "60/1", "avg_frame_rate": "60/1"}
    values.update(changes)
    return VideoSpec(Path("source.mp4"), values, {}, ())


@pytest.mark.parametrize("change", [
    {"pix_fmt": "yuv420p10le"}, {"chroma_location": "center"}, {"chroma_location": None},
    {"color_transfer": "smpte2084"}, {"color_range": "pc"}, {"start_pts": 1},
    {"width": 1280}, {"avg_frame_rate": "30000/1001"}, {"sample_aspect_ratio": "2:1"},
    {"field_order": "tt"}, {"side_data_list": [{"side_data_type": "Display Matrix", "rotation": 0}]},
])
def test_reject_unknown_or_unsupported(change):
    with pytest.raises(ValueError):
        ProcessingProfileRegistry().resolve(spec(**change))


def test_coded_height_not_used_for_weights():
    plan = ProcessingProfileRegistry().resolve(spec())
    assert plan.frame_size == (1920, 1080)
    assert plan.frame_bytes == 3110400
    assert plan.ticks_per_frame == 1500


def frame(pts):
    stream = spec().stream
    values = {key: str(stream[key]) for key in ("width", "height", "pix_fmt", "sample_aspect_ratio",
              "color_range", "color_space", "color_primaries", "color_transfer", "chroma_location")}
    values.update(pts=str(pts), pkt_duration="1500", interlaced_frame="0")
    return values


def test_full_timeline_not_just_fps(monkeypatch, tmp_path):
    monkeypatch.setattr("assglass.video.iter_frames", lambda *args: iter([frame(0), frame(1500), frame(3100)]))
    with pytest.raises(ValueError, match="PTS"):
        verify_timeline(ProcessingProfileRegistry().resolve(spec()), "ffprobe", tmp_path / "ledger")


def test_frame_format_change_rejected(monkeypatch, tmp_path):
    changed = frame(1500)
    changed["color_range"] = "pc"
    monkeypatch.setattr("assglass.video.iter_frames", lambda *args: iter([frame(0), changed]))
    with pytest.raises(ValueError, match="color_range"):
        verify_timeline(ProcessingProfileRegistry().resolve(spec()), "ffprobe", tmp_path / "ledger")


def test_ledger_exact_times_and_hash(monkeypatch, tmp_path):
    monkeypatch.setattr("assglass.video.iter_frames", lambda *args: iter([frame(k * 1500) for k in range(6)]))
    ledger = verify_timeline(ProcessingProfileRegistry().resolve(spec()), "ffprobe", tmp_path / "ledger")
    assert ledger.count == 6
    assert [f.pts * f.time_base for f in ledger.frames()] == [Fraction(k, 60) for k in range(6)]


def test_video_registry_extension_is_explicit():
    registry = ProcessingProfileRegistry()
    registry.register("test-other", lambda video: (video.width, "other-sampler"))
    assert registry.resolve(spec(width=640), "test-other") == (640, "other-sampler")
    with pytest.raises(ValueError):
        registry.resolve(spec(width=640))


def spec_5994(**changes):
    values = {"time_base": "1/60000", "r_frame_rate": "60000/1001", "avg_frame_rate": "60000/1001"}
    values.update(changes)
    return spec(**values)


def frames_5994(count):
    result = [frame(k * 1001) for k in range(count)]
    for item in result:
        item["pkt_duration"] = "1001"
    return result


def test_5994_profile_preserves_exact_pts(monkeypatch, tmp_path):
    plan = ProcessingProfileRegistry().resolve(spec_5994())
    assert plan.profile_id == PROFILE_5994_ID
    assert plan.frame_rate == Fraction(60000, 1001)
    assert plan.ticks_per_frame == 1001
    monkeypatch.setattr("assglass.video.iter_frames", lambda *args: iter(frames_5994(120)))
    ledger = verify_timeline(plan, "ffprobe", tmp_path / "ledger")
    assert [f.pts * f.time_base for f in ledger.frames()] == [Fraction(k * 1001, 60000) for k in range(120)]
    monkeypatch.setattr("assglass.video.VideoProbe.inspect", lambda *args: spec_5994())
    assert verify_output("out.mp4", plan, 120, "ffprobe")["frames"] == 120


@pytest.mark.parametrize("changes", [
    {"time_base": "1/90000"}, {"r_frame_rate": "60/1"}, {"avg_frame_rate": "60/1"},
])
def test_5994_profile_rejects_inexact_clock_or_mismatched_rates(changes):
    with pytest.raises(ValueError):
        ProcessingProfileRegistry().resolve(spec_5994(**changes), PROFILE_5994_ID)


def test_5994_explicit_60_profile_rejected():
    with pytest.raises(ValueError):
        ProcessingProfileRegistry().resolve(spec_5994(), PROFILE_ID)


def test_5994_output_rejects_changed_fps(monkeypatch):
    plan = ProcessingProfileRegistry().resolve(spec_5994())
    monkeypatch.setattr("assglass.video.VideoProbe.inspect", lambda *args: spec())
    with pytest.raises(ValueError, match="frame_rate"):
        verify_output("out.mp4", plan, 120, "ffprobe")


def test_5994_output_rejects_shifted_pts(monkeypatch):
    plan = ProcessingProfileRegistry().resolve(spec_5994())
    values = frames_5994(3)
    values[2]["pts"] = "2003"
    monkeypatch.setattr("assglass.video.VideoProbe.inspect", lambda *args: spec_5994())
    monkeypatch.setattr("assglass.video.iter_frames", lambda *args: iter(values))
    with pytest.raises(ValueError, match="时间戳"):
        verify_output("out.mp4", plan, 3, "ffprobe")
