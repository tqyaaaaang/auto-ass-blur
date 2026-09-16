"""Build a platform wheel containing the coarse C ABI native bridge."""
import os
from pathlib import Path
import shlex
import subprocess
import sys

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext


class LibassBuildExt(build_ext):
    def build_extensions(self):
        env = os.environ.copy()
        prefix = Path(__file__).resolve().parent / '.tools/libass-event-images'
        if (prefix / 'lib/pkgconfig/libass.pc').is_file() and env.get('ASSGLASS_USE_SYSTEM_LIBASS') != '1':
            env['PKG_CONFIG_PATH'] = str(prefix / 'lib/pkgconfig') + (os.pathsep + env['PKG_CONFIG_PATH'] if env.get('PKG_CONFIG_PATH') else '')
        try:
            cflags = shlex.split(subprocess.check_output(['pkg-config', '--cflags', 'libass'], env=env, universal_newlines=True))
            ldflags = shlex.split(subprocess.check_output(['pkg-config', '--libs', 'libass'], env=env, universal_newlines=True))
            libdir = subprocess.check_output(['pkg-config', '--variable=libdir', 'libass'], env=env, universal_newlines=True).strip()
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeError('Install a C++14 compiler, pkg-config and libass development files before installing assglass') from exc
        for extension in self.extensions:
            extension.extra_compile_args = ['-std=c++14', '-O2', '-fno-fast-math'] + cflags
            extension.extra_link_args = ldflags + ([] if sys.platform == 'darwin' else ['-ldl', '-pthread', '-Wl,-rpath,' + libdir])
        super().build_extensions()


setup(
    name='assglass',
    version='0.1.0',
    description='Native ASS subtitle background Gaussian blur and single-pass encoding',
    packages=['assglass'],
    python_requires='>=3.8',
    install_requires=['PyYAML>=5.4'],
    extras_require={'test': ['pytest>=6.2']},
    entry_points={'console_scripts': ['assglass=assglass.cli:main']},
    ext_modules=[Extension('assglass._assglass_native', ['native/assglass.cpp'], language='c++')],
    cmdclass={'build_ext': LibassBuildExt},
)
