"""Unit tests for the media-pool watcher's content-hash thumbnail cache
(added 2026-06-30 to make media-pool opens fast-on-connect while always
reflecting the ATEM's current pool).

Contract under test:
  * SPEED  — an MPfe whose content hash is already cached restores the thumb
             and does NOT enqueue a download; _do_raw_download short-circuits
             on a cache hit (no socket transfer).
  * ACCURACY — a slot whose content changed (new hash) never shows the old
             cached image; a cleared slot shows empty; a zero/empty hash is
             never cached or matched.
  * RESILIENCE — _do_raw_download retries once on FTDE code 1, then gives up
             without crashing (leaves a spinner, not an exception).
  * cache helpers — put/get round-trip, empty-hash no-op, bounded LRU.

These run without a live ATEM; the decode (C extension) path is monkeypatched
where a successful download is simulated.
"""

import pytest

from atem_control.media_pool import watcher as W


@pytest.fixture(autouse=True)
def _clear_cache():
    W._thumb_cache.clear()
    yield
    W._thumb_cache.clear()


class _FakeMPfe:
    """Minimal stand-in for a mediaplayer-file-info (MPfe) Recv."""

    def __init__(self, index, is_used, hash_bytes, name=b'clip'):
        self.index = index
        self.is_used = is_used
        self.hash = hash_bytes
        self.name = name


def _drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def _downloads(q):
    return [it for it in _drain(q) if it and it[0] == W._TASK_DOWNLOAD]


# --------------------------------------------------------------------------
# cache helpers
# --------------------------------------------------------------------------

def test_cache_put_get_roundtrip():
    W._thumb_cache_put('1.2.3.4', 'ab' * 16, 'data:image/jpeg;base64,XYZ')
    assert W._thumb_cache_get('1.2.3.4', 'ab' * 16) == 'data:image/jpeg;base64,XYZ'
    # keyed by (ip, hash): same hash on a different IP is a miss
    assert W._thumb_cache_get('9.9.9.9', 'ab' * 16) is None


def test_cache_empty_or_zero_hash_is_noop():
    W._thumb_cache_put('ip', W.EMPTY_HASH_HEX, 'x')
    W._thumb_cache_put('ip', '', 'x')
    W._thumb_cache_put('ip', 'cd' * 16, '')        # empty thumb
    assert W._thumb_cache_get('ip', W.EMPTY_HASH_HEX) is None
    assert W._thumb_cache_get('ip', '') is None
    assert len(W._thumb_cache) == 0


def test_cache_lru_eviction_bounded():
    cap = W.THUMB_CACHE_MAX_ENTRIES
    for i in range(1, cap + 11):                    # i>=1 → never the zero hash
        W._thumb_cache_put('ip', f'{i:032x}', f't{i}')
    assert len(W._thumb_cache) <= cap
    assert W._thumb_cache_get('ip', f'{cap + 10:032x}') == f't{cap + 10}'   # MRU kept
    assert W._thumb_cache_get('ip', f'{1:032x}') is None                    # oldest gone


def test_cache_get_is_lru_touch():
    cap = W.THUMB_CACHE_MAX_ENTRIES
    for i in range(1, cap + 1):
        W._thumb_cache_put('ip', f'{i:032x}', f't{i}')
    assert W._thumb_cache_get('ip', f'{1:032x}') == 't1'   # touch → MRU
    W._thumb_cache_put('ip', f'{cap + 1:032x}', 'new')     # overflow by one
    assert W._thumb_cache_get('ip', f'{1:032x}') == 't1'   # survived (touched)
    assert W._thumb_cache_get('ip', f'{2:032x}') is None   # new LRU evicted


# --------------------------------------------------------------------------
# MPfe path — hash hit skips download (speed), miss downloads (accuracy)
# --------------------------------------------------------------------------

def test_mpfe_cache_hit_restores_thumb_and_skips_download():
    w = W.MediaPoolWatcher('10.0.0.11')
    w._connected_flag = True
    h = b'\x55' * 16
    hash_hex = W.md5_hex(h)
    W._thumb_cache_put('10.0.0.11', hash_hex, 'data:image/jpeg;base64,HIT')

    w._on_file_info(_FakeMPfe(2, True, h))

    assert w._slots[2]['thumb'] == 'data:image/jpeg;base64,HIT'
    assert _downloads(w._work_queue) == []          # SPEED: no re-download


def test_mpfe_cache_miss_enqueues_download():
    w = W.MediaPoolWatcher('10.0.0.12')
    w._connected_flag = True
    h = b'\x66' * 16
    hash_hex = W.md5_hex(h)

    w._on_file_info(_FakeMPfe(3, True, h))

    assert w._slots[3]['thumb'] is None
    assert _downloads(w._work_queue) == [(W._TASK_DOWNLOAD, 3, hash_hex)]   # new pulls


def test_mpfe_changed_hash_never_shows_old_cached_thumb():
    """ACCURACY: a slot whose content changed (new hash) must not show the old
    image, even though the old hash is still in the cache."""
    w = W.MediaPoolWatcher('10.0.0.13')
    w._connected_flag = True
    old, new = b'\x01' * 16, b'\x02' * 16
    old_hex, new_hex = W.md5_hex(old), W.md5_hex(new)
    W._thumb_cache_put('10.0.0.13', old_hex, 'data:image/jpeg;base64,OLD')

    w._on_file_info(_FakeMPfe(1, True, old))
    assert w._slots[1]['thumb'] == 'data:image/jpeg;base64,OLD'
    _drain(w._work_queue)

    w._on_file_info(_FakeMPfe(1, True, new))        # content replaced
    assert w._slots[1]['hash'] == new_hex
    assert w._slots[1]['thumb'] is None             # old image cleared
    assert _downloads(w._work_queue) == [(W._TASK_DOWNLOAD, 1, new_hex)]


