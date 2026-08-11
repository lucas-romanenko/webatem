"""Lightweight operator-activity logging.

The monorepo this app was extracted from records these events in a DB-backed
cross-app audit table; standalone writes them to the application log instead.
Signature-compatible with the original ``record_activity`` /
``arecord_activity`` so call sites read identically. Connect/disconnect
events additionally persist in the ``ATEMControlLog`` table (which also
drives the Connect page's "Recent ATEMs").
"""
import logging

_log = logging.getLogger('atem_control.activity')


class ActivityLog:
    """Constant namespace kept for call-site compatibility."""
    FEATURE_ATEM_CONTROL = 'atem_control'
    DEVICE_ATEM = 'atem'
    DEVICE_HYPERDECK = 'hyperdeck'


def record_activity(*, feature='', device='', action='', user=None,
                    target='', target_name='', summary='', **extra):
    """Write one operator-activity line to the application log.

    ``user`` is vestigial — kept for call-site compatibility with the
    original monorepo's audit log. This app has no authentication, so
    callers always pass ``None`` and entries are logged without a user
    (the ``-`` placeholder).
    """
    username = getattr(user, 'username', None) or str(user or '-')
    _log.info("activity user=%s action=%s target=%s: %s",
              username, action, target_name or target, summary)


async def arecord_activity(**kwargs):
    record_activity(**kwargs)
