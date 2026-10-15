"""Clean-unlock / clean-goodbye guarantees (2026-07-06 hardening).

Root cause of the field's "fossil locks": clients that took the ATEM's
media store lock and then exited without the protocol goodbye. Verified
live on the test ATEM: a clean ``close_session`` frees a held lock
INSTANTLY; even abrupt process death (host alive) frees it in ~5s via
ICMP-unreachable; only a vanished host (or a corrupted switcher session
table) leaves a lock stranded.

Contracts pinned here:
  * uploader ``_run_for_single_ip`` sends the goodbye on EVERY exit path
    (unexpected exception included) — a skipped goodbye is an abandoned
    session holding the store lock;
  * uploader ``_upload_one`` failure paths (upload() raised / timeout /
    cancel) call ``abort_transfers`` — un-wedging the transfer lane AND
    releasing the store lock for the batch's next item;
  * protocol FTDE with a fatal (non-1/5) status pops the dead task and
    advances the lane instead of wedging it at queue head with the lock
    held;
  * the pool's atexit hook sends the goodbye for every live pooled
    connection (deploy restarts / watchmedo reloads / SIGTERM).
"""

import threading
import types

import pytest

from atem_control import uploader as U
from atemwire.pool import ATEMInstanceManager, close_all_sessions_at_exit
from atemwire.protocol import AtemProtocol


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class _FakeTransport:
    def __init__(self):
        self.goodbyes = 0
        self.sock = types.SimpleNamespace(close=lambda: None)

    def close_session(self):
        self.goodbyes += 1


class _FakeUploadProtocol:
    """Stands in for AtemProtocol in the uploader's connect loop."""

    def __init__(self, ip=None, aggressive_drain=False):
        self.ip = ip
        self.transport = _FakeTransport()
        self.mixerstate = {}
        self.aborts = 0
        self._handlers = {}
        self._next_id = 0

    def connect(self):
        pass

    def loop(self):
        pass

    def on(self, event, cb):
        self._next_id += 1
        self._handlers[self._next_id] = (event, cb)
        return self._next_id

    def off(self, event, cb_id):
        del self._handlers[cb_id]

    def abort_transfers(self):
        self.aborts += 1

    def upload(self, **kw):
        pass


# ---------------------------------------------------------------------------
# uploader: goodbye on every exit path
# ---------------------------------------------------------------------------

def test_run_for_single_ip_says_goodbye_on_unexpected_exception(monkeypatch):
    """The body raising (here: 'video-mode' missing from mixerstate — a
    KeyError before the first upload) must STILL send the protocol
    goodbye. Pre-fix, the close call sat after the body with no finally,
    so any exception abandoned the session with whatever it held."""
    proto_holder = {}

    class _P(_FakeUploadProtocol):
        def __init__(self, ip=None, aggressive_drain=False):
            super().__init__(ip, aggressive_drain)
            proto_holder['p'] = self
            # mixerstate stays EMPTY -> body raises KeyError('video-mode')

    monkeypatch.setattr(U, 'AtemProtocol', _P)
    monkeypatch.setattr(U, 'wait_ready', lambda p, timeout, pump_interval: None)

    with pytest.raises(KeyError):
        U._run_for_single_ip(
            '10.9.9.9', [(0, '/nonexistent.png')],
            True, U._Canceller(None), lambda m: None,
        )

    assert proto_holder['p'].transport.goodbyes == 1


def test_run_for_single_ip_says_goodbye_on_normal_path(monkeypatch):
    proto_holder = {}

    class _P(_FakeUploadProtocol):
        def __init__(self, ip=None, aggressive_drain=False):
            super().__init__(ip, aggressive_drain)
            proto_holder['p'] = self
            self.mixerstate = {
                'video-mode': types.SimpleNamespace(get_resolution=lambda: (16, 9)),
            }

    monkeypatch.setattr(U, 'AtemProtocol', _P)
    monkeypatch.setattr(U, 'wait_ready', lambda p, timeout, pump_interval: None)

    out = U._run_for_single_ip(
        '10.9.9.8', [], True, U._Canceller(None), lambda m: None,
    )

    assert out == []
    assert proto_holder['p'].transport.goodbyes == 1


# ---------------------------------------------------------------------------
# uploader: failed upload aborts the transfer (lane un-wedged, lock freed)
# ---------------------------------------------------------------------------

def _tiny_png(tmp_path):
    from PIL import Image
    path = tmp_path / 'tiny.png'
    Image.new('RGB', (16, 9), (10, 20, 30)).save(path)
    return str(path)


