"""Shared pytest fixtures for the pyatem unit tests.

These tests run without a live ATEM. They use a ``FakeAtemProtocol`` that
mimics the surface area pyatem.connection.ATEMConnection actually exercises:

  * .connect()                  — handshake; deterministic
  * .loop()                     — pumps a fake recv queue
  * .send_commands(cmds)        — captures sent commands
  * .mixerstate                 — populated minimally with 'video-mode'
  * .on(event, callback)        — registers a callback
  * .transport.thread_recv_queue.put(None)  — used by the disconnect path
                                  to wake the worker

The fake's behaviour is configurable per-test via constructor args:

  * connect_delay         — sleep this long inside connect() before
                            firing 'connected' + setting mixerstate
  * loop_blocks           — when True, loop() blocks on thread_recv_queue
                            (matches real behaviour) so the worker is
                            actually parked between events
  * raise_on_loop         — raise this exception on the next loop() call
                            (used to simulate the daemon-thread struct
                            error class of bugs)

Tests use these to assemble specific scenarios — e.g. "worker is parked,
disconnect fires, verify wake makes worker exit within 200ms."
"""

import os
import sys
import time
from queue import Queue

import pytest


# Make pyatem importable without needing the C extension to be available.
# We monkey-patch sys.modules so transport.py / media.py / etc. are NOT
# pulled in transitively. The tests don't need them.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# ---------------------------------------------------------------------------
# FakeAtemProtocol
# ---------------------------------------------------------------------------


class FakeTransport:
    """Mimics pyatem.transport.UdpProtocol's surface used by ATEMConnection."""

    def __init__(self):
        self.thread_recv_queue: "Queue" = Queue()
        # ATEMConnection._close_protocol calls .sock.close()
        self.sock = _FakeSocket()


class _FakeSocket:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeAtemProtocol:
    """Stand-in for pyatem.protocol.AtemProtocol.

    Implements just enough surface to exercise ATEMConnection.

    The crucial behaviour the real AtemProtocol has — that ``loop()`` blocks
    on ``transport.thread_recv_queue.get()`` until either a packet arrives
    or someone puts ``None`` (disconnect signal) — is replicated here so
    the wake-the-worker path under test actually has something to wake.
    """

    def __init__(self, ip=None, *, connect_delay=0.0, loop_blocks=True,
                 fail_connect=False, fail_loop_after=None,
                 fail_loop_exception=None):
        self.ip = ip
        self.transport = FakeTransport()
        self.mixerstate = {}
        self.connected = False

        self._callbacks = {}
        self._connect_delay = connect_delay
        self._loop_blocks = loop_blocks
        self._fail_connect = fail_connect
        self._fail_loop_after = fail_loop_after  # raise after N loop() calls
        # The exception class to raise. Default RuntimeError gets caught by
        # _pump_loop_once's except Exception. Pass SystemExit (a BaseException
        # subclass) to simulate a worker-killing failure.
        self._fail_loop_exception = fail_loop_exception or RuntimeError
        self._loop_count = 0

        # Test-side instrumentation:
        self.send_log = []          # list of [Command, ...] batches sent
        self.connect_called = 0

    def connect(self):
        self.connect_called += 1
        if self._fail_connect:
            raise RuntimeError("simulated connect() failure")
        # Simulate handshake delay.
        if self._connect_delay > 0:
            time.sleep(self._connect_delay)
        # Real pyatem fires 'connected' AFTER the initial state dump
        # completes (post-InCm). Our wait_ready check is "connected event
        # fired AND 'video-mode' in mixerstate" — populate both.
        self.mixerstate['video-mode'] = _FakeVideoMode()
        self.connected = True
        # Wake any blocked loop().
        self.transport.thread_recv_queue.put('CONNECTED_PACKET')
        # Fire callbacks registered for 'connected'.
        for cb in self._callbacks.get('connected', []):
            try:
                cb()
            except Exception:
                pass

    def loop(self):
        self._loop_count += 1
        if self._fail_loop_after is not None and self._loop_count > self._fail_loop_after:
            raise self._fail_loop_exception("simulated loop() failure")
        if self._loop_blocks:
            # Mirror real pyatem: receive_packet blocks on the queue.
            packet = self.transport.thread_recv_queue.get()
            if packet is None:
                # Disconnect signal.
                self.connected = False
                self.mixerstate = {}
                for cb in self._callbacks.get('disconnected', []):
                    try:
                        cb()
                    except Exception:
                        pass

    def on(self, event, callback):
        self._callbacks.setdefault(event, []).append(callback)
        return len(self._callbacks[event]) - 1

    def send_commands(self, commands):
        if not self.connected:
            raise RuntimeError("send_commands while disconnected")
        self.send_log.append(list(commands))


class _FakeVideoMode:
    def get_resolution(self):
        return (1920, 1080)

    def get_label(self):
        return '1080p50'


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_protocol_factory(monkeypatch):
    """Patch ``pyatem.connection.AtemProtocol`` to a fake. Returns a factory
    function so tests can configure the fake per-test.

    Usage:
        def test_x(fake_protocol_factory):
            p = fake_protocol_factory(connect_delay=0.05)
            # ATEMConnection.connect() will instantiate this fake
    """
    instances = []

    class _Factory:
        def __init__(self):
            self.ctor_kwargs = {}

        def __call__(self, **kwargs):
            self.ctor_kwargs = kwargs
            return None

        def make_class(self):
            ctor_kwargs = self.ctor_kwargs

            class _Configured(FakeAtemProtocol):
                def __init__(self, ip=None, **runtime_kwargs):
                    super().__init__(ip=ip, **ctor_kwargs)
                    instances.append(self)
            return _Configured

    factory = _Factory()

    def configure_and_install(**kwargs):
        factory(**kwargs)
        cls = factory.make_class()
        monkeypatch.setattr('pyatem.connection.AtemProtocol', cls)
        return instances  # the list grows as tests instantiate

    return configure_and_install


@pytest.fixture
def reset_pool():
    """Yield, then clear ATEMInstanceManager._instances. Each test gets a
    clean pool so ordering doesn't matter."""
    from pyatem.pool import ATEMInstanceManager
    # Defensive — a prior test might have leaked entries.
    with ATEMInstanceManager._instance_lock:
        for ip in list(ATEMInstanceManager._instances.keys()):
            entry = ATEMInstanceManager._instances[ip]
            timer = entry.get('disconnect_timer')
            if timer is not None:
                timer.cancel()
            try:
                entry['connection'].disconnect()
            except Exception:
                pass
            del ATEMInstanceManager._instances[ip]
    yield
    with ATEMInstanceManager._instance_lock:
        for ip in list(ATEMInstanceManager._instances.keys()):
            entry = ATEMInstanceManager._instances[ip]
            timer = entry.get('disconnect_timer')
            if timer is not None:
                timer.cancel()
            try:
                entry['connection'].disconnect()
            except Exception:
                pass
            del ATEMInstanceManager._instances[ip]


@pytest.fixture
def short_grace(monkeypatch):
    """Override DISCONNECT_GRACE_PERIOD for fast tests."""
    monkeypatch.setattr('pyatem.pool.DISCONNECT_GRACE_PERIOD', 0.1)


@pytest.fixture
def short_connect_timeout(monkeypatch):
    """Override CONNECT_TIMEOUT and DISCONNECT_JOIN_TIMEOUT for fast tests."""
    monkeypatch.setattr('pyatem.connection.CONNECT_TIMEOUT', 1.0)
    monkeypatch.setattr('pyatem.connection.DISCONNECT_JOIN_TIMEOUT', 0.5)