def test_mpfe_cleared_slot_shows_empty_not_cached_thumb():
    """ACCURACY: a slot cleared on the ATEM (is_used=False, zero hash) shows
    empty — no stale thumb, no download."""
    w = W.MediaPoolWatcher('10.0.0.14')
    w._connected_flag = True
    h = b'\x07' * 16
    hash_hex = W.md5_hex(h)
    W._thumb_cache_put('10.0.0.14', hash_hex, 'data:image/jpeg;base64,IMG')

    w._on_file_info(_FakeMPfe(0, True, h))
    assert w._slots[0]['thumb'] == 'data:image/jpeg;base64,IMG'
    _drain(w._work_queue)

    w._on_file_info(_FakeMPfe(0, False, b'\x00' * 16))   # cleared
    assert w._slots[0]['isUsed'] is False
    assert w._slots[0]['thumb'] is None
    assert _downloads(w._work_queue) == []


def test_mpfe_download_gated_until_connected():
    """During the initial state dump (_connected_flag False) a cache MISS must
    not enqueue a download — _on_connected back-fills after the dump. A cache
    HIT still restores the thumb (no download needed)."""
    w = W.MediaPoolWatcher('10.0.0.15')
    w._connected_flag = False
    miss = b'\x09' * 16
    w._on_file_info(_FakeMPfe(4, True, miss))
    assert _downloads(w._work_queue) == []          # gated off during dump

    hit_bytes = b'\x0a' * 16
    W._thumb_cache_put('10.0.0.15', W.md5_hex(hit_bytes), 'data:image/jpeg;base64,H')
    w._on_file_info(_FakeMPfe(5, True, hit_bytes))
    assert w._slots[5]['thumb'] == 'data:image/jpeg;base64,H'   # cache hit always works
    assert _downloads(w._work_queue) == []


# --------------------------------------------------------------------------
# _do_download — cache short-circuit + retry-requeue (pooled native path)
# --------------------------------------------------------------------------

import types as _types


class _FakeStopEvent:
    """threading.Event stand-in whose wait() returns immediately — used
    where a test would otherwise sit through a real retry backoff."""

    def __init__(self, is_set=False):
        self._set = is_set

    def is_set(self):
        return self._set

    def set(self):
        self._set = True

    def clear(self):
        self._set = False

    def wait(self, timeout=None):
        return self._set


def _fake_conn(download=None, connected=True, mixerstate=None):
    """Stand-in for the pooled ATEMConnection: download_still + is_connected."""
    ns = _types.SimpleNamespace()
    ns.is_connected = connected
    ns.mixerstate = mixerstate or {}
    calls = []

    def _download_still(slot, timeout=0, **kw):
        calls.append(slot)
        if download is None:
            raise AssertionError('wire should not be touched')
        return download(slot)

    ns.download_still = _download_still
    ns._calls = calls
    return ns


def test_do_download_uses_cache_no_wire_download():
    w = W.MediaPoolWatcher('10.0.0.20')
    h = b'\x44' * 16
    hash_hex = W.md5_hex(h)
    W._thumb_cache_put('10.0.0.20', hash_hex, 'data:image/jpeg;base64,CACHED')
    w._slots[0] = {'index': 0, 'isUsed': True, 'hash': hash_hex,
                   'fileName': 'x', 'thumb': None}
    w._conn = _fake_conn()                                   # download would raise

    w._do_download(0, hash_hex)

    assert w._conn._calls == []                              # SPEED: never hit the wire
    assert w._slots[0]['thumb'] == 'data:image/jpeg;base64,CACHED'


def test_do_download_downloads_decodes_and_caches(monkeypatch):
    w = W.MediaPoolWatcher('10.0.0.21')
    h = b'\x33' * 16
    hash_hex = W.md5_hex(h)
    w._slots[0] = {'index': 0, 'isUsed': True, 'hash': hash_hex,
                   'fileName': 'x', 'thumb': None}
    w._conn = _fake_conn(download=lambda slot: b'rawbytes')

    monkeypatch.setattr(W, 'atem_to_rgb', lambda raw, w_, h_: b'rgba')
    monkeypatch.setattr(W, '_encode_thumbnail_from_rgba', lambda rgba, w_, h_: b'jpeg')

    w._do_download(0, hash_hex)

    assert w._conn._calls == [0]                             # one native transfer
    assert w._slots[0]['thumb'] is not None                  # applied
    assert W._thumb_cache_get('10.0.0.21', hash_hex) is not None   # cached for reuse


def test_do_download_requeues_on_failure():
    """A failed download must NOT be dropped on the floor — settled content
    gets no further MPfe, so a dropped task stranded the tile as a permanent
    spinner. The task re-queues itself with an incremented attempt count."""
    w = W.MediaPoolWatcher('10.0.0.22')
    h = b'\x77' * 16
    hash_hex = W.md5_hex(h)
    w._slots[0] = {'index': 0, 'isUsed': True, 'hash': hash_hex,
                   'fileName': 'x', 'thumb': None}

    def _fail(slot):
        raise TimeoutError('slot 0 timed out')
    w._conn = _fake_conn(download=_fail)

    w._do_download(0, hash_hex)                              # must not raise

    assert w._conn._calls == [0]
    assert w._slots[0]['thumb'] is None
    # re-queued as attempt 2 so the tile eventually resolves
    assert _downloads(w._work_queue) == [(W._TASK_DOWNLOAD, 0, hash_hex, 2, False)]