def test_upload_one_timeout_aborts_transfers(monkeypatch, tmp_path):
    p = _FakeUploadProtocol()
    p.mixerstate = {'mediaplayer-file-info': {}}
    monkeypatch.setattr(U, 'UPLOAD_TIMEOUT', 0.05)

    result = U._upload_one(
        p, 0, _tiny_png(tmp_path), 16, 9,
        U._Canceller(None), lambda m: None, True,
    )

    assert result.success is False
    assert 'timeout' in result.error
    assert p.aborts == 1                       # lane un-wedged, lock released
    assert p._handlers == {}                   # upload-done handler removed


def test_upload_one_cancel_aborts_transfers(monkeypatch, tmp_path):
    p = _FakeUploadProtocol()
    p.mixerstate = {'mediaplayer-file-info': {}}
    cancelled = {'v': False}

    def _check():
        was = cancelled['v']
        cancelled['v'] = True                  # cancel at the SECOND poll
        return was

    result = U._upload_one(
        p, 0, _tiny_png(tmp_path), 16, 9,
        U._Canceller(_check), lambda m: None, True,
    )

    assert result.success is False
    assert 'cancel' in result.error
    assert p.aborts == 1
    assert p._handlers == {}


def test_upload_one_upload_raise_aborts_transfers(monkeypatch, tmp_path):
    class _P(_FakeUploadProtocol):
        def upload(self, **kw):
            raise RuntimeError('wire died')

    p = _P()
    p.mixerstate = {'mediaplayer-file-info': {}}

    result = U._upload_one(
        p, 0, _tiny_png(tmp_path), 16, 9,
        U._Canceller(None), lambda m: None, True,
    )

    assert result.success is False
    assert 'upload() raised' in result.error
    assert p.aborts == 1


# ---------------------------------------------------------------------------
# protocol: fatal FTDE pops the dead task and advances the lane
# ---------------------------------------------------------------------------

def _bare_protocol():
    p = AtemProtocol(ip='127.0.0.1')
    sent = []
    p.send_commands = lambda cmds: sent.append(cmds)
    return p, sent


@pytest.fixture
def clock(monkeypatch):
    """atemwire.protocol's clock, moved by hand: the lock's turn for other
    clients (bmdwire 1.5.2) waits for the store to be announced free and a
    frame more (LOCK_COURTESY)."""
    t = [1000.0]
    monkeypatch.setattr('atemwire.protocol.time.monotonic', lambda: t[0])
    return t


def _announced_free_and_a_frame(p, clock):
    from atemwire.protocol import LOCK_COURTESY
    p.save_field_data(b'LKST', _lkst_unlocked())
    clock[0] += LOCK_COURTESY
    p._service_yields()


def test_ftde_fatal_status_pops_task_and_advances(clock):
    """Per-frame lock discipline (2026-07-07): the fatal FTDE pops the
    corpse and RELEASES the lock. Since bmdwire 1.5.2 the next task's
    request waits until the store is announced free and a frame more, so
    another client waiting for the store (ATEM Software Control) has its
    turn instead of being aborted with FTDE 6."""
    from atemwire.protocol import TransferTask
    from atemwire.messages.lock import LockCommand

    p, sent = _bare_protocol()
    try:
        t1 = TransferTask(0, 3)
        t2 = TransferTask(0, 7)
        p.transfer_queue = {0: [t1, t2]}
        p.transfer = t1
        p.transfer.tid = 41
        p.transfer_requested = True
        p.locks[0] = True                       # we hold the lock

        # FTDE status=2 (not-found / rejected) for tid 41
        p.save_field_data(b'FTDE', (41).to_bytes(2, 'big') + bytes([2, 0]))

        assert p.transfer_queue[0] == [t2]      # corpse popped
        assert p.transfer is None
        assert p.locks.get(0) is False          # lock released per-frame
        from atemwire.messages.lock import PartialLockCommand
        assert [type(c) for c in sent[-1]] == [LockCommand]   # alone

        # Announced free, a frame passes: t2 asks.
        _announced_free_and_a_frame(p, clock)
        assert [type(c) for c in sent[-1]] == [PartialLockCommand]
        assert sent[-1][0].slot == 7

        # LKOB grant -> FTSU for t2 goes out
        p.save_field_data(b'LKOB', bytes([0, 0, 0, 0]))
        assert p.transfer is t2
        assert p.transfer_requested is True
    finally:
        try:
            p.transport.sock.close()
        except Exception:
            pass


