"""Check the concrete FFmpeg/libass installation instead of trusting versions."""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path


def file_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def linked_libass(ffmpeg):
    if sys.platform == "darwin":
        seen = set()
        pending = [ffmpeg]
        while pending:
            binary = pending.pop()
            if binary in seen:
                continue
            seen.add(binary)
            result = subprocess.run(["otool", "-L", binary], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for line in result.stdout.splitlines()[1:]:
                path = line.strip().split(" (", 1)[0]
                if Path(path).name.startswith("libass.") and Path(path).is_file():
                    return str(Path(path).resolve())
                if Path(path).name.startswith("libavfilter.") and Path(path).is_file():
                    pending.append(path)
    elif sys.platform.startswith("linux"):
        result = subprocess.run(["ldd", ffmpeg], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        match = re.search(r"libass\.so[^ ]*\s+=>\s+(\S+)", result.stdout)
        if match and Path(match[1]).is_file():
            return str(Path(match[1]).resolve())
    raise ValueError("无法验证 FFmpeg 实际使用的共享 libass；需要可检查动态依赖的 Linux/macOS 构建")


def validate_weight_transport(ffmpeg):
    """Exercise each plane and 0/128/255 endpoints in the actual filter family."""
    width = height = 16
    plane_sizes = (256, 64, 64)
    base = b"".join(bytes([v]) * n for v, n in zip((40, 72, 180), plane_sizes))
    overlay = b"".join(bytes([v]) * n for v, n in zip((210, 210, 30), plane_sizes))
    # All three sources share one demuxer; select creates aligned one-frame branches.
    for weight in (0, 128, 255):
        mask = bytes([weight]) * sum(plane_sizes)
        graph = ("[0:v]split=3[a][b][c];[a]select=eq(n\\,0),setpts=0[x];"
                 "[b]select=eq(n\\,1),setpts=0[y];"
                 "[c]select=eq(n\\,2),setpts=0,"
                 "setparams=range=limited:color_primaries=bt709:color_trc=bt709:colorspace=bt709[z];"
                 "[x][y][z]maskedmerge=planes=7[out]")
        result = subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-filter_complex_threads", "1",
                                 "-f", "rawvideo", "-pixel_format", "yuv420p", "-video_size", "16x16",
                                 "-i", "pipe:0", "-filter_complex", graph, "-map", "[out]",
                                 "-frames:v", "1", "-f", "rawvideo", "pipe:1"],
                                input=base + overlay + mask, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode or len(result.stdout) != len(base):
            raise ValueError("权重传输自检失败: " + result.stderr.decode("utf-8", "replace")[-2000:])
        expected = bytes((a * (255 - weight) + b * weight + 127) // 255 for a, b in zip(base, overlay))
        if result.stdout != expected:
            raise ValueError("FFmpeg maskedmerge 三平面端点/数值自检失败，拒绝此构建")
    return "passed: three planes, weights 0/128/255"


def verify_ass_runtime(caps, cwd, helper_path, helper_version, fonts=False):
    linked = linked_libass(caps["ffmpeg"])
    helper = str(Path(helper_path).resolve())
    if linked != helper:
        raise ValueError(f"helper 与 FFmpeg libass 不同：{helper} / {linked}")
    graph = "ass=original.ass" + (":fontsdir=fonts" if fonts else "")
    result = subprocess.run([caps["ffmpeg"], "-hide_banner", "-nostdin", "-loglevel", "verbose",
                             "-f", "lavfi", "-i", "color=black:size=1920x1080:rate=60", "-vf", graph,
                             "-frames:v", "1", "-f", "null", "-"], cwd=cwd,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if result.returncode:
        raise ValueError("FFmpeg 原 ASS 初始化失败: " + result.stderr[-6000:])
    version = re.search(r"libass API version:\s*(0x[0-9a-fA-F]+)", result.stderr)
    if not version or int(version[1], 16) != helper_version:
        raise ValueError("FFmpeg 与 helper libass API 版本验证失败")
    return {"libass_path": helper, "libass_sha256": file_hash(helper),
            "libass_version": hex(helper_version), "weight_selftest": validate_weight_transport(caps["ffmpeg"]),
            "ass_initialization_log": result.stderr[-12000:]}
