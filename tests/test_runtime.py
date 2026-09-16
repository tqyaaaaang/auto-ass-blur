import subprocess

import pytest

from assglass.ffmpeg import build_graph
from assglass.runtime import validate_weight_transport
from assglass.video import ProcessingProfileRegistry, VideoProbe
from test_pipeline import ffmpeg, source_video, FFPROBE, require_success, run


pytestmark = pytest.mark.integration


def test_weight_endpoints_on_installed_build(ffmpeg):
    assert validate_weight_transport(ffmpeg).startswith("passed")


@pytest.mark.parametrize("weight", [0, 255])
def test_production_graph_before_encoding_exact_endpoints(source_video, ffmpeg, weight):
    spec = VideoProbe(FFPROBE).inspect(source_video)
    plan = ProcessingProfileRegistry().resolve(spec)
    graph = build_graph(plan, 18.0, False, observe=False)
    raw_size = plan.frame_bytes
    argv = [ffmpeg, "-v", "error", "-nostdin", "-filter_complex_threads", "1", "-i", str(source_video),
            "-f", "rawvideo", "-pixel_format", "yuv420p", "-video_size", "1920x1080", "-framerate", "60",
            "-i", "pipe:0", "-filter_complex", graph, "-map", "[outv]", "-frames:v", "1", "-f", "rawvideo", "pipe:1"]
    result = subprocess.run(argv, input=bytes([weight]) * raw_size, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    require_success(result)
    reference_argv = [ffmpeg, "-v", "error", "-i", str(source_video)]
    if weight == 255:
        reference_argv += ["-vf", "gblur=sigma=18:steps=2:planes=7"]
    reference_argv += ["-frames:v", "1", "-f", "rawvideo", "pipe:1"]
    reference = run(reference_argv)
    require_success(reference)
    assert len(result.stdout) == raw_size
    assert result.stdout == reference.stdout