def test_ftde_fatal_status_releases_lock_when_queue_empties():
    from atemwire.protocol import TransferTask
    from atemwire.messages.lock import LockCommand

    p, sent = _bare_protocol()
    try:
        t1 = TransferTask(0, 3)
        p.transfer_queue = {0: [t1]}
        p.transfer = t1
        p.transfer.tid = 42
        p.transfer_requested = True
        p.locks[0] = True

        p.save_field_data(b'FTDE', (42).to_bytes(2, 'big') + bytes([2, 0]))

        assert p.transfer_queue[0] == []
        # queue empty -> trigger's cleanup path sends the unlock
        unlocks = [c for batch in sent for c in batch
                   if isinstance(c, LockCommand)]
        assert len(unlocks) == 1
    finally:
        try:
            p.transport.sock.close()
        except Exception:
            pass


def test_ftde_try_again_still_retries():
    from atemwire.protocol import TransferTask

    p, sent = _bare_protocol()
    try:
        t1 = TransferTask(0, 3)
        p.transfer_queue = {0: [t1]}
        p.transfer = t1
        p.transfer.tid = 43
        p.transfer_requested = True
        p.locks[0] = True

        p.save_field_data(b'FTDE', (43).to_bytes(2, 'big') + bytes([1, 0]))

        assert p.transfer_queue[0] == [t1]      # task kept for retry
        assert p.transfer_requested is True     # re-requested
    finally:
        try:
            p.transport.sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# pool: atexit goodbye for live pooled connections
# ---------------------------------------------------------------------------

def test_pool_atexit_sends_goodbye_for_live_instances():
    transport = _FakeTransport()
    fake_conn = types.SimpleNamespace(
        ip_address='10.7.7.7',
        _protocol=types.SimpleNamespace(transport=transport),
    )
    with ATEMInstanceManager._instance_lock:
        ATEMInstanceManager._instances['__test_atexit__'] = {
            'connection': fake_conn,
        }
    try:
        close_all_sessions_at_exit()
        assert transport.goodbyes == 1
    finally:
        with ATEMInstanceManager._instance_lock:
            ATEMInstanceManager._instances.pop('__test_atexit__', None)


def test_pool_atexit_survives_dead_entries():
    with ATEMInstanceManager._instance_lock:
        ATEMInstanceManager._instances['__test_atexit2__'] = {
            'connection': types.SimpleNamespace(ip_address='x', _protocol=None),
        }
        ATEMInstanceManager._instances['__test_atexit3__'] = {'connection': None}
    try:
        close_all_sessions_at_exit()          # must not raise
    finally:
        with ATEMInstanceManager._instance_lock:
            ATEMInstanceManager._instances.pop('__test_atexit2__', None)
            ATEMInstanceManager._instances.pop('__test_atexit3__', None)


# ---------------------------------------------------------------------------
# Per-frame lock discipline + LKST pounce (ASC parity, 2026-07-07)
#
# The old whole-queue lock hold kept the ATEM's media lock for an entire
# download sweep (40s+ on a full pool) — ASC's media page showed LOCKED and
# its fetches starved (observed live on a freshly rebooted switcher), then
# the roles ping-ponged. Now: release after EVERY frame; the next queued
# transfer re-requests on the release's LKST echo; a FOREIGN holder's
# release broadcast triggers the same pounce.
# ---------------------------------------------------------------------------

def _lkst_unlocked():
    return bytes([0, 0, 0, 0x13])              # store 0, state 0


def test_ftdc_releases_lock_per_frame_and_echo_continues_queue(clock):
    from atemwire.protocol import TransferTask
    from atemwire.messages.lock import LockCommand, PartialLockCommand

    p, sent = _bare_protocol()
    try:
        t1 = TransferTask(0, 3)
        t2 = TransferTask(0, 7)
        p.transfer_queue = {0: [t1, t2]}
        p.transfer = t1
        p.transfer.tid = 51
        p.transfer_requested = True
        p.locks[0] = True
        p.transfer_buffer = [b'']               # empty frame; rle_decode(b'')
        p.transfer_buffer_bytes = 0
        done = []
        p.on('download-done', lambda st, s, d: done.append((st, s)))

        # FTDC for t1
        p.save_field_data(b'FTDC', (51).to_bytes(2, 'big') + bytes([1, 2]))

        assert done == [(0, 3)]                 # frame delivered
        assert p.transfer_queue[0] == [t2]
        assert p.locks.get(0) is False          # released after ONE frame
        assert p.transfer is None
        # The release alone; t2 waits for the store to be seen free, so
        # another client waiting for it gets its turn (bmdwire 1.5.2).
        assert [type(c) for c in sent[-1]] == [LockCommand]
        assert p.transfer_requested is False

        _announced_free_and_a_frame(p, clock)
        assert [type(c) for c in sent[-1]] == [PartialLockCommand]

        p.save_field_data(b'LKOB', bytes([0, 0, 0, 0]))
        assert p.transfer is t2                 # queue continues
        assert p.transfer_requested is True
    finally:
        try:
            p.transport.sock.close()
        except Exception:
            pass


