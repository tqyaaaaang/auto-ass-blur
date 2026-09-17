"""FFmpeg capability checks, protected options and bounded pipe lifecycle."""
from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import subprocess
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path

ENCODER_DEFAULTS = {"crf": 18, "deblock": "-1:-1", "preset": "veryslow", "level": "4.2",
                    "flags": "cgop", "aq-mode": 3, "aq-strength": 0.8, "me_range": 32,
                    "subq": 10, "b:v": "10M", "maxrate": "20M", "bufsize": "10M"}
ENCODER_ALIASES = {"vb": "b:v", "b": "b:v", "level:v": "level", "flags:v": "flags"}
ENCODER_FLAGS = {"level": "level:v", "flags": "flags:v"}
ACTIVITY_COMMAND_BASENAME = "activity.cmd"
ACTIVITY_BLUR_TARGET = "gblur@assglass_blur"
ACTIVITY_MERGE_TARGET = "maskedmerge@assglass_merge"


def normalize_encoder_options(options):
    result = {}
    for key, value in options.items():
        key = ENCODER_ALIASES.get(key, key)
        if key not in ENCODER_DEFAULTS:
            raise ValueError("不支持的编码参数（不能覆盖帧率、滤镜、映射）: " + key)
        if key in result:
            raise ValueError("编码参数别名重复: " + key)
        result[key] = value
    return result


def resolve_encoder(section, cli_options=None):
    unknown = set(section) - {"codec", "rate_control", "options", "audio_codec"}
    if unknown:
        raise ValueError("未知 encoder 配置: " + ", ".join(sorted(unknown)))
    if section.get("codec", "libx264") != "libx264":
        raise ValueError("首版仅支持 libx264 编码器")
    options = dict(ENCODER_DEFAULTS)
    options.update(normalize_encoder_options(section.get("options", {})))
    options.update(normalize_encoder_options(cli_options or {}))
    mode = section.get("rate_control", "crf")
    if mode not in ("crf", "abr"):
        raise ValueError("encoder.rate_control 必须是 crf 或 abr")
    if (mode == "crf" and options["crf"] is None) or (mode == "abr" and (options["crf"] is not None or options["b:v"] is None)):
        raise ValueError("CRF 模式需要 crf；ABR 模式必须显式移除 crf 并设置 b:v")
    for key, value in options.items():
        if value is not None and (isinstance(value, bool) or not isinstance(value, (str, int, float))):
            raise ValueError("非法编码参数: " + key)
        if value is not None and any(c in str(value) for c in "\n\r\0"):
            raise ValueError("编码参数不能含换行或 NUL")
    numeric_ranges = {"crf": (0, 51), "aq-mode": (0, 3), "aq-strength": (0, 3),
                      "me_range": (4, 1024), "subq": (0, 11)}
    for key, bounds in numeric_ranges.items():
        if options[key] is None:
            continue
        try:
            value = float(options[key])
        except (TypeError, ValueError):
            raise ValueError("编码参数必须为数字: " + key) from None
        if not math.isfinite(value) or not bounds[0] <= value <= bounds[1]:
            raise ValueError(f"编码参数 {key} 必须在 {bounds[0]}..{bounds[1]}")
        if key in ("aq-mode", "me_range", "subq") and value != int(value):
            raise ValueError("编码参数必须为整数: " + key)
    if options["preset"] is not None and options["preset"] not in (
            "ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow", "placebo"):
        raise ValueError("无效的 x264 preset")
    if options["deblock"] is not None:
        if not re.fullmatch(r"-?[0-6]:-?[0-6]", str(options["deblock"])):
            raise ValueError("deblock 应为 -6..6:-6..6")
    if options["level"] is not None and str(options["level"]) not in (
            "1", "1.0", "1.1", "1.2", "1.3", "2", "2.0", "2.1", "2.2", "3", "3.0", "3.1", "3.2",
            "4", "4.0", "4.1", "4.2", "5", "5.0", "5.1", "5.2", "6", "6.0", "6.1", "6.2"):
        raise ValueError("无效的 H.264 level")
    for key in ("b:v", "maxrate", "bufsize"):
        if options[key] is not None and not re.fullmatch(r"\d+(?:\.\d+)?[kKmMgG]?", str(options[key])):
            raise ValueError("无效的码率/缓冲参数: " + key)
    return {"codec": "libx264", "rate_control": mode, "options": options}


def encoder_argv(encoder):
    result = ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    for key, value in encoder["options"].items():
        if value is not None:
            result.extend(["-" + ENCODER_FLAGS.get(key, key), str(value)])
    result.extend(["-color_range", "tv", "-colorspace", "bt709", "-color_trc", "bt709",
                   "-color_primaries", "bt709", "-chroma_sample_location", "left"])
    return result


def executable(name):
    path = shutil.which(name)
    if not path:
        raise ValueError("找不到可执行程序: " + name)
    return str(Path(path).resolve())


def capture(argv):
    p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if p.returncode:
        raise ValueError("命令执行失败：" + p.stdout[-6000:])
    return p.stdout


