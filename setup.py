"""Build a platform wheel containing the coarse C ABI native bridge."""
import os
import shlex
import subprocess
import sys

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext


class LibassBuildExt(build_ext):
    def build_extensions(self):
        try:
            cflags = shlex.split(subprocess.check_output(['pkg-config', '--cflags', 'libass'], universal_newlines=True))
            ldflags = shlex.split(subprocess.check_output(['pkg-config', '--libs', 'libass'], universal_newlines=True))
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeError('Install a C++14 compiler, pkg-config and libass development files before installing assglass') from exc
        for extension in self.extensions:
            extension.extra_compile_args = ['-std=c++14', '-O2', '-fno-fast-math'] + cflags
            extension.extra_link_args = ldflags + ([] if sys.platform == 'darwin' else ['-ldl', '-pthread'])
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
