"""Minimal packaging shim to compile the pyatem mediaconvert C extension
portably. The Docker image compiles it with an explicit gcc line; the
desktop builds (macOS / Windows / Linux) use this so each OS's own compiler
(clang / MSVC / gcc) picks the right flags and output suffix:

    python setup.py build_ext --inplace

produces pyatem/mediaconvert<EXT_SUFFIX>.so (or .pyd on Windows) in place,
which the PyInstaller spec then bundles.
"""
from setuptools import Extension, setup

setup(
    name='webatem-mediaconvert',
    version='0.0.0',
    ext_modules=[
        Extension('pyatem.mediaconvert', ['pyatem/mediaconvertmodule.c']),
    ],
)
