"""Operator-activity logging — a facade over the host's hooks.

Call sites keep the original signatures (``record_activity`` /
``arecord_activity`` and the ``ActivityLog`` constants); where the entries
go is the host's business: standalone WebATEM writes a log line, a hosting
platform writes its audit table (``atem_control.hooks``).
"""
from atem_control import hooks


class ActivityLog:
    """Constant namespace kept for call-site compatibility."""
    FEATURE_ATEM_CONTROL = 'atem_control'
    DEVICE_ATEM = 'atem'
    DEVICE_HYPERDECK = 'hyperdeck'


def record_activity(**kwargs):
    return hooks.get().record_activity(**kwargs)


async def arecord_activity(**kwargs):
    return await hooks.get().arecord_activity(**kwargs)