def capabilities(ffmpeg, ffprobe):
    ffmpeg, ffprobe = executable(ffmpeg), executable(ffprobe)
    version = capture([ffmpeg, "-version"])
    probe_version = capture([ffprobe, "-version"])
    m = re.search(r"ffmpeg version (\d+)\.(\d+)", version)
    if not m or not 5 <= int(m[1]) <= 9:
        raise ValueError("需要 FFmpeg 5–9 的发行构建；此版本没有已定义 adapter")
    filters = capture([ffmpeg, "-hide_banner", "-filters"])
    for name in ("ass", "gblur", "maskedmerge", "settb", "setpts", "setparams", "showinfo"):
        if not re.search(r"\s" + name + r"\s", filters):
            raise ValueError("FFmpeg 缺少滤镜: " + name)
    enc = capture([ffmpeg, "-hide_banner", "-h", "encoder=libx264"])
    if "libx264 AVOptions" not in enc:
        raise ValueError("FFmpeg 缺少 libx264")
    for name in ("crf", "aq-mode", "aq-strength", "deblock"):
        if "-" + name not in enc:
            raise ValueError("libx264 缺少选项: " + name)
    filter_flags = {name: flags for flags, name in re.findall(r"^\s*([TSC.]{3})\s+(\w+)\s", filters, re.MULTILINE)}
    return {"ffmpeg": ffmpeg, "ffprobe": ffprobe, "version": version.strip(),
            "ffprobe_version": probe_version.strip(), "fps_mode": tuple(map(int, m.groups())) >= (5, 1),
            "activity_commands": "sendcmd" in filter_flags and "T" in filter_flags.get("gblur", ""),
            "activity_merge": "T" in filter_flags.get("maskedmerge", "")}


