"""Per-IP cached HyperDeck connections for the transport modal.

``pyhyperdeck.Hyperdeck`` is a blocking, single-socket client. These views run
in Django's sync threadpool, and the modal polls ``hyperdeck/state`` ~1 Hz while
open, so we keep one connection per deck IP behind a per-IP lock and reuse it
instead of re-handshaking every request. A socket-level error (stale/closed
connection) drops the cached client and reconnects once, transparently; a
protocol-level ``HyperdeckError`` (e.g. "timeline empty") is a real response and
propagates unchanged.

SH-20 FIX (2026-07-06): decks are near-single-controller devices — a cached
9993 session held forever starves the uploader container and every other tool.
Cached decks are now released two ways:

  * Idle eviction — every ``with_deck`` call records ``_LAST_USED[ip]`` and
    opportunistically closes OTHER cached decks unused for more than
    ``DECK_IDLE_CLOSE_SECONDS``. Best-effort by design: the sweep only runs
    when SOME deck is used, so a fully idle process keeps its decks until
    process exit — good enough, since the point is freeing decks a closed
    modal left behind while other activity continues. An OPEN modal polls at
    ~1 Hz so its own deck never goes idle.
  * An ``atexit`` hook closes every cached deck (``Hyperdeck.close()`` sends a
    polite ``quit``), mirroring ``pyatem.pool._close_all_sessions_at_exit``.
"""
import atexit
import logging
import threading
import time

from hyperdeckwire import Hyperdeck, HyperdeckError

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 4.0
READ_TIMEOUT = 4.0
DECK_IDLE_CLOSE_SECONDS = 60.0

_DECKS = {}          # ip -> Hyperdeck
_LOCKS = {}          # ip -> threading.Lock (one in-flight op per deck)
_LAST_USED = {}      # ip -> time.monotonic() of the last with_deck call
_REGISTRY_LOCK = threading.Lock()


def _lock_for(ip):
    with _REGISTRY_LOCK:
        lock = _LOCKS.get(ip)
        if lock is None:
            lock = _LOCKS[ip] = threading.Lock()
        return lock


def _connect(ip):
    hd = Hyperdeck(ip, connect_timeout=CONNECT_TIMEOUT, read_timeout=READ_TIMEOUT)
    hd.connect()
    # SH-20 FIX (2026-07-06): remote_enable can raise HyperdeckError, which
    # with_deck's OSError handler does not catch — pre-fix, every failed
    # setup (~1 Hz while the modal polls) abandoned a connected socket.
    try:
        hd.remote_enable(True)
    except Exception:
        try:
            hd.close()
        except Exception:
            pass
        raise
    return hd


def _drop(ip):
    hd = _DECKS.pop(ip, None)
    if hd is not None:
        try:
            hd.close()
        except Exception:
            pass


def _sweep_idle(current_ip):
    """Close cached decks (other than ``current_ip``) unused for more than
    ``DECK_IDLE_CLOSE_SECONDS``. Best-effort: skips decks whose per-IP lock
    is busy, and re-checks last-use under the lock so a racing fresh use
    isn't torn down."""
    now = time.monotonic()
    with _REGISTRY_LOCK:
        stale = [
            ip for ip in _DECKS
            if ip != current_ip
            and now - _LAST_USED.get(ip, 0.0) > DECK_IDLE_CLOSE_SECONDS
        ]
    for ip in stale:
        lock = _lock_for(ip)
        if not lock.acquire(blocking=False):
            continue  # in use right now — clearly not idle
        try:
            if time.monotonic() - _LAST_USED.get(ip, 0.0) <= DECK_IDLE_CLOSE_SECONDS:
                continue  # raced with a fresh use; leave it
            logger.info('hyperdeck %s idle > %.0fs; closing cached session',
                        ip, DECK_IDLE_CLOSE_SECONDS)
            _drop(ip)
        finally:
            lock.release()


def with_deck(ip, fn):
    """Run ``fn(hd)`` against a connected, remote-enabled deck at ``ip``,
    holding the per-IP lock. Reconnects once if the cached socket is stale
    (``OSError``); ``HyperdeckError`` (a real protocol response) is not retried.
    Returns ``fn``'s result; raises ``OSError`` / ``HyperdeckError`` on failure.
    """
    _sweep_idle(ip)
    lock = _lock_for(ip)
    with lock:
        _LAST_USED[ip] = time.monotonic()
        last_exc = None
        for attempt in (1, 2):
            hd = _DECKS.get(ip)
            if hd is None:
                hd = _DECKS[ip] = _connect(ip)
            try:
                return fn(hd)
            except OSError as e:
                # Socket likely dead — drop and retry once with a fresh connect.
                logger.warning('hyperdeck %s socket error (%s); reconnecting', ip, e)
                _drop(ip)
                last_exc = e
        raise last_exc


def _close_all_decks_at_exit():
    """Interpreter-exit safety net (SH-20 FIX 2026-07-06): close every cached
    deck session. ``Hyperdeck.close()`` sends a polite ``quit`` before closing
    the socket, so the deck sees a clean controller departure instead of an
    abandoned session. Mirrors ``pyatem.pool._close_all_sessions_at_exit`` —
    no per-IP lock juggling at interpreter shutdown, just best-effort closes.
    """
    with _REGISTRY_LOCK:
        decks = list(_DECKS.values())
        _DECKS.clear()
    for hd in decks:
        try:
            hd.close()
        except Exception:
            pass


atexit.register(_close_all_decks_at_exit)