def test_do_download_dead_connection_requeues_without_wire():
    """No pooled connection (or a dead one) is a transient failure too —
    re-queue, don't strand, and never call the wire."""
    w = W.MediaPoolWatcher('10.0.0.27')
    h = b'\x79' * 16
    hash_hex = W.md5_hex(h)
    w._slots[0] = {'index': 0, 'isUsed': True, 'hash': hash_hex,
                   'fileName': 'x', 'thumb': None}
    w._conn = _fake_conn(connected=False)                    # dead pooled conn

    w._do_download(0, hash_hex)

    assert w._conn._calls == []
    assert _downloads(w._work_queue) == [(W._TASK_DOWNLOAD, 0, hash_hex, 2, False)]


def test_do_download_reconnect_gap_requeues_without_wire():
    """During a transport re-handshake the worker is alive (is_connected)
    but the state is empty (is_ready False, bmdwire >= 1.3). A download
    sent then waits out its timeout and reads as a foreign lock-holder;
    re-queue without touching the wire instead."""
    w = W.MediaPoolWatcher('10.0.0.28')
    h = b'\x7a' * 16
    hash_hex = W.md5_hex(h)
    w._slots[0] = {'index': 0, 'isUsed': True, 'hash': hash_hex,
                   'fileName': 'x', 'thumb': None}
    w._conn = _fake_conn(connected=True)
    w._conn.is_ready = False                                 # mid re-handshake

    w._do_download(0, hash_hex)

    assert w._conn._calls == []
    assert _downloads(w._work_queue) == [(W._TASK_DOWNLOAD, 0, hash_hex, 2, False)]


def test_do_download_never_gives_up_while_needed(monkeypatch):
    """There is NO terminal attempt (changed 2026-07-03): while the slot
    still needs its thumb the task re-queues forever — a foreign client
    holding the media lock for minutes used to exhaust a fixed budget and
    strand the pool as permanent spinners even after the lock freed. The
    per-task recheck retires the task the moment it stops being needed."""
    w = W.MediaPoolWatcher('10.0.0.24')
    h = b'\x78' * 16
    hash_hex = W.md5_hex(h)
    w._slots[0] = {'index': 0, 'isUsed': True, 'hash': hash_hex,
                   'fileName': 'x', 'thumb': None}

    def _fail(slot):
        raise TimeoutError('timed out waiting for transfer')
    w._conn = _fake_conn(download=_fail)
    w._stop_event = _FakeStopEvent()                         # skip the 30s backoff wait

    w._do_download(0, hash_hex, attempt=47)                  # deep into patience

    assert w._slots[0]['thumb'] is None
    assert _downloads(w._work_queue) == [(W._TASK_DOWNLOAD, 0, hash_hex, 48, False)]

    # ...but once the slot no longer needs it (cleared), the task retires.
    w2 = W.MediaPoolWatcher('10.0.0.28')
    w2._conn = _fake_conn(download=_fail)                    # would fail if hit
    w2._do_download(0, hash_hex, attempt=48)                 # no such slot
    assert _downloads(w2._work_queue) == []                  # retired, no loop


def test_requeue_missing_rearms_thumbless_slots_only():
    """Opening the media-pool modal re-arms every used slot with a settled
    hash and no thumb — the belt-and-suspenders that makes a visible pool
    always converge."""
    w = W.MediaPoolWatcher('10.0.0.29')
    w._slots[0] = {'index': 0, 'isUsed': True, 'hash': 'aa' * 16,
                   'fileName': 'x', 'thumb': None}            # needs re-arm
    w._slots[1] = {'index': 1, 'isUsed': True, 'hash': 'bb' * 16,
                   'fileName': 'x', 'thumb': 'data:done'}     # already thumbed
    w._slots[2] = {'index': 2, 'isUsed': False, 'hash': '',
                   'fileName': '', 'thumb': None}             # empty slot
    w._slots[3] = {'index': 3, 'isUsed': True, 'hash': 'cc' * 16,
                   'fileName': 'x', 'thumb': None}
    w.note_upload_started(3)                                  # uploader owns it

    w.requeue_missing()

    assert _downloads(w._work_queue) == [(W._TASK_DOWNLOAD, 0, 'aa' * 16)]


def test_worker_dispatch_carries_attempt_count(monkeypatch):
    """The worker must unpack 3-tuple (fresh), 4-tuple (an older retry) and
    5-tuple (retry) download tasks and pass the attempt, and whether the
    last one only waited its turn, through to _do_download."""
    w = W.MediaPoolWatcher('10.0.0.25')
    seen = []
    monkeypatch.setattr(
        w, '_do_download',
        lambda slot, h, attempt=1, waited=False:
        seen.append((slot, h, attempt, waited)),
    )
    w._work_queue.put_nowait((W._TASK_DOWNLOAD, 1, 'aa' * 16))
    w._work_queue.put_nowait((W._TASK_DOWNLOAD, 2, 'bb' * 16, 3))
    w._work_queue.put_nowait((W._TASK_DOWNLOAD, 3, 'cc' * 16, 4, True))
    w._work_queue.put_nowait((W._TASK_STOP,))

    w._run_worker()

    assert seen == [(1, 'aa' * 16, 1, False), (2, 'bb' * 16, 3, False),
                    (3, 'cc' * 16, 4, True)]


# --------------------------------------------------------------------------
# _sync_from_mixerstate — rebuild from the pooled connection's own state
# --------------------------------------------------------------------------

