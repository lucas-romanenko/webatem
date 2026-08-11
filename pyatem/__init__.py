# SPDX-License-Identifier: LGPL-3.0-only
"""
pyatem — control Blackmagic ATEM switchers from Python.

Typical usage::

    from pyatem import ATEM

    with ATEM('192.168.1.10') as atem:
        atem.set_program(5)
        atem.cut()
        print(atem.program_source, atem.video_mode)

For one-shot identity queries (warm if a session is already pooled
for the IP, ~6s cold)::

    from pyatem import probe
    info = probe('192.168.1.10')

Threading: ATEM instances are safe to call from any thread. Write
methods enqueue commands and return immediately; state property reads
are safe from any thread.

Internals (pyatem.messages, pyatem.protocol, pyatem.pool,
pyatem._state, pyatem.helpers, pyatem.transport, ...)
remain importable for advanced or low-level usage. The blessed public
surface is what's listed in ``__all__`` below.
"""

from pyatem.atem import ATEM
from pyatem.probe import probe
from pyatem.profile import ApplyOptions, ApplyResult, Profile

__all__ = [
    'ATEM',
    'ApplyOptions',
    'ApplyResult',
    'Profile',
    'probe',
]
