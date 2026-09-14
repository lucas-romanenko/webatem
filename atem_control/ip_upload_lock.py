"""Per-IP upload lock — standalone stand-in for the monorepo's
Postgres-advisory-lock version.

The monorepo coordinates media-pool uploads ACROSS PROCESSES (a pool of
uploader containers plus the web app) via Postgres advisory locks. WebATEM
is a single process, so a per-IP ``threading.Lock`` gives the same
guarantee — one upload at a time per switcher — with the same call-site
contract: ``with hold_ip_upload_lock(ip) as ok:`` where ``ok`` is False if
the lock couldn't be taken within ``wait_timeout`` (fail-open: callers
proceed but log, matching upstream's behaviour).
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