def test_foreign_lock_release_broadcast_pounces_waiting_queue(clock):
    """A transfer queued while ANOTHER client holds the lock: their release
    LKST must trigger a re-request, not a blind 20s-timeout retry. Since
    bmdwire 1.5.2 it waits a frame first (LOCK_COURTESY): a client that
    releases and takes the store again for its next transfer is announced
    as a release and a lock together, and a request sent on the release
    would abort its next transfer."""
    from atemwire.protocol import LOCK_COURTESY, TransferTask
    from atemwire.messages.lock import PartialLockCommand

    p, sent = _bare_protocol()
    try:
        t1 = TransferTask(0, 5)
        p.transfer_queue = {0: [t1]}
        p.transfer = None
        p.transfer_requested = False
        # no lock held; our earlier request went unanswered (foreign holder)

        p.save_field_data(b'LKST', _lkst_unlocked())   # THEY released
        plcks = [c for batch in sent for c in batch
                 if isinstance(c, PartialLockCommand)]
        assert plcks == []                      # a frame for them first

        clock[0] += LOCK_COURTESY
        p._service_yields()
        plcks = [c for batch in sent for c in batch
                 if isinstance(c, PartialLockCommand)]
        assert len(plcks) == 1                  # then we ask
    finally:
        try:
            p.transport.sock.close()
        except Exception:
            pass


def test_lkst_release_with_empty_queue_is_calm():
    """Nothing queued: a release sends nothing. Since atemwire the LKST
    handler no longer returns early — after the pounce check it falls
    through to the generic state path, so the release also lands in
    ``mixerstate['lock-state']`` and fires ``change:lock-state`` (the
    vendored copy short-circuited both). Pinned here so a future
    early-return regression is caught."""
    p, sent = _bare_protocol()
    try:
        seen = []
        p.on('change:lock-state', lambda c: seen.append(c))
        p.save_field_data(b'LKST', _lkst_unlocked())
        assert sent == []                       # nothing to do, nothing sent
        assert p.mixerstate['lock-state'].store == 0
        assert not p.mixerstate['lock-state'].state
        assert len(seen) == 1                   # the release is now an event
    finally:
        try:
            p.transport.sock.close()
        except Exception:
            pass


def test_a_download_queued_after_our_release_waits_its_turn(clock):
    """The media pool watcher queues one still at a time, the next after
    decoding the last. Nothing is queued at the release, and the request
    that follows a moment later waits too, until the store is announced
    free and a frame more: another client waiting for the store (ATEM
    Software Control fetching its thumbnails) is handed it at the release,
    and a request of ours then would be granted beside it and abort its
    transfer (bmdwire 1.5.2, from captures of 2026-10-15)."""
    import struct

    from atemwire.protocol import TransferTask
    from atemwire.messages.lock import PartialLockCommand

    p, sent = _bare_protocol()
    try:
        t1 = TransferTask(0, 3)
        p.transfer_queue = {0: [t1]}
        p.transfer = t1
        p.transfer.tid = 61
        p.transfer_requested = True
        p.locks[0] = True
        p.transfer_buffer = [b'']

        # FTDC completes t1 -> the per-frame unlock goes out
        p.save_field_data(b'FTDC', (61).to_bytes(2, 'big') + bytes([1, 2]))
        assert p.locks.get(0) is False

        # the caller queues the next download a moment later: it waits
        clock[0] += 0.25
        p.download(0, 5)
        plcks = [c for batch in sent for c in batch
                 if isinstance(c, PartialLockCommand)]
        assert plcks == []

        # announced free, a frame passes: it asks, and is granted
        _announced_free_and_a_frame(p, clock)
        plcks = [c for batch in sent for c in batch
                 if isinstance(c, PartialLockCommand)]
        assert len(plcks) == 1
        p.save_field_data(b'LKOB', struct.pack('>Hxx', 0))
        assert p.locks.get(0) is True
    finally:
        try:
            p.transport.sock.close()
        except Exception:
            pass


def test_abort_clears_the_lock_request():
    p, sent = _bare_protocol()
    try:
        p._lock_requested[0] = 123.0
        p.abort_transfers()
        assert p._lock_requested == {}
    finally:
        try:
            p.transport.sock.close()
        except Exception:
            pass