def test_sync_from_mixerstate_prioritizes_player_loaded_slots():
    """On a (re)bind the slot view is rebuilt from the pooled connection's
    mixerstate; slots loaded in media players must be first in the download
    queue (operator-relevant), then the rest ascending."""
    w = W.MediaPoolWatcher('10.0.0.26')
    infos = {i: _FakeMPfe(i, True, bytes([i + 1]) * 16) for i in range(4)}
    players = {
        0: _types.SimpleNamespace(index=0, source_type=1, slot=2),
        1: _types.SimpleNamespace(index=1, source_type=1, slot=3),
    }
    w._conn = _fake_conn(mixerstate={
        'mediaplayer-slots': _types.SimpleNamespace(stills=4),
        'mediaplayer-file-info': infos,
        'mediaplayer-selected': players,
    })

    w._sync_from_mixerstate('test bind')

    order = [t[1] for t in _downloads(w._work_queue)]
    assert order == [2, 3, 0, 1]                            # players first, then ascending
    assert w._connected_flag is True
    assert all(w._slots[i]['isUsed'] for i in range(4))


def test_do_download_skips_when_hash_moved_on():
    """If the slot's hash changed after the task was queued, no download runs."""
    w = W.MediaPoolWatcher('10.0.0.23')
    queued_hex = W.md5_hex(b'\x11' * 16)
    current_hex = W.md5_hex(b'\x22' * 16)
    w._slots[0] = {'index': 0, 'isUsed': True, 'hash': current_hex,
                   'fileName': 'x', 'thumb': None}
    w._conn = _fake_conn(download=lambda slot: b'x')

    w._do_download(0, queued_hex)                            # stale task

    assert w._conn._calls == []


# --------------------------------------------------------------------------
# note_upload_finished — scheduled-upload (connected) vs lockout (dropped)
# --------------------------------------------------------------------------

def test_upload_finished_connected_requeues_thumbless_slot():
    """Scheduled uploads never drop the socket, so no reconnect re-dump (and
    no further MPfe for settled content) is coming. A slot with a settled
    hash but no thumb must have a live download task after the lockout
    clears — the old wipe-hash-and-hope stranded it as a permanent spinner.

    With pending-claim dedup the shape is: while the MPfe's task is still
    pending, note_upload_finished's re-queue is a no-op (exactly one task,
    no duplicate); once the claim is gone (task retired — e.g. a worker
    restart cleared it), note_upload_finished re-arms the slot itself."""
    w = W.MediaPoolWatcher('10.0.0.30')
    w._connected_flag = True
    h = b'\x77' * 16
    hash_hex = W.md5_hex(h)
    w._on_file_info(_FakeMPfe(4, True, h))       # cache miss -> download queued

    w.note_upload_finished(4)

    # deduped: exactly one task for the slot, not two
    assert _downloads(w._work_queue) == [(W._TASK_DOWNLOAD, 4, hash_hex)]
    # hash preserved (the old behaviour wiped it to '') so the task
    # hash-matches and the content cache stays usable
    assert w._slots[4]['hash'] == hash_hex

    # claim gone (task retired without a thumb) → re-queues a fresh task
    w._pending_downloads.clear()
    w.note_upload_finished(4)
    assert _downloads(w._work_queue) == [(W._TASK_DOWNLOAD, 4, hash_hex)]


def test_upload_finished_connected_leaves_settled_thumb_alone():
    """Identical-content re-upload: ATEM re-fires no MPfe and the displayed
    thumb is already correct (content-addressed). No wipe, no re-download."""
    w = W.MediaPoolWatcher('10.0.0.31')
    w._connected_flag = True
    h = b'\x88' * 16
    hash_hex = W.md5_hex(h)
    W._thumb_cache_put('10.0.0.31', hash_hex, 'data:image/jpeg;base64,KEEP')
    w._on_file_info(_FakeMPfe(5, True, h))       # cache hit -> thumb restored
    _drain(w._work_queue)

    w.note_upload_finished(5)

    assert w._slots[5]['thumb'] == 'data:image/jpeg;base64,KEEP'
    assert w._slots[5]['hash'] == hash_hex
    assert _downloads(w._work_queue) == []


def test_upload_finished_disconnected_defers_to_reconnect():
    """If the ATEM genuinely dropped mid-upload (real connection loss — the
    lockout itself no longer drops the socket), the supervisor's reconnect
    re-dumps all state. Nothing gets queued against a dead protocol, and the
    lockout is cleared."""
    w = W.MediaPoolWatcher('10.0.0.32')
    w._connected_flag = True
    h = b'\x99' * 16
    w._on_file_info(_FakeMPfe(6, True, h))
    _drain(w._work_queue)
    w.note_upload_started(6)
    w._connected_flag = False                    # ATEM died mid-upload

    w.note_upload_finished(6)

    assert _downloads(w._work_queue) == []
    assert 6 not in w._upload_locks


# --------------------------------------------------------------------------
# Lockout keeps the session alive (2026-07-02): the abrupt socket-drop was
# observed to wedge switchers' network engines. The watcher now stays
# connected through uploads — metadata flows, downloads defer.
# --------------------------------------------------------------------------