def cpu_threads():
    cpus = os.cpu_count() or 1
    try:
        cpus = min(cpus, len(os.sched_getaffinity(0)))
    except AttributeError:
        pass
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max":
            cpus = min(cpus, max(1, (int(quota) + int(period) - 1) // int(period)))
    except (OSError, ValueError):
        pass
    return min(cpus, 8)


def thread_count(value):
    if value == "auto":
        return cpu_threads()
    if isinstance(value, bool) or int(value) != value or not 1 <= int(value) <= 256:
        raise ValueError("线程数应为 auto 或 1..256 的整数")
    return int(value)


def build_graph(plan, sigma, burn_subtitles, fonts=False, observe=True,
                activity_commands=None, activity_merge=True):
    """Build the reference graph or its activity-controlled equivalent.

    The activity file contains finite, half-open sendcmd intervals and sends
    ``enable 0/1`` to ACTIVITY_BLUR_TARGET and (when enabled) ACTIVITY_MERGE_TARGET.
    Commands run before split, so frame zero is configured before either branch
    consumes it. Frame timestamps, source branches and the mask stream remain
    intact; disabled gblur forwards its input, while maskedmerge's internal
    timeline path clones the synchronized base without blending pixels.

    FFmpeg's timeline command contract and native implementations:
    https://ffmpeg.org/ffmpeg-filters.html#Timeline-editing
    https://github.com/FFmpeg/FFmpeg/blob/n6.1.1/libavfilter/vf_maskedmerge.c
    https://github.com/FFmpeg/FFmpeg/blob/n6.1.1/libavfilter/vf_gblur.c
    """
    tb = str(plan.spec.time_base)
    controlled = activity_commands is not None
    if controlled and str(activity_commands) != ACTIVITY_COMMAND_BASENAME:
        raise ValueError("activity commands must use the private fixed basename activity.cmd")
    control = "sendcmd=f=activity.cmd," if controlled else ""
    blur = ACTIVITY_BLUR_TARGET if controlled else "gblur"
    merge = ACTIVITY_MERGE_TARGET if controlled and activity_merge else "maskedmerge"
    blur_enable = ":enable=0" if controlled else ""
    merge_enable = ":enable=0" if controlled and activity_merge else ""
    # Private working directory gives the ASS, fonts and commands safe names.
    graph = (f"[0:{plan.spec.stream['index']}]format=pix_fmts=yuv420p,{control}split=2[base][tmp];"
             f"[tmp]{blur}=sigma={sigma:g}:steps=2:planes=7{blur_enable}[blurred];"
             "[1:v]setparams=range=limited:color_primaries=bt709:color_trc=bt709:colorspace=bt709,"
             f"settb=expr={tb},setpts=N*{plan.ticks_per_frame}[mask];"
             f"[base][blurred][mask]{merge}=planes=7{merge_enable}[glass];"
             f"[glass]settb=expr={tb}")
    if observe:
        graph += ",showinfo"
    if burn_subtitles:
        graph += ",ass=filename=original.ass" + (":fontsdir=fonts" if fonts else "")
    return graph + "[outv]"


def build_command(caps, plan, encoder, graph, output, runtime, max_in_flight_frames=4, audio_codec="copy"):
    threads = thread_count(runtime.get("ffmpeg_threads", "auto"))
    filter_threads = thread_count(runtime.get("ffmpeg_filter_threads", "auto"))
    argv = [caps["ffmpeg"], "-hide_banner", "-nostdin", "-y", "-loglevel", "verbose", "-nostats",
            "-filter_complex_threads", str(filter_threads), "-threads", str(threads), "-noautorotate",
            "-i", str(plan.spec.path), "-thread_queue_size", str(max_in_flight_frames),
            "-f", "rawvideo", "-pixel_format", plan.pix_fmt, "-video_size", f"{plan.spec.width}x{plan.spec.height}",
            "-framerate", str(plan.frame_rate), "-i", "pipe:0", "-filter_complex", graph,
            "-map", "[outv]"]
    if audio_codec != "none":
        argv += ["-map", "0:a?", "-c:a", audio_codec]
    argv += ["-map_metadata", "0", "-sn", "-dn"]
    argv += ["-fps_mode:v", "passthrough"] if caps["fps_mode"] else ["-vsync", "0"]
    argv += encoder_argv(encoder)
    argv += ["-threads", str(threads), "-enc_time_base", str(plan.spec.time_base),
             "-video_track_timescale", str(plan.spec.time_base.denominator), str(output)]
    return argv


def write_all(sink, data):
    view = memoryview(data).cast("B")
    sent = 0
    while sent < len(view):
        try:
            n = sink.write(view[sent:])
        except InterruptedError:
            continue
        if not n:
            raise BrokenPipeError("mask pipe 提前关闭/短写")
        sent += n
    return sent


class PipePipeline:
    def __init__(self, argv, cwd, log_path, count, ticks_per_frame):
        self.argv, self.expected_count, self.ticks = argv, count, ticks_per_frame
        self.tail = deque(maxlen=50)
        self.observed = self.sent = self.sent_bytes = 0
        self.errors = []
        self.font_lines = set()
        self.process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.PIPE, bufsize=0)
        self.log_path = Path(log_path)
        self.reader = threading.Thread(target=self._read, name="assglass-ffmpeg-log", daemon=True)
        self.reader.start()

    def _read(self):
        try:
            with self.log_path.open("w", encoding="utf-8") as sink:
                for raw in self.process.stderr:
                    line = raw.decode("utf-8", "replace")
                    sink.write(line)
                    self.tail.append(line.rstrip())
                    match = re.search(r"Parsed_showinfo.*?\bn:\s*(\d+)\s+pts:\s*(-?\d+)", line)
                    if match:
                        n, pts = map(int, match.groups())
                        if n != self.observed or pts != n * self.ticks:
                            if not self.errors:
                                self.errors.append(f"实际字幕入口帧不匹配: n={n}, pts={pts}")
                        self.observed += 1
                    if "auto_scale" in line and not self.errors:
                        self.errors.append("FFmpeg 插入非预期的像素转换: " + line.strip())
                    if "Command reply for command" in line and "ret:" in line:
                        reply = line.split("ret:", 1)[1].split(" res:", 1)[0].strip()
                        # av_err2str(0) follows libc: glibc says Success, Darwin
                        # says Undefined error: 0. A failed sendcmd otherwise
                        # only logs its reply and can still yield exit code 0.
                        if reply not in ("Success", "Undefined error: 0") and not self.errors:
                            self.errors.append("FFmpeg 活动区间命令失败: " + line.strip())
                    if "fontselect:" in line or "Using font provider" in line:
                        if len(self.font_lines) < 256:
                            self.font_lines.add(line.strip())
        except BaseException as exc:
            self.errors.append("读取 FFmpeg 日志失败: " + str(exc))
            # Otherwise the child can fill stderr while our writer is blocked
            # on stdin, with nobody left to drain either pipe.
            if self.process.poll() is None:
                try:
                    self.process.kill()
                except OSError:
                    pass

    def write_frame(self, data, expected_bytes):
        if self.errors:
            raise ValueError(self.errors[0])
        if len(memoryview(data).cast("B")) != expected_bytes:
            raise ValueError("mask 单帧字节数错误")
        try:
            self.sent_bytes += write_all(self.process.stdin, data)
            self.sent += 1
        except BrokenPipeError:
            raise ValueError("FFmpeg 提前关闭 mask pipe：\n" + "\n".join(self.tail)) from None

    def finish(self):
        self.process.stdin.close()
        code = self.process.wait()
        self.reader.join(timeout=10)
        if self.reader.is_alive():
            raise ValueError("FFmpeg 日志线程没有退出")
        if code:
            raise ValueError(f"FFmpeg 退出码 {code}：\n" + "\n".join(self.tail))
        if self.errors:
            raise ValueError(self.errors[0])
        if not (self.sent == self.observed == self.expected_count):
            raise ValueError(f"帧完整性检查失败 ledger={self.expected_count}, sent={self.sent}, observed={self.observed}")
        return {"sent_frames": self.sent, "sent_bytes": self.sent_bytes, "observed_frames": self.observed,
                "font_observations": sorted(self.font_lines)}

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if not self.process.stdin.closed:
            self.process.stdin.close()
        self.reader.join(timeout=5)
        self.process.stderr.close()
