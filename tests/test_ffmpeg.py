import io
from pathlib import Path

import pytest

from assglass.ffmpeg import build_command, build_graph, encoder_argv, resolve_encoder, write_all
from assglass.cli import parser, publish
from assglass.video import ProcessingProfileRegistry
from test_video import spec, spec_5994


def test_default_encoder_values_and_negative_deblock():
    argv = encoder_argv(resolve_encoder({}))
    assert argv[argv.index("-deblock") + 1] == "-1:-1"
    for flag, expected in (("-crf", "18"), ("-preset", "veryslow"), ("-level:v", "4.2"),
                           ("-flags:v", "cgop"), ("-aq-mode", "3"), ("-b:v", "10M")):
        assert argv[argv.index(flag) + 1] == expected


def test_encoder_precedence_null_abr_and_injection():
    enc = resolve_encoder({"options": {"crf": 20}}, {"crf": 22, "deblock": None})
    assert enc["options"]["crf"] == 22
    assert "-deblock" not in encoder_argv(enc)
    assert resolve_encoder({"rate_control": "abr", "options": {"crf": None}})["rate_control"] == "abr"
    for section in ({"rate_control": "abr"}, {"options": {"vf": "null"}},
                    {"options": {"r": 30}}, {"options": {"vb": "10M", "b:v": "10M"}}):
        with pytest.raises(ValueError):
            resolve_encoder(section)


def test_single_blur_and_only_final_ass_changes():
    plan = ProcessingProfileRegistry().resolve(spec())
    on = build_graph(plan, 20, True)
    off = build_graph(plan, 20, False)
    assert on.count("gblur=") == 1
    assert on == off.replace("[outv]", ",ass=filename=original.ass[outv]")
    assert "setpts=N*1500" in off


@pytest.mark.parametrize("video, rate, ticks, timescale", [
    (spec(), "60", 1500, "90000"), (spec_5994(), "60000/1001", 1001, "60000"),
])
def test_command_preserves_exact_profile_clock(video, rate, ticks, timescale):
    plan = ProcessingProfileRegistry().resolve(video)
    graph = build_graph(plan, 20, False)
    assert f"setpts=N*{ticks}" in graph
    argv = build_command({"ffmpeg": "ffmpeg", "fps_mode": True}, plan, resolve_encoder({}),
                         graph, "out.mp4", {"ffmpeg_threads": 1, "ffmpeg_filter_threads": 1})
    assert argv[argv.index("-framerate") + 1] == rate
    assert argv[argv.index("-video_track_timescale") + 1] == timescale
    assert argv[argv.index("-enc_time_base") + 1] == str(video.time_base)
    assert argv[argv.index("-fps_mode:v") + 1] == "passthrough"
    assert "-r" not in argv


@pytest.mark.integration
def test_real_5994_pipeline_preserves_frame_timestamps(tmp_path):
    import shutil
    from fractions import Fraction
    from test_pipeline import FFMPEG, FFPROBE, execute, probe, require_success, run, write_ass
    if not shutil.which(FFMPEG) or not shutil.which(FFPROBE):
        pytest.skip("integration test requires ffmpeg and ffprobe")
    source, output = tmp_path / "source-5994.mp4", tmp_path / "out-5994.mp4"
    require_success(run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
        "-i", "testsrc2=size=1920x1080:rate=60000/1001", "-frames:v", "12",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-color_range", "tv", "-colorspace", "bt709",
        "-color_primaries", "bt709", "-color_trc", "bt709",
        "-chroma_sample_location", "left", "-video_track_timescale", "60000", source,
    ]))
    ass = write_ass(tmp_path / "marked.ass")
    require_success(execute(source, ass, output))
    for path in (source, output):
        data = probe(path)
        stream = data["streams"][0]
        assert Fraction(stream["avg_frame_rate"]) == Fraction(60000, 1001)
        time_base = Fraction(stream["time_base"])
        assert [int(f["pts"]) * time_base for f in data["frames"]] == [
            Fraction(index * 1001, 60000) for index in range(12)
        ]


def test_burn_cli_tristate_and_conflict():
    p = parser()
    base = ["in.mp4", "sub.ass", "-o", "out.mp4"]
    assert p.parse_args(base).burn_subtitles is None
    assert p.parse_args(base + ["--no-burn-subtitles"]).burn_subtitles is False
    with pytest.raises(SystemExit):
        p.parse_args(base + ["--burn-subtitles", "--no-burn-subtitles"])


