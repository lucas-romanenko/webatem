"""Per-IP upload lock: one upload at a time per switcher, in this process.

WebATEM is a single process, so a per-IP ``threading.Lock`` is enough:
``with hold_ip_upload_lock(ip) as ok:`` where ``ok`` is False if the lock
couldn't be taken within ``wait_timeout`` (callers proceed). It is the
default of ``Hooks.hold_upload_lock``; a host whose uploads also run in
other processes supplies a lock they share there, and may refuse instead
by raising ``UploadBusy``.
"""
import threading
from contextlib import contextmanager

_locks = {}
_registry_lock = threading.Lock()


def _lock_for(ip):
    with _registry_lock:
        return _locks.setdefault(str(ip), threading.Lock())


@contextmanager
def hold_ip_upload_lock(ip, *, wait_timeout=30.0):
    lock = _lock_for(ip)
    acquired = lock.acquire(timeout=wait_timeout)
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()


class UploadBusy(RuntimeError):
    """Raised by a host's ``Hooks.hold_upload_lock`` to refuse an upload:
    the switcher stayed busy with another one. ``str()`` is the message the
    operator sees, as it is."""