def test_mpfe_during_lockout_tracks_metadata_but_defers_download():
    """Full drag-drop chain with a live watcher: the mid-upload MPfe must
    update the slot's hash (it's the only carrier of the new content's
    identity) but must NOT trigger a download while the uploader owns the
    wire. After note_upload_finished, the refresh keys on that hash."""
    w = W.MediaPoolWatcher('10.0.0.40')
    w._connected_flag = True
    old, new = b'\x21' * 16, b'\x42' * 16
    w._on_file_info(_FakeMPfe(7, True, old))
    _drain(w._work_queue)

    w.note_upload_started(7)
    w._on_file_info(_FakeMPfe(7, True, new))     # mid-upload MPfe

    new_hex = W.md5_hex(new)
    assert w._slots[7]['hash'] == new_hex        # metadata tracked
    assert w._slots[7]['thumb'] is None          # old thumb dropped (hash changed)
    assert _downloads(w._work_queue) == []       # download deferred

    w.note_upload_finished(7)
    assert _downloads(w._work_queue) == [(W._TASK_DOWNLOAD, 7, new_hex)]


def test_worker_defers_all_downloads_while_any_upload_active(monkeypatch):
    """While ANY slot's upload is in flight, the worker must not take the
    still-store lock at all — even for a different slot. The task is
    re-queued, not dropped."""
    w = W.MediaPoolWatcher('10.0.0.41')
    w._connected_flag = True
    h = b'\x53' * 16
    hash_hex = W.md5_hex(h)
    w._slots[0] = {'index': 0, 'isUsed': True, 'hash': hash_hex,
                   'fileName': 'x', 'thumb': None}
    w._conn = _fake_conn(download=lambda slot: b'x')
    w.note_upload_started(3)                     # upload on a DIFFERENT slot

    w._stop_event = _FakeStopEvent()             # skip the 0.5s defer wait

    w._do_download(0, hash_hex)

    assert w._conn._calls == []                  # never touched the wire
    assert _downloads(w._work_queue) == [(W._TASK_DOWNLOAD, 0, hash_hex)]  # deferred


# --------------------------------------------------------------------------
# Level-triggered convergence — divergence from mixerstate always heals
# --------------------------------------------------------------------------

def test_divergence_detected_when_bind_raced_the_dump():
    """The 'bound: 0 used' race: the watcher's view is empty but the pooled
    mixerstate has slots. Must read as diverged so the binder resyncs."""
    w = W.MediaPoolWatcher('10.0.0.50')
    conn = _fake_conn(mixerstate={
        'mediaplayer-file-info': {0: _FakeMPfe(0, True, b'\x11' * 16)},
    })
    assert w._diverged_from_mixerstate(conn) is True

    # after a sync, agreement is reached and no further resyncs churn
    w._conn = conn
    w._sync_from_mixerstate('test')
    assert w._diverged_from_mixerstate(conn) is False


def test_divergence_detected_on_hash_change():
    w = W.MediaPoolWatcher('10.0.0.51')
    h_old, h_new = b'\x22' * 16, b'\x33' * 16
    w._connected_flag = True
    w._on_file_info(_FakeMPfe(2, True, h_old))
    conn = _fake_conn(mixerstate={
        'mediaplayer-file-info': {2: _FakeMPfe(2, True, h_new)},
    })
    assert w._diverged_from_mixerstate(conn) is True


def test_no_divergence_when_in_agreement_including_cleared():
    w = W.MediaPoolWatcher('10.0.0.52')
    w._connected_flag = True
    w._on_file_info(_FakeMPfe(0, True, b'\x44' * 16))
    w._on_file_info(_FakeMPfe(1, False, b'\x00' * 16))
    conn = _fake_conn(mixerstate={
        'mediaplayer-file-info': {
            0: _FakeMPfe(0, True, b'\x44' * 16),
            1: _FakeMPfe(1, False, b'\x00' * 16),
        },
    })
    assert w._diverged_from_mixerstate(conn) is False


# --------------------------------------------------------------------------
# Pending-download claims — duplicate suppression + stop-aware dropping
# (added 2026-07-02 after a live foreign-lock-holder starve: 15s-per-attempt
# downloads let the 5s heal tick + modal opens stack ~10 duplicate tasks
# per slot, which then drained as a log flood during teardown)
# --------------------------------------------------------------------------

def test_queue_download_deduplicates_same_slot_and_hash():
    w = W.MediaPoolWatcher('10.0.0.60')
    assert w._queue_download(0, 'aa' * 16) is True
    assert w._queue_download(0, 'aa' * 16) is False          # duplicate refused
    assert w._queue_download(0, 'aa' * 16) is False
    assert _downloads(w._work_queue) == [(W._TASK_DOWNLOAD, 0, 'aa' * 16)]


def test_queue_download_new_hash_supersedes_stale_claim():
    """Content changed while a task for the old hash was pending: the new
    hash must enqueue (not be suppressed), and the stale task's retirement
    must not release the NEW claim."""
    w = W.MediaPoolWatcher('10.0.0.61')
    w._queue_download(0, 'aa' * 16)
    assert w._queue_download(0, 'bb' * 16) is True           # supersedes
    w._slots[0] = {'index': 0, 'isUsed': True, 'hash': 'bb' * 16,
                   'fileName': 'x', 'thumb': None}
    w._conn = _fake_conn()                                   # wire must not be hit
    w._do_download(0, 'aa' * 16)                             # the stale task retires
    assert w._pending_downloads[0] == 'bb' * 16              # new claim intact
    assert w._queue_download(0, 'bb' * 16) is False          # still deduped