def test_write_all_short_write_eintr_and_broken_pipe():
    class ShortSink:
        def __init__(self):
            self.calls = 0
            self.data = bytearray()
        def write(self, data):
            self.calls += 1
            if self.calls == 1:
                raise InterruptedError()
            size = min(3, len(data))
            self.data.extend(data[:size])
            return size
    sink = ShortSink()
    assert write_all(sink, b"12345678") == 8
    assert sink.data == b"12345678"
    class Closed:
        def write(self, _):
            return 0
    with pytest.raises(BrokenPipeError):
        write_all(Closed(), b"x")


def test_atomic_no_clobber(tmp_path):
    source, target = tmp_path / "partial", tmp_path / "target"
    source.write_bytes(b"new")
    target.write_bytes(b"old")
    with pytest.raises(FileExistsError):
        publish(source, target, False)
    assert target.read_bytes() == b"old"
    publish(source, target, True)
    assert target.read_bytes() == b"new"


def test_cross_device_diagnostic_publish(tmp_path, monkeypatch):
    import errno
    import os
    original_link = os.link
    source, target = tmp_path / "partial", tmp_path / "diagnostic"
    source.write_bytes(b"large diagnostic payload")
    def cross_device_once(src, dst):
        if Path(src) == source:
            raise OSError(errno.EXDEV, "cross device")
        return original_link(src, dst)
    monkeypatch.setattr(os, "link", cross_device_once)
    publish(source, target, False)
    assert target.read_bytes() == b"large diagnostic payload"
    assert not source.exists()
    assert not list(tmp_path.glob(".assglass-publish-*"))


def test_diagnostics_cannot_overwrite_config_input(tmp_path):
    from assglass.cli import run
    video, ass, config = (tmp_path / x for x in ("input.mp4", "input.ass", "config.json"))
    for path in (video, ass, config):
        path.write_text("untouched")
    args = parser().parse_args([str(video), str(ass), "-o", str(tmp_path / "out.mp4"),
                              "--config", str(config), "--manifest", str(config), "--overwrite"])
    with pytest.raises(ValueError, match="路径"):
        run(args)
    assert config.read_text() == "untouched"


def test_activity_graph_keeps_reference_default_and_names_safe_targets():
    from assglass.ffmpeg import ACTIVITY_BLUR_TARGET, ACTIVITY_MERGE_TARGET
    plan = ProcessingProfileRegistry().resolve(spec())
    reference = build_graph(plan, 20, False)
    graph = build_graph(plan, 20, False, activity_commands='activity.cmd')
    assert 'sendcmd=' not in reference and 'enable=' not in reference
    assert graph.index('sendcmd=f=activity.cmd') < graph.index('split=2')
    assert ACTIVITY_BLUR_TARGET + '=sigma=20:steps=2:planes=7:enable=0' in graph
    assert ACTIVITY_MERGE_TARGET + '=planes=7:enable=0' in graph
    assert graph.count('setpts=') == reference.count('setpts=') == 1
    fallback = build_graph(plan, 20, False, activity_commands='activity.cmd', activity_merge=False)
    assert 'gblur@assglass_blur' in fallback and 'maskedmerge=planes=7[glass]' in fallback
    for unsafe in ('/tmp/activity.cmd', '../activity.cmd', 'activity.cmd,select=0'):
        with pytest.raises(ValueError, match='basename'):
            build_graph(plan, 20, False, activity_commands=unsafe)


def test_activity_capabilities_require_sendcmd_and_timeline(monkeypatch):
    from assglass.ffmpeg import capabilities
    monkeypatch.setattr('assglass.ffmpeg.executable', lambda path: path)
    flags = {'gblur': 'TSC', 'maskedmerge': 'TSC', 'sendcmd': '...'}
    def fake_capture(argv):
        if '-version' in argv:
            return 'ffmpeg version 6.1.1' if argv[0] == 'ffmpeg' else 'ffprobe version 6.1.1'
        if '-filters' in argv:
            names = ('ass', 'gblur', 'maskedmerge', 'settb', 'setpts', 'setparams', 'showinfo', 'sendcmd')
            return '\n'.join(' %s %s V->V test' % (flags.get(name, '...'), name)
                             for name in names if name != 'sendcmd' or name in flags)
        return 'libx264 AVOptions -crf -aq-mode -aq-strength -deblock'
    monkeypatch.setattr('assglass.ffmpeg.capture', fake_capture)
    assert capabilities('ffmpeg', 'ffprobe')['activity_commands']
    assert capabilities('ffmpeg', 'ffprobe')['activity_merge']
    del flags['sendcmd']
    assert not capabilities('ffmpeg', 'ffprobe')['activity_commands']
    flags['sendcmd'], flags['maskedmerge'] = '...', '.SC'
    assert not capabilities('ffmpeg', 'ffprobe')['activity_merge']
