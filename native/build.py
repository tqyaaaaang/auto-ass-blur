#!/usr/bin/env python3
"""Build the coarse native bridge; requires a C++14 compiler and libass dev files."""
import argparse
import os
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parent

def build_environment():
    env = os.environ.copy()
    prefix = ROOT.parent / '.tools' / 'libass-event-images'
    if (prefix / 'lib/pkgconfig/libass.pc').is_file() and env.get('ASSGLASS_USE_SYSTEM_LIBASS') != '1':
        env['PKG_CONFIG_PATH'] = str(prefix / 'lib/pkgconfig') + (os.pathsep + env['PKG_CONFIG_PATH'] if env.get('PKG_CONFIG_PATH') else '')
    return env

def build(output=None):
    output = Path(output) if output else ROOT / ('libassglass.dylib' if sys.platform == 'darwin' else 'libassglass.so')
    try:
        env = build_environment()
        flags = shlex.split(subprocess.check_output(['pkg-config', '--cflags', '--libs', 'libass'], env=env, universal_newlines=True))
        libdir = subprocess.check_output(['pkg-config', '--variable=libdir', 'libass'], env=env, universal_newlines=True).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError('libass development files and pkg-config are required; see README installation instructions') from exc
    cmd = [os.environ.get('CXX', 'c++'), '-std=c++14', '-O2', '-fPIC', '-fno-fast-math', '-shared', str(ROOT / 'assglass.cpp'), '-o', str(output)] + flags
    if sys.platform != 'darwin':
        cmd += ['-ldl', '-pthread', '-Wl,-rpath,' + libdir]
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(cmd, check=True)
    return output

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output')
    print(build(parser.parse_args().output))