def test_requeue_missing_noop_while_task_pending():
    """The heal tick + modal opens fire requeue_missing repeatedly while a
    slow download grinds against a foreign lock-holder — they must not
    stack duplicate tasks behind it."""
    w = W.MediaPoolWatcher('10.0.0.62')
    w._slots[0] = {'index': 0, 'isUsed': True, 'hash': 'aa' * 16,
                   'fileName': 'x', 'thumb': None}
    w.requeue_missing()
    w.requeue_missing(reason='heal tick')
    w.requeue_missing(reason='heal tick')
    assert _downloads(w._work_queue) == [(W._TASK_DOWNLOAD, 0, 'aa' * 16)]


def test_failed_download_keeps_claim_success_releases_it(monkeypatch):
    """The retry chain owns the slot's claim across attempts (so the log's
    attempt counter is honest and producers stay deduped); a success — or
    any retirement — releases it."""
    w = W.MediaPoolWatcher('10.0.0.63')
    hash_hex = 'aa' * 16
    w._slots[0] = {'index': 0, 'isUsed': True, 'hash': hash_hex,
                   'fileName': 'x', 'thumb': None}
    w._stop_event = _FakeStopEvent()                         # skip backoff waits

    def _fail(slot):
        raise TimeoutError('timed out')
    w._conn = _fake_conn(download=_fail)
    assert w._queue_download(0, hash_hex) is True
    w._do_download(0, hash_hex)                              # fails → retry queued
    assert w._pending_downloads == {0: hash_hex}             # claim retained
    assert w._queue_download(0, hash_hex) is False           # producers still deduped

    w._conn = _fake_conn(download=lambda slot: b'rawbytes')
    monkeypatch.setattr(W, 'atem_to_rgb', lambda raw, w_, h_: b'rgba')
    monkeypatch.setattr(W, '_encode_thumbnail_from_rgba', lambda rgba, w_, h_: b'jpeg')
    w._do_download(0, hash_hex, attempt=2)                   # succeeds
    assert w._pending_downloads == {}                        # claim released
    assert w._slots[0]['thumb'] is not None


def test_stop_drops_download_tasks_without_retry_or_noise():
    """Teardown must not grind the queued backlog against a dead connection
    (the 2026-07-02 'pooled connection not ready' log flood): once the stop
    event is set, download tasks drop instantly — no wire, no requeue —
    and release their claim."""
    w = W.MediaPoolWatcher('10.0.0.64')
    hash_hex = 'aa' * 16
    w._slots[0] = {'index': 0, 'isUsed': True, 'hash': hash_hex,
                   'fileName': 'x', 'thumb': None}
    w._conn = _fake_conn()                                   # wire must not be hit
    w._queue_download(0, hash_hex)
    w._stop_event.set()

    w._do_download(0, hash_hex)

    assert w._conn._calls == []
    assert w._pending_downloads == {}                        # claim released
    # only the original producer task remains; no retry was appended
    assert _downloads(w._work_queue) == [(W._TASK_DOWNLOAD, 0, hash_hex)]


def test_worker_restart_clears_stale_claims():
    """acquire() replaces the work queue when (re)starting the worker — the
    claims from the old queue's abandoned tasks must go with it, or they
    would suppress every fresh enqueue for those slots forever."""
    w = W.MediaPoolWatcher('10.0.0.65')
    w._queue_download(0, 'aa' * 16)                          # stale claim
    w.acquire()                                              # starts fresh worker
    try:
        assert w._pending_downloads == {}
        assert w._queue_download(0, 'aa' * 16) is True       # not suppressed
    finally:
        w._stop_event.set()
        w._work_queue.put_nowait((W._TASK_STOP,))
        w.release()


# --------------------------------------------------------------------------
# Foreign lock-holder surfacing (added 2026-07-06) — a download timing out
# with the store lock never granted flips poolLocked (broadcast + snapshot);
# a successful wire download clears it. Verified live 2026-07-02: while any
# client holds the media lock, NO other client can download (ASC included).
# --------------------------------------------------------------------------

def _lock_starve_failure(slot):
    raise TimeoutError(
        'download: store 0 slot 0 on 10.0.0.70 timed out (15.0s) — media '
        'store lock never granted; another client is holding it')


class _WaitLog(_FakeStopEvent):
    """_FakeStopEvent that records each backoff it was asked to sit."""

    def __init__(self):
        super().__init__()
        self.waits = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return False


def _needing(w, hash_hex):
    w._slots[0] = dict(W._empty_slot(0), isUsed=True, hash=hash_hex, fileName='x')


def test_a_turn_wait_retries_at_once_and_reports_no_lock():
    # Another client's turn (ATEM Software Control loading its thumbnails):
    # the tile keeps its spinner, nothing says locked, and the download is
    # queued again marked as having only waited.
    w = W.MediaPoolWatcher('10.0.0.70')
    hash_hex = 'aa' * 16
    _needing(w, hash_hex)
    w._conn = _fake_conn(download=_lock_starve_failure)
    w._stop_event = _WaitLog()

    w._do_download(0, hash_hex)

    items = _drain(w._work_queue)
    assert [it for it in items if it[0] == W._TASK_BROADCAST] == []
    assert [it for it in items if it[0] == W._TASK_DOWNLOAD] == [
        (W._TASK_DOWNLOAD, 0, hash_hex, 2, True)]
    assert 'poolLocked' not in w.get_snapshot()


def test_a_retry_after_a_turn_wait_has_no_backoff():
    w = W.MediaPoolWatcher('10.0.0.71')
    hash_hex = 'aa' * 16
    _needing(w, hash_hex)
    w._conn = _fake_conn(download=_lock_starve_failure)
    w._stop_event = _WaitLog()

    w._do_download(0, hash_hex, attempt=5, waited=True)

    assert w._stop_event.waits == [], 'sat a backoff after only waiting its turn'


