"""Macro codec — macro-flow ops (MacroSleep)."""

from pyatem.macrotransfer._helpers import _u32le, _u32le_bytes


def _d_macro_sleep(params, mx):
    # u32 LE frames. Captured value 0x1e=30 matches "MacroSleep frames=30"
    # in Software Control's XML output.
    return {'frames': str(_u32le(params, 0))}


def _e_macro_sleep(attrs):
    return _u32le_bytes(int(attrs.get('frames', 0)))
