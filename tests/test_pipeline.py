"""Real CLI/FFmpeg tests. Fixtures deliberately use the first supported profile."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

pytestmark = pytest.mark.integration


ROOT = Path(__file__).resolve().parents[1]
WIDTH, HEIGHT, FRAMES = 1920, 1080, 12
Y_BYTES = WIDTH * HEIGHT
UV_BYTES = Y_BYTES // 4
FRAME_BYTES = Y_BYTES + 2 * UV_BYTES
PRIVATE_TOOLS = ROOT / ".tools" / "ffmpeg-6.1.1"
FFMPEG = os.environ.get("ASSGLASS_TEST_FFMPEG", str(PRIVATE_TOOLS / "ffmpeg") if (PRIVATE_TOOLS / "ffmpeg").is_file() else "ffmpeg")
FFPROBE = os.environ.get("ASSGLASS_TEST_FFPROBE", str(PRIVATE_TOOLS / "ffprobe") if (PRIVATE_TOOLS / "ffprobe").is_file() else "ffprobe")

HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,64,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,0,2,40,40,80,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def run(argv, timeout=120):
    return subprocess.run(
        [str(item) for item in argv], cwd=str(ROOT), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=timeout,
    )


def require_success(result):
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")


@pytest.fixture(scope="module")
def ffmpeg():
    binary = shutil.which(FFMPEG)
    if not binary or not shutil.which(FFPROBE):
        pytest.skip("integration test requires ffmpeg and ffprobe")
    return binary


@pytest.fixture(scope="module")
def native_core():
    from assglass.native import library
    return library()


@pytest.fixture(scope="module")
def source_video(tmp_path_factory, ffmpeg):
    directory = tmp_path_factory.mktemp("real_ffmpeg")
    path = directory / "source.mp4"
    result = run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
        "-i", "testsrc2=size=1920x1080:rate=60", "-frames:v", str(FRAMES),
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-color_range", "tv", "-colorspace", "bt709",
        "-color_primaries", "bt709", "-color_trc", "bt709",
        "-chroma_sample_location", "left", "-video_track_timescale", "90000", path,
    ])
    require_success(result)
    return path


def write_ass(path, marked=True):
    events = [
        # The explicit opaque override exercises normalization of an ordinary
        # unmarked line; it must remain visible in the ORIGINAL burn-in.
        r"Dialogue: 0,0:00:00.00,0:00:00.20,Default,narrator,0,0,0,,{\alpha&H00&\an7\pos(100,80)}UNMARKED VISIBLE",
    ]
    if marked:
        events += [
            # Arbitrary suffix is intentional: Actor has startswith semantics.
            r"Dialogue: 0,0:00:00.05,0:00:00.10,Default,bgblur_speaker,0,0,0,,BLUR TARGET",
            r"Dialogue: 0,0:00:00.10,0:00:00.15,Default,bgblur,0,0,0,,{\alpha&HFF&}BLUR TARGET",
            r"Dialogue: 0,0:00:00.15,0:00:00.20,Default,bgblur,0,0,0,,BLUR TARGET",
        ]
    path.write_text(HEADER + "\n".join(events) + "\n", encoding="utf-8")
    return path


def execute(source, ass, output, *extra):
    return run([
        sys.executable, "-m", "assglass", source, ass, "-o", output,
        "--ffmpeg", FFMPEG, "--ffprobe", FFPROBE,
        "--preset", "ultrafast", "--encoder", "subq=0", "--encoder", "me_range=16",
        *extra,
    ])


def read_masks(path):
    """Keep memory bounded while checking the actual three-plane transport."""
    assert path.stat().st_size == FRAMES * FRAME_BYTES
    active = []
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for index in range(FRAMES):
            frame = stream.read(FRAME_BYTES)
            assert len(frame) == FRAME_BYTES
            digest.update(frame)
            y, u, v = frame[:Y_BYTES], frame[Y_BYTES:Y_BYTES + UV_BYTES], frame[-UV_BYTES:]
            assert u == v
            assert bool(any(y)) == bool(any(u))
            # There must be no source contribution from the upper unmarked line.
            assert not any(y[:WIDTH * 250])
            if any(y):
                active.append(index)
        assert stream.read(1) == b""
    return active, digest.hexdigest()


def probe(path):
    result = run([
        shutil.which(FFPROBE), "-v", "error", "-select_streams", "v:0",
        "-show_streams", "-show_frames", "-of", "json", path,
    ])
    require_success(result)
    return json.loads(result.stdout)


def assert_timeline(path):
    from fractions import Fraction
    data = probe(path)
    stream = data["streams"][0]
    assert stream["width"] == WIDTH and stream["height"] == HEIGHT
    assert stream["pix_fmt"] == "yuv420p"
    assert stream["color_range"] == "tv"
    assert stream["color_space"] == "bt709"
    assert stream["color_primaries"] == "bt709"
    assert stream["color_transfer"] == "bt709"
    assert stream["chroma_location"] == "left"
    time_base = Fraction(stream["time_base"])
    pts = [Fraction(int(frame["best_effort_timestamp"])) * time_base for frame in data["frames"]]
    assert pts == [Fraction(index, 60) for index in range(FRAMES)]


def cropped_first_frame(ffmpeg, path):
    result = run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-i", path,
        "-vf", "crop=900:180:50:30", "-frames:v", "1", "-pix_fmt", "gray",
        "-f", "rawvideo", "pipe:1",
    ])
    require_success(result)
    assert len(result.stdout) == 900 * 180
    return result.stdout


def test_real_pipeline_burn_switch_keeps_masks_and_frame_alignment(
    tmp_path, source_video, ffmpeg, native_core,
):
    # Spaces and punctuation force paths to go through the real path/graph adapter.
    ass = write_ass(tmp_path / "subtitle 空格.ass")
    original = ass.read_bytes()
    outcomes = {}
    for burn in (False, True):
        stem = "burn" if burn else "background"
        output, mask, manifest = [tmp_path / (stem + suffix) for suffix in (".mp4", ".raw", ".json")]
        result = execute(
            source_video, ass, output, "--debug-mask", mask, "--manifest", manifest,
            "--burn-subtitles" if burn else "--no-burn-subtitles",
        )
        require_success(result)
        active, digest = read_masks(mask)
        assert active == [3, 4, 5, 9, 10, 11], "target boundaries or fully transparent frames shifted"
        assert_timeline(output)
        assert manifest.is_file()
        report = json.loads(manifest.read_text())
        assert report["status"] == "complete"
        assert report["mask_sha256"] == digest
        assert report["ledger"]["frames"] == FRAMES
        assert report["transport"]["sent_frames"] == FRAMES
        assert report["transport"]["observed_frames"] == FRAMES
        assert report["transport"]["sent_bytes"] == FRAMES * FRAME_BYTES
        assert report["filtergraph"].count("gblur=") == 1
        assert report["filtergraph"].count("maskedmerge=") == 1
        assert report["filtergraph"].count(",ass=filename=original.ass") == int(burn)
        assert report["ffmpeg_argv"].count("-c:v") == 1
        assert report["output"]["burn_subtitles"] is burn
        assert report["memory"]["native_peak_bytes"] <= report["memory"]["max_in_flight_bytes"]
        outcomes[burn] = (output, digest, report)
    assert ass.read_bytes() == original
    assert outcomes[False][1] == outcomes[True][1]
    assert outcomes[False][2]["ledger"] == outcomes[True][2]["ledger"]
    # First frame has no selected event. The unmarked original subtitle still
    # appears in burn mode, whereas no-burn must not render the analysis copy.
    plain = cropped_first_frame(ffmpeg, outcomes[False][0])
    burned = cropped_first_frame(ffmpeg, outcomes[True][0])
    assert sum(abs(a - b) > 32 for a, b in zip(plain, burned)) > 100


def test_no_markers_sends_zero_all_planes_but_still_burns_original(
    tmp_path, source_video, ffmpeg, native_core,
):
    ass = write_ass(tmp_path / "no-markers.ass", marked=False)
    output = tmp_path / "no-markers.mp4"
    mask = tmp_path / "no-markers.raw"
    result = execute(source_video, ass, output, "--debug-mask", mask)
    require_success(result)
    active, _ = read_masks(mask)
    assert active == []
    assert_timeline(output)
    original = cropped_first_frame(ffmpeg, source_video)
    burned = cropped_first_frame(ffmpeg, output)
    assert sum(abs(a - b) > 32 for a, b in zip(original, burned)) > 100


def test_invalid_per_line_sigma_fails_without_publishing_output(
    tmp_path, source_video, native_core,
):
    ass = tmp_path / "invalid.ass"
    ass.write_text(
        HEADER + "Dialogue: 0,0:00:00.00,0:00:00.20,Default,bgblur{blur_sigma=18},0,0,0,,bad\n",
        encoding="utf-8",
    )
    output = tmp_path / "must-not-exist.mp4"
    result = execute(source_video, ass, output)
    assert result.returncode != 0
    assert not output.exists()
    assert b"blur_sigma" in result.stderr


def test_existing_output_is_not_silently_replaced(tmp_path, source_video, native_core):
    ass = write_ass(tmp_path / "valid.ass")
    output = tmp_path / "existing.mp4"
    sentinel = b"existing user output, preserve until explicitly overwritten"
    output.write_bytes(sentinel)
    result = execute(source_video, ass, output)
    assert result.returncode != 0
    assert output.read_bytes() == sentinel


def test_default_encoder_options_are_reported_by_real_x264(tmp_path, source_video, native_core):
    ass = write_ass(tmp_path / "default-encoder.ass", marked=False)
    output = tmp_path / "default-encoder.mp4"
    result = run([
        sys.executable, "-m", "assglass", source_video, ass, "-o", output,
        "--ffmpeg", FFMPEG, "--ffprobe", FFPROBE, "--no-burn-subtitles",
    ])
    require_success(result)
    report = json.loads(Path(str(output) + ".manifest.json").read_text())
    options = report["encoder"]["options"]
    assert options["preset"] == "veryslow"
    assert report["verification"]["stream"]["level"] == 42
    # This build reports only profile/level in stderr. Read x264's ASCII
    # unregistered SEI from the actual output file, not the source SEI printed
    # by pre-encode showinfo or the requested command line.
    reports = [record.decode("ascii") for record in re.findall(
        rb"x264 - core [^\x00]*?options: [^\x00]+", output.read_bytes(),
    )]
    effective_options = (
        "rc=crf", "crf=18.0", "deblock=1:-1:-1", "open_gop=0", "aq=3:0.80",
        "me_range=32", "subme=10", "vbv_maxrate=20000", "vbv_bufsize=10000",
    )
    assert any(all(option in record for option in effective_options) for record in reports), reports
    assert_timeline(output)