def test_other_failures_keep_the_backoff():
    """A timeout WITHOUT the never-granted hint (lock obtained, data
    stalled) is a real failure: the retry backs off as before."""
    w = W.MediaPoolWatcher('10.0.0.73')
    hash_hex = 'aa' * 16
    _needing(w, hash_hex)

    def _fail(slot):
        raise TimeoutError('download: store 0 slot 0 on 10.0.0.73 timed out (15.0s)')
    w._conn = _fake_conn(download=_fail)
    w._stop_event = _WaitLog()

    w._do_download(0, hash_hex, attempt=2)

    assert w._stop_event.waits == [W.DOWNLOAD_RETRY_BACKOFF]
    assert _downloads(w._work_queue) == [(W._TASK_DOWNLOAD, 0, hash_hex, 3, False)]


# --------------------------------------------------------------------------
# seed_uploaded_thumb — webhook seeds the cache with the uploader's verified
# hash so the post-upload refresh never re-downloads what we just sent
# (added 2026-07-07: removes the contention window in which ASC's one-shot
# preview fetch lost the race and showed its placeholder circle)
# --------------------------------------------------------------------------

def test_seed_applies_thumb_when_mpfe_already_arrived():
    """MPfe landed first (slot carries the new hash, download queued):
    seeding applies the thumb immediately; the queued task then retires on
    its recheck without touching the wire."""
    w = W.MediaPoolWatcher('10.0.0.80')
    W._watchers['10.0.0.80'] = w
    try:
        hash_hex = 'ab' * 16
        w._slots[4] = {'index': 4, 'isUsed': True, 'hash': hash_hex,
                       'fileName': 'x', 'thumb': None}
        w._queue_download(4, hash_hex)          # the post-upload refresh task

        W.seed_uploaded_thumb('10.0.0.80', 4, hash_hex, 'data:image/jpeg;base64,SEED')

        assert w._slots[4]['thumb'] == 'data:image/jpeg;base64,SEED'
        assert W._thumb_cache_get('10.0.0.80', hash_hex) == 'data:image/jpeg;base64,SEED'
        # the queued task retires without a wire download
        w._conn = _fake_conn()                  # download would raise
        w._do_download(4, hash_hex)
        assert w._conn._calls == []
    finally:
        del W._watchers['10.0.0.80']


def test_seed_before_mpfe_caches_for_later_hit():
    """Webhook beat the MPfe: the slot still shows the OLD hash. Seeding
    must not stamp the thumb onto the old content — it caches, and the
    arriving MPfe cache-hits with no download queued."""
    w = W.MediaPoolWatcher('10.0.0.81')
    W._watchers['10.0.0.81'] = w
    try:
        w._connected_flag = True
        old = b'\x11' * 16
        new = b'\x22' * 16
        w._on_file_info(_FakeMPfe(2, True, old))
        _drain(w._work_queue)
        old_thumb = 'data:image/jpeg;base64,OLD'
        w._slots[2]['thumb'] = old_thumb

        W.seed_uploaded_thumb('10.0.0.81', 2, W.md5_hex(new), 'data:image/jpeg;base64,NEW')

        assert w._slots[2]['thumb'] == old_thumb          # old content untouched
        w._on_file_info(_FakeMPfe(2, True, new))          # MPfe arrives
        assert w._slots[2]['thumb'] == 'data:image/jpeg;base64,NEW'
        assert _downloads(w._work_queue) == []            # no wire transfer
    finally:
        del W._watchers['10.0.0.81']


def test_seed_without_watcher_still_populates_cache():
    W.seed_uploaded_thumb('10.0.0.82', 0, 'cd' * 16, 'data:image/jpeg;base64,T')
    assert W._thumb_cache_get('10.0.0.82', 'cd' * 16) == 'data:image/jpeg;base64,T'


def test_seed_rejects_empty_or_zero_hash():
    W.seed_uploaded_thumb('10.0.0.83', 0, '', 'x')
    W.seed_uploaded_thumb('10.0.0.83', 0, W.EMPTY_HASH_HEX, 'x')
    W.seed_uploaded_thumb('10.0.0.83', 0, 'ee' * 16, '')
    assert W._thumb_cache_get('10.0.0.83', 'ee' * 16) is None
    assert len(W._thumb_cache) == 0


class _Clock:
    """time.monotonic stand-in the tests move by hand."""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _waiting_events(q):
    return [it[2] for it in _drain(q)
            if it[0] == W._TASK_BROADCAST and it[1] == 'preview_wait']


def _taking(clock, seconds, error):
    """A download that takes ``seconds`` on the clock, then raises."""
    def _download(slot):
        clock.t += seconds
        raise error
    return _download


TURN = TimeoutError('download: store 0 slot 0 on 10.0.0.80 timed out (15.0s) '
                    '— media store lock never granted; another client is holding it')
STALL = TimeoutError('download: store 0 slot 0 on 10.0.0.80 timed out (15.0s)')


def _watcher_needing(ip, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(W.time, 'monotonic', clock)
    w = W.MediaPoolWatcher(ip)
    hash_hex = 'aa' * 16
    _needing(w, hash_hex)
    w._pending_downloads[0] = hash_hex
    w._stop_event = _WaitLog()
    return w, clock, hash_hex


def test_a_short_turn_wait_says_nothing(monkeypatch):
    w, clock, h = _watcher_needing('10.0.0.80', monkeypatch)
    w._conn = _fake_conn(download=_taking(clock, 15, TURN))
    w._do_download(0, h)
    assert _waiting_events(w._work_queue) == []
    assert w.get_snapshot()['previewWaiting'] is False


def test_a_long_turn_wait_shows_the_notice_once(monkeypatch):
    w, clock, h = _watcher_needing('10.0.0.81', monkeypatch)
    w._conn = _fake_conn(download=_taking(clock, 15, TURN))
    for attempt in (1, 2, 3):
        w._do_download(0, h, attempt=attempt, waited=attempt > 1)
    assert _waiting_events(w._work_queue) == [{'waiting': True}]
    assert w.get_snapshot()['previewWaiting'] is True


def test_the_notice_clears_when_a_preview_comes_through(monkeypatch):
    w, clock, h = _watcher_needing('10.0.0.82', monkeypatch)
    w._preview_waiting, w._waiting_since = True, clock.t - 60
    w._conn = _fake_conn(download=lambda slot: b'rawbytes')
    monkeypatch.setattr(W, 'atem_to_rgb', lambda raw, w_, h_: b'rgba')
    monkeypatch.setattr(W, '_encode_thumbnail_from_rgba', lambda rgba, w_, h_: b'jpeg')
    w._do_download(0, h)
    assert _waiting_events(w._work_queue) == [{'waiting': False}]


def test_the_notice_clears_when_nothing_is_left_to_fetch(monkeypatch):
    # The last preview came from the still cache: no download says the wait
    # is over, so the empty queue of downloads has to.
    w, clock, h = _watcher_needing('10.0.0.83', monkeypatch)
    w._preview_waiting, w._waiting_since = True, clock.t - 60
    w._slots[0]['thumb'] = 'data:image/jpeg;base64,FROMCACHE'
    w._conn = _fake_conn()                        # the wire is never touched
    w._do_download(0, h)
    assert w._pending_downloads == {}
    assert _waiting_events(w._work_queue) == [{'waiting': False}]


def test_a_tile_says_unavailable_after_two_minutes_of_failures(monkeypatch):
    w, clock, h = _watcher_needing('10.0.0.84', monkeypatch)
    w._conn = _fake_conn(download=_taking(clock, 15, STALL))
    w._do_download(0, h)
    assert w._slots[0]['previewFailed'] is False
    clock.t += W.PREVIEW_FAILED_AFTER
    w._do_download(0, h, attempt=2)
    assert w._slots[0]['previewFailed'] is True
    updates = [it[2]['slot'] for it in _drain(w._work_queue)
               if it[0] == W._TASK_BROADCAST and it[1] == 'slot_updated']
    assert [u['previewFailed'] for u in updates] == [True]


def test_waiting_its_turn_never_marks_a_tile(monkeypatch):
    w, clock, h = _watcher_needing('10.0.0.85', monkeypatch)
    w._conn = _fake_conn(download=_taking(clock, 15, TURN))
    for attempt in range(1, 30):                 # over seven minutes waiting
        w._do_download(0, h, attempt=attempt, waited=attempt > 1)
    assert w._slots[0]['previewFailed'] is False


def test_the_picture_replaces_the_marker(monkeypatch):
    w, clock, h = _watcher_needing('10.0.0.86', monkeypatch)
    w._slots[0]['previewFailed'] = True
    w._failing_since[0] = clock.t - 300
    w._conn = _fake_conn(download=lambda slot: b'rawbytes')
    monkeypatch.setattr(W, 'atem_to_rgb', lambda raw, w_, h_: b'rgba')
    monkeypatch.setattr(W, '_encode_thumbnail_from_rgba', lambda rgba, w_, h_: b'jpeg')
    w._do_download(0, h)
    assert w._slots[0]['thumb'] and w._slots[0]['previewFailed'] is False
    assert 0 not in w._failing_since


def test_a_new_picture_in_the_slot_starts_clean(monkeypatch):
    w, clock, h = _watcher_needing('10.0.0.87', monkeypatch)
    w._connected_flag = True
    w._slots[0]['previewFailed'] = True
    w._failing_since[0] = clock.t - 300
    w._on_file_info(_FakeMPfe(0, True, b'\x09' * 16))
    assert w._slots[0]['previewFailed'] is False
    assert 0 not in w._failing_since


def test_a_picture_that_cannot_be_decoded_counts_as_failing(monkeypatch):
    w, clock, h = _watcher_needing('10.0.0.88', monkeypatch)

    def _bad(raw, w_, h_):
        raise ValueError('not a frame')
    monkeypatch.setattr(W, 'atem_to_rgb', _bad)
    w._do_decode(0, b'raw', h, 1920, 1080)
    clock.t += W.PREVIEW_FAILED_AFTER
    w._do_decode(0, b'raw', h, 1920, 1080)
    assert w._slots[0]['previewFailed'] is True


def test_the_waiting_notice_reaches_the_page():
    import asyncio
    from atem_control.control.consumer import ATEMConsumer
    c = ATEMConsumer()
    sent = []

    async def _send(payload):
        sent.append(payload)
    c.send_json = _send
    asyncio.run(c.mediapool_preview_wait({'payload': {'waiting': True}}))
    assert sent == [{'type': 'media_pool_preview_wait', 'data': {'waiting': True}}]


def test_an_old_pool_lock_event_is_dropped():
    # 2.1.6 has no lock banner. An older process can still send the event
    # through a restart: the consumer takes it and sends the page nothing.
    import asyncio
    from atem_control.control.consumer import ATEMConsumer
    c = ATEMConsumer()
    sent = []

    async def _send(payload):
        sent.append(payload)
    c.send_json = _send
    asyncio.run(c.mediapool_pool_lock({'payload': {'locked': True}}))
    assert sent == []
